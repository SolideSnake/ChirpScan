import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from src.notifier.registry import PUBLISHER_DEFINITIONS, PUBLISHER_DEFINITIONS_BY_ID


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


def _as_int_fallback(primary: str, fallback: str, default: int) -> int:
    if os.getenv(primary) is not None:
        return _as_int(primary, default)
    return _as_int(fallback, default)


def _as_float_fallback(primary: str, fallback: str, default: float) -> float:
    if os.getenv(primary) is not None:
        return _as_float(primary, default)
    return _as_float(fallback, default)


def _as_str_fallback(primary: str, fallback: str, default: str) -> str:
    if os.getenv(primary) is not None:
        return os.getenv(primary, default).strip()
    return os.getenv(fallback, default).strip()


def _coerce_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


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


def _normalize_binance_template(raw: object) -> str:
    value = str(raw or "plain_with_link").strip()
    return value if value in {"plain", "plain_with_link"} else "plain_with_link"


def _default_enabled_for_platform(platform: str) -> bool:
    definition = PUBLISHER_DEFINITIONS_BY_ID.get(platform)
    return definition.default_enabled if definition is not None else False


@dataclass(slots=True)
class PlatformRoute:
    enabled: bool = False
    include_keywords: str = ""
    exclude_keywords: str = ""


def build_default_platform_routes() -> Dict[str, PlatformRoute]:
    return {
        definition.platform: PlatformRoute(enabled=definition.default_enabled)
        for definition in PUBLISHER_DEFINITIONS
    }


def build_disabled_platform_routes() -> Dict[str, PlatformRoute]:
    return {
        definition.platform: PlatformRoute(enabled=False)
        for definition in PUBLISHER_DEFINITIONS
    }


@dataclass(slots=True)
class MonitorTarget:
    username: str
    enabled: bool = True
    platforms: Dict[str, PlatformRoute] = field(default_factory=build_default_platform_routes)

    def route_for(self, platform: str) -> PlatformRoute:
        route = self.platforms.get(platform)
        if route is None:
            route = PlatformRoute(enabled=_default_enabled_for_platform(platform))
            self.platforms[platform] = route
        return route

    @property
    def include_keywords(self) -> str:
        return self.route_for("telegram").include_keywords

    @property
    def exclude_keywords(self) -> str:
        return self.route_for("telegram").exclude_keywords


def _load_json_array(raw: str, env_name: str) -> list[object]:
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{env_name} must be valid JSON.") from exc
    if not isinstance(data, list):
        raise ValueError(f"{env_name} must be a JSON array.")
    return data


def _route_from_dict(raw: object, *, default_enabled: bool = False) -> PlatformRoute:
    if not isinstance(raw, dict):
        raw = {}
    return PlatformRoute(
        enabled=_coerce_bool(raw.get("enabled"), default_enabled),
        include_keywords=_normalize_filter_expression(raw.get("include_keywords")),
        exclude_keywords=_normalize_filter_expression(raw.get("exclude_keywords")),
    )


def _legacy_telegram_route(item: Dict[str, Any]) -> Dict[str, Any]:
    alert_raw = item.get("alert") if isinstance(item.get("alert"), dict) else {}
    return {
        "enabled": alert_raw.get("enabled", True),
        "include_keywords": alert_raw.get("include_keywords", item.get("include_keywords")),
        "exclude_keywords": alert_raw.get("exclude_keywords", item.get("exclude_keywords")),
    }


def _legacy_binance_route(item: Dict[str, Any]) -> Dict[str, Any]:
    publish_raw = item.get("publish") if isinstance(item.get("publish"), dict) else {}
    legacy_binance = publish_raw.get("binance_square") if isinstance(publish_raw.get("binance_square"), dict) else {}
    return {
        "enabled": legacy_binance.get("enabled", publish_raw.get("enabled", False)),
        "include_keywords": legacy_binance.get("include_keywords", publish_raw.get("include_keywords")),
        "exclude_keywords": legacy_binance.get("exclude_keywords", publish_raw.get("exclude_keywords")),
    }


