from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict
from urllib import request

from src.config.settings import MonitorTarget, Settings
from src.filters.expression import KeywordFilterSet
from src.models.event import TweetEvent
from src.store.delivery_status_store import DeliveryRecord, DeliveryStatusStore


BINANCE_SQUARE_ENDPOINT = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"
_URL_RE = re.compile(r"(?:https?://|www\.|t\.co/)\S+", re.IGNORECASE)
_MANY_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _truncate_text(text: str, limit: int = 200) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def clean_binance_body_text(text: str) -> str:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    without_urls = _URL_RE.sub("", normalized)
    lines = [line.rstrip() for line in without_urls.split("\n")]
    cleaned = "\n".join(lines).strip()
    return _MANY_BLANK_LINES_RE.sub("\n\n", cleaned)


class BinanceSquareNotifier:
    platform = "binance_square"
    display_name = "Binance Square"
    persists_delivery = True
    _non_retryable_codes = {
        "10005",
        "10007",
        "20002",
        "20013",
        "20020",
        "20022",
        "20041",
        "220003",
        "220004",
        "220009",
        "220010",
        "220011",
        "30004",
        "30008",
        "2000001",
        "2000002",
    }

    def __init__(self, settings: Settings, delivery_store: DeliveryStatusStore) -> None:
        self._settings = settings
        self._delivery_store = delivery_store
        self._log = logging.getLogger("notifier.binance_square")

    def _build_body_text(self, event: TweetEvent) -> str:
        return _truncate_text(clean_binance_body_text(event.text or ""))

    def _is_key_configured(self) -> bool:
        key = (self._settings.binance_square_api_key or "").strip()
        return bool(key) and key != "your_api_key"

    def _post_once(self, body_text: str) -> Dict[str, Any]:
        if not self._is_key_configured():
            raise RuntimeError("BINANCE_SQUARE_API_KEY is required.")

        payload = json.dumps({"bodyTextOnly": body_text}, ensure_ascii=False).encode("utf-8")
        req = request.Request(BINANCE_SQUARE_ENDPOINT, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("clienttype", "binanceSkill")
        req.add_header("X-Square-OpenAPI-Key", self._settings.binance_square_api_key)

        with request.urlopen(req, timeout=20.0) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw)
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected Binance response.")
        return data

    def should_send_event(self, event: TweetEvent, target: MonitorTarget) -> tuple[bool, str]:
        route = target.route_for(self.platform)
        if not self._settings.binance_square_enabled:
            return False, "binance global disabled"
        if not target.enabled:
            return False, "target disabled"
        if not route.enabled:
            return False, "binance disabled"

        target_filter = KeywordFilterSet(route.include_keywords, route.exclude_keywords)
        return target_filter.matches(event.text or "")

    async def process_event(self, event: TweetEvent, target: MonitorTarget) -> DeliveryRecord | None:
        should_send, reason = self.should_send_event(event, target)
        if not should_send:
            if reason in {"binance global disabled", "target disabled", "binance disabled"}:
                return None
            record = DeliveryRecord.create(
                platform=self.platform,
                tweet_id=event.tweet_id,
                status="skipped",
                reason=reason,
                retryable=False,
            )
            return self._delivery_store.save_record(record)

        existing = self._delivery_store.status_for(self.platform, event.tweet_id)
        if existing and existing.get("status") == "success":
            return DeliveryRecord.create(
                platform=self.platform,
                tweet_id=event.tweet_id,
                status="skipped",
                reason="already published",
                external_id=str(existing.get("external_id", "") or ""),
                attempts=int(existing.get("attempts", 0) or 0),
                url=str(existing.get("url", "") or ""),
                retryable=False,
            )

        body_text = self._build_body_text(event)
        if not body_text:
            record = DeliveryRecord.create(
                platform=self.platform,
                tweet_id=event.tweet_id,
                status="skipped",
                reason="empty body after url cleanup",
                retryable=False,
            )
            return self._delivery_store.save_record(record)

        attempts = max(1, int(self._settings.binance_retry_max_attempts))
        base_delay = max(0.1, float(self._settings.binance_retry_base_delay_sec))
        last_record: DeliveryRecord | None = None
        for attempt in range(1, attempts + 1):
            record = await self._send_once(event, body_text, attempt)
            last_record = record
            if record.success or not record.retryable or attempt >= attempts:
                return self._delivery_store.save_record(record)
            delay = base_delay * (2 ** (attempt - 1))
            self._log.warning(
                "Binance Square publish failed for tweet %s (attempt %d/%d), retry in %.1fs: %s",
                event.tweet_id,
                attempt,
                attempts,
                delay,
                record.reason,
            )
            await asyncio.sleep(delay)

        if last_record is None:
            last_record = DeliveryRecord.create(
                platform=self.platform,
                tweet_id=event.tweet_id,
                status="failed",
                reason="publish failed",
                attempts=attempts,
            )
        return self._delivery_store.save_record(last_record)

    async def _send_once(self, event: TweetEvent, body_text: str, attempt: int) -> DeliveryRecord:
        try:
            response = await asyncio.to_thread(self._post_once, body_text)
            code = str(response.get("code", "")).strip()
            message = response.get("message")
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            external_id = str(data.get("id", "")).strip()
            if code != "000000":
                reason = str(message or f"binance error {code}").strip()
                return DeliveryRecord.create(
                    platform=self.platform,
                    tweet_id=event.tweet_id,
                    status="failed",
                    reason=reason,
                    attempts=attempt,
                    payload_text=body_text,
                    retryable=code not in self._non_retryable_codes,
                )
            post_url = f"https://www.binance.com/square/post/{external_id}" if external_id else ""
            return DeliveryRecord.create(
                platform=self.platform,
                tweet_id=event.tweet_id,
                status="success",
                attempts=attempt,
                external_id=external_id,
                url=post_url,
                payload_text=body_text,
                retryable=False,
            )
        except Exception as exc:
            reason = str(exc).strip() or exc.__class__.__name__
            self._log.error("Failed to publish tweet %s to Binance Square: %s", event.tweet_id, reason)
            return DeliveryRecord.create(
                platform=self.platform,
                tweet_id=event.tweet_id,
                status="failed",
                reason=reason,
                attempts=attempt,
                payload_text=body_text,
                retryable=True,
            )
