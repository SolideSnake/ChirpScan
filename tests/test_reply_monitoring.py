import os
import tempfile
import unittest
from unittest.mock import patch

from src.collector.twitter_collector import TwikitTweetSource, TwitterCollector
from src.config.settings import load_settings
from src.models.event import TweetEvent
from src.notifier.binance_square_notifier import BinanceSquareNotifier
from src.notifier.feishu_notifier import FeishuNotifier
from src.notifier.telegram_notifier import TelegramNotifier
from src.runtime.manager import RuntimeManager
from src.store.dedup_store import DedupStore
from src.store.delivery_status_store import DeliveryStatusStore


def _monitor_targets(include_replies: bool | None = None) -> str:
    include_fragment = "" if include_replies is None else f',"include_replies":{str(include_replies).lower()}'
    return (
        '[{"username":"elonmusk","enabled":true'
        f'{include_fragment},'
        '"platforms":{'
        '"telegram":{"enabled":true,"include_keywords":"","exclude_keywords":""},'
        '"feishu":{"enabled":true,"include_keywords":"","exclude_keywords":""},'
        '"binance_square":{"enabled":true,"include_keywords":"","exclude_keywords":""}'
        "}}]"
    )


def _load_settings(include_replies: bool | None = None):
    env = {
        "TWITTER_PROVIDER": "mock",
        "TWITTER_BOOTSTRAP_DROP_EXISTING": "false",
        "MONITOR_TARGETS": _monitor_targets(include_replies),
        "RETRY_MAX_ATTEMPTS": "1",
        "BINANCE_RETRY_MAX_ATTEMPTS": "1",
    }
    with patch.dict(os.environ, env, clear=True):
        return load_settings()


def _tweet_response(
    *,
    user_id: str = "123",
    tweet_id: str = "tweet-1",
    extra_legacy: dict[str, object] | None = None,
) -> dict[str, object]:
    legacy = {
        "user_id_str": user_id,
        "id_str": tweet_id,
        "full_text": "reply body",
        "created_at": "Tue May 05 00:00:00 +0000 2026",
        "conversation_id_str": tweet_id,
    }
    legacy.update(extra_legacy or {})
    return {
        "data": {
            "timeline": {
                "entries": [
                    {
                        "content": {
                            "itemContent": {
                                "tweet_results": {
                                    "result": {
                                        "rest_id": tweet_id,
                                        "legacy": legacy,
                                    }
                                }
                            }
                        }
                    }
                ]
            }
        }
    }


def _reply_event() -> TweetEvent:
    return TweetEvent(
        tweet_id="reply-1",
        author="elonmusk",
        text="reply body",
        url="https://x.com/elonmusk/status/reply-1",
        created_at="",
        tweet_type="reply",
        in_reply_to_status_id="root-1",
        in_reply_to_user="satoshi",
        conversation_id="root-1",
    )


class _RecordingSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, bool]] = []

    async def fetch_latest(self, username: str, limit: int, include_replies: bool = False) -> list[TweetEvent]:
        self.calls.append((username, limit, include_replies))
        return [
            TweetEvent(
                tweet_id=f"{username}-1",
                author=username,
                text="hello",
                url=f"https://x.com/{username}/status/1",
                created_at="",
            )
        ]


class _FakeGql:
    def __init__(self, *, replies_fail: bool = False) -> None:
        self.calls: list[str] = []
        self.replies_fail = replies_fail

    async def user_tweets(self, user_id: str, count: int, cursor: object) -> tuple[dict[str, object], None]:
        self.calls.append("user_tweets")
        return _tweet_response(user_id=user_id), None

    async def user_tweets_and_replies(self, user_id: str, count: int, cursor: object) -> tuple[dict[str, object], None]:
        self.calls.append("user_tweets_and_replies")
        if self.replies_fail:
            raise RuntimeError("status: 404, message: \"\"")
        return _tweet_response(user_id=user_id), None


class _FakeClient:
    def __init__(self, *, replies_fail: bool = False) -> None:
        self.gql = _FakeGql(replies_fail=replies_fail)


