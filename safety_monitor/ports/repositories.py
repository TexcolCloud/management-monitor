from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Protocol, Sequence

from safety_monitor.domain.monitoring import PersistedWindow


WorkOrderRow = Mapping[str, Any]


class CheckpointConflictError(RuntimeError):
    """The persisted monitor checkpoint no longer matches the caller's view."""


@dataclass(frozen=True)
class MonitorCheckpoint:
    stream_name: str
    last_success_at: datetime
    revision: int


@dataclass(frozen=True)
class OutboxNotification:
    safety_code: str
    payload: Mapping[str, Any]
    card_sent: bool
    sent_image_paths: tuple[str, ...]
    attempts: int
    lease_owner: str
    lease_until: datetime | None


class WorkOrderRepository(Protocol):
    def existing_codes(self, safety_codes: Iterable[str]) -> set[str]: ...

    def upsert_complete(self, rows: Iterable[WorkOrderRow]) -> int: ...

    def created_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]: ...


class CheckpointRepository(Protocol):
    def load_for_update(self, stream_name: str) -> MonitorCheckpoint | None: ...

    def save(
        self,
        stream_name: str,
        last_success_at: datetime,
        current: MonitorCheckpoint | None,
    ) -> MonitorCheckpoint: ...


class NotificationOutboxRepository(Protocol):
    def claim_due(
        self,
        lease_owner: str,
        limit: int = 50,
        lease_duration: timedelta = timedelta(minutes=2),
        now: datetime | None = None,
    ) -> Sequence[OutboxNotification]: ...

    def mark_card_sent(self, safety_code: str, lease_owner: str) -> None: ...

    def mark_image_sent(self, safety_code: str, saved_path: str, lease_owner: str) -> None: ...

    def mark_sent(self, safety_code: str, lease_owner: str) -> None: ...

    def record_failure(
        self,
        safety_code: str,
        error: object,
        lease_owner: str,
        error_kind: str | None = None,
    ) -> None: ...


class MonitoringUnitOfWork(Protocol):
    def read_checkpoint(self, stream: str) -> datetime | None: ...

    def persist_complete_window(
        self,
        stream: str,
        records: Iterable[WorkOrderRow],
        expected_checkpoint: datetime | None,
        next_checkpoint: datetime,
    ) -> PersistedWindow: ...
