from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.workflow_paths import PROJECT_ROOT
from data_acquisition.run_daily_acquisition import main
from safety_monitor.adapters.filesystem_snapshot import (
    FilesystemSnapshotPublisher,
    inspect_snapshot,
)


class DailyAcquisitionSnapshotTest(unittest.TestCase):
    @staticmethod
    def _write_candidate(output_dir: Path, *, complete: bool, label: str = "candidate") -> None:
        (output_dir / "management-api-all.json").write_text(
            json.dumps(
                {
                    "label": label,
                    "statistics": {
                        "total_consistent": complete,
                        "incomplete_reasons": [] if complete else ["simulated mismatch"],
                    },
                    "rows": [],
                }
            ),
            encoding="utf-8",
        )
        (output_dir / "management-api-details.json").write_text(
            json.dumps({"rows": []}), encoding="utf-8"
        )
        (output_dir / "management-api-detail-failures.json").write_text(
            json.dumps({"rows": []}), encoding="utf-8"
        )

    @staticmethod
    def _write_empty_attachment_manifest(output_dir: Path) -> None:
        (output_dir / "daily-attachments-manifest.json").write_text(
            json.dumps({"rows": []}), encoding="utf-8"
        )

    @classmethod
    def _write_complete_snapshot(cls, output_dir: Path, label: str) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        cls._write_candidate(output_dir, complete=True, label=label)
        cls._write_empty_attachment_manifest(output_dir)

    def test_attachment_failure_preserves_previous_current_snapshot(self) -> None:
        temp_root = PROJECT_ROOT / "tmp-validation"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
            capture_root = Path(temp_dir)
            current_dir = capture_root / "current"
            current_dir.mkdir()
            (current_dir / "management-api-all.json").write_text("old-list", encoding="utf-8")
            (current_dir / "management-api-details.json").write_text(
                "old-details",
                encoding="utf-8",
            )
            (current_dir / "daily-attachments-manifest.json").write_text(
                "old-manifest",
                encoding="utf-8",
            )

            args = argparse.Namespace(
                out=None,
                capture_root=capture_root,
                date_field="createTime",
                skip_login=True,
                skip_attachments=False,
                result_file=None,
                browser_bridge_url="",
            )

            def write_new_details(_args, _selected, output_dir, _logger, **_kwargs) -> None:
                self._write_candidate(output_dir, complete=True, label="new")

            with patch(
                "data_acquisition.run_daily_acquisition.parse_args", return_value=args
            ), patch(
                "data_acquisition.run_daily_acquisition.capture_range",
                side_effect=write_new_details,
            ), patch(
                "data_acquisition.run_daily_acquisition.download_attachments",
                side_effect=RuntimeError("attachment download failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "attachment download failed"):
                    main()

            self.assertEqual((current_dir / "management-api-all.json").read_text(encoding="utf-8"), "old-list")
            self.assertEqual(
                (current_dir / "management-api-details.json").read_text(encoding="utf-8"),
                "old-details",
            )
            self.assertEqual(
                (current_dir / "daily-attachments-manifest.json").read_text(encoding="utf-8"),
                "old-manifest",
            )
            self.assertFalse(any(capture_root.glob(".current-staging-*")))
            self.assertEqual(len(list((capture_root / "partial-attempts").glob("partial-*"))), 1)

    def test_partial_capture_is_published_as_diagnostic_attempt(self) -> None:
        temp_root = PROJECT_ROOT / "tmp-validation"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
            capture_root = Path(temp_dir)
            current_dir = capture_root / "current"
            current_dir.mkdir()
            (current_dir / "management-api-all.json").write_text("old-list", encoding="utf-8")

            args = argparse.Namespace(
                out=None,
                capture_root=capture_root,
                date_field="createTime",
                skip_login=True,
                skip_attachments=True,
                result_file=None,
                browser_bridge_url="",
            )

            def write_partial_details(_args, _selected, output_dir, _logger, **_kwargs) -> None:
                self._write_candidate(output_dir, complete=False)

            with patch(
                "data_acquisition.run_daily_acquisition.parse_args", return_value=args
            ), patch(
                "data_acquisition.run_daily_acquisition.capture_range",
                side_effect=write_partial_details,
            ):
                main()

            self.assertEqual((current_dir / "management-api-all.json").read_text(encoding="utf-8"), "old-list")
            attempts = list((capture_root / "partial-attempts").glob("partial-*"))
            self.assertEqual(len(attempts), 1)
            self.assertTrue((attempts[0] / "management-api-details.json").is_file())

    def test_complete_capture_replaces_current_snapshot(self) -> None:
        temp_root = PROJECT_ROOT / "tmp-validation"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
            capture_root = Path(temp_dir)
            current_dir = capture_root / "current"
            current_dir.mkdir()
            (current_dir / "management-api-all.json").write_text("old-list", encoding="utf-8")

            args = argparse.Namespace(
                out=None,
                capture_root=capture_root,
                date_field="createTime",
                skip_login=True,
                skip_attachments=False,
                result_file=None,
                browser_bridge_url="",
            )

            def write_complete_details(_args, _selected, output_dir, _logger, **_kwargs) -> None:
                self._write_candidate(output_dir, complete=True)

            def write_attachments(_args, _runtime, output_dir, _logger, **_kwargs) -> None:
                self._write_empty_attachment_manifest(output_dir)

            with patch(
                "data_acquisition.run_daily_acquisition.parse_args", return_value=args
            ), patch(
                "data_acquisition.run_daily_acquisition.capture_range",
                side_effect=write_complete_details,
            ), patch(
                "data_acquisition.run_daily_acquisition.download_attachments",
                side_effect=write_attachments,
            ):
                main()

            published = json.loads((current_dir / "management-api-all.json").read_text(encoding="utf-8"))
            self.assertTrue(published["statistics"]["total_consistent"])
            self.assertFalse(any(capture_root.glob(".current-staging-*")))
            self.assertFalse(any(capture_root.glob(".current-backup-*")))

    def test_direct_acquisition_reuses_one_in_memory_browser_session(self) -> None:
        temp_root = PROJECT_ROOT / "tmp-validation"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
            capture_root = Path(temp_dir)
            args = argparse.Namespace(
                out=None,
                capture_root=capture_root,
                date_field="createTime",
                skip_login=False,
                skip_attachments=False,
                result_file=None,
                browser_bridge_url="",
            )

            def write_complete_details(
                _args, _selected, output_dir, _logger, **kwargs
            ) -> None:
                self.assertEqual(kwargs["browser_bridge_url"], "http://127.0.0.1:43123")
                self.assertEqual(kwargs["browser_bridge_token"], "ephemeral-bridge-token")
                self._write_candidate(output_dir, complete=True)

            def write_attachments(
                received_args, _runtime, output_dir, _logger, **_kwargs
            ) -> None:
                self.assertEqual(
                    received_args.browser_bridge_url, "http://127.0.0.1:43123"
                )
                self.assertEqual(
                    received_args.browser_bridge_token, "ephemeral-bridge-token"
                )
                self._write_empty_attachment_manifest(output_dir)

            with patch(
                "data_acquisition.run_daily_acquisition.parse_args", return_value=args
            ), patch(
                "data_acquisition.run_daily_acquisition.capture_range",
                side_effect=write_complete_details,
            ), patch(
                "data_acquisition.run_daily_acquisition.download_attachments",
                side_effect=write_attachments,
            ), patch(
                "data_acquisition.run_daily_acquisition.BrowserSession"
            ) as browser_session:
                browser_session.return_value.start.return_value = "http://127.0.0.1:43123"
                browser_session.return_value.token = "ephemeral-bridge-token"
                main()

            browser_session.return_value.stop.assert_called_once()

    def test_snapshot_requires_expected_attachment_file_and_matching_sha256(self) -> None:
        temp_root = Path(tempfile.gettempdir())
        with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
            output_dir = Path(temp_dir)
            attachment = output_dir / "daily-attachments" / "evidence.png"
            attachment.parent.mkdir()
            attachment.write_bytes(b"evidence")
            digest = hashlib.sha256(b"evidence").hexdigest()
            group = {
                "fileType": "1",
                "fileId": "F-1",
                "url": json.dumps(
                    [{"fileOldName": "evidence.png", "filePath": "source/path", "type": "image/png"}]
                ),
            }
            row = {
                "id": "1",
                "safetyCode": "CODE-1",
                "createTime": "2026-07-23 10:00:00",
                "dailySafetyFiles": [group],
            }
            (output_dir / "management-api-all.json").write_text(
                json.dumps({"statistics": {"total_consistent": True}, "rows": [row]}),
                encoding="utf-8",
            )
            (output_dir / "management-api-details.json").write_text(
                json.dumps({"rows": [row]}), encoding="utf-8"
            )
            (output_dir / "management-api-detail-failures.json").write_text(
                json.dumps({"rows": []}), encoding="utf-8"
            )
            manifest_row = {
                **row,
                "fileType": "1",
                "fileId": "F-1",
                "fileOldName": "evidence.png",
                "filePath": "source/path",
                "type": "image/png",
                "savedPath": "daily-attachments/evidence.png",
                "status": "downloaded",
                "sha256": digest,
            }
            (output_dir / "daily-attachments-manifest.json").write_text(
                json.dumps({"rows": [manifest_row]}), encoding="utf-8"
            )
            self.assertTrue(inspect_snapshot(output_dir).complete)
            manifest_row["sha256"] = "0" * 64
            (output_dir / "daily-attachments-manifest.json").write_text(
                json.dumps({"rows": [manifest_row]}), encoding="utf-8"
            )
            result = inspect_snapshot(output_dir)
            self.assertFalse(result.complete)
            self.assertTrue(any("does not match" in reason for reason in result.reasons))

    def test_publish_interruption_restores_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current"
            staging = root / ".current-staging-test"
            self._write_complete_snapshot(current, "old")
            self._write_complete_snapshot(staging, "new")

            def interrupt(phase, _transaction) -> None:
                if phase == "previous_moved":
                    raise RuntimeError("simulated interruption")

            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                FilesystemSnapshotPublisher(phase_hook=interrupt).publish(staging, current)
            self.assertEqual(
                json.loads((current / "management-api-all.json").read_text(encoding="utf-8"))["label"],
                "old",
            )
            self.assertFalse(FilesystemSnapshotPublisher.marker_path(current).exists())

    def test_failed_rollback_preserves_unique_backup_for_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current"
            staging = root / ".current-staging-test"
            self._write_complete_snapshot(current, "old")
            self._write_complete_snapshot(staging, "new")
            original_replace = Path.replace

            def selective_replace(path: Path, target: Path):
                if path.name.startswith(".current-backup-") and Path(target) == current:
                    raise OSError("simulated locked current path")
                return original_replace(path, target)

            def interrupt(phase, _transaction) -> None:
                if phase == "previous_moved":
                    raise RuntimeError("simulated interruption")

            with patch.object(Path, "replace", new=selective_replace), self.assertRaisesRegex(
                RuntimeError, "simulated interruption"
            ):
                FilesystemSnapshotPublisher(phase_hook=interrupt).publish(staging, current)

            backups = list(root.glob(".current-backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertTrue(FilesystemSnapshotPublisher.marker_path(current).is_file())
            self.assertFalse(current.exists())

            FilesystemSnapshotPublisher().recover(current)
            self.assertTrue(current.is_dir())
            self.assertFalse(FilesystemSnapshotPublisher.marker_path(current).exists())
            self.assertEqual(
                json.loads((current / "management-api-all.json").read_text(encoding="utf-8"))["label"],
                "old",
            )

    def test_recovery_rejects_tampered_marker_without_deleting_user_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current"
            protected = root / "user-owned-directory"
            self._write_complete_snapshot(current, "current")
            protected.mkdir()
            (protected / "keep.txt").write_text("keep", encoding="utf-8")
            marker = FilesystemSnapshotPublisher.marker_path(current)
            marker.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "transactionId": "a" * 32,
                        "phase": "published",
                        "staging": str(root / ".current-staging-test"),
                        "current": str(current),
                        "backup": str(protected),
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "backup path is not owned"):
                FilesystemSnapshotPublisher().recover(current)

            self.assertEqual((protected / "keep.txt").read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
