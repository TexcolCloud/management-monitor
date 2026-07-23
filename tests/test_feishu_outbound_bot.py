from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

from common.feishu_app_bot import (
    AUTH_URL,
    IMAGE_UPLOAD_URL,
    MESSAGE_URL,
    FeishuOutboundBot,
    FeishuPermanentError,
    FeishuRetryPolicy,
    FeishuRetryableError,
)


def response_context(payload: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    context = MagicMock()
    context.__enter__.return_value = response
    return context


class FeishuOutboundBotTest(unittest.TestCase):
    @staticmethod
    def http_error(status: int, reason: str = "remote failure") -> urllib.error.HTTPError:
        return urllib.error.HTTPError(MESSAGE_URL, status, reason, {}, None)

    def test_send_work_order_uses_outbound_api_without_callback(self) -> None:
        bot = FeishuOutboundBot("app-id", "app-secret", "chat-id", 12)
        with patch(
            "common.feishu_app_bot.urllib.request.urlopen",
            side_effect=[
                response_context({"code": 0, "tenant_access_token": "token", "expire": 3600}),
                response_context({"code": 0}),
            ],
        ) as urlopen:
            bot.send_work_order({"safetyCode": "CODE-1", "theme": "New order"})

        auth_request = urlopen.call_args_list[0].args[0]
        message_request = urlopen.call_args_list[1].args[0]
        payload = json.loads(message_request.data.decode("utf-8"))
        self.assertEqual(auth_request.full_url, AUTH_URL)
        self.assertEqual(message_request.full_url, f"{MESSAGE_URL}?receive_id_type=chat_id")
        self.assertEqual(message_request.get_header("Authorization"), "Bearer token")
        self.assertEqual(payload["receive_id"], "chat-id")
        self.assertEqual(payload["msg_type"], "interactive")

    def test_send_work_order_supports_direct_user_recipient(self) -> None:
        bot = FeishuOutboundBot(
            "app-id", "app-secret", "ou_user", receive_id_type="open_id"
        )
        with patch(
            "common.feishu_app_bot.urllib.request.urlopen",
            side_effect=[
                response_context({"code": 0, "tenant_access_token": "token", "expire": 3600}),
                response_context({"code": 0}),
            ],
        ) as urlopen:
            bot.send_work_order({"safetyCode": "CODE-1"})

        message_request = urlopen.call_args_list[1].args[0]
        payload = json.loads(message_request.data.decode("utf-8"))
        self.assertEqual(message_request.full_url, f"{MESSAGE_URL}?receive_id_type=open_id")
        self.assertEqual(payload["receive_id"], "ou_user")

    def test_send_image_uploads_then_posts_image_key(self) -> None:
        bot = FeishuOutboundBot("app-id", "app-secret", "chat-id")
        bot._tenant_access_token = "token"
        bot._token_expires_at = 2_000_000_000
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "evidence.png"
            image.write_bytes(b"png-content")
            with patch("common.feishu_app_bot.time.time", return_value=1_700_000_000), patch(
                "common.feishu_app_bot.urllib.request.urlopen",
                side_effect=[
                    response_context({"code": 0, "data": {"image_key": "img_123"}}),
                    response_context({"code": 0}),
                ],
            ) as urlopen:
                bot.send_image(str(image))

        upload_request = urlopen.call_args_list[0].args[0]
        message_request = urlopen.call_args_list[1].args[0]
        payload = json.loads(message_request.data.decode("utf-8"))
        self.assertEqual(upload_request.full_url, IMAGE_UPLOAD_URL)
        self.assertIn(b"png-content", upload_request.data)
        self.assertEqual(payload["msg_type"], "image")
        self.assertEqual(json.loads(payload["content"])["image_key"], "img_123")

    def test_network_failure_retries_with_injected_sleep(self) -> None:
        sleep = Mock()
        bot = FeishuOutboundBot(
            "app-id",
            "app-secret",
            "chat-id",
            retry_policy=FeishuRetryPolicy(3, 0.25, 1),
            _sleep=sleep,
        )
        with patch(
            "common.feishu_app_bot.urllib.request.urlopen",
            side_effect=[
                urllib.error.URLError("credential-like-detail"),
                response_context({"code": 0}),
            ],
        ):
            result = bot._request_json(MESSAGE_URL, {"value": "not-logged"})

        self.assertEqual(result, {"code": 0})
        sleep.assert_called_once_with(0.25)

    def test_retryable_http_failure_has_bounded_exponential_backoff(self) -> None:
        sleep = Mock()
        bot = FeishuOutboundBot(
            "app-id",
            "app-secret",
            "chat-id",
            retry_policy=FeishuRetryPolicy(3, 0.5, 0.75),
            _sleep=sleep,
        )
        with patch(
            "common.feishu_app_bot.urllib.request.urlopen",
            side_effect=[self.http_error(500, "secret-one") for _ in range(3)],
        ):
            with self.assertRaises(FeishuRetryableError) as raised:
                bot._request_json(MESSAGE_URL, {"app_secret": "must-not-leak"})

        self.assertEqual(raised.exception.status_code, 500)
        self.assertNotIn("secret", str(raised.exception).lower())
        self.assertEqual(sleep.call_args_list, [call(0.5), call(0.75)])

    def test_http_4xx_is_permanent_and_does_not_retry(self) -> None:
        sleep = Mock()
        bot = FeishuOutboundBot(
            "app-id",
            "app-secret",
            "chat-id",
            retry_policy=FeishuRetryPolicy(3, 0.25, 1),
            _sleep=sleep,
        )
        with patch(
            "common.feishu_app_bot.urllib.request.urlopen",
            side_effect=self.http_error(400, "app-secret"),
        ) as urlopen:
            with self.assertRaises(FeishuPermanentError) as raised:
                bot._request_json(MESSAGE_URL, {"app_secret": "app-secret"})

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(urlopen.call_count, 1)
        self.assertNotIn("app-secret", str(raised.exception))
        sleep.assert_not_called()

    def test_http_429_and_timeout_are_retryable(self) -> None:
        for failure in (self.http_error(429), TimeoutError("socket detail")):
            with self.subTest(failure=type(failure).__name__):
                sleep = Mock()
                bot = FeishuOutboundBot(
                    "app-id",
                    "app-secret",
                    "chat-id",
                    retry_policy=FeishuRetryPolicy(2, 0.1, 1),
                    _sleep=sleep,
                )
                with patch(
                    "common.feishu_app_bot.urllib.request.urlopen",
                    side_effect=[failure, response_context({"code": 0})],
                ):
                    self.assertEqual(bot._request_json(MESSAGE_URL, {}), {"code": 0})
                sleep.assert_called_once_with(0.1)

    def test_feishu_business_error_is_permanent_and_redacted(self) -> None:
        sleep = Mock()
        bot = FeishuOutboundBot(
            "app-id",
            "app-secret",
            "chat-id",
            retry_policy=FeishuRetryPolicy(3, 0.1, 1),
            _sleep=sleep,
        )
        with patch(
            "common.feishu_app_bot.urllib.request.urlopen",
            return_value=response_context(
                {"code": 99991663, "msg": "app-secret must not leak"}
            ),
        ) as urlopen:
            with self.assertRaises(FeishuPermanentError) as raised:
                bot._request_json(MESSAGE_URL, {"app_secret": "app-secret"})

        self.assertEqual(raised.exception.api_code, 99991663)
        self.assertNotIn("app-secret", str(raised.exception))
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_secret_is_redacted_from_dataclass_representation(self) -> None:
        representation = repr(FeishuOutboundBot("app-id", "top-secret", "chat-id"))
        self.assertNotIn("top-secret", representation)
        self.assertNotIn("chat-id", representation)


if __name__ == "__main__":
    unittest.main()
