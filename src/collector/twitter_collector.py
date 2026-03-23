import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Protocol

from src.config.settings import Settings
from src.models.event import TweetEvent
from src.store.dedup_store import DedupStore


@dataclass(slots=True)
class TargetFetchResult:
    username: str
    ok: bool
    fetched_count: int
    error: str = ""


def summarize_fetch_error(exc: Exception, cookies_file: str = "") -> str:
    raw = str(exc).strip()
    lowered = raw.lower()

    if isinstance(exc, FileNotFoundError):
        filename = getattr(exc, "filename", "") or cookies_file or "未指定"
        return f"Cookies 文件不存在：{filename}"

    if "twikit_username and twikit_password are required" in lowered:
        return "缺少 X 用户名或密码。"

    if "attention required" in lowered or "cloudflare" in lowered or "you have been blocked" in lowered:
        return "X 登录被 Cloudflare 风控拦截（403），请优先提供可用 Cookies，或更换网络环境后再试。"

    if "403" in lowered and "forbidden" in lowered:
        return "X 登录失败：403 Forbidden，可能触发了风控或账号校验。"

    if "status: 401" in lowered or "unauthorized" in lowered:
        return "X 登录失败：账号信息无效或会话已失效。"

    if "status: 429" in lowered or "too many requests" in lowered:
        return "X 请求过于频繁，被限流了，请稍后再试。"

    compact = raw.splitlines()[0].strip() if raw else "未知错误"
    if len(compact) > 160:
        compact = compact[:157] + "..."
    return compact


class TweetSource(Protocol):
    async def fetch_latest(self, username: str, limit: int) -> List[TweetEvent]:
        raise NotImplementedError


class MockTweetSource:
    def __init__(self) -> None:
        self._counter = 0

    async def fetch_latest(self, username: str, limit: int) -> List[TweetEvent]:
        self._counter += 1
        now = datetime.now(timezone.utc).isoformat()
        tweet_id = f"mock-{username}-{self._counter}"
        events = [
            TweetEvent(
                tweet_id=tweet_id,
                author=username,
                text=f"[mock] message #{self._counter} from @{username}",
                url=f"https://x.com/{username}/status/{tweet_id}",
                created_at=now,
            )
        ]
        await asyncio.sleep(0)
        return events


class TwikitTweetSource:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._log = logging.getLogger("collector.twikit")
        self._client = None
        self._ready = False
        self._user_cache: Dict[str, object] = {}

    async def _ensure_client(self) -> None:
        if self._ready:
            return
        try:
            from twikit import Client  # type: ignore
        except ImportError as exc:
            raise RuntimeError("twikit is not installed. Install dependencies first.") from exc

        self._client = Client(language="en-US")

        cookies_file = self._settings.twikit_cookies_file
        cookies_path = Path(cookies_file)

        async def login_with_credentials() -> None:
            if not self._settings.twikit_username or not self._settings.twikit_password:
                raise RuntimeError("缺少可用 Cookies，且未提供 TWIKIT_USERNAME / TWIKIT_PASSWORD。")
            await self._client.login(
                auth_info_1=self._settings.twikit_username,
                auth_info_2=self._settings.twikit_email or None,
                password=self._settings.twikit_password,
            )
            self._client.save_cookies(cookies_file)
            self._log.info("Saved new twikit cookies to %s", cookies_file)

        if not cookies_path.exists():
            self._log.warning("Cookies 文件不存在：%s，将尝试账号密码登录。", cookies_file)
            await login_with_credentials()
        else:
            try:
                if cookies_path.stat().st_size == 0:
                    self._log.info("Cookies 文件为空：%s，将尝试账号密码登录。", cookies_file)
                    await login_with_credentials()
                else:
                    self._client.load_cookies(cookies_file)
                    self._log.info("Loaded twikit cookies from %s", cookies_file)
            except FileNotFoundError:
                self._log.warning("Cookies 文件不存在：%s，将尝试账号密码登录。", cookies_file)
                await login_with_credentials()
            except Exception as exc:
                self._log.warning(
                    "加载 Cookies 失败：%s 将尝试账号密码登录。",
                    summarize_fetch_error(exc, cookies_file),
                )
                await login_with_credentials()

        self._ready = True

    async def _fetch_user_tweets(self, username: str, limit: int) -> List[TweetEvent]:
        await self._ensure_client()
        assert self._client is not None

        user = self._user_cache.get(username)
        if user is None:
            user = await self._client.get_user_by_screen_name(username)
            self._user_cache[username] = user

        tweets = await self._client.get_user_tweets(user.id, "Tweets", count=limit)
        events: List[TweetEvent] = []

        for tweet in tweets:
            tweet_id = str(getattr(tweet, "id", ""))
            if not tweet_id:
                continue
            text = str(getattr(tweet, "text", "")).strip()
            created_at = str(getattr(tweet, "created_at", ""))
            events.append(
                TweetEvent(
                    tweet_id=tweet_id,
                    author=username,
                    text=text,
                    url=f"https://x.com/{username}/status/{tweet_id}",
                    created_at=created_at,
                )
            )
        return events

    async def fetch_latest(self, username: str, limit: int) -> List[TweetEvent]:
        return await self._fetch_user_tweets(username, limit)


class TwitterCollector:
    def __init__(self, source: TweetSource, settings: Settings, dedup_store: DedupStore) -> None:
        self._source = source
        self._settings = settings
        self._dedup = dedup_store
        self._log = logging.getLogger("collector")
        self._bootstrapped = False

    async def collect(self) -> tuple[List[TweetEvent], List[TargetFetchResult]]:
        events: List[TweetEvent] = []
        target_results: List[TargetFetchResult] = []
        for username in self._settings.enabled_usernames():
            try:
                fetched = await self._source.fetch_latest(
                    username=username,
                    limit=self._settings.twitter_fetch_limit,
                )
                events.extend(fetched)
                target_results.append(
                    TargetFetchResult(
                        username=username,
                        ok=True,
                        fetched_count=len(fetched),
                    )
                )
            except Exception as exc:
                summarized = summarize_fetch_error(
                    exc,
                    getattr(self._settings, "twikit_cookies_file", ""),
                )
                self._log.error("抓取 @%s 失败：%s", username, summarized)
                target_results.append(
                    TargetFetchResult(
                        username=username,
                        ok=False,
                        fetched_count=0,
                        error=summarized,
                    )
                )

        # Keep collection order deterministic before dedup.
        events.sort(key=lambda item: item.tweet_id)

        if not self._bootstrapped and self._settings.twitter_bootstrap_drop_existing:
            for result in target_results:
                if result.ok:
                    self._log.info("@%s fetched %d", result.username, result.fetched_count)
                else:
                    self._log.warning("@%s fetch failed: %s", result.username, result.error)
            for event in events:
                self._dedup.add_if_new(event.tweet_id)
            self._dedup.save()
            self._bootstrapped = True
            self._log.info("bootstrap ignored total %d", len(events))
            return [], target_results

        self._bootstrapped = True
        fresh: List[TweetEvent] = []
        for event in events:
            if self._dedup.add_if_new(event.tweet_id):
                fresh.append(event)

        if fresh:
            self._dedup.save()
        return fresh, target_results

