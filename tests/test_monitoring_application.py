from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock

from safety_monitor.application.monitoring import (
    DeliverPendingNotifications,
    MonitorNewOrders,
)
from safety_monitor.domain.monitoring import (
    CapturedWindow,
    IncompleteWindowError,
    PersistedWindow,
    PollingPolicy,
)


class MonitoringApplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 23, 10, 0)
        self.policy = PollingPolicy(
            lookback=timedelta(minutes=5),
            overlap=timedelta(seconds=30),
            initial_lookback=timedelta(minutes=5),
        )

    def test_complete_window_is_persisted_with_checkpoint_in_one_port_call(self) -> None:
        source = Mock()
        source.capture.return_value = CapturedWindow(records=({"safetyCode": "A-1"},))
        unit_of_work = Mock()
        unit_of_work.read_checkpoint.return_value = datetime(2026, 7, 23, 9, 58)
        unit_of_work.persist_complete_window.return_value = PersistedWindow(
            inserted_codes=frozenset({"A-1"}),
            upserted_count=1,
            checkpoint=self.now,
        )

        result = MonitorNewOrders("daily-management", self.policy, source, unit_of_work).run(
            self.now
        )

        window = source.capture.call_args.args[0]
        self.assertEqual(window.start, datetime(2026, 7, 23, 9, 55))
        unit_of_work.persist_complete_window.assert_called_once_with(
            stream="daily-management",
            records=({"safetyCode": "A-1"},),
            expected_checkpoint=datetime(2026, 7, 23, 9, 58),
            next_checkpoint=self.now,
        )
        self.assertEqual(result.inserted_codes, frozenset({"A-1"}))

    def test_incomplete_capture_never_reaches_transaction_port(self) -> None:
        source = Mock()
        source.capture.return_value = CapturedWindow(
            records=({"safetyCode": "A-1"},),
            failed_details=({"id": "1", "_captureError": "timeout"},),
        )
        unit_of_work = Mock()
        unit_of_work.read_checkpoint.return_value = None

        with self.assertRaisesRegex(IncompleteWindowError, "检查点未推进"):
            MonitorNewOrders("daily-management", self.policy, source, unit_of_work).run(self.now)

        unit_of_work.persist_complete_window.assert_not_called()

    def test_missing_safety_code_does_not_advance_checkpoint(self) -> None:
        source = Mock()
        source.capture.return_value = CapturedWindow(records=({"id": "1"},))
        unit_of_work = Mock()
        unit_of_work.read_checkpoint.return_value = None

        with self.assertRaisesRegex(IncompleteWindowError, "safety_code"):
            MonitorNewOrders("daily-management", self.policy, source, unit_of_work).run(self.now)

        unit_of_work.persist_complete_window.assert_not_called()

    def test_notification_delivery_is_a_separate_use_case(self) -> None:
        dispatcher = Mock()
        expected = (2, 1)
        dispatcher.dispatch_due.return_value = expected

        self.assertIs(DeliverPendingNotifications(dispatcher).run(), expected)
        dispatcher.dispatch_due.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