def _platform_routes_from_monitor_item(item: Dict[str, Any]) -> Dict[str, PlatformRoute]:
    routes = build_default_platform_routes()
    platforms = item.get("platforms") if isinstance(item.get("platforms"), dict) else {}

    for platform, raw in platforms.items():
        routes[str(platform)] = _route_from_dict(raw, default_enabled=_default_enabled_for_platform(str(platform)))

    if "telegram" not in platforms and (
        isinstance(item.get("alert"), dict)
        or "include_keywords" in item
        or "exclude_keywords" in item
    ):
        routes["telegram"] = _route_from_dict(
            _legacy_telegram_route(item),
            default_enabled=_default_enabled_for_platform("telegram"),
        )

    publish_raw = item.get("publish") if isinstance(item.get("publish"), dict) else {}
    if "binance_square" not in platforms and publish_raw:
        routes["binance_square"] = _route_from_dict(
            _legacy_binance_route(item),
            default_enabled=_default_enabled_for_platform("binance_square"),
        )

    return routes


def _target_from_monitor_item(item: Dict[str, Any]) -> MonitorTarget:
    return MonitorTarget(
        username=str(item.get("username", "")).strip(),
        enabled=_coerce_bool(item.get("enabled"), True),
        platforms=_platform_routes_from_monitor_item(item),
    )


def _target_from_split_item(item: Dict[str, Any], *, platform: str) -> MonitorTarget:
    username = str(item.get("username", "")).strip()
    routes = build_disabled_platform_routes()

    if platform == "telegram":
        routes["telegram"] = _route_from_dict(
            {
                "enabled": item.get("alert", {}).get("enabled", item.get("enabled", True))
                if isinstance(item.get("alert"), dict)
                else item.get("enabled", True),
                "include_keywords": item.get("alert", {}).get("include_keywords", item.get("include_keywords"))
                if isinstance(item.get("alert"), dict)
                else item.get("include_keywords"),
                "exclude_keywords": item.get("alert", {}).get("exclude_keywords", item.get("exclude_keywords"))
                if isinstance(item.get("alert"), dict)
                else item.get("exclude_keywords"),
            },
            default_enabled=True,
        )
    else:
        publish_raw = item.get("publish") if isinstance(item.get("publish"), dict) else {}
        binance_raw = publish_raw.get("binance_square") if isinstance(publish_raw.get("binance_square"), dict) else {}
        routes[platform] = _route_from_dict(
            {
                "enabled": binance_raw.get("enabled", publish_raw.get("enabled", item.get("enabled", True))),
                "include_keywords": binance_raw.get("include_keywords", publish_raw.get("include_keywords")),
                "exclude_keywords": binance_raw.get("exclude_keywords", publish_raw.get("exclude_keywords")),
            },
            default_enabled=True,
        )

    return MonitorTarget(
        username=username,
        enabled=_coerce_bool(item.get("enabled"), True),
        platforms=routes,
    )


def _load_targets(raw: str, env_name: str, *, source: str = "monitor") -> List[MonitorTarget]:
    targets: List[MonitorTarget] = []
    seen: set[str] = set()
    for item in _load_json_array(raw, env_name):
        if not isinstance(item, dict):
            raise ValueError(f"Each {env_name} item must be an object.")
        username = str(item.get("username", "")).strip()
        if not username:
            continue
        key = username.lower()
        if key in seen:
            continue
        seen.add(key)
        if source == "telegram":
            target = _target_from_split_item(item, platform="telegram")
        elif source == "binance_square":
            target = _target_from_split_item(item, platform="binance_square")
        else:
            target = _target_from_monitor_item(item)
        if target.username:
            targets.append(target)
    return targets


