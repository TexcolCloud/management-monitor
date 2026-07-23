from __future__ import annotations

import json
import logging
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from workflows.feishu_export_service import FeishuExportService, _enabled, start_feishu_export_listener


class FeishuExportServiceTest(unittest.TestCase):
    def service(self) -> FeishuExportService:
        return FeishuExportService(
            app_id="app-id",
            app_secret="app-secret",
            allowed_open_ids=frozenset({"ou_allowed"}),
            command="export",
            timeout_seconds=10,
            enabled=True,
            logger=logging.getLogger("feishu-export-test"),
        )

    def test_authorized_export_commands_send_date_card_to_sender(self) -> None:
        event = {
            "sender": {"sender_id": {"open_id": "ou_allowed"}},
            "message": {"message_type": "text", "content": json.dumps({"text": "导出Excel表格"})},
        }
        service = self.service()
        with patch.object(FeishuExportService, "send_export_card") as send_card:
            self.assertTrue(service.handle_event(event))
        send_card.assert_called_once_with("ou_allowed")

    def test_unauthorized_or_non_command_event_cannot_export(self) -> None:
        service = self.service()
        unauthorized = {
            "sender": {"sender_id": {"open_id": "ou_other"}},
            "message": {"message_type": "text", "content": json.dumps({"text": "export"})},
        }
        other_text = {
            "sender": {"sender_id": {"open_id": "ou_allowed"}},
            "message": {"message_type": "text", "content": json.dumps({"text": "export now"})},
        }
        with patch.object(FeishuExportService, "send_export_card") as send_card:
            self.assertFalse(service.handle_event(unauthorized))
            self.assertFalse(service.handle_event(other_text))
        send_card.assert_not_called()

    def test_date_card_submits_a_valid_date_range_for_export(self) -> None:
        service = self.service()
        with patch("workflows.feishu_export_service.threading.Thread") as thread:
            accepted, message = service.handle_card_action(
                "ou_allowed",
                {"action": "export_by_date"},
                {"start_date": "2026-07-01 +0800", "end_date": "2026-07-02 +0800"},
            )
        self.assertTrue(accepted)
        self.assertIn("已开始导出", message)
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["args"], (
            "ou_allowed",
            datetime(2026, 7, 1),
            datetime(2026, 7, 2, 23, 59, 59, 999999),
        ))
        thread.return_value.start.assert_called_once()

    def test_date_card_rejects_missing_or_reversed_dates(self) -> None:
        service = self.service()
        accepted, message = service.handle_card_action(
            "ou_allowed", {"action": "export_by_date"}, {"start_date": "2026-07-03"}
        )
        self.assertFalse(accepted)
        self.assertIn("开始日期", message)
        accepted, message = service.handle_card_action(
            "ou_allowed",
            {"action": "export_by_date"},
            {"start_date": "2026-07-03", "end_date": "2026-07-02"},
        )
        self.assertFalse(accepted)
        self.assertIn("开始日期", message)

    def test_export_card_contains_date_form_and_submit_action(self) -> None:
        form = FeishuExportService.export_card()["elements"][1]
        self.assertEqual(form["tag"], "form")
        self.assertEqual(form["elements"][0]["name"], "start_date")
        self.assertEqual(form["elements"][1]["name"], "end_date")
        self.assertTrue(form["elements"][0]["required"])
        self.assertTrue(form["elements"][1]["required"])
        self.assertEqual(form["elements"][2]["action_type"], "form_submit")
        self.assertEqual(form["elements"][2]["text"]["content"], "导出Excel表格")

    def test_export_service_does_not_require_a_capture_directory(self) -> None:
        environment = {"FEISHU_EXPORT_ENABLED": "false"}
        with patch.dict(os.environ, environment, clear=True):
            service = FeishuExportService.from_environment()
        self.assertFalse(service.enabled)

    def test_disabled_listener_reports_why_it_was_not_started(self) -> None:
        logger = logging.getLogger("feishu-export-test")
        with patch.dict(os.environ, {"FEISHU_EXPORT_ENABLED": "false"}, clear=True), patch.object(
            logger, "info"
        ) as log_info:
            listener = start_feishu_export_listener(logger)

        self.assertIsNone(listener)
        log_info.assert_called_once_with(
            "飞书 Excel 导出监听器未启动：FEISHU_EXPORT_ENABLED=false"
        )

    def test_export_for_open_id_reads_rows_from_database(self) -> None:
        service = self.service()
        start = datetime(2026, 7, 1)
        end = datetime(2026, 7, 1, 23, 59, 59, 999999)
        with patch(
            "workflows.feishu_export_service.rows_by_create_time",
            return_value=[{"safetyCode": "CODE-1"}],
        ) as database_rows, patch(
            "workflows.feishu_export_service.write_xlsx"
        ) as write, patch.object(FeishuExportService, "_bot_for_open_id") as bot:
            service.export_for_open_id("ou_allowed", start, end)
        database_rows.assert_called_once_with(start, end)
        write.assert_called_once()
        bot.return_value.send_file.assert_called_once()

    def test_export_configuration_errors_are_chinese(self) -> None:
        with self.assertRaisesRegex(ValueError, "必须为布尔值"):
            _enabled("enabled")
        with patch.dict(os.environ, {"FEISHU_EXPORT_ENABLED": "true"}, clear=True):
            with self.assertRaisesRegex(ValueError, "Excel 导出需要配置"):
                FeishuExportService.from_environment()

    def test_secret_is_redacted_from_service_representation(self) -> None:
        representation = repr(self.service())
        self.assertNotIn("app-secret", representation)
        self.assertNotIn("ou_allowed", representation)

    def test_background_database_failure_notifies_user_without_logging_details(self) -> None:
        service = self.service()
        with patch.object(
            FeishuExportService,
            "export_for_open_id",
            side_effect=Exception("app-secret must not leak"),
        ), patch.object(FeishuExportService, "_bot_for_open_id") as bot, patch.object(
            service.logger, "error"
        ) as log_error:
            service._export_date_range(
                "ou_allowed",
                datetime(2026, 7, 1),
                datetime(2026, 7, 1, 23, 59, 59, 999999),
            )

        bot.return_value.send_text.assert_called_once_with(
            "Excel 导出失败，请检查数据库连接和配置后重试。"
        )
        self.assertNotIn("app-secret", repr(log_error.call_args_list))


if __name__ == "__main__":
    unittest.main()
