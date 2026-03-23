import asyncio
import json
import logging
import random
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from src.collector.twitter_collector import MockTweetSource, TargetFetchResult, TwikitTweetSource, TwitterCollector
from src.config.settings import Settings, load_settings, _normalize_filter_expression
from src.models.event import TweetEvent
from src.notifier.telegram_notifier import TelegramNotifier
from src.queue.in_memory_queue import InMemoryMessageQueue
from src.store.dedup_store import DedupStore


def configure_logging(level: str) -> None:
    root_logger = logging.getLogger()
    if root_logger.handlers:
        root_logger.setLevel(getattr(logging, level, logging.INFO))
        return
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


class InMemoryLogHandler(logging.Handler):
    def __init__(self, max_lines: int = 300) -> None:
        super().__init__()
        self._lines: Deque[str] = deque(maxlen=max_lines)
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self._lines.append(self.format(record))

    def lines(self) -> List[str]:
        return list(self._lines)

    def clear(self) -> None:
        self._lines.clear()


class RuntimeManager:
    _poll_interval_jitter_ratio = 0.1

    def __init__(self, config_file: str = ".state/ui_config.json") -> None:
        self._config_file = Path(config_file)
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._log = logging.getLogger("runtime")
        self._log_handler = InMemoryLogHandler()
        logging.getLogger().addHandler(self._log_handler)
        self._target_statuses: Dict[str, Dict[str, Any]] = {}

        self._status: Dict[str, Any] = {
            "running": False,
            "started_at": None,
            "last_loop_at": None,
            "collected_count": 0,
            "sent_count": 0,
            "failed_count": 0,
            "last_error": "",
        }

    def _update_env_from_dict(self, data: Dict[str, Any]) -> None:
        mapping = {
            "twitter_provider": "TWITTER_PROVIDER",
            "monitor_targets": "MONITOR_TARGETS",
            "twitter_poll_interval_sec": "TWITTER_POLL_INTERVAL_SEC",
            "twitter_fetch_limit": "TWITTER_FETCH_LIMIT",
            "twitter_bootstrap_drop_existing": "TWITTER_BOOTSTRAP_DROP_EXISTING",
            "twikit_username": "TWIKIT_USERNAME",
            "twikit_email": "TWIKIT_EMAIL",
            "twikit_password": "TWIKIT_PASSWORD",
            "twikit_cookies_file": "TWIKIT_COOKIES_FILE",
            "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
            "telegram_chat_id": "TELEGRAM_CHAT_ID",
            "dedup_file": "DEDUP_FILE",
            "dedup_max_ids": "DEDUP_MAX_IDS",
            "retry_max_attempts": "RETRY_MAX_ATTEMPTS",
            "retry_base_delay_sec": "RETRY_BASE_DELAY_SEC",
            "log_level": "LOG_LEVEL",
            "dry_run": "DRY_RUN",
        }
        for key, env_name in mapping.items():
            if key not in data:
                continue
            value = data[key]
            if key == "monitor_targets":
                os_value = json.dumps(self._normalize_monitor_targets(value), ensure_ascii=True)
            elif isinstance(value, list):
                os_value = ",".join(str(x).strip() for x in value if str(x).strip())
            elif isinstance(value, bool):
                os_value = "true" if value else "false"
            else:
                os_value = str(value)
            import os

            os.environ[env_name] = os_value

    def _normalize_monitor_targets(self, raw_targets: Any) -> List[Dict[str, Any]]:
        if not isinstance(raw_targets, list):
            return []
        normalized_targets: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username", "")).strip()
            if not username:
                continue
            key = username.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized_targets.append(
                {
                    "username": username,
                    "enabled": bool(item.get("enabled", True)),
                    "include_keywords": _normalize_filter_expression(item.get("include_keywords")),
                    "exclude_keywords": _normalize_filter_expression(item.get("exclude_keywords")),
                }
            )
        return normalized_targets

    def _serialize_settings(self, settings: Settings) -> Dict[str, Any]:
        return asdict(settings)

    def _ensure_target_status_snapshot(self, settings: Settings) -> None:
        active_usernames = {target.username for target in settings.monitor_targets}
        self._target_statuses = {
            username: status
            for username, status in self._target_statuses.items()
            if username in active_usernames
        }
        for target in settings.monitor_targets:
            self._target_statuses.setdefault(
                target.username,
                {
                    "last_fetch_status": "paused" if not target.enabled else "idle",
                    "last_fetch_at": None,
                    "last_error": "",
                    "last_fetched_count": 0,
                },
            )

    def _record_target_result(self, result: TargetFetchResult) -> None:
        self._target_statuses[result.username] = {
            "last_fetch_status": "success" if result.ok else "error",
            "last_fetch_at": datetime.now(timezone.utc).isoformat(),
            "last_error": "" if result.ok else result.error,
            "last_fetched_count": result.fetched_count,
        }

    def _build_target_statuses(self, settings: Settings) -> List[Dict[str, Any]]:
        self._ensure_target_status_snapshot(settings)
        items: List[Dict[str, Any]] = []
        for target in settings.monitor_targets:
            runtime_state = self._target_statuses.get(target.username, {})
            status = runtime_state.get("last_fetch_status", "idle")
            if not target.enabled:
                status = "paused"
            items.append(
                {
                    "username": target.username,
                    "enabled": target.enabled,
                    "include_keywords": target.include_keywords,
                    "exclude_keywords": target.exclude_keywords,
                    "last_fetch_status": status,
                    "last_fetch_at": runtime_state.get("last_fetch_at"),
                    "last_error": runtime_state.get("last_error", ""),
                    "last_fetched_count": runtime_state.get("last_fetched_count", 0),
                }
            )
        return items

    def load_saved_config(self) -> None:
        if not self._config_file.exists():
            return
        raw = json.loads(self._config_file.read_text(encoding="utf-8"))
        self._update_env_from_dict(raw)

    def save_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self._update_env_from_dict(data)
        settings = self.get_settings()
        self._ensure_target_status_snapshot(settings)
        self._config_file.parent.mkdir(parents=True, exist_ok=True)
        self._config_file.write_text(
            json.dumps(self._serialize_settings(settings), ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        return self._serialize_settings(settings)

    def get_settings(self) -> Settings:
        return load_settings()

    def get_config(self) -> Dict[str, Any]:
        settings = self.get_settings()
        self._ensure_target_status_snapshot(settings)
        return self._serialize_settings(settings)

    def status(self) -> Dict[str, Any]:
        settings = self.get_settings()
        data = dict(self._status)
        data["monitor_targets"] = self._build_target_statuses(settings)
        return data

    def logs(self) -> List[str]:
        return self._log_handler.lines()

    def clear_logs(self) -> None:
        self._log_handler.clear()

    async def start(self) -> Dict[str, Any]:
        async with self._lock:
            if self._task and not self._task.done():
                return self.status()

            self._ensure_target_status_snapshot(self.get_settings())
            self._stop_event = asyncio.Event()
            self._task = asyncio.create_task(self._run_loop(), name="tweet-runtime-loop")
            self._status["running"] = True
            self._status["started_at"] = datetime.now(timezone.utc).isoformat()
            self._log.info("Runtime started.")
            return self.status()

    async def stop(self) -> Dict[str, Any]:
        async with self._lock:
            if not self._task or self._task.done():
                self._status["running"] = False
                return self.status()
            self._stop_event.set()
            await self._task
            self._status["running"] = False
            self._log.info("Runtime stopped.")
            return self.status()

    async def restart(self) -> Dict[str, Any]:
        async with self._lock:
            if self._task and not self._task.done():
                self._stop_event.set()
                await self._task
                self._status["running"] = False
                self._log.info("Runtime stopped for restart.")

            self._ensure_target_status_snapshot(self.get_settings())
            self._stop_event = asyncio.Event()
            self._task = asyncio.create_task(self._run_loop(), name="tweet-runtime-loop")
            self._status["running"] = True
            self._status["started_at"] = datetime.now(timezone.utc).isoformat()
            self._log.info("Runtime restarted.")
            return self.status()

    async def test_send(self) -> bool:
        settings = self.get_settings()
        notifier = TelegramNotifier(settings=settings)
        event = TweetEvent(
            tweet_id=f"ui-test-{int(datetime.now(timezone.utc).timestamp())}",
            author="ui_test",
            text="这是来自 Web UI 的测试消息。",
            url="https://x.com/ui_test/status/0",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return await notifier.send_event(event)

    def _poll_wait_timeout(self, settings: Settings) -> float:
        base_interval = max(1.0, float(settings.twitter_poll_interval_sec))
        if settings.twitter_provider != "twikit":
            return base_interval
        jitter_window = base_interval * self._poll_interval_jitter_ratio
        timeout = base_interval + random.uniform(-jitter_window, jitter_window)
        return max(1.0, timeout)

    async def _run_loop(self) -> None:
        settings = self.get_settings()
        configure_logging(settings.log_level)
        self._ensure_target_status_snapshot(settings)
        dedup_store = DedupStore(storage_file=settings.dedup_file, max_ids=settings.dedup_max_ids)
        dedup_store.load()

        source = MockTweetSource()
        if settings.twitter_provider == "twikit":
            source = TwikitTweetSource(settings)
        elif settings.twitter_provider != "mock":
            raise ValueError(f"Unsupported TWITTER_PROVIDER: {settings.twitter_provider}")

        collector = TwitterCollector(source=source, settings=settings, dedup_store=dedup_store)
        queue = InMemoryMessageQueue[TweetEvent]()
        notifier = TelegramNotifier(settings=settings)

        while not self._stop_event.is_set():
            self._status["last_loop_at"] = datetime.now(timezone.utc).isoformat()
            try:
                events, target_results = await collector.collect()
                for result in target_results:
                    self._record_target_result(result)
                    if not result.ok:
                        self._status["failed_count"] += 1
                self._status["collected_count"] += len(events)
                for event in events:
                    await queue.put(event)
                while not queue.empty():
                    event = await queue.get()
                    try:
                        should_send, _reason = notifier.should_send_event(event)
                        if not should_send:
                            continue
                        sent = await notifier.send_event(event)
                        if sent:
                            self._status["sent_count"] += 1
                        else:
                            self._status["failed_count"] += 1
                    finally:
                        queue.task_done()
            except Exception as exc:
                self._status["last_error"] = str(exc)
                self._status["failed_count"] += 1
                self._log.exception("Runtime loop error: %s", exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_wait_timeout(settings))
            except asyncio.TimeoutError:
                pass

