import os
import tempfile
import unittest
from unittest.mock import patch

from src.collector.twitter_collector import TwikitTweetSource, TwitterCollector
from src.config.settings import MONITOR_MODE_REPLIES, MONITOR_MODE_TWEETS, MONITOR_MODE_TWEETS_AND_REPLIES
from src.config.settings import load_settings
from src.models.event import TweetEvent
from src.notifier.binance_square_notifier import BinanceSquareNotifier
from src.notifier.feishu_notifier import FeishuNotifier
from src.notifier.telegram_notifier import TelegramNotifier
from src.runtime.manager import RuntimeManager
from src.store.dedup_store import DedupStore
from src.store.delivery_status_store import DeliveryStatusStore


def _monitor_targets(include_replies: bool | None = None, monitor_mode: str | None = None) -> str:
    include_fragment = "" if include_replies is None else f',"include_replies":{str(include_replies).lower()}'
    mode_fragment = "" if monitor_mode is None else f',"monitor_mode":"{monitor_mode}"'
    return (
        '[{"username":"elonmusk","enabled":true'
        f'{include_fragment}{mode_fragment},'
        '"platforms":{'
        '"telegram":{"enabled":true,"include_keywords":"","exclude_keywords":""},'
        '"feishu":{"enabled":true,"include_keywords":"","exclude_keywords":""},'
        '"binance_square":{"enabled":true,"include_keywords":"","exclude_keywords":""}'
        "}}]"
    )


