from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta


DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COMPACT_TIMEZONE = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_datetime(value: str, *, end_of_day: bool = False) -> datetime:
    """Parse a portal timestamp into a local, timezone-naive datetime."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("time value is empty")

    if DATE_ONLY.fullmatch(text):
        suffix = "23:59:59.999999" if end_of_day else "00:00:00"
        return datetime.fromisoformat(f"{text}T{suffix}")

    normalized = text.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    normalized = COMPACT_TIMEZONE.sub(r"\1:\2", normalized)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid time value: {value}") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def portal_datetime_text(value: datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(sep=" ", timespec=timespec)


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("time window start must not be after end")

    @property
    def start_text(self) -> str:
        return portal_datetime_text(self.start)

    @property
    def end_text(self) -> str:
        return portal_datetime_text(self.end)


def recent_natural_days(days: int = 30, *, now: datetime | None = None) -> TimeWindow:
    if days < 1:
        raise ValueError("days must be >= 1")
    current = now or datetime.now()
    end = current.replace(hour=23, minute=59, second=59, microsecond=999999)
    start = (end - timedelta(days=days - 1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return TimeWindow(start=start, end=end)
