from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from workflows.daily_workflow import (
    acquired_data_dir,
    attachments_complete,
    build_capture_command,
    build_monitor_command,
    main,
)


class WorkflowResultTest(unittest.TestCase):
    def test_result_file_selects_exact_capture_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            expected = root / "expected"
            newer = root / "newer"
            expected.mkdir()
            newer.mkdir()
            (expected / "management-api-all.json").write_text("{}", encoding="utf-8")
            (newer / "management-api-all.json").write_text("{}", encoding="utf-8")
            result_file = root / "result.json"
            result_file.write_text(
                json.dumps({"outputDir": str(expected)}),
                encoding="utf-8",
            )
            self.assertEqual(acquired_data_dir(result_file), expected.resolve())

    def test_capture_command_uses_default_thirty_day_acquisition_without_date_arguments(self) -> None:
        args = Namespace(
            capture_out=None,
            capture_root=Path("capture-output/daily-management"),
            page_size=None,
            max_pages=None,
            request_timeout=None,
            retries=None,
            max_detail_failures=None,
            max_attachment_failures=None,
            attachment_concurrency=None,
            date_field="",
            login_wait_ms=None,
            skip_login=False,
            no_login_replay=False,
            skip_attachments=False,
            keep_existing_attachments=False,
            allow_partial=False,
        )
        command = build_capture_command(args, Path("result.json"))
        self.assertNotIn("--start", " ".join(command))
        self.assertNotIn("--end", " ".join(command))
        self.assertNotIn("--last-days", " ".join(command))

    def test_workflow_always_captures_recent_thirty_days_when_database_has_records(self) -> None:
        args = Namespace(
            skip_login=False,
            skip_export=True,
            capture_out=None,
            capture_root=Path("capture-output/daily-management"),
            page_size=None,
            max_pages=None,
            request_timeout=None,
            retries=None,
            max_detail_failures=None,
            max_attachment_failures=None,
            attachment_concurrency=None,
            date_field="",
            login_wait_ms=None,
            no_login_replay=False,
            skip_attachments=False,
            keep_existing_attachments=False,
            allow_partial=False,
            request_start_field="",
            request_end_field="",
        )
        data_dir = Path(tempfile.gettempdir()) / "daily-workflow-data"
        commands: list[tuple[list[str], str, dict[str, str] | None]] = []

        def run_command(command, label, _logger, *, env_overrides=None) -> None:
            commands.append((command, label, env_overrides))
            if label == "新工单监听":
                raise KeyboardInterrupt

        with patch("workflows.daily_workflow.parse_args", return_value=args), patch(
            "workflows.daily_workflow.setup_logging"
        ), patch("workflows.daily_workflow.BrowserSession") as session, patch(
            "workflows.daily_workflow.acquired_data_dir", return_value=data_dir
        ), patch(
            "workflows.daily_workflow.capture_data_complete", return_value=True
        ), patch("workflows.daily_workflow.attachments_complete", return_value=True), patch(
            "workflows.daily_workflow.data_dir_records_complete", return_value=True
        ), patch("workflows.daily_workflow.run_command", side_effect=run_command):
            session.return_value.start.return_value = "http://127.0.0.1:43123"
            session.return_value.token = "ephemeral-bridge-secret"
            main()
        self.assertEqual(commands[0][1], "最近30天数据获取")
        self.assertIn("--browser-bridge-url=http://127.0.0.1:43123", commands[0][0])
        self.assertNotIn("ephemeral-bridge-secret", " ".join(commands[0][0]))
        self.assertEqual(
            commands[0][2],
            {"WORKORDER_BROWSER_BRIDGE_TOKEN": "ephemeral-bridge-secret"},
        )
        self.assertEqual(commands[-1][1], "新工单监听")
        self.assertEqual(commands[-1][2], commands[0][2])

    def test_workflow_does_not_import_partial_capture(self) -> None:
        args = Namespace(
            skip_login=True,
            skip_export=True,
            capture_out=None,
            capture_root=Path("capture-output/daily-management"),
            page_size=None,
            max_pages=None,
            request_timeout=None,
            retries=None,
            max_detail_failures=None,
            max_attachment_failures=None,
            attachment_concurrency=None,
            date_field="",
            login_wait_ms=None,
            no_login_replay=False,
            skip_attachments=False,
            keep_existing_attachments=False,
            allow_partial=True,
            request_start_field="",
            request_end_field="",
        )
        data_dir = Path(tempfile.gettempdir()) / "partial-daily-workflow-data"
        commands: list[tuple[list[str], str]] = []

        def run_command(command, label, _logger, *, env_overrides=None) -> None:
            commands.append((command, label))

        with patch("workflows.daily_workflow.parse_args", return_value=args), patch(
            "workflows.daily_workflow.setup_logging"
        ), patch(
            "workflows.daily_workflow.acquired_data_dir", return_value=data_dir
        ), patch(
            "workflows.daily_workflow.capture_data_complete", return_value=False
        ), patch("workflows.daily_workflow.run_command", side_effect=run_command):
            with self.assertRaisesRegex(RuntimeError, "列表抓取不完整"):
                main()

        self.assertEqual([label for _, label in commands], ["最近30天数据获取"])

    def test_attachments_complete_requires_all_manifest_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            attachment_dir = data_dir / "daily-attachments"
            attachment_dir.mkdir()
            attachment = attachment_dir / "evidence.txt"
            attachment.write_text("ok", encoding="utf-8")
            (data_dir / "daily-attachments-manifest.json").write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "status": "downloaded",
                                "savedPath": "daily-attachments/evidence.txt",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(attachments_complete(data_dir))
            attachment.unlink()
            self.assertFalse(attachments_complete(data_dir))

    def test_workflow_handles_keyboard_interrupt_without_traceback(self) -> None:
        args = Namespace(
            skip_login=True,
            skip_export=True,
            capture_out=None,
            capture_root=Path("capture-output/daily-management"),
            page_size=None,
            max_pages=None,
            request_timeout=None,
            retries=None,
            max_detail_failures=None,
            max_attachment_failures=None,
            attachment_concurrency=None,
            date_field="",
            login_wait_ms=None,
            no_login_replay=False,
            skip_attachments=False,
            keep_existing_attachments=False,
            allow_partial=False,
            request_start_field="",
            request_end_field="",
        )
        logger = unittest.mock.Mock()
        with patch("workflows.daily_workflow.parse_args", return_value=args), patch(
            "workflows.daily_workflow.setup_logging", return_value=logger
        ), patch(
            "workflows.daily_workflow.run_command", side_effect=KeyboardInterrupt
        ):
            main()
        logger.info.assert_any_call("已停止日常管理工作流")

    def test_monitor_starts_persistent_session_and_forwards_time_field_configuration(self) -> None:
        args = Namespace(
            skip_login=False,
            request_start_field="createTimeStart",
            request_end_field="createTimeEnd",
        )
        command = build_monitor_command(args, reuse_login=True)
        self.assertNotIn("--skip-login", command)
        self.assertIn("--request-start-field=createTimeStart", command)
        self.assertIn("--request-end-field=createTimeEnd", command)

    def test_initial_capture_and_monitor_reuse_the_same_browser_bridge(self) -> None:
        args = Namespace(
            capture_out=None,
            capture_root=Path("capture-output/daily-management"),
            page_size=None,
            max_pages=None,
            request_timeout=None,
            retries=None,
            max_detail_failures=None,
            max_attachment_failures=None,
            attachment_concurrency=None,
            date_field="",
            login_wait_ms=None,
            skip_login=False,
            no_login_replay=False,
            skip_attachments=False,
            keep_existing_attachments=False,
            allow_partial=False,
            request_start_field="",
            request_end_field="",
        )
        bridge_url = "http://127.0.0.1:43123"
        capture_command = build_capture_command(args, Path("result.json"), bridge_url)
        monitor_command = build_monitor_command(args, reuse_login=True, browser_bridge_url=bridge_url)

        self.assertIn("--skip-login", capture_command)
        self.assertIn(f"--browser-bridge-url={bridge_url}", capture_command)
        self.assertNotIn("browser-bridge-token", " ".join(capture_command))
        self.assertIn("--skip-login", monitor_command)
        self.assertIn(f"--browser-bridge-url={bridge_url}", monitor_command)
        self.assertNotIn("browser-bridge-token", " ".join(monitor_command))


if __name__ == "__main__":
    unittest.main()
