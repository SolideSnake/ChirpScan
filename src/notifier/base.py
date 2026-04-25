from __future__ import annotations

from typing import Protocol

from src.config.settings import MonitorTarget
from src.models.event import TweetEvent
from src.store.delivery_status_store import DeliveryRecord


class EventPublisher(Protocol):
    platform: str
    display_name: str
    persists_delivery: bool

    async def process_event(self, event: TweetEvent, target: MonitorTarget) -> DeliveryRecord | None:
        raise NotImplementedError

