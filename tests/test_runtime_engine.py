import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from src.config.settings import load_settings
from src.runtime.engine import build_runtime_context, run_cycle


class RuntimeEngineTests(unittest.IsolatedAsyncioTestCase):
    @patch.dict(
        os.environ,
        {
            "TWITTER_PROVIDER": "mock",
            "TWITTER_BOOTSTRAP_DROP_EXISTING": "false",
            "MONITOR_TARGETS": '[{"username":"elonmusk","enabled":true,"platforms":{"telegram":{"enabled":true,"include_keywords":"","exclude_keywords":""},"binance_square":{"enabled":false,"include_keywords":"","exclude_keywords":""}}}]',
        },
        clear=False,
    )
    async def test_run_cycle_uses_unified_publish_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "DEDUP_FILE": os.path.join(tmpdir, "dedup.json"),
                    "DELIVERY_STATUS_FILE": os.path.join(tmpdir, "delivery.json"),
                },
                clear=False,
            ):
                settings = load_settings()
                runtime = build_runtime_context(settings)

                with patch(
                    "src.notifier.telegram_notifier.TelegramNotifier._send_once",
                    new_callable=AsyncMock,
                ):
                    report = await run_cycle(runtime)

                self.assertEqual(report.collected_count, 1)
                self.assertEqual(len(report.publish_attempts), 1)
                self.assertEqual(report.publish_attempts[0].record.platform, "telegram")
                self.assertEqual(report.publish_attempts[0].record.status, "success")

    @patch.dict(
        os.environ,
        {
            "TWITTER_PROVIDER": "mock",
            "TWITTER_BOOTSTRAP_DROP_EXISTING": "false",
            "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/test",
            "BINANCE_SQUARE_API_KEY": "test-key",
            "MONITOR_TARGETS": '[{"username":"elonmusk","enabled":true,"platforms":{"telegram":{"enabled":true,"include_keywords":"","exclude_keywords":""},"feishu":{"enabled":true,"include_keywords":"","exclude_keywords":""},"binance_square":{"enabled":true,"include_keywords":"","exclude_keywords":""}}}]',
        },
        clear=False,
    )
    async def test_run_cycle_can_publish_to_all_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "DEDUP_FILE": os.path.join(tmpdir, "dedup.json"),
                    "DELIVERY_STATUS_FILE": os.path.join(tmpdir, "delivery.json"),
                },
                clear=False,
            ):
                settings = load_settings()
                runtime = build_runtime_context(settings)

                with (
                    patch("src.notifier.telegram_notifier.TelegramNotifier._send_once", new_callable=AsyncMock),
                    patch("src.notifier.feishu_notifier.FeishuNotifier._post_once", new_callable=AsyncMock),
                    patch(
                        "src.notifier.binance_square_notifier.BinanceSquareNotifier._post_once",
                        return_value={"code": "000000", "data": {"id": "post-1"}},
                    ),
                ):
                    report = await run_cycle(runtime)

                self.assertEqual(report.collected_count, 1)
                self.assertEqual(len(report.publish_attempts), 3)
                self.assertEqual(
                    [attempt.record.platform for attempt in report.publish_attempts],
                    ["telegram", "feishu", "binance_square"],
                )
                self.assertTrue(all(attempt.record.status == "success" for attempt in report.publish_attempts))


class SettingsTests(unittest.TestCase):
    @patch.dict(
        os.environ,
        {
            "MONITOR_TARGETS": '[{"username":"elonmusk","enabled":true,"platforms":{"telegram":{"enabled":true,"include_keywords":"btc","exclude_keywords":"spam"},"binance_square":{"enabled":false,"include_keywords":"","exclude_keywords":""}}}]',
        },
        clear=False,
    )
    def test_load_settings_builds_platform_routes(self) -> None:
        settings = load_settings()
        target = settings.monitor_targets[0]

        self.assertTrue(target.route_for("telegram").enabled)
        self.assertEqual(target.route_for("telegram").include_keywords, "btc")
        self.assertFalse(target.route_for("feishu").enabled)
        self.assertFalse(target.route_for("binance_square").enabled)
        self.assertFalse(target.route_for("future_platform").enabled)


if __name__ == "__main__":
    unittest.main()
