from __future__ import annotations

import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from common.runtime_config import load_runtime_config
from common.workflow_paths import ensure_inside_project, safe_slug
from data_acquisition.time_range import DEFAULT_CAPTURE_DAYS, recent_range
from data_processing.classifier import DailyManagementClassifier


class RuntimeAndPathsTest(unittest.TestCase):
    def test_runtime_environment_overrides(self) -> None:
        overrides = {
            "WORKORDER_PAGE_SIZE": "55",
            "WORKORDER_REQUEST_RETRIES": "4",
            "WORKORDER_STOP_ON_EMPTY_PAGE": "false",
        }
        with patch.dict(os.environ, overrides, clear=False):
            config = load_runtime_config()
        self.assertEqual(config.page_size, 55)
        self.assertEqual(config.request_retries, 4)
        self.assertFalse(config.stop_on_empty_page)

    def test_runtime_rejects_unsafe_attachment_directory(self) -> None:
        with patch.dict(
            os.environ,
            {"WORKORDER_ATTACHMENT_DIR_NAME": ".."},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "single directory name"):
                load_runtime_config()

    def test_project_path_guard_and_slug(self) -> None:
        self.assertEqual(safe_slug('a<b:c>'), "a_b_c_")
        with self.assertRaises(ValueError):
            ensure_inside_project(Path("..") / "outside")

    def test_default_classifier(self) -> None:
        classifier = DailyManagementClassifier(Path("config/classification_rules.json"))
        result = classifier.classify(
            {
                "companyName": "示例分公司/城区分公司",
                "theme": "消防隐患整改",
                "content": "重大风险",
            }
        )
        self.assertEqual(result["归属单位"], "城区分公司")
        self.assertEqual(result["归类"], "隐患排查整改")
        self.assertEqual(result["风险等级"], "重大")

    def test_default_recent_range_covers_thirty_calendar_days(self) -> None:
        selected = recent_range(now=datetime(2026, 7, 23, 10, 30, 15, 123456))
        self.assertEqual(DEFAULT_CAPTURE_DAYS, 30)
        self.assertEqual((selected.end.date() - selected.start.date()).days, 29)
        self.assertEqual(selected.start, datetime(2026, 6, 24, 0, 0, 0))
        self.assertEqual(selected.end, datetime(2026, 7, 23, 23, 59, 59, 999999))
        self.assertEqual(selected.start_text, "2026-06-24 00:00:00")
        self.assertEqual(selected.end_text, "2026-07-23 23:59:59.999999")


if __name__ == "__main__":
    unittest.main()
