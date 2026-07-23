from __future__ import annotations

import hashlib
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from database.migrations import Migration, apply_migrations, checksum_is_compatible
from database.postgres_store import (
    claim_feishu_notifications,
    connect,
    load_config,
    mark_feishu_notification_card_sent,
    pending_feishu_notifications,
    record_feishu_notification_failure,
    rows_by_create_time,
    sanitize_error,
)
from safety_monitor.adapters.postgres import (
    PostgresCheckpointRepository,
    PostgresMonitoringUnitOfWork,
)
from safety_monitor.ports.repositories import (
    CheckpointConflictError,
    MonitorCheckpoint,
)


CONFIG = {"enabled": True, "schema": "public", "table": "daily_manage"}
LEGACY_001_CHECKSUMS = (
    "89417bba4487f40eb8e0c7d6bc933151ae9fb52bb178d98cb54cc4472ef3e676",
    "f8e4bb6a62be918429805cc2134eff7b04b5cfdb5a9d6333699a3305bafcf016",
)


class MigrationSafetyTest(unittest.TestCase):
    def test_original_migration_files_are_byte_for_byte_unchanged(self) -> None:
        root = Path(__file__).parents[1] / "database" / "migrations"
        expected = {
            "001_initial.sql": "eea683f27f145fcb6289497e544bdddd8b561ffbf05083ff4b6f9c478cd3eb7b",
            "002_feishu_notification_outbox.sql": "d718d2684692b2786e12b8a08f20e7843e8f1d217121ce978fc8f884214410fc",
            "003_feishu_notification_delivery_progress.sql": "31c9cfffbe3a1b1705a04bff24b10338d58efb9e06a02afab545a8626335664d",
            "004_create_time_index.sql": "7318d7457e079827cea192156f809dc71ff8428dc91ff8a06b607d92acc9d337",
        }
        actual = {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in expected
        }
        self.assertEqual(actual, expected)

    def test_only_known_001_checksums_are_accepted(self) -> None:
        migration = Migration(1, "001_initial.sql", "current", "SELECT 1")
        self.assertTrue(checksum_is_compatible(migration, "current"))
        for checksum in LEGACY_001_CHECKSUMS:
            self.assertTrue(checksum_is_compatible(migration, checksum))
        self.assertFalse(checksum_is_compatible(migration, "unknown"))
        self.assertFalse(
            checksum_is_compatible(Migration(2, "002.sql", "current", ""), LEGACY_001_CHECKSUMS[0])
        )

    def test_migration_005_is_additive_and_idempotent(self) -> None:
        path = (
            Path(__file__).parents[1]
            / "database"
            / "migrations"
            / "005_monitor_checkpoint_and_outbox_leases.sql"
        )
        sql = path.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS", sql)
        self.assertEqual(sql.count("ALTER TABLE"), sql.count("ADD COLUMN IF NOT EXISTS"))
        self.assertIn("next_attempt_at", sql)
        self.assertIn("lease_until", sql)
        self.assertIn("error_kind", sql)

    def test_apply_migrations_accepts_known_alias_and_takes_transaction_lock(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(1, LEGACY_001_CHECKSUMS[0])]
        migration = Migration(1, "001_initial.sql", "current", "SELECT 1")

        with patch("database.migrations.load_migrations", return_value=[migration]):
            self.assertEqual(apply_migrations(connection, CONFIG), [])

        first_query, first_params = cursor.execute.call_args_list[0].args
        self.assertIn("pg_advisory_xact_lock", first_query)
        self.assertEqual(first_params[1], "public")
        self.assertFalse(any(call.args[0] == "SELECT 1" for call in cursor.execute.call_args_list))
        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()

    def test_apply_migrations_rejects_unknown_checksum_and_rolls_back(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(1, "not-a-known-checksum")]
        migration = Migration(1, "001_initial.sql", "current", "SELECT 1")

        with patch("database.migrations.load_migrations", return_value=[migration]):
            with self.assertRaisesRegex(RuntimeError, "checksum changed"):
                apply_migrations(connection, CONFIG)

        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()

    def test_existing_legacy_table_is_reconciled_before_older_index_migration(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = ('"public"."daily_manage"',)
        create_index = Migration(4, "004_create_time_index.sql", "four", "CREATE OLD INDEX")
        reconcile = Migration(
            6,
            "006_reconcile_legacy_work_order_table.sql",
            "six",
            "ALTER LEGACY TABLE",
        )

        with patch(
            "database.migrations.load_migrations",
            return_value=[create_index, reconcile],
        ):
            self.assertEqual(apply_migrations(connection, CONFIG), [4, 6])

        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertLess(statements.index("ALTER LEGACY TABLE"), statements.index("CREATE OLD INDEX"))
        self.assertEqual(statements.count("ALTER LEGACY TABLE"), 1)


class DatabaseConnectionConfigurationTest(unittest.TestCase):
    def test_standard_connect_timeout_environment_variable_remains_compatible(self) -> None:
        with patch("database.postgres_store.load_env_file"), patch.dict(
            os.environ,
            {
                "PGCONNECT_TIMEOUT": "17",
                "PGSTATEMENT_TIMEOUT_MS": "31000",
                "PGLOCK_TIMEOUT_MS": "9000",
            },
        ):
            config = load_config()

        self.assertEqual(config["connect_timeout"], "17")
        self.assertEqual(config["statement_timeout_ms"], "31000")
        self.assertEqual(config["lock_timeout_ms"], "9000")

    def test_optional_session_timeouts_are_numeric_and_not_interpolated_from_secrets(self) -> None:
        driver = MagicMock()
        config = {
            "host": "db",
            "port": 5432,
            "database": "safety",
            "user": "service",
            "password": "not-logged",
            "connect_timeout": 12,
            "statement_timeout_ms": 30000,
            "lock_timeout_ms": 8000,
        }

        with patch(
            "database.postgres_store._require_driver",
            return_value=(driver, MagicMock(), MagicMock()),
        ):
            connect(config)

        kwargs = driver.connect.call_args.kwargs
        self.assertEqual(kwargs["connect_timeout"], 12)
        self.assertEqual(kwargs["options"], "-c statement_timeout=30000 -c lock_timeout=8000")
        self.assertNotIn(config["password"], kwargs["options"])

    def test_invalid_session_timeout_is_rejected_before_connecting(self) -> None:
        driver = MagicMock()
        config = {
            "host": "db",
            "database": "safety",
            "user": "service",
            "password": "secret",
            "statement_timeout_ms": "30 seconds",
        }
        with patch(
            "database.postgres_store._require_driver",
            return_value=(driver, MagicMock(), MagicMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "statement_timeout_ms"):
                connect(config)
        driver.connect.assert_not_called()


class LegacyQueryAndOutboxTest(unittest.TestCase):
    def test_export_reconstructs_raw_null_row_from_relational_columns(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        create_time = datetime(2026, 7, 1, 9, 30)
        cursor.fetchall.return_value = [
            ("source-1", "CODE-1", None, "检查", None, "张三", create_time, None)
        ]
        start = datetime(2026, 7, 1)
        end = datetime(2026, 7, 1, 23, 59, 59, 999999)

        with patch("database.postgres_store.load_config", return_value=CONFIG), patch(
            "database.postgres_store.connect", return_value=connection
        ), patch("database.postgres_store.ensure_table"):
            rows = rows_by_create_time(start, end)

        self.assertEqual(
            rows,
            [
                {
                    "id": "source-1",
                    "safetyCode": "CODE-1",
                    "companyName": "",
                    "safetyType": "检查",
                    "theme": "",
                    "createBy": "张三",
                    "createTime": "2026-07-01 09:30:00",
                }
            ],
        )
        query = cursor.execute.call_args.args[0]
        self.assertNotIn("raw_data IS NOT NULL", query)
        self.assertIn("source_id", query)

    def test_pending_uses_relational_code_and_only_returns_due_unleased_rows(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [
            ("REL-CODE", {"safetyCode": "PAYLOAD-CODE", "theme": "x"}, None, [])
        ]

        rows = pending_feishu_notifications(connection, CONFIG)

        self.assertEqual(rows[0]["safetyCode"], "REL-CODE")
        self.assertNotIn("_feishuAttempts", rows[0])
        query = cursor.execute.call_args.args[0]
        self.assertIn("next_attempt_at <= CURRENT_TIMESTAMP", query)
        self.assertIn("lease_until IS NULL OR lease_until <= CURRENT_TIMESTAMP", query)

    def test_claim_uses_skip_locked_and_persists_a_lease(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        now = datetime(2026, 7, 23, 10, 0)
        lease_until = datetime(2026, 7, 23, 10, 2)
        cursor.fetchall.return_value = [
            ("CODE-1", {"safetyCode": "WRONG"}, None, [], 0, "worker-1", lease_until)
        ]

        rows = claim_feishu_notifications(
            connection,
            CONFIG,
            "worker-1",
            now=now,
            lease_seconds=120,
            commit=False,
        )

        query, params = cursor.execute.call_args.args
        self.assertIn("FOR UPDATE SKIP LOCKED", query)
        self.assertIn("lease_owner = %s", query)
        self.assertEqual(params[-2:], ("worker-1", lease_until))
        self.assertEqual(rows[0]["safetyCode"], "CODE-1")
        self.assertEqual(rows[0]["_feishuLeaseOwner"], "worker-1")
        connection.commit.assert_not_called()

    def test_failure_is_sanitized_classified_and_exponentially_delayed(self) -> None:
        connection = MagicMock()
        secret_error = (
            "timeout token=token-value password:password-value "
            "Authorization=Bearer-value Bearer bearer-value https://user:url-password@host"
        )

        record_feishu_notification_failure(connection, CONFIG, "CODE-1", secret_error)

        cursor = connection.cursor.return_value.__enter__.return_value
        query, params = cursor.execute.call_args.args
        serialized_params = " ".join(str(value) for value in params)
        self.assertIn("POWER(2, LEAST(attempts, 16))", query)
        self.assertIn("next_attempt_at", query)
        self.assertEqual(params[1], "timeout")
        for secret in ("token-value", "password-value", "Bearer-value", "bearer-value", "url-password"):
            self.assertNotIn(secret, serialized_params)
        self.assertIn("<redacted>", sanitize_error(secret_error))

    def test_outbox_progress_rejects_a_lost_lease(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 0

        with self.assertRaisesRegex(RuntimeError, "lease was lost"):
            mark_feishu_notification_card_sent(
                connection,
                CONFIG,
                "CODE-1",
                lease_owner="worker-that-lost-ownership",
            )


class MonitoringUnitOfWorkTest(unittest.TestCase):
    def test_checkpoint_repository_locks_and_updates_by_revision(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        previous_time = datetime(2026, 7, 23, 9, 55)
        next_time = datetime(2026, 7, 23, 10, 0)
        cursor.fetchone.side_effect = [(previous_time, 3), (4,)]
        repository = PostgresCheckpointRepository(connection, CONFIG)

        current = repository.load_for_update("monitor")
        saved = repository.save("monitor", next_time, current)

        self.assertEqual(saved.revision, 4)
        load_query = cursor.execute.call_args_list[0].args[0]
        update_query = cursor.execute.call_args_list[1].args[0]
        self.assertIn("FOR UPDATE", load_query)
        self.assertIn("revision = %s", update_query)
        self.assertEqual(cursor.execute.call_args_list[1].args[1][-1], 3)

    def test_read_checkpoint_implements_monitoring_application_port(self) -> None:
        connection = MagicMock()
        expected = datetime(2026, 7, 23, 9, 55)
        checkpoints = MagicMock()
        checkpoints.load.return_value = MonitorCheckpoint("monitor", expected, 3)
        uow = PostgresMonitoringUnitOfWork(
            CONFIG,
            connection_factory=lambda _config: connection,
            migration_runner=lambda _connection, _config: [],
        )

        with patch(
            "safety_monitor.adapters.postgres.PostgresCheckpointRepository",
            return_value=checkpoints,
        ):
            self.assertEqual(uow.read_checkpoint("monitor"), expected)

        connection.commit.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_work_order_outbox_and_checkpoint_commit_together(self) -> None:
        connection = MagicMock()
        checkpoints = MagicMock()
        checkpoints.load_for_update.return_value = None
        checkpoints.save.return_value = MonitorCheckpoint(
            "monitor", datetime(2026, 7, 23, 10, 0), 1
        )
        rows = [
            {"safetyCode": "NEW", "theme": "new"},
            {"safetyCode": "EXISTING", "theme": "existing"},
        ]
        uow = PostgresMonitoringUnitOfWork(
            CONFIG,
            connection_factory=lambda _config: connection,
            migration_runner=lambda _connection, _config: [],
        )

        with patch(
            "safety_monitor.adapters.postgres.PostgresCheckpointRepository",
            return_value=checkpoints,
        ), patch(
            "safety_monitor.adapters.postgres.prepare_records",
            return_value=[("record",)],
        ), patch(
            "safety_monitor.adapters.postgres._insert_new_records",
            return_value={"NEW"},
        ), patch(
            "safety_monitor.adapters.postgres._upsert_prepared_records",
            return_value=2,
        ), patch(
            "safety_monitor.adapters.postgres.enqueue_feishu_notifications"
        ) as enqueue:
            result = uow.persist_complete_window(
                "monitor",
                rows,
                expected_checkpoint=None,
                next_checkpoint=datetime(2026, 7, 23, 10, 0),
            )

        self.assertEqual(result.inserted_codes, frozenset({"NEW"}))
        self.assertEqual(result.upserted_count, 2)
        self.assertEqual(enqueue.call_args.args[2], [rows[0]])
        checkpoints.save.assert_called_once()
        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()
        connection.close.assert_called_once_with()

    def test_monitoring_uow_deduplicates_safety_code_before_postgres_upsert(self) -> None:
        connection = MagicMock()
        checkpoints = MagicMock()
        checkpoints.load_for_update.return_value = None
        checkpoints.save.return_value = MonitorCheckpoint(
            "monitor", datetime(2026, 7, 23, 10, 0), 1
        )
        rows = [
            {"safetyCode": "DUPLICATE", "theme": "first"},
            {"safetyCode": "DUPLICATE", "theme": "second"},
        ]
        uow = PostgresMonitoringUnitOfWork(
            CONFIG,
            connection_factory=lambda _config: connection,
            migration_runner=lambda _connection, _config: [],
        )

        with patch(
            "safety_monitor.adapters.postgres.PostgresCheckpointRepository",
            return_value=checkpoints,
        ), patch(
            "safety_monitor.adapters.postgres.prepare_records",
            return_value=[("record",)],
        ) as prepare, patch(
            "safety_monitor.adapters.postgres._insert_new_records",
            return_value=set(),
        ), patch(
            "safety_monitor.adapters.postgres._upsert_prepared_records",
            return_value=1,
        ):
            uow.persist_complete_window(
                "monitor",
                rows,
                expected_checkpoint=None,
                next_checkpoint=datetime(2026, 7, 23, 10, 0),
            )

        prepare.assert_called_once_with([rows[0]])

    def test_outbox_failure_rolls_back_work_orders_and_checkpoint(self) -> None:
        connection = MagicMock()
        checkpoints = MagicMock()
        checkpoints.load_for_update.return_value = None
        uow = PostgresMonitoringUnitOfWork(
            CONFIG,
            connection_factory=lambda _config: connection,
            migration_runner=lambda _connection, _config: [],
        )

        with patch(
            "safety_monitor.adapters.postgres.PostgresCheckpointRepository",
            return_value=checkpoints,
        ), patch(
            "safety_monitor.adapters.postgres.prepare_records",
            return_value=[("record",)],
        ), patch(
            "safety_monitor.adapters.postgres._insert_new_records",
            return_value={"NEW"},
        ), patch(
            "safety_monitor.adapters.postgres._upsert_prepared_records",
            return_value=1,
        ), patch(
            "safety_monitor.adapters.postgres.enqueue_feishu_notifications",
            side_effect=RuntimeError("outbox unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
                uow.persist_complete_window(
                    "monitor",
                    [{"safetyCode": "NEW"}],
                    expected_checkpoint=None,
                    next_checkpoint=datetime(2026, 7, 23, 10, 0),
                )

        checkpoints.save.assert_not_called()
        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()
        connection.close.assert_called_once_with()

    def test_checkpoint_conflict_rolls_back_before_any_business_write(self) -> None:
        connection = MagicMock()
        checkpoints = MagicMock()
        checkpoints.load_for_update.return_value = MonitorCheckpoint(
            "monitor", datetime(2026, 7, 23, 9, 55), 2
        )
        uow = PostgresMonitoringUnitOfWork(
            CONFIG,
            connection_factory=lambda _config: connection,
            migration_runner=lambda _connection, _config: [],
        )

        with patch(
            "safety_monitor.adapters.postgres.PostgresCheckpointRepository",
            return_value=checkpoints,
        ), patch("safety_monitor.adapters.postgres.prepare_records") as prepare:
            with self.assertRaises(CheckpointConflictError):
                uow.persist_complete_window(
                    "monitor",
                    [{"safetyCode": "CODE-1"}],
                    expected_checkpoint=datetime(2026, 7, 23, 9, 50),
                    next_checkpoint=datetime(2026, 7, 23, 10, 0),
                )

        prepare.assert_not_called()
        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_incomplete_direct_call_is_rejected_before_opening_database(self) -> None:
        connection_factory = MagicMock()
        uow = PostgresMonitoringUnitOfWork(CONFIG, connection_factory=connection_factory)

        with self.assertRaisesRegex(ValueError, "incomplete work orders"):
            uow.persist_complete_window(
                stream="monitor",
                records=[{"safetyCode": "", "_captureError": "timeout"}],
                expected_checkpoint=None,
                next_checkpoint=datetime(2026, 7, 23, 10, 0),
            )

        connection_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
