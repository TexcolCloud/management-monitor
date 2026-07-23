from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common.runtime_config import load_runtime_config
from common.workflow_paths import PROJECT_ROOT
from data_acquisition.acquisition_steps import (
    _prune_attachment_store,
    _prune_legacy_range_attachments,
    download_attachments,
)


class AttachmentStagingTest(unittest.TestCase):
    @staticmethod
    def _args(output_dir: Path) -> SimpleNamespace:
        return SimpleNamespace(
            token_source=output_dir,
            headers_file=output_dir / "headers.json",
            max_attachment_failures=0,
            attachment_concurrency=1,
            keep_existing_attachments=False,
        )

    def test_failed_download_preserves_published_attachments(self) -> None:
        temp_root = PROJECT_ROOT / "tmp-validation"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "management-api-details.json").write_text(
                json.dumps({"rows": []}),
                encoding="utf-8",
            )
            attachments_dir = output_dir / "daily-attachments"
            attachments_dir.mkdir()
            old_attachment = attachments_dir / "existing.txt"
            old_attachment.write_text("existing", encoding="utf-8")
            old_outputs = {
                "daily-attachments-manifest.json": '{"rows":[]}',
                "daily-attachments-manifest.csv": "old-csv",
                "daily-attachments-manifest-excel-safe.csv": "old-safe-csv",
            }
            for name, content in old_outputs.items():
                (output_dir / name).write_text(content, encoding="utf-8")

            with patch(
                "data_acquisition.acquisition_steps.resolve_node_executable",
                return_value="node",
            ), patch(
                "data_acquisition.acquisition_steps.run_command",
                side_effect=RuntimeError("simulated download failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated download failure"):
                    download_attachments(
                        self._args(output_dir),
                        load_runtime_config(),
                        output_dir,
                        logging.getLogger("attachment-staging-test"),
                    )

            self.assertEqual(old_attachment.read_text(encoding="utf-8"), "existing")
            for name, content in old_outputs.items():
                self.assertEqual((output_dir / name).read_text(encoding="utf-8"), content)
            self.assertFalse(any(output_dir.glob(".attachment-staging-*")))
            self.assertFalse(any(output_dir.glob(".attachment-backup-*")))

    def test_successful_download_replaces_outputs_without_deleting_user_files(self) -> None:
        temp_root = PROJECT_ROOT / "tmp-validation"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "management-api-details.json").write_text(
                json.dumps({"rows": []}),
                encoding="utf-8",
            )
            attachments_dir = output_dir / "daily-attachments"
            attachments_dir.mkdir()
            (attachments_dir / "obsolete.txt").write_text("obsolete", encoding="utf-8")
            (output_dir / "daily-attachments-manifest.json").write_text(
                '{"rows":[]}',
                encoding="utf-8",
            )

            def write_staged_outputs(command, _label, _logger, **_kwargs) -> None:
                data_arg = next(item for item in command if item.startswith("--data-dir="))
                staging_dir = Path(data_arg.split("=", 1)[1])
                staged_attachments = staging_dir / "daily-attachments"
                staged_attachments.mkdir(exist_ok=True)
                (staged_attachments / "current.txt").write_text("current", encoding="utf-8")
                (staging_dir / "daily-attachments-manifest.json").write_text(
                    json.dumps(
                        {
                            "rows": [
                                {
                                    "savedPath": "daily-attachments/current.txt",
                                    "status": "downloaded",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                (staging_dir / "daily-attachments-manifest.csv").write_text(
                    "new-csv",
                    encoding="utf-8",
                )
                (staging_dir / "daily-attachments-manifest-excel-safe.csv").write_text(
                    "new-safe-csv",
                    encoding="utf-8",
                )

            with patch(
                "data_acquisition.acquisition_steps.resolve_node_executable",
                return_value="node",
            ), patch(
                "data_acquisition.acquisition_steps.run_command",
                side_effect=write_staged_outputs,
            ):
                download_attachments(
                    self._args(output_dir),
                    load_runtime_config(),
                    output_dir,
                    logging.getLogger("attachment-staging-test"),
                )

            self.assertTrue((attachments_dir / "obsolete.txt").exists())
            self.assertEqual(
                (attachments_dir / "current.txt").read_text(encoding="utf-8"),
                "current",
            )
            self.assertEqual(
                (output_dir / "daily-attachments-manifest.csv").read_text(encoding="utf-8"),
                "new-csv",
            )
            self.assertFalse(any(output_dir.glob(".attachment-staging-*")))
            self.assertFalse(any(output_dir.glob(".attachment-backup-*")))

    def test_attachment_store_preserves_content_owned_by_older_snapshots(self) -> None:
        temp_root = PROJECT_ROOT / "tmp-validation"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "current"
            attachments_dir = output_dir / "daily-attachments"
            attachment_store = root / "attachment-store"
            attachments_dir.mkdir(parents=True)
            attachment_store.mkdir()
            current_hash = "a" * 64
            expired_hash = "b" * 64
            current_attachment = attachments_dir / "current.txt"
            current_attachment.write_text("current", encoding="utf-8")
            (attachment_store / current_hash).write_text("current", encoding="utf-8")
            (attachment_store / expired_hash).write_text("expired", encoding="utf-8")
            (output_dir / "daily-attachments-manifest.json").write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "sha256": current_hash,
                                "savedPath": "daily-attachments/current.txt",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (attachment_store / "source-index.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "entries": {
                            "current": {"sha256": current_hash},
                            "expired": {"sha256": expired_hash},
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(_prune_attachment_store(output_dir, "daily-attachments"), 0)
            self.assertTrue((attachment_store / current_hash).is_file())
            self.assertTrue((attachment_store / expired_hash).is_file())
            source_index = json.loads(
                (attachment_store / "source-index.json").read_text(encoding="utf-8")
            )
            self.assertEqual(set(source_index["entries"]), {"current", "expired"})

    def test_current_snapshot_preserves_legacy_range_attachment_copies(self) -> None:
        temp_root = PROJECT_ROOT / "tmp-validation"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
            capture_root = Path(temp_dir)
            current_dir = capture_root / "current"
            legacy_dir = capture_root / "range-20260701_000000-20260707_235959"
            legacy_attachments = legacy_dir / "daily-attachments"
            current_dir.mkdir()
            legacy_attachments.mkdir(parents=True)
            (legacy_attachments / "old.txt").write_text("old", encoding="utf-8")
            (legacy_dir / "management-api-details.json").write_text("{}", encoding="utf-8")

            self.assertEqual(
                _prune_legacy_range_attachments(current_dir, "daily-attachments"),
                0,
            )
            self.assertTrue(legacy_attachments.exists())
            self.assertTrue((legacy_dir / "management-api-details.json").is_file())


if __name__ == "__main__":
    unittest.main()
