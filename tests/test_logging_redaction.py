from __future__ import annotations

import io
import logging
import unittest
from contextlib import redirect_stderr

from common.cli import run_cli_safely
from common.logging_utils import SensitiveDataFilter
from common.redaction import REDACTED, diagnostic_error, redact_text


class LoggingRedactionTest(unittest.TestCase):
    def test_redacts_credentials_and_feishu_identifiers(self) -> None:
        message = (
            'Authorization=Bearer abc.def password="db-secret" '
            'app_secret: top-secret open_id=ou_real_user cookie=session-value '
            'https://example.test/?access_token=query-secret'
        )
        redacted = redact_text(message)
        for secret in (
            "abc.def",
            "db-secret",
            "top-secret",
            "ou_real_user",
            "session-value",
            "query-secret",
        ):
            self.assertNotIn(secret, redacted)
        self.assertIn(REDACTED, redacted)

    def test_logging_filter_redacts_message_arguments_and_exception(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(SensitiveDataFilter())
        logger = logging.getLogger("redaction-test")
        logger.handlers[:] = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        try:
            raise RuntimeError("token=runtime-secret")
        except RuntimeError:
            logger.exception("request for open_id=%s failed", "ou_requester")

        output = stream.getvalue()
        self.assertNotIn("runtime-secret", output)
        self.assertNotIn("ou_requester", output)
        self.assertIn(REDACTED, output)

    def test_url_redaction_drops_userinfo_query_and_fragment(self) -> None:
        value = redact_text(
            "open https://user:pass@example.test/path?signature=secret#private-fragment"
        )
        self.assertEqual(value, "open https://example.test/path")
        self.assertEqual(
            redact_text("connect wss://gateway.example.test/ws?ticket=secret#private"),
            "connect wss://gateway.example.test/ws",
        )

    def test_persisted_diagnostic_is_redacted_and_bounded(self) -> None:
        value = diagnostic_error(RuntimeError("password=secret " + "x" * 3000))
        self.assertNotIn("secret", value)
        self.assertLessEqual(len(value), 2003)

    def test_cli_boundary_returns_nonzero_without_unfiltered_traceback(self) -> None:
        stderr = io.StringIO()

        def fail() -> None:
            raise RuntimeError(
                "request failed https://user:pass@example.test/path?ticket=secret#private"
            )

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as stopped:
            run_cli_safely(
                fail,
                failure_message="执行失败",
                interrupted_message="已停止",
            )
        self.assertEqual(stopped.exception.code, 1)
        output = stderr.getvalue()
        self.assertNotIn("secret", output)
        self.assertNotIn("Traceback", output)
        self.assertIn("https://example.test/path", output)


if __name__ == "__main__":
    unittest.main()
