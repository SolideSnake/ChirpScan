import argparse
import asyncio
import logging

from src.collector.twitter_collector import MockTweetSource, TwikitTweetSource, TwitterCollector
from src.config.settings import load_settings
from src.models.event import TweetEvent
from src.notifier.telegram_notifier import TelegramNotifier
from src.queue.in_memory_queue import InMemoryMessageQueue
from src.store.dedup_store import DedupStore


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def _drain_queue(
    queue: InMemoryMessageQueue[TweetEvent],
    notifier: TelegramNotifier,
    log: logging.Logger,
) -> None:
    while not queue.empty():
        event = await queue.get()
        try:
            should_send, _reason = notifier.should_send_event(event)
            if not should_send:
                continue
            sent = await notifier.send_event(event)
            if not sent:
                log.error("Dropping unsent tweet %s after retries.", event.tweet_id)
        finally:
            queue.task_done()


async def _run(max_loops: int | None) -> None:
    settings = load_settings()
    _configure_logging(settings.log_level)
    log = logging.getLogger("main")

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

    loop_count = 0
    while True:
        loop_count += 1
        events = await collector.collect()
        if events:
            log.info("Collected %d new tweet events.", len(events))
        else:
            log.debug("No new events.")

        for event in events:
            await queue.put(event)

        await _drain_queue(queue=queue, notifier=notifier, log=log)

        if max_loops is not None and loop_count >= max_loops:
            log.info("Reached max loops (%d), exiting.", max_loops)
            break

        await asyncio.sleep(settings.twitter_poll_interval_sec)


def main() -> None:
    parser = argparse.ArgumentParser(description="Twitter/X to Telegram notifier")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one polling loop then exit (for smoke test).",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_run(max_loops=1 if args.once else None))
    except KeyboardInterrupt:
        logging.getLogger("main").info("Interrupted, exiting.")


if __name__ == "__main__":
    main()

