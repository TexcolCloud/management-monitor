from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call

from safety_monitor.adapters.feishu_notifications import DownloadedAttachment, DownloadedImage
from workflows.feishu_latest_notification import (
    LatestNotification,
    latest_snapshot_notification,
    send_latest_snapshot_notification,
)


class FeishuLatestNotificationTest(unittest.TestCase):
    def snapshot(self, root: Path) -> Path:
        attachments = root / "daily-attachments"
        attachments.mkdir()
        latest_image = attachments / "latest.png"
        latest_image.write_bytes(b"image")
        latest_document = attachments / "latest.pdf"
        latest_document.write_bytes(b"pdf")
        old_image = attachments / "old.png"
        old_image.write_bytes(b"image")
        (root / "management-api-details.json").write_text(
            json.dumps(
                {
                    "rows": [
                        {"id": "old", "safetyCode": "CODE-OLD", "createTime": "2026-07-22 09:00:00"},
                        {
                            "id": "latest",
                            "safetyCode": "CODE-LATEST",
                            "companyName": "示例分公司/夷陵区分公司",
                            "theme": "最新工单",
                            "createTime": "2026-07-23 13:28:52",
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (root / "daily-attachments-manifest.json").write_text(
            json.dumps(
                {
                    "rows": [
                        {"safetyCode": "CODE-OLD", "isImage": True, "status": "downloaded", "savedPath": "daily-attachments/old.png"},
                        {"safetyCode": "CODE-LATEST", "isImage": True, "status": "reused", "fileLabel": "培训照片", "savedPath": "daily-attachments/latest.png"},
                        {"safetyCode": "CODE-LATEST", "isImage": False, "status": "downloaded", "fileLabel": "会议通知", "savedPath": "daily-attachments/latest.pdf"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return root

    def test_latest_snapshot_notification_selects_latest_row_and_all_its_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            notification = latest_snapshot_notification(self.snapshot(Path(temp_dir)), "daily-attachments")

        self.assertEqual(notification.row["safetyCode"], "CODE-LATEST")
        self.assertEqual([attachment.path.name for attachment in notification.attachments], ["latest.png", "latest.pdf"])
        self.assertEqual([attachment.attachment_type for attachment in notification.attachments], ["培训照片", "会议通知"])
        self.assertEqual([attachment.is_image for attachment in notification.attachments], [True, False])

    def test_send_uses_text_then_each_local_attachment_without_persistence(self) -> None:
        bot = Mock(enabled=True)
        notification = LatestNotification(
            {"safetyCode": "CODE-1"},
            (
                DownloadedImage("first.png", Path("first.png"), "会议通知"),
                DownloadedAttachment("second.pdf", Path("second.pdf"), "培训资料"),
            ),
        )

        send_latest_snapshot_notification(notification, bot)

        self.assertEqual(
            bot.method_calls,
            [
                call.send_work_order({"safetyCode": "CODE-1"}),
                call.send_text("【附件类型】 会议通知"),
                call.send_image("first.png"),
                call.send_text("【附件类型】 培训资料"),
                call.send_file("second.pdf"),
            ],
        )

    def test_send_rejects_missing_feishu_configuration(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "FEISHU_APP_ID"):
            send_latest_snapshot_notification(LatestNotification({}, ()), Mock(enabled=False))


if __name__ == "__main__":
    unittest.main()
