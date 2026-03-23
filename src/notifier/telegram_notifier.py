import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
from urllib import parse, request
from typing import Dict, Tuple
from zoneinfo import ZoneInfo

from src.config.settings import Settings
from src.models.event import TweetEvent


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._log = logging.getLogger("notifier.telegram")
        self._target_filters: Dict[str, Dict[str, str]] = {
            target.username.lower(): {
                "include": target.include_keywords.lower(),
                "exclude": target.exclude_keywords.lower(),
            }
            for target in settings.monitor_targets
        }

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

        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

    def _format_message(self, event: TweetEvent) -> str:
        safe_text = event.text.strip() if event.text else "(无正文)"
        created_at = self._format_created_at(event.created_at)
        return (
            f"老大！ @{event.author} 发推了🫡\n\n"
            f"{safe_text}\n\n"
            f"链接：{event.url}\n"
            f"时间：{created_at}"
        )

    def _match_expression(self, text: str, expression: str) -> str:
        if not expression:
            return ""

        normalized = expression.replace("\r", "\n").replace("\n", ",")
        for clause in normalized.split(","):
            parts = [part.strip() for part in clause.split("+") if part.strip()]
            if parts and all(part in text for part in parts):
                return "+".join(parts)
        return ""

    def should_send_event(self, event: TweetEvent) -> Tuple[bool, str]:
        target_rules = self._target_filters.get(event.author.lower(), {"include": "", "exclude": ""})
        text = (event.text or "").lower()

        include_hit = self._match_expression(text, target_rules["include"])
        if target_rules["include"] and not include_hit:
            reason = f"未命中 @{event.author} 的包含关键词规则：{target_rules['include']}"
            self._log.info("Skipped tweet %s: %s.", event.tweet_id, reason)
            return False, reason

        exclude_hit = self._match_expression(text, target_rules["exclude"])
        if exclude_hit:
            reason = f"命中 @{event.author} 的排除关键词规则：{exclude_hit}"
            self._log.info("Skipped tweet %s: %s.", event.tweet_id, reason)
            return False, reason

        return True, ""

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

    async def send_event(self, event: TweetEvent) -> bool:
        text = self._format_message(event)

        if self._settings.dry_run:
            self._log.info("[DRY RUN] %s", text.replace("\n", " | "))
            return True

        attempts = max(1, self._settings.retry_max_attempts)
        base_delay = max(0.1, self._settings.retry_base_delay_sec)

        for attempt in range(1, attempts + 1):
            try:
                await self._send_once(text)
                self._log.info("Sent tweet %s to Telegram.", event.tweet_id)
                return True
            except Exception as exc:
                if attempt == attempts:
                    self._log.error(
                        "Failed to send tweet %s after %d attempts: %s",
                        event.tweet_id,
                        attempts,
                        exc,
                    )
                    return False

                delay = base_delay * (2 ** (attempt - 1))
                self._log.warning(
                    "Send failed for tweet %s (attempt %d/%d), retry in %.1fs: %s",
                    event.tweet_id,
                    attempt,
                    attempts,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        return False

