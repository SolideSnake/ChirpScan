from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List


@dataclass(frozen=True, slots=True)
class PublisherDefinition:
    platform: str
    label: str
    default_enabled: bool = False
    persists_delivery: bool = False


PUBLISHER_DEFINITIONS = (
    PublisherDefinition(
        platform="telegram",
        label="Telegram",
        default_enabled=True,
        persists_delivery=False,
    ),
    PublisherDefinition(
        platform="feishu",
        label="飞书",
        default_enabled=False,
        persists_delivery=False,
    ),
    PublisherDefinition(
        platform="binance_square",
        label="Binance Square",
        default_enabled=False,
        persists_delivery=True,
    ),
)

PUBLISHER_DEFINITIONS_BY_ID = {
    definition.platform: definition for definition in PUBLISHER_DEFINITIONS
}


def serialize_publisher_definitions() -> List[Dict[str, Any]]:
    return [asdict(definition) for definition in PUBLISHER_DEFINITIONS]
