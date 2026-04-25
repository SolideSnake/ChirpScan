import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Tuple
from urllib import parse, request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.config.settings import MonitorTarget, Settings
from src.filters.expression import KeywordFilterSet
from src.models.event import TweetEvent
from src.store.delivery_status_store import DeliveryRecord


class TelegramNotifier:
    platform = "telegram"
    display_name = "Telegram"
    persists_delivery = False

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._log = logging.getLogger("notifier.telegram")
        self._target_map = settings.target_map()

    def _display_timezone(self) -> timezone | ZoneInfo:
        try:
            return ZoneInfo("Asia/Shanghai")
        except ZoneInfoNotFoundError:
            return timezone(timedelta(hours=8))

    def _format_created_at(self, created_at: str) -> str:
        raw = (created_at or "").strip()
        if not raw:
            return "未知时间"

        parsed: datetime | None = None
        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
            except (TypeError, ValueError, IndexError):
                parsed = None

        if parsed is None:
            return raw

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(self._display_timezone()).strftime("%Y-%m-%d %H:%M:%S")

    def _format_message(self, event: TweetEvent) -> str:
        safe_text = event.text.strip() if event.text else "(无正文)"
        created_at = self._format_created_at(event.created_at)
        return (
            f"老大，@{event.author} 发推了\n\n"
            f"{safe_text}\n\n"
            f"链接：{event.url}\n"
            f"时间：{created_at}"
        )

    def should_send_event(self, event: TweetEvent, target: MonitorTarget | None = None) -> Tuple[bool, str]:
        target = target or self._target_map.get(event.author.lower())
        if target is None:
            reason = f"@{event.author} 不在监控目标中"
            self._log.info("Skipped tweet %s: %s.", event.tweet_id, reason)
            return False, reason

        route = target.route_for(self.platform)
        if not target.enabled:
            return False, "target disabled"
        if not route.enabled:
            return False, "telegram disabled"

        target_filter = KeywordFilterSet(route.include_keywords, route.exclude_keywords)
        should_send, reason = target_filter.matches(event.text or "")
        if not should_send:
            self._log.info("Skipped tweet %s for Telegram: %s.", event.tweet_id, reason)
        return should_send, reason

    async def _send_once(self, text: str) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.")

        endpoint = f"https://api.telegram.org/bot{self._settings.telegram_bot_token}/sendMessage"
        payload = parse.urlencode(
            {
                "chat_id": self._settings.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")

        def _do_post() -> None:
            req = request.Request(endpoint, data=payload, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with request.urlopen(req, timeout=20) as response:
                body = response.read().decode("utf-8")
                data = json.loads(body)
                if not data.get("ok"):
                    raise RuntimeError(f"Telegram API failed: {body}")

        await asyncio.to_thread(_do_post)

    async def _send_text_record(self, text: str, *, label: str, tweet_id: str) -> DeliveryRecord:
        attempts = max(1, self._settings.retry_max_attempts)
        base_delay = max(0.1, self._settings.retry_base_delay_sec)

        for attempt in range(1, attempts + 1):
            try:
                await self._send_once(text)
                self._log.info("Sent %s to Telegram.", label)
                return DeliveryRecord.create(
                    platform=self.platform,
                    tweet_id=tweet_id,
                    status="success",
                    attempts=attempt,
                    payload_text=text,
                    retryable=False,
                )
            except Exception as exc:
                reason = str(exc).strip() or exc.__class__.__name__
                if attempt == attempts:
                    self._log.error(
                        "Failed to send %s after %d attempts: %s",
                        label,
                        attempts,
                        exc,
                    )
                    return DeliveryRecord.create(
                        platform=self.platform,
                        tweet_id=tweet_id,
                        status="failed",
                        reason=reason,
                        attempts=attempt,
                        payload_text=text,
                        retryable=False,
                    )

                delay = base_delay * (2 ** (attempt - 1))
                self._log.warning(
                    "Send failed for %s (attempt %d/%d), retry in %.1fs: %s",
                    label,
                    attempt,
                    attempts,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        return DeliveryRecord.create(
            platform=self.platform,
            tweet_id=tweet_id,
            status="failed",
            reason="Telegram send failed",
            attempts=attempts,
            payload_text=text,
            retryable=False,
        )

    async def process_event(self, event: TweetEvent, target: MonitorTarget | None = None) -> DeliveryRecord | None:
        should_send, reason = self.should_send_event(event, target)
        if not should_send:
            if reason in {"target disabled", "telegram disabled"}:
                return None
            return DeliveryRecord.create(
                platform=self.platform,
                tweet_id=event.tweet_id,
                status="skipped",
                reason=reason,
                retryable=False,
            )

        text = self._format_message(event)
        return await self._send_text_record(text, label=f"tweet {event.tweet_id}", tweet_id=event.tweet_id)

    async def send_event(self, event: TweetEvent) -> bool:
        record = await self.process_event(event)
        return bool(record and record.success and record.status != "skipped")

    async def send_text(self, text: str, *, label: str = "message") -> bool:
        record = await self._send_text_record(text, label=label, tweet_id=label.replace(" ", "-"))
        return record.success
