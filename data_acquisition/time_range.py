from __future__ import annotations

from datetime import datetime

from safety_monitor.domain.time_window import TimeWindow, recent_natural_days


DEFAULT_CAPTURE_DAYS = 30


SelectedRange = TimeWindow


def recent_range(days: int = DEFAULT_CAPTURE_DAYS, *, now: datetime | None = None) -> SelectedRange:
    return recent_natural_days(days, now=now)
