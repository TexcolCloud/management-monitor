from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from common.cli import run_cli_safely
from common.dataset_io import load_rows
from common.feishu_app_bot import FeishuOutboundBot
from common.logging_utils import setup_logging
from common.runtime_config import load_runtime_config
from common.workflow_paths import DEFAULT_CAPTURE_ROOT, resolve_capture_data_dir
from database.postgres_store import load_env_file
from safety_monitor.adapters.feishu_notifications import DownloadedAttachment, DownloadedImage


LOGGER = logging.getLogger("workorder_daily_manage")
SUCCESSFUL_ATTACHMENT_STATUSES = frozenset({"downloaded", "reused", "skipped_duplicate"})


@dataclass(frozen=True)
class LatestNotification:
    row: dict[str, Any]
    attachments: tuple[DownloadedAttachment, ...]


def _create_time(row: Mapping[str, Any]) -> datetime:
    text = str(row.get("createTime") or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min
    return value.astimezone().replace(tzinfo=None) if value.tzinfo else value


def latest_work_order(data_dir: Path) -> dict[str, Any]:
    rows = [
        row
        for row in load_rows(data_dir / "management-api-details.json")
        if not row.get("_captureError") and str(row.get("safetyCode") or "").strip()
    ]
    if not rows:
        raise RuntimeError("最新完整快照中没有可发送的工单详情。")
    return max(rows, key=lambda row: (_create_time(row), str(row.get("safetyCode") or "")))


def attachments_for_work_order(
    data_dir: Path,
    row: Mapping[str, Any],
    attachment_dir_name: str,
) -> tuple[DownloadedAttachment, ...]:
    try:
        payload = json.loads((data_dir / "daily-attachments-manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("最新完整快照的附件清单不可读取。") from exc
    entries = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("最新完整快照的附件清单格式无效。")

    safety_code = str(row.get("safetyCode") or "").strip()
    source_id = str(row.get("id") or "").strip()
    attachment_root = (data_dir / attachment_dir_name).resolve()
    attachments: list[DownloadedAttachment] = []
    seen: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        entry_code = str(entry.get("safetyCode") or "").strip()
        entry_id = str(entry.get("id") or "").strip()
        if entry_code != safety_code and (not source_id or entry_id != source_id):
            continue
        if str(entry.get("status") or "") not in SUCCESSFUL_ATTACHMENT_STATUSES:
            continue
        saved_path = str(entry.get("savedPath") or "").strip()
        candidate = (data_dir / saved_path).resolve()
        try:
            candidate.relative_to(attachment_root)
        except ValueError as exc:
            raise RuntimeError("工单附件路径不在完整快照目录内。") from exc
        if not candidate.is_file():
            raise RuntimeError("工单附件文件不存在。")
        if candidate not in seen:
            seen.add(candidate)
            attachment_type = str(entry.get("fileLabel") or entry.get("fileType") or "附件").strip()
            attachment_class = DownloadedImage if bool(entry.get("isImage")) else DownloadedAttachment
            attachments.append(
                attachment_class(
                    saved_path=saved_path,
                    path=candidate,
                    attachment_type=attachment_type or "附件",
                )
            )
    return tuple(attachments)


def latest_snapshot_notification(data_dir: Path, attachment_dir_name: str) -> LatestNotification:
    row = latest_work_order(data_dir)
    return LatestNotification(row, attachments_for_work_order(data_dir, row, attachment_dir_name))


def send_latest_snapshot_notification(
    notification: LatestNotification,
    bot: FeishuOutboundBot,
    logger: logging.Logger = LOGGER,
) -> None:
    if not bot.enabled:
        raise RuntimeError("最新工单飞书联调需要配置 FEISHU_APP_ID、FEISHU_APP_SECRET 和 FEISHU_RECEIVE_ID。")
    bot.send_work_order(notification.row)
    for attachment in notification.attachments:
        bot.send_text(f"【附件类型】 {attachment.attachment_type or '附件'}")
        if attachment.is_image:
            bot.send_image(str(attachment.path))
        else:
            bot.send_file(str(attachment.path))
    logger.info("最新工单联调已发送：附件=%s", len(notification.attachments))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将最新完整快照中的工单按新工单样式发送到飞书。")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="只验证最新工单和本地附件，不发送飞书消息。")
    return parser.parse_args()


def main() -> None:
    setup_logging()
    args = parse_args()
    load_env_file()
    runtime = load_runtime_config()
    data_dir = resolve_capture_data_dir(args.data_dir, DEFAULT_CAPTURE_ROOT)
    notification = latest_snapshot_notification(data_dir, runtime.attachment_dir_name)
    if args.dry_run:
        LOGGER.info("最新工单联调验证完成：附件=%s", len(notification.attachments))
        return
    send_latest_snapshot_notification(notification, FeishuOutboundBot.from_environment(), LOGGER)


if __name__ == "__main__":
    run_cli_safely(
        main,
        failure_message="最新工单飞书联调失败",
        interrupted_message="已停止最新工单飞书联调",
    )
