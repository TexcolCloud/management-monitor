"""Ports used by the monitoring application service."""

from __future__ import annotations

from typing import Protocol

from safety_monitor.domain.monitoring import CapturedWindow, PollingWindow
from safety_monitor.ports.repositories import MonitoringUnitOfWork


class WorkOrderSource(Protocol):
    def capture(self, window: PollingWindow) -> CapturedWindow:
        """Fetch a complete list/detail window or raise a classified error."""


class NotificationDispatcher(Protocol):
    def dispatch_due(self) -> tuple[int, int]:
        """Claim and deliver currently due notification tasks."""