def _merge_targets(*target_groups: List[MonitorTarget]) -> List[MonitorTarget]:
    merged: dict[str, MonitorTarget] = {}
    order: List[str] = []
    for targets in target_groups:
        for target in targets:
            key = target.username.lower()
            if key not in merged:
                merged[key] = target
                order.append(key)
                continue
            existing = merged[key]
            existing.enabled = existing.enabled or target.enabled
            for platform, route in target.platforms.items():
                if route.enabled or route.include_keywords or route.exclude_keywords:
                    existing.platforms[platform] = route
                else:
                    existing.platforms.setdefault(platform, route)
    return [merged[key] for key in order]


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

    binance_square_enabled: bool
    binance_square_api_key: str
    binance_publish_template: str
    binance_retry_max_attempts: int
    binance_retry_base_delay_sec: float

    delivery_status_file: str
    delivery_status_max_records: int

    dedup_file: str
    dedup_max_ids: int

    retry_max_attempts: int
    retry_base_delay_sec: float

    log_level: str

    def enabled_usernames(self) -> List[str]:
        usernames: List[str] = []
        seen: set[str] = set()
        for target in self.monitor_targets:
            key = target.username.lower()
            if target.enabled and key not in seen:
                seen.add(key)
                usernames.append(target.username)
        return usernames

    def target_map(self) -> Dict[str, MonitorTarget]:
        return {target.username.lower(): target for target in self.monitor_targets}


def load_settings() -> Settings:
    monitor_targets = _load_targets(os.getenv("MONITOR_TARGETS", ""), "MONITOR_TARGETS")
    alert_targets = _load_targets(os.getenv("ALERT_TARGETS", ""), "ALERT_TARGETS", source="telegram")
    publish_targets = _load_targets(os.getenv("PUBLISH_TARGETS", ""), "PUBLISH_TARGETS", source="binance_square")
    merged_targets = _merge_targets(monitor_targets, alert_targets, publish_targets)

    return Settings(
        twitter_provider=os.getenv("TWITTER_PROVIDER", "twikit").strip().lower(),
        monitor_targets=merged_targets,
        twitter_poll_interval_sec=_as_int("TWITTER_POLL_INTERVAL_SEC", 300),
        twitter_fetch_limit=_as_int("TWITTER_FETCH_LIMIT", 5),
        twitter_bootstrap_drop_existing=_as_bool("TWITTER_BOOTSTRAP_DROP_EXISTING", True),
        twikit_username=os.getenv("TWIKIT_USERNAME", "").strip(),
        twikit_email=os.getenv("TWIKIT_EMAIL", "").strip(),
        twikit_password=os.getenv("TWIKIT_PASSWORD", "").strip(),
        twikit_cookies_file=os.getenv("TWIKIT_COOKIES_FILE", ".twikit_cookies.json").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        binance_square_enabled=_as_bool("BINANCE_SQUARE_ENABLED", True),
        binance_square_api_key=os.getenv("BINANCE_SQUARE_API_KEY", "").strip(),
        binance_publish_template=_normalize_binance_template(os.getenv("BINANCE_PUBLISH_TEMPLATE", "plain_with_link")),
        binance_retry_max_attempts=_as_int_fallback("BINANCE_RETRY_MAX_ATTEMPTS", "PUBLISH_RETRY_MAX_ATTEMPTS", 3),
        binance_retry_base_delay_sec=_as_float_fallback("BINANCE_RETRY_BASE_DELAY_SEC", "PUBLISH_RETRY_BASE_DELAY_SEC", 2.0),
        delivery_status_file=_as_str_fallback("DELIVERY_STATUS_FILE", "SYNC_STATUS_FILE", ".state/delivery_status.json"),
        delivery_status_max_records=_as_int_fallback("DELIVERY_STATUS_MAX_RECORDS", "SYNC_STATUS_MAX_RECORDS", 5000),
        dedup_file=os.getenv("DEDUP_FILE", ".state/dedup.json").strip(),
        dedup_max_ids=_as_int("DEDUP_MAX_IDS", 5000),
        retry_max_attempts=_as_int("RETRY_MAX_ATTEMPTS", 4),
        retry_base_delay_sec=_as_float("RETRY_BASE_DELAY_SEC", 1.5),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )
