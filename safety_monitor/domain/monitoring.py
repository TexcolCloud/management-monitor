"""Pure monitoring rules shared by the runtime and offline tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence


class IncompleteWindowError(RuntimeError):
    """Raised when a portal window cannot be persisted without losing data."""


@dataclass(frozen=True)
class PollingPolicy:
    lookback: timedelta
    overlap: timedelta
    initial_lookback: timedelta

    def __post_init__(self) -> None:
        if self.lookback <= timedelta():
            raise ValueError("lookback must be positive")
        if self.initial_lookback <= timedelta():
            raise ValueError("initial_lookback must be positive")
        if self.overlap < timedelta():
            raise ValueError("overlap must not be negative")

    def window(
        self,
        now: datetime,
        last_success_at: datetime | None,
    ) -> "PollingWindow":
        recent_start = now - self.lookback
        if last_success_at is None:
            start = now - self.initial_lookback
        else:
            start = min(recent_start, last_success_at - self.overlap)
        return PollingWindow(start=start, end=now)


@dataclass(frozen=True)
class PollingWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("polling window start must not be after end")


@dataclass(frozen=True)
class CapturedWindow:
    """A transport-neutral capture result passed into the monitoring use case."""

    records: Sequence[Mapping[str, Any]]
    failed_details: Sequence[Mapping[str, Any]] = ()
    total_consistent: bool = True

    def complete_records(self) -> tuple[Mapping[str, Any], ...]:
        if not self.total_consistent:
            raise IncompleteWindowError("列表分页总数不一致；检查点未推进")
        if self.failed_details:
            raise IncompleteWindowError(
                f"本轮有 {len(self.failed_details)} 条工单详情抓取失败；检查点未推进"
            )
        missing_codes = [row for row in self.records if not str(row.get("safetyCode") or "").strip()]
        if missing_codes:
            raise IncompleteWindowError(
                f"本轮有 {len(missing_codes)} 条工单缺少 safety_code；检查点未推进"
            )
        return tuple(self.records)


@dataclass(frozen=True)
class PersistedWindow:
    inserted_codes: frozenset[str]
    upserted_count: int
    checkpoint: datetime


@dataclass(frozen=True)
class MonitoringCycle:
    window: PollingWindow
    details_seen: int
    inserted_codes: frozenset[str]
    upserted_count: int

