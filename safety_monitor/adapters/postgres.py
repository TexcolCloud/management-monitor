from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

from database.migrations import apply_migrations
from database.postgres_store import (
    _export_row_from_record,
    _require_driver,
    claim_feishu_notifications,
    connect,
    enqueue_feishu_notifications,
    existing_safety_codes,
    mark_feishu_notification_card_sent,
    mark_feishu_notification_image_sent,
    mark_feishu_notification_sent,
    migration_target_table,
    monitor_checkpoint_table,
    prepare_records,
    qualified_table,
    record_feishu_notification_failure,
)
from safety_monitor.domain.monitoring import PersistedWindow
from safety_monitor.ports.repositories import (
    CheckpointConflictError,
    MonitorCheckpoint,
    OutboxNotification,
    WorkOrderRow,
)


ConnectionFactory = Callable[[Mapping[str, Any]], Any]
MigrationRunner = Callable[[Any, Mapping[str, Any]], list[int]]

_WORK_ORDER_COLUMNS = """
    source_id, safety_code, company_name, belong_company,
    safety_type, category, risk_level, theme, create_by,
    create_time, raw_data
"""


def _insert_new_records(
    connection: Any,
    config: Mapping[str, Any],
    records: Sequence[tuple[Any, ...]],
) -> set[str]:
    if not records:
        return set()
    _, _, execute_values = _require_driver()
    sql = """
        INSERT INTO {table} ({columns})
        VALUES %s
        ON CONFLICT (safety_code) DO NOTHING
        RETURNING safety_code
    """.format(table=qualified_table(config), columns=_WORK_ORDER_COLUMNS)
    with connection.cursor() as cursor:
        returned = execute_values(cursor, sql, records, page_size=500, fetch=True)
    return {str(code) for (code,) in returned}


def _upsert_prepared_records(
    connection: Any,
    config: Mapping[str, Any],
    records: Sequence[tuple[Any, ...]],
) -> int:
    if not records:
        return 0
    _, _, execute_values = _require_driver()
    sql = """
        INSERT INTO {table} ({columns})
        VALUES %s
        ON CONFLICT (safety_code) DO UPDATE SET
            source_id = EXCLUDED.source_id,
            company_name = EXCLUDED.company_name,
            belong_company = EXCLUDED.belong_company,
            safety_type = EXCLUDED.safety_type,
            category = EXCLUDED.category,
            risk_level = EXCLUDED.risk_level,
            theme = EXCLUDED.theme,
            create_by = EXCLUDED.create_by,
            create_time = EXCLUDED.create_time,
            raw_data = EXCLUDED.raw_data,
            update_time = CURRENT_TIMESTAMP
        RETURNING safety_code
    """.format(table=qualified_table(config), columns=_WORK_ORDER_COLUMNS)
    with connection.cursor() as cursor:
        returned = execute_values(cursor, sql, records, page_size=500, fetch=True)
    return len(returned)


def _eligible_rows(rows: Sequence[WorkOrderRow]) -> dict[str, dict[str, Any]]:
    eligible: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("_captureError"):
            continue
        safety_code = str(row.get("safetyCode") or "").strip()
        if safety_code and safety_code not in eligible:
            eligible[safety_code] = dict(row)
    return eligible


