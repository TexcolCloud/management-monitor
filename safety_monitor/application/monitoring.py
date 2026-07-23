"""Monitoring and notification use cases with no infrastructure dependencies."""

from __future__ import annotations

from datetime import datetime

from safety_monitor.domain.monitoring import MonitoringCycle, PollingPolicy
from safety_monitor.ports.monitoring import (
    MonitoringUnitOfWork,
    NotificationDispatcher,
    WorkOrderSource,
)


class MonitorNewOrders:
    def __init__(
        self,
        stream: str,
        policy: PollingPolicy,
        source: WorkOrderSource,
        unit_of_work: MonitoringUnitOfWork,
    ) -> None:
        self._stream = stream
        self._policy = policy
        self._source = source
        self._unit_of_work = unit_of_work

    def run(self, now: datetime) -> MonitoringCycle:
        checkpoint = self._unit_of_work.read_checkpoint(self._stream)
        window = self._policy.window(now, checkpoint)
        captured = self._source.capture(window)
        records = captured.complete_records()
        persisted = self._unit_of_work.persist_complete_window(
            stream=self._stream,
            records=records,
            expected_checkpoint=checkpoint,
            next_checkpoint=window.end,
        )
        return MonitoringCycle(
            window=window,
            details_seen=len(records),
            inserted_codes=persisted.inserted_codes,
            upserted_count=persisted.upserted_count,
        )


class DeliverPendingNotifications:
    """A separately schedulable use case, independent from portal availability."""

    def __init__(self, dispatcher: NotificationDispatcher) -> None:
        self._dispatcher = dispatcher

    def run(self):
        return self._dispatcher.dispatch_due()

