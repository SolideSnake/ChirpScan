from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Tuple
from urllib import request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.config.settings import MonitorTarget, Settings
from src.filters.expression import KeywordFilterSet
from src.models.event import TweetEvent
from src.store.delivery_status_store import DeliveryRecord


class FeishuNotifier:
    platform = "feishu"
    display_name = "飞书"
    persists_delivery = False

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._log = logging.getLogger("notifier.feishu")
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
            "X 监控通知\n\n"
            f"作者：@{event.author}\n\n"
            f"{safe_text}\n\n"
            f"链接：{event.url}\n"
            f"时间：{created_at}"
        )

    def _build_sign(self, timestamp: str) -> str:
        string_to_sign = f"{timestamp}\n{self._settings.feishu_secret}"
        digest = hmac.new(
            string_to_sign.encode("utf-8"),
            b"",
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _build_payload(self, text: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        if self._settings.feishu_secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = self._build_sign(timestamp)
        return payload

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
            return False, "feishu disabled"

        target_filter = KeywordFilterSet(route.include_keywords, route.exclude_keywords)
        should_send, reason = target_filter.matches(event.text or "")
        if not should_send:
            self._log.info("Skipped tweet %s for Feishu: %s.", event.tweet_id, reason)
        return should_send, reason

    async def _post_once(self, text: str) -> None:
        webhook_url = (self._settings.feishu_webhook_url or "").strip()
        if not webhook_url:
            raise RuntimeError("FEISHU_WEBHOOK_URL is required.")

        payload = json.dumps(self._build_payload(text), ensure_ascii=False).encode("utf-8")

        def _do_post() -> None:
            req = request.Request(webhook_url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            with request.urlopen(req, timeout=20) as response:
                body = response.read().decode("utf-8")
                data = json.loads(body)
                code = data.get("code", data.get("StatusCode", 0))
                if str(code) not in {"0", ""}:
                    message = data.get("msg") or data.get("message") or data.get("StatusMessage") or body
                    raise RuntimeError(f"Feishu API failed: {message}")

        await asyncio.to_thread(_do_post)

    async def _send_text_record(self, text: str, *, label: str, tweet_id: str) -> DeliveryRecord:
        attempts = max(1, self._settings.retry_max_attempts)
        base_delay = max(0.1, self._settings.retry_base_delay_sec)

        for attempt in range(1, attempts + 1):
            try:
                await self._post_once(text)
                self._log.info("Sent %s to Feishu.", label)
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
                        "Failed to send %s to Feishu after %d attempts: %s",
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
                    "Feishu send failed for %s (attempt %d/%d), retry in %.1fs: %s",
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
            reason="Feishu send failed",
            attempts=attempts,
            payload_text=text,
            retryable=False,
        )

    async def process_event(self, event: TweetEvent, target: MonitorTarget | None = None) -> DeliveryRecord | None:
        should_send, reason = self.should_send_event(event, target)
        if not should_send:
            if reason in {"target disabled", "feishu disabled"}:
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

