import argparse
import asyncio
import logging

from src.config.settings import load_settings
from src.runtime.engine import build_runtime_context, run_cycle


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def _run(max_loops: int | None) -> None:
    settings = load_settings()
    _configure_logging(settings.log_level)
    log = logging.getLogger("main")
    runtime = build_runtime_context(settings)

    loop_count = 0
    while True:
        loop_count += 1
        report = await run_cycle(runtime)
        if report.collected_count:
            log.info("Collected %d new tweet events.", report.collected_count)
        else:
            log.debug("No new events.")

        for attempt in report.publish_attempts:
            if not attempt.record.success and attempt.record.status != "skipped":
                log.error(
                    "Failed to handle tweet %s on %s: %s",
                    attempt.event.tweet_id,
                    attempt.record.platform,
                    attempt.record.reason,
                )

        if max_loops is not None and loop_count >= max_loops:
            log.info("Reached max loops (%d), exiting.", max_loops)
            break

        await asyncio.sleep(settings.twitter_poll_interval_sec)


def main() -> None:
    parser = argparse.ArgumentParser(description="Twitter/X monitor with platform delivery modules")
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
