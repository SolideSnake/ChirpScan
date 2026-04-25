from __future__ import annotations

from src.models.event import TweetEvent
from src.store.delivery_status_store import DeliveryRecord


class HonorBoardService:
    """
    Separate domain service for the future honor-board flow.

    The runtime can notify this service after a successful platform delivery
    without coupling extraction or persistence logic into publisher modules.
    """

    async def handle_delivery(self, event: TweetEvent, record: DeliveryRecord) -> None:
        del event, record
        return None