def _load_settings(include_replies: bool | None = None, monitor_mode: str | None = None):
    env = {
        "TWITTER_PROVIDER": "mock",
        "TWITTER_BOOTSTRAP_DROP_EXISTING": "false",
        "MONITOR_TARGETS": _monitor_targets(include_replies, monitor_mode),
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


def _mixed_timeline_response() -> dict[str, object]:
    def result(tweet_id: str, legacy: dict[str, object]) -> dict[str, object]:
        return {
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

    return {
        "data": {
            "timeline": {
                "entries": [
                    result(
                        "post-1",
                        {
                            "user_id_str": "123",
                            "id_str": "post-1",
                            "full_text": "post body",
                            "created_at": "Tue May 05 00:00:00 +0000 2026",
                            "conversation_id_str": "post-1",
                        },
                    ),
                    result(
                        "reply-1",
                        {
                            "user_id_str": "123",
                            "id_str": "reply-1",
                            "full_text": "reply body",
                            "created_at": "Tue May 05 00:00:00 +0000 2026",
                            "conversation_id_str": "post-1",
                            "in_reply_to_status_id_str": "post-1",
                            "in_reply_to_user_id_str": "456",
                            "in_reply_to_screen_name": "satoshi",
                        },
                    ),
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
        in_reply_to_tweet_id="root-1",
        in_reply_to_user="satoshi",
        in_reply_to_user_id="456",
        conversation_id="root-1",
    )


class _RecordingSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    async def fetch_latest(self, username: str, limit: int, monitor_mode: str = MONITOR_MODE_TWEETS) -> list[TweetEvent]:
        self.calls.append((username, limit, monitor_mode))
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
    def __init__(
        self,
        *,
        replies_fail: bool = False,
        replies_fail_once: bool = False,
        timeline_post_fail: bool = False,
        search_post_fail: bool = False,
        response: dict[str, object] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.replies_fail = replies_fail
        self.replies_fail_once = replies_fail_once
        self.timeline_post_fail = timeline_post_fail
        self.search_post_fail = search_post_fail
        self.response = response

    async def user_tweets(self, user_id: str, count: int, cursor: object) -> tuple[dict[str, object], None]:
        self.calls.append("user_tweets")
        return self.response or _tweet_response(user_id=user_id), None

    async def user_tweets_and_replies(self, user_id: str, count: int, cursor: object) -> tuple[dict[str, object], None]:
        self.calls.append("user_tweets_and_replies")
        if self.replies_fail:
            raise RuntimeError("status: 404, message: \"\"")
        if self.replies_fail_once:
            self.replies_fail_once = False
            raise RuntimeError("status: 404, message: \"\"")
        return self.response or _tweet_response(user_id=user_id), None

    async def gql_post(
        self,
        url: str,
        variables: dict[str, object],
        features: object = None,
        headers: object = None,
        extra_data: object = None,
        **kwargs: object,
    ) -> tuple[dict[str, object], None]:
        del variables, features, headers, extra_data, kwargs
        if "SearchTimeline" in url:
            self.calls.append("post:SearchTimeline")
            if self.search_post_fail:
                raise RuntimeError("status: 404, message: \"\"")
            return _mixed_timeline_response(), None

        self.calls.append("post:UserTweetsAndReplies")
        if self.timeline_post_fail:
            raise RuntimeError("status: 404, message: \"\"")
        return self.response or _tweet_response(), None


class _FakeTweet:
    def __init__(self, tweet_id: str = "reply-search-1") -> None:
        self.id = tweet_id
        self.full_text = "reply from search"
        self.text = "reply from search"
        self.created_at = "Tue May 05 00:00:00 +0000 2026"
        self._legacy = {
            "id_str": tweet_id,
            "user_id_str": "123",
            "conversation_id_str": "root-search-1",
            "in_reply_to_status_id_str": "root-search-1",
            "in_reply_to_user_id_str": "456",
            "in_reply_to_screen_name": "satoshi",
        }

    @property
    def in_reply_to(self) -> str:
        return str(self._legacy["in_reply_to_status_id_str"])


class _FakeClient:
    def __init__(
        self,
        *,
        replies_fail: bool = False,
        replies_fail_once: bool = False,
        timeline_post_fail: bool = False,
        search_post_fail: bool = False,
        response: dict[str, object] | None = None,
        search_fail: bool = False,
        search_response: list[object] | None = None,
    ) -> None:
        self.gql = _FakeGql(
            replies_fail=replies_fail,
            replies_fail_once=replies_fail_once,
            timeline_post_fail=timeline_post_fail,
            search_post_fail=search_post_fail,
            response=response,
        )
        self.search_fail = search_fail
        self.search_response = search_response if search_response is not None else [_FakeTweet()]
        self.search_calls: list[tuple[str, str, int]] = []

    async def search_tweet(self, query: str, product: str, count: int, cursor: object = None) -> list[object]:
        del cursor
        self.search_calls.append((query, product, count))
        if self.search_fail:
            raise RuntimeError("status: 404, message: \"\"")
        return self.search_response


class ReplyMonitoringTests(unittest.IsolatedAsyncioTestCase):
    def test_old_target_defaults_to_main_posts_only(self) -> None:
        settings = _load_settings(include_replies=None)

        self.assertFalse(settings.monitor_targets[0].include_replies)
        self.assertEqual(settings.monitor_targets[0].monitor_mode, MONITOR_MODE_TWEETS)

    def test_legacy_include_replies_maps_to_tweets_and_replies(self) -> None:
        settings = _load_settings(include_replies=True)

        self.assertTrue(settings.monitor_targets[0].include_replies)
        self.assertEqual(settings.monitor_targets[0].monitor_mode, MONITOR_MODE_TWEETS_AND_REPLIES)

    def test_monitor_mode_replies_loads_from_target_config(self) -> None:
        settings = _load_settings(monitor_mode=MONITOR_MODE_REPLIES)

        self.assertTrue(settings.monitor_targets[0].include_replies)
        self.assertEqual(settings.monitor_targets[0].monitor_mode, MONITOR_MODE_REPLIES)

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
        self.assertEqual(status["monitor_targets"][0]["monitor_mode"], MONITOR_MODE_TWEETS_AND_REPLIES)

    async def test_collector_passes_target_monitor_mode_to_source(self) -> None:
        settings = _load_settings(monitor_mode=MONITOR_MODE_REPLIES)
        with tempfile.TemporaryDirectory() as tmpdir:
            source = _RecordingSource()
            collector = TwitterCollector(
                source=source,
                settings=settings,
                dedup_store=DedupStore(os.path.join(tmpdir, "dedup.json")),
            )

            events, _ = await collector.collect()

        self.assertEqual(len(events), 1)
        self.assertEqual(source.calls, [("elonmusk", settings.twitter_fetch_limit, MONITOR_MODE_REPLIES)])

    async def test_twikit_uses_posts_or_posts_with_replies_endpoint(self) -> None:
        settings = _load_settings(include_replies=True)
        source = TwikitTweetSource(settings)
        fake_client = _FakeClient()
        source._client = fake_client
        source._ready = True
        source._user_cache["elonmusk"] = "123"

        await source._fetch_user_tweets("elonmusk", 5, monitor_mode=MONITOR_MODE_TWEETS)
        await source._fetch_user_tweets("elonmusk", 5, monitor_mode=MONITOR_MODE_TWEETS_AND_REPLIES)
        await source._fetch_user_tweets("elonmusk", 5, monitor_mode=MONITOR_MODE_REPLIES)

        self.assertEqual(fake_client.gql.calls, ["user_tweets", "user_tweets_and_replies", "user_tweets_and_replies"])

    async def test_twikit_replies_mode_filters_out_main_posts(self) -> None:
        settings = _load_settings(monitor_mode=MONITOR_MODE_REPLIES)
        source = TwikitTweetSource(settings)
        fake_client = _FakeClient(response=_mixed_timeline_response())
        source._client = fake_client
        source._ready = True
        source._user_cache["elonmusk"] = "123"

        events = await source._fetch_user_tweets("elonmusk", 1, monitor_mode=MONITOR_MODE_REPLIES)

        self.assertEqual([event.tweet_id for event in events], ["reply-1"])
        self.assertEqual(events[0].in_reply_to_tweet_id, "post-1")
        self.assertEqual(events[0].in_reply_to_user_id, "456")

    async def test_twikit_uses_search_fallback_when_replies_endpoint_404s(self) -> None:
        settings = _load_settings(include_replies=True)
        source = TwikitTweetSource(settings)
        fake_client = _FakeClient(replies_fail=True, timeline_post_fail=True)
        source._client = fake_client
        source._ready = True
        source._user_cache["elonmusk"] = "123"

        with patch.object(source, "_patch_twikit_graphql_endpoint", return_value=False):
            events = await source._fetch_user_tweets("elonmusk", 5, monitor_mode=MONITOR_MODE_TWEETS_AND_REPLIES)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[1].tweet_id, "reply-1")
        self.assertEqual(events[1].tweet_type, "reply")
        self.assertEqual(
            fake_client.gql.calls,
            ["user_tweets_and_replies", "post:UserTweetsAndReplies", "post:SearchTimeline", "user_tweets"],
        )

    async def test_twikit_replies_mode_uses_search_fallback_when_timeline_404s(self) -> None:
        settings = _load_settings(monitor_mode=MONITOR_MODE_REPLIES)
        source = TwikitTweetSource(settings)
        fake_client = _FakeClient(replies_fail=True, timeline_post_fail=True)
        source._client = fake_client
        source._ready = True
        source._user_cache["elonmusk"] = "123"

        with patch.object(source, "_patch_twikit_graphql_endpoint", return_value=False):
            events = await source._fetch_user_tweets("elonmusk", 5, monitor_mode=MONITOR_MODE_REPLIES)

        self.assertEqual([event.tweet_id for event in events], ["reply-1"])
        self.assertEqual(
            fake_client.gql.calls,
            ["user_tweets_and_replies", "post:UserTweetsAndReplies", "post:SearchTimeline"],
        )

    async def test_twikit_falls_back_to_posts_when_search_fallback_404s(self) -> None:
        settings = _load_settings(include_replies=True)
        source = TwikitTweetSource(settings)
        fake_client = _FakeClient(replies_fail=True, timeline_post_fail=True, search_post_fail=True)
        source._client = fake_client
        source._ready = True
        source._user_cache["elonmusk"] = "123"

        with patch.object(source, "_patch_twikit_graphql_endpoint", return_value=False):
            events = await source._fetch_user_tweets("elonmusk", 5, monitor_mode=MONITOR_MODE_TWEETS_AND_REPLIES)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].tweet_type, "post")
        self.assertEqual(
            fake_client.gql.calls,
            ["user_tweets_and_replies", "post:UserTweetsAndReplies", "post:SearchTimeline", "user_tweets"],
        )

    async def test_twikit_refreshes_replies_endpoint_after_404(self) -> None:
        settings = _load_settings(include_replies=True)
        source = TwikitTweetSource(settings)
        fake_client = _FakeClient(replies_fail_once=True)
        source._client = fake_client
        source._ready = True
        source._user_cache["elonmusk"] = "123"

        with patch.object(source, "_patch_twikit_graphql_endpoint", return_value=True):
            events = await source._fetch_user_tweets("elonmusk", 5, monitor_mode=MONITOR_MODE_TWEETS_AND_REPLIES)

        self.assertEqual(len(events), 1)
        self.assertEqual(fake_client.gql.calls, ["user_tweets_and_replies", "post:UserTweetsAndReplies"])

    def test_reply_metadata_is_parsed_from_legacy_tweet(self) -> None:
        settings = _load_settings(include_replies=True)
        source = TwikitTweetSource(settings)

        events = source._timeline_response_to_events(
            _tweet_response(
                tweet_id="reply-1",
                extra_legacy={
                    "conversation_id_str": "root-1",
                    "in_reply_to_status_id_str": "root-1",
                    "in_reply_to_user_id_str": "456",
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
        self.assertEqual(events[0].in_reply_to_tweet_id, "root-1")
        self.assertEqual(events[0].in_reply_to_user, "satoshi")
        self.assertEqual(events[0].in_reply_to_user_id, "456")
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
