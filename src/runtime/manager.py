import asyncio
import json
import logging
import random
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from src.config.settings import (
    Settings,
    _normalize_filter_expression,
    load_settings,
)
from src.notifier.registry import PUBLISHER_DEFINITIONS, PUBLISHER_DEFINITIONS_BY_ID, serialize_publisher_definitions
from src.runtime.engine import build_runtime_context, run_cycle, test_publishers
from src.store.delivery_status_store import DeliveryRecord, DeliveryStatusStore


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
        self._platform_statuses: Dict[str, Dict[str, Dict[str, Any]]] = {}

        self._status: Dict[str, Any] = {
            "running": False,
            "started_at": None,
            "last_loop_at": None,
            "collected_count": 0,
            "published_count": 0,
            "failed_count": 0,
            "last_error": "",
            "last_delivery_error": "",
            "publisher_counts": {
                definition.platform: 0 for definition in PUBLISHER_DEFINITIONS
            },
        }
        self._sync_legacy_status_aliases()

    def _default_platform_enabled(self, platform: str) -> bool:
        definition = PUBLISHER_DEFINITIONS_BY_ID.get(platform)
        return definition.default_enabled if definition is not None else False

    def _empty_platform_status(self) -> Dict[str, Any]:
        return {
            "last_status": "idle",
            "last_at": None,
            "last_error": "",
            "last_url": "",
            "last_external_id": "",
        }

    def _platform_payload_template(self) -> Dict[str, Dict[str, Any]]:
        return {
            definition.platform: {
                "enabled": definition.default_enabled,
                "include_keywords": "",
                "exclude_keywords": "",
            }
            for definition in PUBLISHER_DEFINITIONS
        }

    def _serialize_settings(self, settings: Settings) -> Dict[str, Any]:
        return asdict(settings)

    def _with_platform_catalog(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(payload)
        data["available_platforms"] = serialize_publisher_definitions()
        return data

    def _sync_legacy_status_aliases(self) -> None:
        counts = self._status.setdefault("publisher_counts", {})
        self._status["telegram_sent_count"] = int(counts.get("telegram", 0))
        self._status["binance_sent_count"] = int(counts.get("binance_square", 0))
        self._status["sent_count"] = self._status["telegram_sent_count"]
        self._status["published_count"] = sum(int(value) for value in counts.values())

    def _update_env_from_dict(self, data: Dict[str, Any]) -> None:
        normalized_targets = self._normalize_payload_targets(data)
        if normalized_targets is not None:
            import os

            os.environ["MONITOR_TARGETS"] = json.dumps(normalized_targets, ensure_ascii=True)
            os.environ.pop("ALERT_TARGETS", None)
            os.environ.pop("PUBLISH_TARGETS", None)

        mapping = {
            "twitter_provider": "TWITTER_PROVIDER",
            "twitter_poll_interval_sec": "TWITTER_POLL_INTERVAL_SEC",
            "twitter_fetch_limit": "TWITTER_FETCH_LIMIT",
            "twitter_bootstrap_drop_existing": "TWITTER_BOOTSTRAP_DROP_EXISTING",
            "twikit_username": "TWIKIT_USERNAME",
            "twikit_email": "TWIKIT_EMAIL",
            "twikit_password": "TWIKIT_PASSWORD",
            "twikit_cookies_file": "TWIKIT_COOKIES_FILE",
            "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
            "telegram_chat_id": "TELEGRAM_CHAT_ID",
            "binance_square_enabled": "BINANCE_SQUARE_ENABLED",
            "binance_square_api_key": "BINANCE_SQUARE_API_KEY",
            "binance_publish_template": "BINANCE_PUBLISH_TEMPLATE",
            "binance_retry_max_attempts": "BINANCE_RETRY_MAX_ATTEMPTS",
            "binance_retry_base_delay_sec": "BINANCE_RETRY_BASE_DELAY_SEC",
            "delivery_status_file": "DELIVERY_STATUS_FILE",
            "delivery_status_max_records": "DELIVERY_STATUS_MAX_RECORDS",
            "dedup_file": "DEDUP_FILE",
            "dedup_max_ids": "DEDUP_MAX_IDS",
            "retry_max_attempts": "RETRY_MAX_ATTEMPTS",
            "retry_base_delay_sec": "RETRY_BASE_DELAY_SEC",
            "log_level": "LOG_LEVEL",
        }
        legacy_mapping = {
            "publish_retry_max_attempts": "BINANCE_RETRY_MAX_ATTEMPTS",
            "publish_retry_base_delay_sec": "BINANCE_RETRY_BASE_DELAY_SEC",
            "sync_status_file": "DELIVERY_STATUS_FILE",
            "sync_status_max_records": "DELIVERY_STATUS_MAX_RECORDS",
        }
        mapping.update({key: value for key, value in legacy_mapping.items() if key in data})

        for key, env_name in mapping.items():
            if key not in data or key in {"monitor_targets", "alert_targets", "publish_targets"}:
                continue
            value = data[key]
            if isinstance(value, list):
                os_value = ",".join(str(x).strip() for x in value if str(x).strip())
            elif isinstance(value, bool):
                os_value = "true" if value else "false"
            else:
                os_value = str(value)
            import os

            os.environ[env_name] = os_value

    def _normalize_payload_targets(self, data: Dict[str, Any]) -> List[Dict[str, Any]] | None:
        has_any_target_key = any(key in data for key in {"monitor_targets", "alert_targets", "publish_targets"})
        if not has_any_target_key:
            return None

        merged: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []

        def upsert(target: Dict[str, Any]) -> Dict[str, Any]:
            username = str(target.get("username", "")).strip()
            key = username.lower()
            if not username:
                return {}
            if key not in merged:
                merged[key] = {
                    "username": username,
                    "enabled": bool(target.get("enabled", True)),
                    "platforms": self._platform_payload_template(),
                }
                order.append(key)
            else:
                merged[key]["enabled"] = bool(merged[key].get("enabled", True) or target.get("enabled", True))
            return merged[key]

        def apply_platform(target: Dict[str, Any], platform: str, raw: Dict[str, Any] | None) -> None:
            route_raw = raw or {}
            target["platforms"][platform] = {
                "enabled": bool(route_raw.get("enabled", self._default_platform_enabled(platform))),
                "include_keywords": _normalize_filter_expression(route_raw.get("include_keywords")),
                "exclude_keywords": _normalize_filter_expression(route_raw.get("exclude_keywords")),
            }

        for item in data.get("monitor_targets", []) if isinstance(data.get("monitor_targets"), list) else []:
            if not isinstance(item, dict):
                continue
            target = upsert(item)
            if not target:
                continue

            platforms = item.get("platforms") if isinstance(item.get("platforms"), dict) else {}
            for platform, raw in platforms.items():
                if isinstance(raw, dict):
                    apply_platform(target, str(platform), raw)

            if "telegram" not in platforms and (
                isinstance(item.get("alert"), dict)
                or "include_keywords" in item
                or "exclude_keywords" in item
            ):
                alert = item.get("alert") if isinstance(item.get("alert"), dict) else {}
                apply_platform(
                    target,
                    "telegram",
                    {
                        "enabled": alert.get("enabled", True),
                        "include_keywords": alert.get("include_keywords", item.get("include_keywords")),
                        "exclude_keywords": alert.get("exclude_keywords", item.get("exclude_keywords")),
                    },
                )

            if "binance_square" not in platforms and isinstance(item.get("publish"), dict):
                publish = item.get("publish") if isinstance(item.get("publish"), dict) else {}
                binance = publish.get("binance_square") if isinstance(publish.get("binance_square"), dict) else {}
                apply_platform(
                    target,
                    "binance_square",
                    {
                        "enabled": binance.get("enabled", publish.get("enabled", False)),
                        "include_keywords": binance.get("include_keywords", publish.get("include_keywords")),
                        "exclude_keywords": binance.get("exclude_keywords", publish.get("exclude_keywords")),
                    },
                )

        for item in data.get("alert_targets", []) if isinstance(data.get("alert_targets"), list) else []:
            if not isinstance(item, dict):
                continue
            target = upsert(item)
            if not target:
                continue
            alert = item.get("alert") if isinstance(item.get("alert"), dict) else {}
            apply_platform(
                target,
                "telegram",
                {
                    "enabled": alert.get("enabled", item.get("enabled", True)),
                    "include_keywords": alert.get("include_keywords", item.get("include_keywords")),
                    "exclude_keywords": alert.get("exclude_keywords", item.get("exclude_keywords")),
                },
            )

        for item in data.get("publish_targets", []) if isinstance(data.get("publish_targets"), list) else []:
            if not isinstance(item, dict):
                continue
            target = upsert(item)
            if not target:
                continue
            publish = item.get("publish") if isinstance(item.get("publish"), dict) else {}
            binance = publish.get("binance_square") if isinstance(publish.get("binance_square"), dict) else {}
            apply_platform(
                target,
                "binance_square",
                {
                    "enabled": binance.get("enabled", publish.get("enabled", item.get("enabled", True))),
                    "include_keywords": binance.get("include_keywords", publish.get("include_keywords")),
                    "exclude_keywords": binance.get("exclude_keywords", publish.get("exclude_keywords")),
                },
            )

        return [merged[key] for key in order]

    def _ensure_target_status_snapshot(self, settings: Settings) -> None:
        active_usernames = {target.username for target in settings.monitor_targets}
        self._target_statuses = {
            username: status
            for username, status in self._target_statuses.items()
            if username in active_usernames
        }
        self._platform_statuses = {
            username: status
            for username, status in self._platform_statuses.items()
            if username in active_usernames
        }
        known_platforms = {definition.platform for definition in PUBLISHER_DEFINITIONS}
        for target in settings.monitor_targets:
            self._target_statuses.setdefault(
                target.username,
                {
                    "last_fetch_status": "paused" if not target.enabled else "idle",
                    "last_fetch_at": None,
                    "last_error": "",
                    "last_fetched_count": 0,
                    "fetch_failure_streak": 0,
                },
            )
            self._platform_statuses.setdefault(target.username, {})
            for platform in known_platforms.union(target.platforms.keys()):
                self._platform_statuses[target.username].setdefault(platform, self._empty_platform_status())

    def _record_target_result(
        self,
        username: str,
        ok: bool,
        fetched_count: int,
        error: str = "",
        consecutive_failures: int = 0,
        escalated: bool = False,
    ) -> None:
        self._target_statuses[username] = {
            "last_fetch_status": "success" if ok else ("error" if escalated else "warning"),
            "last_fetch_at": datetime.now(timezone.utc).isoformat(),
            "last_error": "" if ok else error,
            "last_fetched_count": fetched_count,
            "fetch_failure_streak": 0 if ok else consecutive_failures,
        }

    def _record_publish_result(self, author: str, record: DeliveryRecord) -> None:
        self._platform_statuses.setdefault(author, {})[record.platform] = {
            "last_status": record.status,
            "last_at": record.updated_at,
            "last_error": "" if record.success or record.status == "skipped" else record.reason,
            "last_url": record.url,
            "last_external_id": record.external_id,
        }
        counts = self._status.setdefault("publisher_counts", {})
        if record.success and record.status != "skipped":
            counts[record.platform] = int(counts.get(record.platform, 0)) + 1
        elif not record.success and record.status != "skipped":
            self._status["failed_count"] += 1
            self._status["last_delivery_error"] = record.reason
        self._sync_legacy_status_aliases()

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
                    "platforms": asdict(target)["platforms"],
                    "last_fetch_status": status,
                    "last_fetch_at": runtime_state.get("last_fetch_at"),
                    "last_error": runtime_state.get("last_error", ""),
                    "last_fetched_count": runtime_state.get("last_fetched_count", 0),
                    "fetch_failure_streak": runtime_state.get("fetch_failure_streak", 0),
                    "delivery": self._platform_statuses.get(target.username, {}),
                }
            )
        return items

    def _build_delivery_summary(self, settings: Settings) -> Dict[str, Any]:
        store = DeliveryStatusStore(settings.delivery_status_file, settings.delivery_status_max_records)
        store.load()
        records = store.all_records()
        persisted_platforms = {
            definition.platform for definition in PUBLISHER_DEFINITIONS if definition.persists_delivery
        }
        persisted_platforms.update(record.platform for record in records)
        summary: Dict[str, Any] = {}
        for platform in persisted_platforms:
            platform_records = [record for record in records if record.platform == platform]
            latest = platform_records[-1] if platform_records else None
            summary[platform] = {
                "total": len(platform_records),
                "success_count": sum(1 for record in platform_records if record.status == "success"),
                "failed_count": sum(1 for record in platform_records if record.status == "failed"),
                "skipped_count": sum(1 for record in platform_records if record.status == "skipped"),
                "latest_status": latest.status if latest else "",
                "latest_reason": latest.reason if latest else "",
                "latest_at": latest.updated_at if latest else "",
                "latest_url": latest.url if latest else "",
            }
        return summary

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
        return self._with_platform_catalog(self._serialize_settings(settings))

    def get_settings(self) -> Settings:
        return load_settings()

    def get_config(self) -> Dict[str, Any]:
        settings = self.get_settings()
        self._ensure_target_status_snapshot(settings)
        return self._with_platform_catalog(self._serialize_settings(settings))

    def status(self) -> Dict[str, Any]:
        settings = self.get_settings()
        data = dict(self._status)
        data["monitor_targets"] = self._build_target_statuses(settings)
        data["delivery"] = self._build_delivery_summary(settings)
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

    async def test_send(self) -> Dict[str, Any]:
        settings = self.get_settings()
        records = await test_publishers(settings)
        results = {
            platform: {
                "ok": record.success,
                "status": record.status,
                "reason": record.reason,
                "url": record.url,
                "external_id": record.external_id,
            }
            for platform, record in records.items()
        }
        return {
            "ok": all(record.success for record in records.values()),
            "message": "测试消息",
            "results": results,
        }

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
        runtime = build_runtime_context(settings)

        while not self._stop_event.is_set():
            self._status["last_loop_at"] = datetime.now(timezone.utc).isoformat()
            try:
                report = await run_cycle(runtime)
                self._status["collected_count"] += report.collected_count
                for result in report.target_results:
                    self._record_target_result(
                        username=result.username,
                        ok=result.ok,
                        fetched_count=result.fetched_count,
                        error=result.error,
                        consecutive_failures=result.consecutive_failures,
                        escalated=result.escalated,
                    )
                    if not result.ok and result.escalated:
                        self._status["failed_count"] += 1
                        self._status["last_error"] = result.error

                for attempt in report.publish_attempts:
                    self._record_publish_result(attempt.event.author, attempt.record)
            except Exception as exc:
                self._status["last_error"] = str(exc)
                self._status["failed_count"] += 1
                self._log.exception("Runtime loop error: %s", exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_wait_timeout(settings))
            except asyncio.TimeoutError:
                pass
