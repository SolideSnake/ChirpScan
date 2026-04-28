import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from src.config.settings import load_settings
from src.models.event import TweetEvent
from src.notifier.feishu_notifier import FeishuNotifier


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _event(text: str = "hello") -> TweetEvent:
    return TweetEvent(
        tweet_id="tweet-1",
        author="elonmusk",
        text=text,
        url="https://x.com/elonmusk/status/tweet-1",
        created_at="",
    )


def _load_feishu_settings(extra_env: dict[str, str] | None = None):
    env = {
        "MONITOR_TARGETS": '[{"username":"elonmusk","enabled":true,"platforms":{"telegram":{"enabled":false},"feishu":{"enabled":true},"binance_square":{"enabled":false}}}]',
        "RETRY_MAX_ATTEMPTS": "1",
        "RETRY_BASE_DELAY_SEC": "0.1",
    }
    env.update(extra_env or {})
    with tempfile.TemporaryDirectory() as tmpdir:
        env["DELIVERY_STATUS_FILE"] = os.path.join(tmpdir, "delivery.json")
        with patch.dict(os.environ, env, clear=False):
            return load_settings()


class FeishuNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_webhook_returns_failed_record(self) -> None:
        settings = _load_feishu_settings()
        notifier = FeishuNotifier(settings)

        record = await notifier.process_event(_event(), settings.monitor_targets[0])

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.status, "failed")
        self.assertIn("FEISHU_WEBHOOK_URL", record.reason)

    def test_payload_without_secret_has_no_signature_fields(self) -> None:
        settings = _load_feishu_settings({"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/test"})
        notifier = FeishuNotifier(settings)

        payload = notifier._build_payload("hello")

        self.assertEqual(payload["msg_type"], "text")
        self.assertEqual(payload["content"], {"text": "hello"})
        self.assertNotIn("timestamp", payload)
        self.assertNotIn("sign", payload)

    def test_payload_with_secret_has_valid_signature(self) -> None:
        settings = _load_feishu_settings(
            {
                "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/test",
                "FEISHU_SECRET": "secret",
            }
        )
        notifier = FeishuNotifier(settings)
        timestamp = "1700000000"
        expected = base64.b64encode(
            hmac.new(
                f"{timestamp}\nsecret".encode("utf-8"),
                b"",
                digestmod=hashlib.sha256,
            ).digest()
        ).decode("utf-8")

        with patch("src.notifier.feishu_notifier.time.time", return_value=int(timestamp)):
            payload = notifier._build_payload("hello")

        self.assertEqual(payload["timestamp"], timestamp)
        self.assertEqual(payload["sign"], expected)

    async def test_success_response_returns_success_record(self) -> None:
        settings = _load_feishu_settings({"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/test"})
        notifier = FeishuNotifier(settings)
        with patch("src.notifier.feishu_notifier.request.urlopen", return_value=_FakeResponse({"code": 0})):
            record = await notifier.process_event(_event(), settings.monitor_targets[0])

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.status, "success")

    async def test_nonzero_response_retries_then_fails(self) -> None:
        settings = _load_feishu_settings(
            {
                "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/test",
                "RETRY_MAX_ATTEMPTS": "2",
            }
        )
        notifier = FeishuNotifier(settings)

        with (
            patch("src.notifier.feishu_notifier.request.urlopen", return_value=_FakeResponse({"code": 999, "msg": "bad"})),
            patch("src.notifier.feishu_notifier.asyncio.sleep", new_callable=AsyncMock),
        ):
            record = await notifier.process_event(_event(), settings.monitor_targets[0])

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.attempts, 2)
        self.assertIn("bad", record.reason)


if __name__ == "__main__":
    unittest.main()
