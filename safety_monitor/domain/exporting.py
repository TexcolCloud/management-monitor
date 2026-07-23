from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any


CARD_COMMAND = "导出Excel表格"
CARD_ACTION = "export_by_date"
FEISHU_DATE_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})(?:\s*[+-]\d{2}:?\d{2})?$"
)


class ExportRequestError(ValueError):
    """A rejected export request with a user-safe Chinese explanation."""


def form_date_text(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("date") or value.get("value") or ""
    return str(value or "").strip()


def normalize_feishu_date(value: Any) -> date:
    """Parse a Feishu date while treating its timezone suffix as display metadata."""
    text = form_date_text(value)
    match = FEISHU_DATE_PATTERN.fullmatch(text)
    if not match:
        raise ExportRequestError("日期格式无效，请重新选择开始日期和结束日期。")
    try:
        return date.fromisoformat(match.group("date"))
    except ValueError as exc:
        raise ExportRequestError("日期格式无效，请重新选择开始日期和结束日期。") from exc


@dataclass(frozen=True)
class ExportDateRange:
    start: datetime
    end: datetime

    @classmethod
    def from_form_values(cls, start_value: Any, end_value: Any) -> "ExportDateRange":
        if not form_date_text(start_value) or not form_date_text(end_value):
            raise ExportRequestError("请先选择开始日期和结束日期。")
        start_date = normalize_feishu_date(start_value)
        end_date = normalize_feishu_date(end_value)
        if start_date > end_date:
            raise ExportRequestError("开始日期不能晚于结束日期。")
        return cls(
            start=datetime.combine(start_date, time.min),
            end=datetime.combine(end_date, time.max),
        )


@dataclass(frozen=True)
class ExportRequest:
    requester_open_id: str = field(repr=False)
    date_range: ExportDateRange


@dataclass(frozen=True)
class ExportPolicy:
    allowed_open_ids: frozenset[str] = field(repr=False)
    custom_command: str

    def is_allowed(self, open_id: str) -> bool:
        return bool(open_id) and open_id in self.allowed_open_ids

    def accepts_command(self, command: str) -> bool:
        commands = {CARD_COMMAND}
        if self.custom_command.strip():
            commands.add(self.custom_command.strip())
        return command.strip() in commands

    def build_request(
        self,
        operator_open_id: str,
        action_value: Mapping[str, Any] | None,
        form_value: Mapping[str, Any] | None,
    ) -> ExportRequest:
        if not self.is_allowed(operator_open_id):
            raise ExportRequestError("你没有导出该数据的权限。")
        if not isinstance(action_value, Mapping) or action_value.get("action") != CARD_ACTION:
            raise ExportRequestError("不支持该卡片操作。")
        values = form_value if isinstance(form_value, Mapping) else {}
        return ExportRequest(
            requester_open_id=operator_open_id,
            date_range=ExportDateRange.from_form_values(
                values.get("start_date"),
                values.get("end_date"),
            ),
        )
