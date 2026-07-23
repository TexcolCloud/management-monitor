from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from safety_monitor.domain.exporting import (
    ExportPolicy,
    ExportRequest,
    ExportRequestError,
)


class DateSelectionCardPort(Protocol):
    def __call__(self, recipient_open_id: str) -> None: ...


class ExportJobPort(Protocol):
    def __call__(self, request: ExportRequest) -> None: ...


@dataclass(frozen=True)
class ExportActionResult:
    accepted: bool
    message: str


@dataclass
class ExportApplicationService:
    policy: ExportPolicy
    send_date_selection_card: DateSelectionCardPort
    submit_export_job: ExportJobPort

    def handle_command(self, sender_open_id: str, message_type: str, command: str) -> bool:
        if message_type != "text":
            return False
        if not self.policy.is_allowed(sender_open_id):
            return False
        if not self.policy.accepts_command(command):
            return False
        self.send_date_selection_card(sender_open_id)
        return True

    def handle_action(
        self,
        operator_open_id: str,
        action_value: Mapping[str, Any] | None,
        form_value: Mapping[str, Any] | None,
    ) -> ExportActionResult:
        try:
            request = self.policy.build_request(
                operator_open_id,
                action_value,
                form_value,
            )
        except ExportRequestError as exc:
            return ExportActionResult(False, str(exc))
        self.submit_export_job(request)
        return ExportActionResult(True, "已开始导出，Excel 文件将私发给你。")
