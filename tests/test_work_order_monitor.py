from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from common.runtime_config import load_runtime_config
from common.workflow_paths import PROJECT_ROOT
from data_acquisition.capture_components import CaptureResult, CaptureService, CaptureStatistics
from workflows.work_order_monitor import (
    BrowserSession,
    DownloadedAttachment,
    DownloadedImage,
    MonitorState,
    WorkOrderMonitor,
    combine_details,
    load_monitor_state,
    main as monitor_main,
    monitor_request_body,
    poll_window,
    write_monitor_state,
)
from safety_monitor.domain.monitoring import PersistedWindow
from safety_monitor.ports.repositories import OutboxNotification


class WorkOrderMonitorTest(unittest.TestCase):
    @staticmethod
    def project_temp_dir() -> tempfile.TemporaryDirectory[str]:
        temp_root = PROJECT_ROOT / ".temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(dir=temp_root)

    def test_poll_window_retries_since_last_success_after_failure(self) -> None:
        now = datetime(2026, 7, 22, 10, 10)
        start, end = poll_window(
            now,
            MonitorState(datetime(2026, 7, 22, 10, 0)),
            timedelta(minutes=5),
            timedelta(seconds=30),
            timedelta(minutes=5),
        )
        self.assertEqual(start, datetime(2026, 7, 22, 9, 59, 30))
        self.assertEqual(end, now)

    def test_combine_details_keeps_list_fields_when_detail_omits_them(self) -> None:
        result = CaptureResult(
            rows=[{"id": "1", "safetyCode": "CODE-1", "companyName": "城区分公司"}],
            details=[{"id": "1", "theme": "新增隐患"}],
            failed_details=[],
            request_body={},
            statistics=CaptureStatistics(),
        )
        self.assertEqual(
            combine_details(result),
            [{"id": "1", "safetyCode": "CODE-1", "companyName": "城区分公司", "theme": "新增隐患"}],
        )

    def test_monitor_request_body_uses_dynamic_range_on_every_page(self) -> None:
        class FakeApiClient:
            def __init__(self) -> None:
                self.request_bodies: list[dict] = []

            def request_page(self, page_no: int, page_size: int, base_body: dict) -> dict:
                self.request_bodies.append(dict(base_body))
                return {
                    "pageInfo": {"totalPage": 2, "totalNumber": 2} if page_no == 1 else {},
                    "rows": [
                        {
                            "id": str(page_no),
                            "createTime": "2026-07-22 10:00:00",
                        }
                    ],
                    "requestBody": base_body,
                }

        start = datetime(2026, 7, 22, 9, 55)
        end = datetime(2026, 7, 22, 10, 0)
        body = monitor_request_body(
            {"createTimeStart": "", "createTimeEnd": "", "pageType": "1"},
            start,
            end,
        )
        client = FakeApiClient()
        CaptureService(client).capture(
            start=start,
            end=end,
            date_field="createTime",
            page_size=100,
            base_body=body,
            include_details=False,
        )
        self.assertEqual(len(client.request_bodies), 2)
        self.assertTrue(
            all(
                request["createTimeStart"] == "2026-07-22 09:55:00"
                and request["createTimeEnd"] == "2026-07-22 10:00:00"
                for request in client.request_bodies
            )
        )

    def test_monitor_request_body_uses_verified_portal_time_fields_when_capture_is_unfiltered(self) -> None:
        body = monitor_request_body({}, datetime(2026, 7, 22, 9, 55), datetime(2026, 7, 22, 10, 0))
        self.assertEqual(body["startTime"], "2026-07-22 09:55:00")
        self.assertEqual(body["endTime"], "2026-07-22 10:00:00")

    def test_successful_cycle_persists_checkpoint_only_after_database_write(self) -> None:
        with self.project_temp_dir() as temp_dir:
            root = Path(temp_dir)
            args = Namespace(
                state_file=root / "state.json",
                lookback_minutes=5,
                initial_lookback_minutes=5,
                overlap_seconds=30,
                request_start_field="",
                request_end_field="",
            )
            logger = logging.getLogger("monitor-test")
            unit_of_work = Mock()
            unit_of_work.read_checkpoint.return_value = None
            unit_of_work.persist_complete_window.return_value = PersistedWindow(
                inserted_codes=frozenset({"CODE-1"}),
                upserted_count=1,
                checkpoint=datetime(2026, 7, 22, 10, 0),
            )
            monitor = WorkOrderMonitor(
                args,
                load_runtime_config(),
                logger,
                monitoring_uow=unit_of_work,
            )
            captured = CaptureResult(
                rows=[{"id": "1", "safetyCode": "CODE-1"}],
                details=[{"id": "1", "theme": "新工单"}],
                failed_details=[],
                request_body={},
                statistics=CaptureStatistics(),
            )
            with patch.object(
                monitor, "_capture", return_value=captured
            ), patch.object(
                monitor, "_deliver_feishu_notifications", return_value=(1, 0)
            ) as deliver:
                summary = monitor.poll_once(datetime(2026, 7, 22, 10, 0))
            self.assertEqual(summary.new_count, 1)
            unit_of_work.persist_complete_window.assert_called_once_with(
                stream="daily-management",
                records=({"id": "1", "safetyCode": "CODE-1", "theme": "新工单"},),
                expected_checkpoint=None,
                next_checkpoint=datetime(2026, 7, 22, 10, 0),
            )
            deliver.assert_called_once()
            self.assertEqual(
                load_monitor_state(root / "state.json").last_success_at,
                datetime(2026, 7, 22, 10, 0),
            )

    def test_monitor_loads_environment_before_initializing_feishu_outbound_bot(self) -> None:
        args = Namespace(state_file=PROJECT_ROOT / ".temp" / "monitor-env-test.json")
        with patch("workflows.work_order_monitor.load_env_file") as load_env:
            WorkOrderMonitor(args, load_runtime_config(), logging.getLogger("monitor-test"))
        load_env.assert_called_once()

    def test_feishu_delivery_sends_only_unfinished_attachments_after_failure(self) -> None:
        with self.project_temp_dir() as temp_dir:
            root = Path(temp_dir)
            args = Namespace(
                state_file=root / "state.json",
                database_config=Path("config/database.json"),
            )
            outbox = Mock()
            monitor = WorkOrderMonitor(
                args,
                load_runtime_config(),
                logging.getLogger("monitor-test"),
                outbox_repository=outbox,
            )
            bot = Mock()
            bot.enabled = True
            monitor.feishu_bot = bot
            row = {"safetyCode": "CODE-1", "dailySafetyFiles": [{"url": "attachments"}]}
            events: list[str] = []

            def download_attachments(_row: dict, _data_dir: Path) -> list[DownloadedAttachment]:
                events.append("download")
                return [
                    DownloadedImage("daily-attachments/one.png", root / "one.png", "会议通知"),
                    DownloadedAttachment("daily-attachments/agenda.pdf", root / "agenda.pdf", "培训资料"),
                    DownloadedImage("daily-attachments/two.jpg", root / "two.jpg", "培训照片"),
                ]

            bot.send_work_order.side_effect = lambda _row: events.append("card")
            state = {"card": False, "images": []}
            second_image_attempts = 0

            def pending_rows(*_args):
                return [
                    OutboxNotification(
                        safety_code="CODE-1",
                        payload={"dailySafetyFiles": [{"url": "attachments"}]},
                        card_sent=state["card"],
                        sent_image_paths=tuple(state["images"]),
                        attempts=0,
                        lease_owner=monitor._outbox_worker_id,
                        lease_until=None,
                    )
                ]

            def send_image(path: str) -> None:
                nonlocal second_image_attempts
                events.append(f"image:{Path(path).name}")
                if Path(path).name == "two.jpg":
                    second_image_attempts += 1
                    if second_image_attempts == 1:
                        raise RuntimeError("temporary upload failure")

            def send_file(path: str) -> None:
                events.append(f"file:{Path(path).name}")

            def mark_card(*_args) -> None:
                state["card"] = True
                events.append("mark-card")

            def mark_image(*args) -> None:
                state["images"].append(args[1])
                events.append(f"mark-image:{Path(args[1]).name}")

            bot.send_image.side_effect = send_image
            bot.send_file.side_effect = send_file
            bot.send_text.side_effect = lambda text: events.append(f"type:{text}")
            outbox.claim_due.side_effect = pending_rows
            outbox.mark_card_sent.side_effect = mark_card
            outbox.mark_image_sent.side_effect = mark_image
            outbox.mark_sent.side_effect = lambda *_args: events.append("mark")
            with patch.object(monitor, "_download_attachments", side_effect=download_attachments):
                self.assertEqual(monitor._deliver_feishu_notifications(), (0, 1))
                self.assertEqual(monitor._deliver_feishu_notifications(), (1, 0))

            self.assertEqual(
                events,
                [
                    "download",
                    "card",
                    "mark-card",
                    "type:【附件类型】 会议通知",
                    "image:one.png",
                    "mark-image:one.png",
                    "type:【附件类型】 培训资料",
                    "file:agenda.pdf",
                    "mark-image:agenda.pdf",
                    "type:【附件类型】 培训照片",
                    "image:two.jpg",
                    "download",
                    "type:【附件类型】 培训照片",
                    "image:two.jpg",
                    "mark-image:two.jpg",
                    "mark",
                ],
            )
            self.assertEqual(bot.send_work_order.call_count, 1)
            self.assertEqual(bot.send_text.call_count, 4)
            self.assertEqual(bot.send_image.call_args_list[0].args[0], str(root / "one.png"))
            self.assertEqual(bot.send_image.call_args_list[2].args[0], str(root / "two.jpg"))
            bot.send_file.assert_called_once_with(str(root / "agenda.pdf"))
            outbox.mark_sent.assert_called_once()
            outbox.record_failure.assert_called_once()

    def test_browser_session_keeps_logged_in_browser_and_publishes_bridge_url(self) -> None:
        with self.project_temp_dir() as temp_dir:
            root = Path(temp_dir)
            args = Namespace(
                login_profile_dir=root / "profile",
                login_navigation_file=Path("data_acquisition/recordings/portal-navigation.local.json"),
                token_source=Path("capture-output/daily-management"),
                headers_file=Path("capture-output/daily-management/daily-management-request.json"),
                login_wait_ms=1,
                no_login_replay=False,
                config=Path("config/runtime.json"),
            )
            process = Mock()
            process.poll.return_value = None

            def start_process(command, **_kwargs):
                ready_arg = next(item for item in command if item.startswith("--bridge-ready-file="))
                Path(ready_arg.split("=", 1)[1]).write_text(
                    json.dumps(
                        {
                            "url": "http://127.0.0.1:43123",
                            "token": "ephemeral-bridge-token-with-at-least-32-characters",
                        }
                    ),
                    encoding="utf-8",
                )
                return process

            with patch(
                "data_acquisition.browser_session.resolve_node_executable", return_value="node"
            ), patch(
                "data_acquisition.browser_session.subprocess.Popen", side_effect=start_process
            ) as popen, patch.dict(
                os.environ,
                {
                    "PGPASSWORD": "fake-database-secret",
                    "FEISHU_APP_SECRET": "fake-feishu-secret",
                    "WORKORDER_TOKEN": "fake-portal-secret",
                },
            ):
                session = BrowserSession(args, load_runtime_config(), root / "state.json", logging.getLogger("monitor-test"))
                self.assertEqual(session.start(), "http://127.0.0.1:43123")
                command = popen.call_args.args[0]
                child_environment = popen.call_args.kwargs["env"]
                self.assertIn("--keep-session", command)
                self.assertIn("--wait-for-login", command)
                self.assertIn("--bridge-port=0", command)
                self.assertTrue(any(item.startswith("--bridge-ready-file=") for item in command))
                self.assertFalse(any(item.startswith("--profile-dir=") for item in command))
                self.assertFalse(any(item.startswith("--portal-url=") for item in command))
                self.assertEqual(
                    session.token,
                    "ephemeral-bridge-token-with-at-least-32-characters",
                )
                self.assertIsNotNone(session.ready_file)
                self.assertFalse(session.ready_file.exists())
                self.assertNotIn("PGPASSWORD", child_environment)
                self.assertNotIn("FEISHU_APP_SECRET", child_environment)
                self.assertNotIn("WORKORDER_TOKEN", child_environment)
                self.assertEqual(
                    child_environment["WORKORDER_CONFIG"],
                    str((PROJECT_ROOT / "config/runtime.json").resolve()),
                )
                session.stop()
                self.assertEqual(session.token, "")

            process.terminate.assert_called_once()

    def test_ctrl_c_during_login_stops_without_traceback(self) -> None:
        with self.project_temp_dir() as temp_dir:
            args = Namespace(
                state_file=Path(temp_dir) / "state.json",
                browser_bridge_url="",
                skip_login=False,
                once=True,
            )
            logger = Mock()
            with patch(
                "workflows.work_order_monitor.parse_args",
                return_value=(args, load_runtime_config()),
            ), patch(
                "workflows.work_order_monitor.setup_logging", return_value=logger
            ), patch(
                "workflows.work_order_monitor.WorkOrderMonitor"
            ), patch(
                "workflows.work_order_monitor.BrowserSession"
            ) as session:
                session.return_value.start.side_effect = KeyboardInterrupt
                monitor_main()

            logger.info.assert_any_call("已停止新工单监听服务")
            session.return_value.stop.assert_called_once()

    def test_failed_detail_does_not_persist_checkpoint(self) -> None:
        with self.project_temp_dir() as temp_dir:
            root = Path(temp_dir)
            args = Namespace(
                state_file=root / "state.json",
                lookback_minutes=5,
                initial_lookback_minutes=5,
                overlap_seconds=30,
                request_start_field="",
                request_end_field="",
            )
            unit_of_work = Mock()
            unit_of_work.read_checkpoint.return_value = None
            monitor = WorkOrderMonitor(
                args,
                load_runtime_config(),
                logging.getLogger("monitor-test"),
                monitoring_uow=unit_of_work,
            )
            captured = CaptureResult(
                rows=[{"id": "1"}],
                details=[],
                failed_details=[{"id": "1", "_captureError": "timeout"}],
                request_body={},
                statistics=CaptureStatistics(),
            )
            with patch.object(monitor, "_capture", return_value=captured):
                with self.assertRaisesRegex(RuntimeError, "检查点未推进"):
                    monitor.poll_once(datetime(2026, 7, 22, 10, 0))
            unit_of_work.persist_complete_window.assert_not_called()
            self.assertFalse((root / "state.json").exists())

    def test_portal_failure_still_retries_persisted_notifications(self) -> None:
        with self.project_temp_dir() as temp_dir:
            root = Path(temp_dir)
            args = Namespace(
                state_file=root / "state.json",
                lookback_minutes=5,
                initial_lookback_minutes=5,
                overlap_seconds=30,
                request_start_field="",
                request_end_field="",
            )
            unit_of_work = Mock()
            unit_of_work.read_checkpoint.return_value = None
            monitor = WorkOrderMonitor(
                args,
                load_runtime_config(),
                logging.getLogger("monitor-test"),
                monitoring_uow=unit_of_work,
            )
            with patch.object(
                monitor,
                "_capture",
                side_effect=RuntimeError("portal unavailable"),
            ), patch.object(
                monitor,
                "_deliver_feishu_notifications",
                return_value=(1, 0),
            ) as deliver:
                with self.assertRaisesRegex(RuntimeError, "portal unavailable"):
                    monitor.run_cycle(datetime(2026, 7, 22, 10, 0))

            deliver.assert_called_once_with()
            unit_of_work.persist_complete_window.assert_not_called()

    def test_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            write_monitor_state(path, MonitorState(datetime(2026, 7, 22, 10, 0, 30)))
            self.assertEqual(load_monitor_state(path).last_success_at, datetime(2026, 7, 22, 10, 0, 30))


if __name__ == "__main__":
    unittest.main()
