import os
import tempfile
import unittest
from unittest.mock import patch

from src.config.settings import load_settings
from src.models.event import TweetEvent
from src.notifier.binance_square_notifier import BinanceSquareNotifier, clean_binance_body_text
from src.store.delivery_status_store import DeliveryStatusStore


def _event(text: str) -> TweetEvent:
    return TweetEvent(
        tweet_id="tweet-1",
        author="elonmusk",
        text=text,
        url="https://x.com/elonmusk/status/tweet-1",
        created_at="",
    )


class BinanceSquareBodyTests(unittest.IsolatedAsyncioTestCase):
    def test_clean_body_preserves_blank_lines(self) -> None:
        self.assertEqual(
            clean_binance_body_text("第一段\r\n\r\n第二段"),
            "第一段\n\n第二段",
        )

    def test_clean_body_removes_urls(self) -> None:
        self.assertEqual(clean_binance_body_text("正文 https://t.co/abc"), "正文")

    @patch.dict(
        os.environ,
        {
            "MONITOR_TARGETS": '[{"username":"elonmusk","enabled":true,"platforms":{"telegram":{"enabled":false},"binance_square":{"enabled":true}}}]',
        },
        clear=False,
    )
    def test_build_body_does_not_silently_truncate_long_text(self) -> None:
        long_text = "long body " * 120
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {"DELIVERY_STATUS_FILE": os.path.join(tmpdir, "delivery.json")},
                clear=False,
            ):
                settings = load_settings()
                store = DeliveryStatusStore(settings.delivery_status_file)
                notifier = BinanceSquareNotifier(settings, store)

                body_text = notifier._build_body_text(_event(long_text))

        self.assertEqual(body_text, long_text.strip())
        self.assertNotIn("...", body_text)

    @patch.dict(
        os.environ,
        {
            "BINANCE_PUBLISH_TEMPLATE": "plain_with_link",
            "MONITOR_TARGETS": '[{"username":"elonmusk","enabled":true,"platforms":{"telegram":{"enabled":false},"binance_square":{"enabled":true}}}]',
        },
        clear=False,
    )
    def test_legacy_template_does_not_append_original_tweet_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {"DELIVERY_STATUS_FILE": os.path.join(tmpdir, "delivery.json")},
                clear=False,
            ):
                settings = load_settings()
                store = DeliveryStatusStore(settings.delivery_status_file)
                notifier = BinanceSquareNotifier(settings, store)

                body_text = notifier._build_body_text(_event("正文"))

        self.assertEqual(body_text, "正文")
        self.assertNotIn("https://x.com", body_text)

    @patch.dict(
        os.environ,
        {
            "MONITOR_TARGETS": '[{"username":"elonmusk","enabled":true,"platforms":{"telegram":{"enabled":false},"binance_square":{"enabled":true}}}]',
        },
        clear=False,
    )
    async def test_empty_body_after_url_cleanup_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {"DELIVERY_STATUS_FILE": os.path.join(tmpdir, "delivery.json")},
                clear=False,
            ):
                settings = load_settings()
                store = DeliveryStatusStore(settings.delivery_status_file)
                notifier = BinanceSquareNotifier(settings, store)

                record = await notifier.process_event(
                    _event("https://t.co/image"),
                    settings.monitor_targets[0],
                )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.status, "skipped")
        self.assertEqual(record.reason, "empty body after url cleanup")


if __name__ == "__main__":
    unittest.main()