class ReplyMonitoringTests(unittest.IsolatedAsyncioTestCase):
    def test_old_target_defaults_to_main_posts_only(self) -> None:
        settings = _load_settings(include_replies=None)

        self.assertFalse(settings.monitor_targets[0].include_replies)

    def test_include_replies_loads_from_target_config(self) -> None:
        settings = _load_settings(include_replies=True)

        self.assertTrue(settings.monitor_targets[0].include_replies)

    def test_status_returns_include_replies_for_frontend(self) -> None:
        env = {
            "TWITTER_PROVIDER": "mock",
            "MONITOR_TARGETS": _monitor_targets(True),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, env, clear=True):
                manager = RuntimeManager(config_file=os.path.join(tmpdir, "ui_config.json"))
                status = manager.status()

        self.assertTrue(status["monitor_targets"][0]["include_replies"])

    async def test_collector_passes_target_include_replies_to_source(self) -> None:
        settings = _load_settings(include_replies=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            source = _RecordingSource()
            collector = TwitterCollector(
                source=source,
                settings=settings,
                dedup_store=DedupStore(os.path.join(tmpdir, "dedup.json")),
            )

            events, _ = await collector.collect()

        self.assertEqual(len(events), 1)
        self.assertEqual(source.calls, [("elonmusk", settings.twitter_fetch_limit, True)])

    async def test_twikit_uses_posts_or_posts_with_replies_endpoint(self) -> None:
        settings = _load_settings(include_replies=True)
        source = TwikitTweetSource(settings)
        fake_client = _FakeClient()
        source._client = fake_client
        source._ready = True
        source._user_cache["elonmusk"] = "123"

        await source._fetch_user_tweets("elonmusk", 5, include_replies=False)
        await source._fetch_user_tweets("elonmusk", 5, include_replies=True)

        self.assertEqual(fake_client.gql.calls, ["user_tweets", "user_tweets_and_replies"])

    async def test_twikit_falls_back_to_posts_when_replies_endpoint_404s(self) -> None:
        settings = _load_settings(include_replies=True)
        source = TwikitTweetSource(settings)
        fake_client = _FakeClient(replies_fail=True)
        source._client = fake_client
        source._ready = True
        source._user_cache["elonmusk"] = "123"

        events = await source._fetch_user_tweets("elonmusk", 5, include_replies=True)

        self.assertEqual(len(events), 1)
        self.assertEqual(fake_client.gql.calls, ["user_tweets_and_replies", "user_tweets"])

    def test_reply_metadata_is_parsed_from_legacy_tweet(self) -> None:
        settings = _load_settings(include_replies=True)
        source = TwikitTweetSource(settings)

        events = source._timeline_response_to_events(
            _tweet_response(
                tweet_id="reply-1",
                extra_legacy={
                    "conversation_id_str": "root-1",
                    "in_reply_to_status_id_str": "root-1",
                    "in_reply_to_screen_name": "satoshi",
                },
            ),
            username="elonmusk",
            user_id="123",
            limit=5,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].tweet_type, "reply")
        self.assertEqual(events[0].in_reply_to_status_id, "root-1")
        self.assertEqual(events[0].in_reply_to_user, "satoshi")
        self.assertEqual(events[0].conversation_id, "root-1")

    def test_notification_text_distinguishes_replies(self) -> None:
        settings = _load_settings(include_replies=True)

        telegram_text = TelegramNotifier(settings)._format_message(_reply_event())
        feishu_text = FeishuNotifier(settings)._format_message(_reply_event())

        self.assertIn("回复了 @satoshi", telegram_text)
        self.assertIn("回复了 @satoshi", feishu_text)

    async def test_binance_skips_reply_events(self) -> None:
        settings = _load_settings(include_replies=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            store = DeliveryStatusStore(os.path.join(tmpdir, "delivery.json"))
            notifier = BinanceSquareNotifier(settings, store)

            record = await notifier.process_event(_reply_event(), settings.monitor_targets[0])

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.status, "skipped")
        self.assertEqual(record.reason, "reply skipped for binance")


if __name__ == "__main__":
    unittest.main()
