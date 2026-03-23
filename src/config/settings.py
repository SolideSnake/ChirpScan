import json
import os
from dataclasses import dataclass
from typing import List


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be int, got: {raw}") from exc


def _as_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be float, got: {raw}") from exc


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _normalize_filter_expression(raw: object) -> str:
    if raw is None:
        return ""

    if isinstance(raw, list):
        raw_text = ",".join(str(item) for item in raw)
    else:
        raw_text = str(raw)

    clauses: List[str] = []
    for clause in raw_text.replace("\r", "\n").replace("\n", ",").split(","):
        parts = [part.strip() for part in clause.split("+") if part.strip()]
        if not parts:
            continue
        clauses.append("+".join(parts))
    return ",".join(clauses)


@dataclass(slots=True)
class MonitorTarget:
    username: str
    enabled: bool = True
    include_keywords: str = ""
    exclude_keywords: str = ""


def _load_monitor_targets(raw: str) -> List[MonitorTarget]:
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("MONITOR_TARGETS must be valid JSON.") from exc

    if not isinstance(data, list):
        raise ValueError("MONITOR_TARGETS must be a JSON array.")

    targets: List[MonitorTarget] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Each monitor target must be an object.")
        username = str(item.get("username", "")).strip()
        if not username:
            continue
        normalized = username.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        enabled = bool(item.get("enabled", True))
        targets.append(
            MonitorTarget(
                username=username,
                enabled=enabled,
                include_keywords=_normalize_filter_expression(item.get("include_keywords")),
                exclude_keywords=_normalize_filter_expression(item.get("exclude_keywords")),
            )
        )
    return targets


@dataclass(slots=True)
class Settings:
    twitter_provider: str
    monitor_targets: List[MonitorTarget]
    twitter_poll_interval_sec: int
    twitter_fetch_limit: int
    twitter_bootstrap_drop_existing: bool

    twikit_username: str
    twikit_email: str
    twikit_password: str
    twikit_cookies_file: str

    telegram_bot_token: str
    telegram_chat_id: str

    dedup_file: str
    dedup_max_ids: int

    retry_max_attempts: int
    retry_base_delay_sec: float

    log_level: str
    dry_run: bool

    def enabled_usernames(self) -> List[str]:
        return [target.username for target in self.monitor_targets if target.enabled]


def load_settings() -> Settings:
    return Settings(
        twitter_provider=os.getenv("TWITTER_PROVIDER", "mock").strip().lower(),
        monitor_targets=_load_monitor_targets(os.getenv("MONITOR_TARGETS", "")),
        twitter_poll_interval_sec=_as_int("TWITTER_POLL_INTERVAL_SEC", 60),
        twitter_fetch_limit=_as_int("TWITTER_FETCH_LIMIT", 5),
        twitter_bootstrap_drop_existing=_as_bool("TWITTER_BOOTSTRAP_DROP_EXISTING", True),
        twikit_username=os.getenv("TWIKIT_USERNAME", "").strip(),
        twikit_email=os.getenv("TWIKIT_EMAIL", "").strip(),
        twikit_password=os.getenv("TWIKIT_PASSWORD", "").strip(),
        twikit_cookies_file=os.getenv("TWIKIT_COOKIES_FILE", ".twikit_cookies.json").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        dedup_file=os.getenv("DEDUP_FILE", ".state/dedup.json").strip(),
        dedup_max_ids=_as_int("DEDUP_MAX_IDS", 5000),
        retry_max_attempts=_as_int("RETRY_MAX_ATTEMPTS", 4),
        retry_base_delay_sec=_as_float("RETRY_BASE_DELAY_SEC", 1.5),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        dry_run=_as_bool("DRY_RUN", False),
    )

