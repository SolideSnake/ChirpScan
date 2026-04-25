import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Protocol

from src.config.settings import Settings
from src.models.event import TweetEvent
from src.store.dedup_store import DedupStore


def _is_twikit_transaction_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        isinstance(exc, AttributeError)
        and "clienttransaction" in message
        and "key" in message
    ) or any(
        marker in message
        for marker in (
            "couldn't get key_byte indices",
            "couldn't get key from the page source",
            "x-client-transaction-id",
        )
    )


def _is_twikit_user_shape_error(exc: Exception) -> bool:
    return isinstance(exc, KeyError) and str(exc).strip("'\"").lower() == "urls"


class _NoopClientTransaction:
    home_page_response = True

    def generate_transaction_id(self, *args: object, **kwargs: object) -> str:
        return ""


@dataclass(slots=True)
class TargetFetchResult:
    username: str
    ok: bool
    fetched_count: int
    error: str = ""
    consecutive_failures: int = 0
    escalated: bool = False


def summarize_fetch_error(exc: Exception, cookies_file: str = "") -> str:
    raw = str(exc).strip()
    lowered = raw.lower()
    exc_name = exc.__class__.__name__
    exc_module = exc.__class__.__module__
    exc_identity = f"{exc_module}.{exc_name}".lower()

    if isinstance(exc, FileNotFoundError):
        filename = getattr(exc, "filename", "") or cookies_file or "未指定"
        return f"Cookies 文件不存在：{filename}"

    if "timeout" in exc_identity:
        return "X 请求超时，可能是网络波动或 X 响应较慢，下一轮会自动重试。"

    if "connecterror" in exc_identity or "networkerror" in exc_identity:
        return "X 网络连接失败，请检查网络或代理后稍后重试。"

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

    if _is_twikit_transaction_error(exc):
        return "X 标准抓取暂时不可用，可能是 X 页面结构有更新。"

    if _is_twikit_user_shape_error(exc):
        return "X 用户资料字段缺失，当前抓取库解析失败。"

    compact = raw.splitlines()[0].strip() if raw else exc_name
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
        self._transaction_fallback_enabled = False
        self._user_cache: Dict[str, str] = {}

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

    def _reset_client(self) -> None:
        self._client = None
        self._ready = False
        self._transaction_fallback_enabled = False
        self._user_cache.clear()

    def _enable_transaction_fallback(self) -> bool:
        if self._client is None:
            return False
        self._client.client_transaction = _NoopClientTransaction()
        self._transaction_fallback_enabled = True
        return True

    def _iter_tweet_results(self, obj: Any) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        if isinstance(obj, dict):
            tweet_results = obj.get("tweet_results")
            if isinstance(tweet_results, dict):
                result = tweet_results.get("result")
                if isinstance(result, dict):
                    results.append(result)
            for value in obj.values():
                results.extend(self._iter_tweet_results(value))
        elif isinstance(obj, list):
            for item in obj:
                results.extend(self._iter_tweet_results(item))
        return results

    def _timeline_response_to_events(
        self,
        response: Dict[str, Any],
        *,
        username: str,
        user_id: str,
        limit: int,
    ) -> List[TweetEvent]:
        events: List[TweetEvent] = []
        seen: set[str] = set()

        for result in self._iter_tweet_results(response):
            if result.get("__typename") == "TweetTombstone":
                continue
            if isinstance(result.get("tweet"), dict):
                result = result["tweet"]

            legacy = result.get("legacy")
            if not isinstance(legacy, dict):
                continue
            if str(legacy.get("user_id_str", "")).strip() != user_id:
                continue

            tweet_id = str(result.get("rest_id") or legacy.get("id_str") or "").strip()
            if not tweet_id or tweet_id in seen:
                continue
            seen.add(tweet_id)

            note_text = ""
            note_result = (
                result.get("note_tweet", {})
                .get("note_tweet_results", {})
                .get("result", {})
            )
            if isinstance(note_result, dict):
                note_text = str(note_result.get("text") or "").strip()

            text = note_text or str(legacy.get("full_text") or legacy.get("text") or "").strip()
            events.append(
                TweetEvent(
                    tweet_id=tweet_id,
                    author=username,
                    text=text,
                    url=f"https://x.com/{username}/status/{tweet_id}",
                    created_at=str(legacy.get("created_at") or ""),
                )
            )
            if len(events) >= limit:
                break

        return events

    async def _fetch_user_id(self, username: str) -> str:
        await self._ensure_client()
        assert self._client is not None

        cache_key = username.lower()
        cached = self._user_cache.get(cache_key)
        if cached:
            return cached

        response, _ = await self._client.gql.user_by_screen_name(username)
        user_data = response.get("data", {}).get("user", {}).get("result", {})
        if user_data.get("__typename") == "UserUnavailable":
            raise RuntimeError(user_data.get("message") or f"X 用户 @{username} 不可用。")

        user_id = str(user_data.get("rest_id", "")).strip()
        if not user_id:
            raise RuntimeError(f"未找到 X 用户 @{username}。")

        self._user_cache[cache_key] = user_id
        return user_id

    async def _fetch_user_tweets(self, username: str, limit: int) -> List[TweetEvent]:
        await self._ensure_client()
        assert self._client is not None

        user_id = await self._fetch_user_id(username)
        response, _ = await self._client.gql.user_tweets(user_id, max(1, limit), None)
        return self._timeline_response_to_events(
            response,
            username=username,
            user_id=user_id,
            limit=limit,
        )

    async def fetch_latest(self, username: str, limit: int) -> List[TweetEvent]:
        try:
            return await self._fetch_user_tweets(username, limit)
        except Exception as exc:
            if not _is_twikit_transaction_error(exc):
                raise

            self._log.warning(
                "X 标准抓取失败，已自动切换备用模式重试一次：%s",
                summarize_fetch_error(exc, self._settings.twikit_cookies_file),
            )
            self._reset_client()
            await self._ensure_client()
            if not self._enable_transaction_fallback():
                self._reset_client()
                raise

            try:
                return await self._fetch_user_tweets(username, limit)
            except Exception:
                self._reset_client()
                raise


