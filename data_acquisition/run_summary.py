from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from data_acquisition.time_range import DEFAULT_CAPTURE_DAYS, SelectedRange


def write_run_summary(
    output_dir: Path,
    selected: SelectedRange,
    date_field: str,
    logger: logging.Logger,
) -> Path:
    manifest_path = output_dir / "daily-attachments-manifest.json"
    attachment_summary = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            rows = manifest.get("rows", [])
            attachment_summary = {
                "total": manifest.get("total", len(rows)),
                "downloaded": sum(1 for row in rows if row.get("status") == "downloaded"),
                "reused": sum(1 for row in rows if row.get("status") == "reused"),
                "failed": sum(1 for row in rows if row.get("status") == "failed"),
                "skippedDuplicate": sum(
                    1 for row in rows if row.get("status") == "skipped_duplicate"
                ),
                "retryAttempts": sum(
                    max(0, int(row.get("attempts", 0)) - 1) for row in rows
                ),
                "images": sum(1 for row in rows if row.get("isImage")),
            }
        except (OSError, json.JSONDecodeError):
            attachment_summary = {"error": "failed_to_read_attachment_manifest"}

    capture_summary = {}
    list_path = output_dir / "management-api-all.json"
    if list_path.exists():
        try:
            capture_summary = json.loads(list_path.read_text(encoding="utf-8")).get(
                "statistics", {}
            )
        except (OSError, json.JSONDecodeError):
            capture_summary = {"error": "failed_to_read_capture_statistics"}

    summary = {
        "exportedAt": datetime.now().isoformat(timespec="seconds"),
        "timeRange": {
            "dateField": date_field,
            "start": selected.start_text,
            "end": selected.end_text,
            "mode": f"automatic_recent_{DEFAULT_CAPTURE_DAYS}_days",
        },
        "outputDir": str(output_dir),
        "capture": capture_summary,
        "attachments": attachment_summary,
    }
    summary_path = output_dir / "acquisition-run-summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("流程摘要: %s", summary_path)
    return summary_path
