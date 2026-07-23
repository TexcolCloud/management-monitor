from __future__ import annotations

import unittest
from datetime import datetime

from safety_monitor.application.export_service import ExportApplicationService
from safety_monitor.domain.exporting import ExportDateRange, ExportPolicy


class ExportApplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cards: list[str] = []
        self.jobs = []
        self.service = ExportApplicationService(
            policy=ExportPolicy(frozenset({"ou_allowed"}), "export"),
            send_date_selection_card=self.cards.append,
            submit_export_job=self.jobs.append,
        )

    def test_chinese_and_custom_commands_are_authorized_offline(self) -> None:
        self.assertTrue(self.service.handle_command("ou_allowed", "text", "导出Excel表格"))
        self.assertTrue(self.service.handle_command("ou_allowed", "text", "export"))
        self.assertFalse(self.service.handle_command("ou_other", "text", "export"))
        self.assertFalse(self.service.handle_command("ou_allowed", "image", "export"))
        self.assertFalse(self.service.handle_command("ou_allowed", "text", "export now"))
        self.assertEqual(self.cards, ["ou_allowed", "ou_allowed"])

    def test_feishu_timezone_variants_produce_closed_calendar_range(self) -> None:
        variants = (
            "2026-07-01 +0800",
            "2026-07-01+0800",
            "2026-07-01 +08:00",
            "2026-07-01+08:00",
        )
        for value in variants:
            with self.subTest(value=value):
                selected = ExportDateRange.from_form_values(value, {"date": value})
                self.assertEqual(selected.start, datetime(2026, 7, 1))
                self.assertEqual(selected.end, datetime(2026, 7, 1, 23, 59, 59, 999999))

    def test_card_operator_is_reauthorized_and_receives_the_job(self) -> None:
        result = self.service.handle_action(
            "ou_allowed",
            {"action": "export_by_date"},
            {"start_date": "2026-07-01", "end_date": "2026-07-02 +0800"},
        )
        self.assertTrue(result.accepted)
        self.assertIn("私发给你", result.message)
        self.assertEqual(self.jobs[0].requester_open_id, "ou_allowed")
        self.assertEqual(self.jobs[0].date_range.end, datetime(2026, 7, 2, 23, 59, 59, 999999))
        self.assertNotIn("ou_allowed", repr(self.jobs[0]))

    def test_invalid_actions_return_safe_chinese_messages(self) -> None:
        unauthorized = self.service.handle_action(
            "ou_other",
            {"action": "export_by_date"},
            {"start_date": "2026-07-01", "end_date": "2026-07-02"},
        )
        self.assertFalse(unauthorized.accepted)
        self.assertIn("没有导出", unauthorized.message)

        invalid = self.service.handle_action(
            "ou_allowed",
            {"action": "export_by_date"},
            {"start_date": "2026-07-03", "end_date": "2026-07-02"},
        )
        self.assertFalse(invalid.accepted)
        self.assertIn("不能晚于", invalid.message)
        self.assertEqual(self.jobs, [])


if __name__ == "__main__":
    unittest.main()