class TwitterCollector:
    _failure_error_threshold = 3

    def __init__(self, source: TweetSource, settings: Settings, dedup_store: DedupStore) -> None:
        self._source = source
        self._settings = settings
        self._dedup = dedup_store
        self._log = logging.getLogger("collector")
        self._bootstrapped = False
        self._failure_streaks: Dict[str, int] = {}

    async def collect(self) -> tuple[List[TweetEvent], List[TargetFetchResult]]:
        events: List[TweetEvent] = []
        target_results: List[TargetFetchResult] = []
        for username in self._settings.enabled_usernames():
            try:
                fetched = await self._source.fetch_latest(
                    username=username,
                    limit=self._settings.twitter_fetch_limit,
                )
                previous_failures = self._failure_streaks.pop(username, 0)
                if previous_failures:
                    self._log.info("抓取 @%s 已恢复，本轮获取 %d 条。", username, len(fetched))
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
                consecutive_failures = self._failure_streaks.get(username, 0) + 1
                self._failure_streaks[username] = consecutive_failures
                escalated = consecutive_failures >= self._failure_error_threshold
                if escalated:
                    self._log.error(
                        "抓取 @%s 连续失败 %d 次：%s",
                        username,
                        consecutive_failures,
                        summarized,
                    )
                else:
                    self._log.warning(
                        "抓取 @%s 暂时失败（第 %d 次，下一轮会自动重试）：%s",
                        username,
                        consecutive_failures,
                        summarized,
                    )
                target_results.append(
                    TargetFetchResult(
                        username=username,
                        ok=False,
                        fetched_count=0,
                        error=summarized,
                        consecutive_failures=consecutive_failures,
                        escalated=escalated,
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

