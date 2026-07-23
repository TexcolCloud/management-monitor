from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from safety_monitor.ports.repositories import NotificationOutboxRepository


@dataclass(frozen=True)
class DownloadedImage:
    saved_path: str
    path: Path


class OutboundBot(Protocol):
    enabled: bool

    def send_work_order(self, row: dict[str, Any]) -> None: ...

    def send_image(self, path: str) -> None: ...


ImageDownloader = Callable[[dict[str, Any], Path], list[DownloadedImage]]


class FeishuOutboxDispatcher:
    """Deliver leased outbox tasks without holding a database transaction over I/O."""

    def __init__(
        self,
        *,
        bot: OutboundBot,
        outbox: NotificationOutboxRepository,
        worker_id: str,
        download_images: ImageDownloader,
        temp_root: Path,
        logger: logging.Logger,
    ) -> None:
        self.bot = bot
        self.outbox = outbox
        self.worker_id = worker_id
        self.download_images = download_images
        self.temp_root = temp_root
        self.logger = logger

    def dispatch_due(self) -> tuple[int, int]:
        if not self.bot.enabled:
            return 0, 0
        sent = 0
        failed = 0
        for notification in self.outbox.claim_due(self.worker_id):
            safety_code = notification.safety_code.strip()
            if not safety_code:
                failed += 1
                self.logger.error("飞书 outbox 任务缺少 safety_code，租约到期后可重新诊断")
                continue
            row = {**dict(notification.payload), "safetyCode": safety_code}
            try:
                self.temp_root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(
                    prefix="feishu-image-attachments-",
                    dir=self.temp_root,
                ) as temp_dir:
                    images = self.download_images(row, Path(temp_dir))
                    if not notification.card_sent:
                        self.bot.send_work_order(row)
                        self.outbox.mark_card_sent(safety_code, self.worker_id)
                    sent_image_paths = set(notification.sent_image_paths)
                    for image in images:
                        if image.saved_path in sent_image_paths:
                            continue
                        self.bot.send_image(str(image.path))
                        self.outbox.mark_image_sent(
                            safety_code,
                            image.saved_path,
                            self.worker_id,
                        )
            except Exception as exc:
                failed += 1
                try:
                    self.outbox.record_failure(safety_code, exc, self.worker_id)
                except Exception:
                    self.logger.exception(
                        "飞书通知失败状态写入 PostgreSQL 失败；任务将在租约到期后恢复: %s",
                        safety_code,
                    )
                self.logger.warning(
                    "飞书通知发送失败，已持久化并按退避策略重试: %s: %s",
                    safety_code,
                    type(exc).__name__,
                )
            else:
                self.outbox.mark_sent(safety_code, self.worker_id)
                sent += 1
        return sent, failed
