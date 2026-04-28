from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List

from src.collector.twitter_collector import MockTweetSource, TargetFetchResult, TwikitTweetSource, TwitterCollector
from src.config.settings import MonitorTarget, PlatformRoute, Settings, build_disabled_platform_routes
from src.honor_board.service import HonorBoardService
from src.models.event import TweetEvent
from src.notifier.base import EventPublisher
from src.notifier.binance_square_notifier import BinanceSquareNotifier
from src.notifier.feishu_notifier import FeishuNotifier
from src.notifier.telegram_notifier import TelegramNotifier
from src.queue.in_memory_queue import InMemoryMessageQueue
from src.store.dedup_store import DedupStore
from src.store.delivery_status_store import DeliveryRecord, DeliveryStatusStore


@dataclass(slots=True)
class PublishAttempt:
    event: TweetEvent
    record: DeliveryRecord


@dataclass(slots=True)
class RuntimeCycleReport:
    collected_count: int
    target_results: List[TargetFetchResult] = field(default_factory=list)
    publish_attempts: List[PublishAttempt] = field(default_factory=list)


@dataclass(slots=True)
class RuntimeContext:
    settings: Settings
    collector: TwitterCollector
    queue: InMemoryMessageQueue[TweetEvent]
    publishers: List[EventPublisher]
    target_map: dict[str, MonitorTarget]
    post_publish_hooks: List[HonorBoardService] = field(default_factory=list)


def build_publishers(settings: Settings, delivery_store: DeliveryStatusStore) -> List[EventPublisher]:
    return [
        TelegramNotifier(settings=settings),
        FeishuNotifier(settings=settings),
        BinanceSquareNotifier(settings=settings, delivery_store=delivery_store),
    ]


def build_runtime_context(settings: Settings) -> RuntimeContext:
    dedup_store = DedupStore(storage_file=settings.dedup_file, max_ids=settings.dedup_max_ids)
    dedup_store.load()

    delivery_store = DeliveryStatusStore(
        storage_file=settings.delivery_status_file,
        max_records=settings.delivery_status_max_records,
    )
    delivery_store.load()

    source = MockTweetSource()
    if settings.twitter_provider == "twikit":
        source = TwikitTweetSource(settings)
    elif settings.twitter_provider != "mock":
        raise ValueError(f"Unsupported TWITTER_PROVIDER: {settings.twitter_provider}")

    collector = TwitterCollector(source=source, settings=settings, dedup_store=dedup_store)
    return RuntimeContext(
        settings=settings,
        collector=collector,
        queue=InMemoryMessageQueue[TweetEvent](),
        publishers=build_publishers(settings, delivery_store),
        target_map=settings.target_map(),
        post_publish_hooks=[HonorBoardService()],
    )


async def run_cycle(context: RuntimeContext) -> RuntimeCycleReport:
    events, target_results = await context.collector.collect()
    for event in events:
        await context.queue.put(event)

    publish_attempts: List[PublishAttempt] = []
    while not context.queue.empty():
        event = await context.queue.get()
        try:
            target = context.target_map.get((event.author or "").strip().lower())
            if target is None:
                continue

            for publisher in context.publishers:
                record = await publisher.process_event(event, target)
                if record is None:
                    continue
                publish_attempts.append(PublishAttempt(event=event, record=record))
                for hook in context.post_publish_hooks:
                    await hook.handle_delivery(event, record)
        finally:
            context.queue.task_done()

    return RuntimeCycleReport(
        collected_count=len(events),
        target_results=target_results,
        publish_attempts=publish_attempts,
    )


def build_test_target() -> MonitorTarget:
    routes = build_disabled_platform_routes()
    for platform in routes:
        routes[platform] = PlatformRoute(enabled=True)
    return MonitorTarget(username="ui_test", enabled=True, platforms=routes)


def build_test_event() -> TweetEvent:
    now = datetime.now(timezone.utc)
    return TweetEvent(
        tweet_id=f"ui-test-{int(now.timestamp())}",
        author="ui_test",
        text="测试消息",
        url="",
        created_at=now.isoformat(),
    )


async def test_publishers(settings: Settings) -> dict[str, DeliveryRecord]:
    delivery_store = DeliveryStatusStore(
        storage_file=settings.delivery_status_file,
        max_records=settings.delivery_status_max_records,
    )
    delivery_store.load()
    publishers = build_publishers(settings, delivery_store)

    event = build_test_event()
    target = build_test_target()
    results: dict[str, DeliveryRecord] = {}
    for publisher in publishers:
        record = await publisher.process_event(event, target)
        if record is None:
            record = DeliveryRecord.create(
                platform=publisher.platform,
                tweet_id=event.tweet_id,
                status="skipped",
                reason="publisher disabled",
                retryable=False,
            )
        results[publisher.platform] = record
    return results