class PostgresWorkOrderRepository:
    """Connection-bound work-order repository used inside an explicit transaction."""

    def __init__(self, connection: Any, config: Mapping[str, Any]) -> None:
        self.connection = connection
        self.config = config

    def existing_codes(self, safety_codes: Iterable[str]) -> set[str]:
        return existing_safety_codes(self.connection, self.config, safety_codes)

    def upsert_complete(self, rows: Iterable[WorkOrderRow]) -> int:
        return _upsert_prepared_records(
            self.connection,
            self.config,
            prepare_records([dict(row) for row in rows]),
        )

    def created_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        if start > end:
            raise ValueError("start must not be after end")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT source_id, safety_code, company_name, safety_type,
                       theme, create_by, create_time, raw_data
                FROM {table}
                WHERE create_time >= %s AND create_time <= %s
                ORDER BY create_time, safety_code
                """.format(table=qualified_table(self.config)),
                (start, end),
            )
            return [_export_row_from_record(record) for record in cursor.fetchall()]


class PostgresCheckpointRepository:
    def __init__(self, connection: Any, config: Mapping[str, Any]) -> None:
        self.connection = connection
        self.config = config
        self.target_table = migration_target_table(config)

    @staticmethod
    def _stream_name(value: str) -> str:
        stream_name = str(value or "").strip()
        if not stream_name or len(stream_name) > 200:
            raise ValueError("stream_name must contain 1 to 200 characters")
        return stream_name

    def _load(self, stream_name: str, for_update: bool) -> MonitorCheckpoint | None:
        stream = self._stream_name(stream_name)
        lock_clause = " FOR UPDATE" if for_update else ""
        with self.connection.cursor() as cursor:
            cursor.execute(
                ("""
                SELECT last_success_at, revision
                FROM {table}
                WHERE target_table = %s AND stream_name = %s
                """ + lock_clause).format(table=monitor_checkpoint_table(self.config)),
                (self.target_table, stream),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return MonitorCheckpoint(stream, row[0], int(row[1]))

    def load(self, stream_name: str) -> MonitorCheckpoint | None:
        return self._load(stream_name, for_update=False)

    def load_for_update(self, stream_name: str) -> MonitorCheckpoint | None:
        return self._load(stream_name, for_update=True)

    def save(
        self,
        stream_name: str,
        last_success_at: datetime,
        current: MonitorCheckpoint | None,
    ) -> MonitorCheckpoint:
        stream = self._stream_name(stream_name)
        if current and last_success_at < current.last_success_at:
            raise ValueError("monitor checkpoint must not move backwards")
        with self.connection.cursor() as cursor:
            if current is None:
                cursor.execute(
                    """
                    INSERT INTO {table} (
                        target_table, stream_name, last_success_at, revision, updated_at
                    ) VALUES (%s, %s, %s, 1, CURRENT_TIMESTAMP)
                    RETURNING revision
                    """.format(table=monitor_checkpoint_table(self.config)),
                    (self.target_table, stream, last_success_at),
                )
            else:
                cursor.execute(
                    """
                    UPDATE {table}
                    SET last_success_at = %s,
                        revision = revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE target_table = %s
                      AND stream_name = %s
                      AND revision = %s
                    RETURNING revision
                    """.format(table=monitor_checkpoint_table(self.config)),
                    (last_success_at, self.target_table, stream, current.revision),
                )
            saved = cursor.fetchone()
        if not saved:
            raise CheckpointConflictError(f"monitor checkpoint changed concurrently: {stream}")
        return MonitorCheckpoint(stream, last_success_at, int(saved[0]))


class PostgresMonitoringUnitOfWork:
    """Atomically persists a complete monitor window and advances its checkpoint."""

    def __init__(
        self,
        config: Mapping[str, Any],
        connection_factory: ConnectionFactory = connect,
        migration_runner: MigrationRunner = apply_migrations,
    ) -> None:
        self.config = dict(config)
        self.connection_factory = connection_factory
        self.migration_runner = migration_runner

    def read_checkpoint(self, stream: str) -> datetime | None:
        connection = self.connection_factory(self.config)
        try:
            self.migration_runner(connection, self.config)
            checkpoint = PostgresCheckpointRepository(connection, self.config).load(stream)
            connection.commit()
            return checkpoint.last_success_at if checkpoint else None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def persist_complete_window(
        self,
        stream: str,
        records: Iterable[WorkOrderRow],
        expected_checkpoint: datetime | None,
        next_checkpoint: datetime,
    ) -> PersistedWindow:
        row_list = [dict(row) for row in records]
        invalid_rows = [
            row
            for row in row_list
            if row.get("_captureError") or not str(row.get("safetyCode") or "").strip()
        ]
        if invalid_rows:
            raise ValueError("complete monitor window contains incomplete work orders")
        eligible = _eligible_rows(row_list)
        eligible_rows = list(eligible.values())
        connection = self.connection_factory(self.config)
        try:
            # Migrations have their own committed transaction; the domain write starts after it.
            self.migration_runner(connection, self.config)
            checkpoints = PostgresCheckpointRepository(connection, self.config)
            current = checkpoints.load_for_update(stream)
            persisted_time = current.last_success_at if current else None
            if persisted_time != expected_checkpoint:
                raise CheckpointConflictError(
                    f"monitor checkpoint does not match expected value: {stream}"
                )

            records = prepare_records(eligible_rows)
            inserted_codes = _insert_new_records(connection, self.config, records)
            upserted_count = _upsert_prepared_records(connection, self.config, records)
            if inserted_codes:
                enqueue_feishu_notifications(
                    connection,
                    self.config,
                    [eligible[code] for code in inserted_codes],
                )
            checkpoint = checkpoints.save(stream, next_checkpoint, current)
            connection.commit()
            return PersistedWindow(
                inserted_codes=frozenset(inserted_codes),
                upserted_count=upserted_count,
                checkpoint=checkpoint.last_success_at,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class PostgresOutboxRepository:
    def __init__(
        self,
        config: Mapping[str, Any],
        connection_factory: ConnectionFactory = connect,
        migration_runner: MigrationRunner = apply_migrations,
    ) -> None:
        self.config = dict(config)
        self.connection_factory = connection_factory
        self.migration_runner = migration_runner

    def _connection(self) -> Any:
        connection = self.connection_factory(self.config)
        try:
            self.migration_runner(connection, self.config)
            return connection
        except Exception:
            connection.rollback()
            connection.close()
            raise

    @staticmethod
    def _notification(row: Mapping[str, Any]) -> OutboxNotification:
        payload = {key: value for key, value in row.items() if not key.startswith("_feishu")}
        return OutboxNotification(
            safety_code=str(row.get("safetyCode") or ""),
            payload=payload,
            card_sent=bool(row.get("_feishuCardSent")),
            sent_image_paths=tuple(str(value) for value in row.get("_feishuSentImagePaths", [])),
            attempts=int(row.get("_feishuAttempts") or 0),
            lease_owner=str(row.get("_feishuLeaseOwner") or ""),
            lease_until=row.get("_feishuLeaseUntil")
            if isinstance(row.get("_feishuLeaseUntil"), datetime)
            else None,
        )

    def claim_due(
        self,
        lease_owner: str,
        limit: int = 50,
        lease_duration: timedelta = timedelta(minutes=2),
        now: datetime | None = None,
    ) -> Sequence[OutboxNotification]:
        connection = self._connection()
        try:
            rows = claim_feishu_notifications(
                connection,
                self.config,
                lease_owner,
                limit=limit,
                lease_seconds=lease_duration.total_seconds(),
                now=now,
                commit=False,
            )
            connection.commit()
            return [self._notification(row) for row in rows]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _update(self, operation: Callable[[Any], None]) -> None:
        connection = self._connection()
        try:
            operation(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_card_sent(self, safety_code: str, lease_owner: str) -> None:
        self._update(
            lambda connection: mark_feishu_notification_card_sent(
                connection, self.config, safety_code, lease_owner
            )
        )

    def mark_image_sent(self, safety_code: str, saved_path: str, lease_owner: str) -> None:
        self._update(
            lambda connection: mark_feishu_notification_image_sent(
                connection, self.config, safety_code, saved_path, lease_owner
            )
        )

    def mark_sent(self, safety_code: str, lease_owner: str) -> None:
        self._update(
            lambda connection: mark_feishu_notification_sent(
                connection, self.config, safety_code, lease_owner
            )
        )

    def record_failure(
        self,
        safety_code: str,
        error: object,
        lease_owner: str,
        error_kind: str | None = None,
    ) -> None:
        self._update(
            lambda connection: record_feishu_notification_failure(
                connection,
                self.config,
                safety_code,
                error,
                error_kind=error_kind,
                lease_owner=lease_owner,
            )
        )
