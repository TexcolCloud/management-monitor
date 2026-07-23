from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from common.dataset_io import load_rows
from common.environment import load_environment
from common.redaction import REDACTED, diagnostic_error
from common.workflow_paths import capture_data_complete, project_path
from data_processing.classifier import DailyManagementClassifier
from database.migrations import apply_migrations, quote_identifier

LOGGER = logging.getLogger("workorder_daily_manage")

DEFAULT_CONFIG_PATH = Path("config/database.json")
DEFAULT_TABLE = "work_orders"
DEFAULT_ENV_PATH = Path(".env")

_URL_CREDENTIALS = re.compile(r"(?i)(://[^\s/:@]+:)[^\s/@]+(@)")


def load_env_file(path: Path = DEFAULT_ENV_PATH) -> None:
    """Load local settings without overriding real environment variables."""
    env_path = project_path(path).resolve()
    load_environment(env_path)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    load_env_file()
    config_path = project_path(path).resolve()
    config: Dict[str, Any] = {}
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))

    pg = dict(config.get("postgresql") or config)
    env_map = {
        "host": "PGHOST",
        "port": "PGPORT",
        "database": "PGDATABASE",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "schema": "PGSCHEMA",
        "table": "PGTABLE",
        "connect_timeout": "PGCONNECT_TIMEOUT",
        "statement_timeout_ms": "PGSTATEMENT_TIMEOUT_MS",
        "lock_timeout_ms": "PGLOCK_TIMEOUT_MS",
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name)
        if value not in (None, ""):
            pg[key] = value

    enabled_env = os.environ.get("PG_ENABLED")
    if enabled_env is not None:
        pg["enabled"] = enabled_env.strip().lower() in {"1", "true", "yes", "on"}

    pg.setdefault("enabled", False)
    pg.setdefault("host", "127.0.0.1")
    pg.setdefault("port", 5432)
    pg.setdefault("schema", "public")
    pg.setdefault("table", DEFAULT_TABLE)
    pg.setdefault("connect_timeout", 10)
    return pg


def is_enabled(config: Mapping[str, Any]) -> bool:
    return bool(config.get("enabled"))


def _require_driver():
    try:
        import psycopg2
        from psycopg2.extras import Json, execute_values
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL 入库已启用，但缺少 psycopg2。请执行：pip install psycopg2-binary"
        ) from exc
    return psycopg2, Json, execute_values


def _optional_timeout_ms(config: Mapping[str, Any], key: str) -> Optional[int]:
    value = config.get(key)
    if value in (None, ""):
        return None
    try:
        timeout_ms = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"PostgreSQL {key} 必须为非负整数毫秒") from exc
    if timeout_ms < 0:
        raise RuntimeError(f"PostgreSQL {key} 必须为非负整数毫秒")
    return timeout_ms


def connect(config: Mapping[str, Any]):
    psycopg2, _, _ = _require_driver()
    required = ["host", "database", "user", "password"]
    missing = [key for key in required if not str(config.get(key, "")).strip()]
    if missing:
        raise RuntimeError("PostgreSQL 配置缺少字段：" + ", ".join(missing))
    statement_timeout_ms = _optional_timeout_ms(config, "statement_timeout_ms")
    lock_timeout_ms = _optional_timeout_ms(config, "lock_timeout_ms")
    options = []
    if statement_timeout_ms is not None:
        options.append(f"-c statement_timeout={statement_timeout_ms}")
    if lock_timeout_ms is not None:
        options.append(f"-c lock_timeout={lock_timeout_ms}")
    connection_options = {
        "host": config["host"],
        "port": int(config.get("port", 5432)),
        "dbname": config["database"],
        "user": config["user"],
        "password": config["password"],
        "connect_timeout": int(config.get("connect_timeout", 10)),
        "application_name": "management-monitorDailyManage",
    }
    if options:
        connection_options["options"] = " ".join(options)
    return psycopg2.connect(
        **connection_options,
    )


def qualified_table(config: Mapping[str, Any]) -> str:
    return "%s.%s" % (
        quote_identifier(str(config.get("schema", "public"))),
        quote_identifier(str(config.get("table", DEFAULT_TABLE))),
    )


def feishu_outbox_table(config: Mapping[str, Any]) -> str:
    return "%s.%s" % (
        quote_identifier(str(config.get("schema", "public"))),
        quote_identifier("workorder_feishu_notification_outbox"),
    )


def monitor_checkpoint_table(config: Mapping[str, Any]) -> str:
    return "%s.%s" % (
        quote_identifier(str(config.get("schema", "public"))),
        quote_identifier("workorder_monitor_checkpoints"),
    )


def migration_target_table(config: Mapping[str, Any]) -> str:
    return "%s.%s" % (
        str(config.get("schema", "public")),
        str(config.get("table", DEFAULT_TABLE)),
    )


def ensure_table(connection, config: Mapping[str, Any]) -> None:
    apply_migrations(connection, config)


def database_has_records(config_path: Path = DEFAULT_CONFIG_PATH) -> bool:
    """Return whether the configured daily-management table already has data."""
    config = load_config(config_path)
    if not is_enabled(config):
        raise RuntimeError("PostgreSQL 入库未启用，无法判断是否需要抓取数据")
    connection = connect(config)
    try:
        ensure_table(connection, config)
        with connection.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM %s LIMIT 1)" % qualified_table(config))
            row = cursor.fetchone()
        return bool(row and row[0])
    finally:
        connection.close()


def rows_by_create_time(
    start: datetime,
    end: datetime,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> List[Dict[str, Any]]:
    """Return stored work orders whose create_time is in the inclusive range."""
    if start > end:
        raise ValueError("开始时间不能晚于结束时间。")
    config = load_config(config_path)
    if not is_enabled(config):
        raise RuntimeError("PostgreSQL 入库未启用，无法导出 Excel。")
    connection = connect(config)
    try:
        ensure_table(connection, config)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT source_id, safety_code, company_name, safety_type,
                       theme, create_by, create_time, raw_data
                FROM {table}
                WHERE create_time >= %s
                  AND create_time <= %s
                ORDER BY create_time, safety_code
                """.format(table=qualified_table(config)),
                (start, end),
            )
            return [_export_row_from_record(record) for record in cursor.fetchall()]
    finally:
        connection.close()


def _export_row_from_record(record: Sequence[Any]) -> Dict[str, Any]:
    # Retain compatibility with callers/tests that supplied the former one-column result.
    if len(record) == 1:
        payload = record[0]
        return dict(payload) if isinstance(payload, Mapping) else {}

    source_id, safety_code, company_name, safety_type, theme, create_by, create_time, payload = record
    if isinstance(payload, Mapping):
        return dict(payload)
    if isinstance(create_time, datetime):
        create_time_text = create_time.isoformat(sep=" ")
    else:
        create_time_text = str(create_time or "")
    return {
        "id": str(source_id or ""),
        "safetyCode": str(safety_code or ""),
        "companyName": str(company_name or ""),
        "safetyType": str(safety_type or ""),
        "theme": str(theme or ""),
        "createBy": str(create_by or ""),
        "createTime": create_time_text,
    }


def data_dir_records_complete(
    data_dir: Path,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> bool:
    """Return whether every successfully captured work order is complete in PostgreSQL."""
    if not capture_data_complete(data_dir) or not (data_dir / "management-api-details.json").is_file():
        return False
    complete_rows, failed_rows = load_detail_batch(data_dir)
    if failed_rows:
        return False
    codes = {str(row.get("safetyCode") or "").strip() for row in complete_rows}
    if "" in codes:
        return False
    if not codes:
        return True

    config = load_config(config_path)
    if not is_enabled(config):
        raise RuntimeError("PostgreSQL 入库未启用，无法校验本次完整快照")
    connection = connect(config)
    try:
        ensure_table(connection, config)
        existing = existing_safety_codes(
            connection,
            config,
            codes,
            complete_only=True,
        )
        return codes.issubset(existing)
    finally:
        connection.close()


def chunked(values: Sequence[str], size: int = 1000) -> Iterable[Sequence[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def existing_safety_codes(
    connection,
    config: Mapping[str, Any],
    safety_codes: Iterable[str],
    complete_only: bool = False,
) -> Set[str]:
    unique_codes = sorted({str(code).strip() for code in safety_codes if str(code).strip()})
    if not unique_codes:
        return set()

    table = qualified_table(config)
    found: Set[str] = set()
    completeness_clause = " AND NOT (raw_data ? '_captureError')" if complete_only else ""
    with connection.cursor() as cursor:
        for code_group in chunked(unique_codes):
            cursor.execute(
                (
                    "SELECT safety_code FROM %s WHERE safety_code = ANY(%%s)%s"
                    % (table, completeness_clause)
                ),
                (list(code_group),),
            )
            found.update(row[0] for row in cursor.fetchall())
    return found


def parse_create_time(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def is_complete_row(row: Mapping[str, Any]) -> bool:
    return not bool(row.get("_captureError"))


def load_detail_batch(data_dir: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    rows = load_rows(data_dir / "management-api-details.json")
    complete_rows = [row for row in rows if is_complete_row(row)]
    failed_rows = [row for row in rows if not is_complete_row(row)]
    failures_path = data_dir / "management-api-detail-failures.json"
    if failures_path.exists():
        failed_rows.extend(load_rows(failures_path))
    return complete_rows, failed_rows


def prepare_records(rows: Iterable[Dict[str, Any]]) -> List[Tuple[Any, ...]]:
    _, Json, _ = _require_driver()
    classifier = DailyManagementClassifier(project_path("config/classification_rules.json"))
    records: List[Tuple[Any, ...]] = []
    seen: Set[str] = set()
    for row in rows:
        if not is_complete_row(row):
            continue
        safety_code = str(row.get("safetyCode") or "").strip()
        if not safety_code or safety_code in seen:
            continue
        seen.add(safety_code)
        classification = classifier.classify(row)
        records.append(
            (
                str(row.get("id") or "").strip() or None,
                safety_code,
                str(row.get("companyName") or "").strip() or None,
                classification.get("归属单位") or None,
                str(row.get("safetyType") or "").strip() or None,
                classification.get("归类") or None,
                classification.get("风险等级") or None,
                str(row.get("theme") or "").strip() or None,
                str(row.get("createBy") or "").strip() or None,
                parse_create_time(row.get("createTime")),
                Json(row),
            )
        )
    return records


def upsert_rows(
    connection,
    config: Mapping[str, Any],
    rows: Iterable[Dict[str, Any]],
    commit: bool = True,
) -> int:
    _, _, execute_values = _require_driver()
    records = prepare_records(rows)
    if not records:
        return 0
    table = qualified_table(config)
    sql = """
        INSERT INTO {table} (
            source_id, safety_code, company_name, belong_company,
            safety_type, category, risk_level, theme, create_by,
            create_time, raw_data
        ) VALUES %s
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
    """.format(table=table)
    with connection.cursor() as cursor:
        returned = execute_values(cursor, sql, records, page_size=500, fetch=True)
    if commit:
        connection.commit()
    return len(returned)


def enqueue_feishu_notifications(
    connection,
    config: Mapping[str, Any],
    rows: Iterable[Dict[str, Any]],
) -> int:
    _, Json, execute_values = _require_driver()
    target_table = migration_target_table(config)
    records = [
        (target_table, safety_code, Json(row))
        for row in rows
        if (safety_code := str(row.get("safetyCode") or "").strip())
    ]
    if not records:
        return 0
    sql = """
        INSERT INTO {table} (target_table, safety_code, payload)
        VALUES %s
        ON CONFLICT (target_table, safety_code) DO NOTHING
    """.format(table=feishu_outbox_table(config))
    with connection.cursor() as cursor:
        execute_values(cursor, sql, records, page_size=500)
        return cursor.rowcount


def _notification_from_record(record: Sequence[Any]) -> Dict[str, Any]:
    safety_code, payload, card_sent_at, sent_image_paths, *progress = record
    attempts = progress[0] if len(progress) > 0 else 0
    lease_owner = progress[1] if len(progress) > 1 else None
    lease_until = progress[2] if len(progress) > 2 else None
    notification = dict(payload) if isinstance(payload, Mapping) else {}
    # The relational key is authoritative even if a stale/corrupt payload disagrees.
    notification.update(
        {
            "safetyCode": str(safety_code),
            "_feishuCardSent": card_sent_at is not None,
            "_feishuSentImagePaths": sent_image_paths
            if isinstance(sent_image_paths, list)
            else [],
        }
    )
    if progress:
        notification.update(
            {
                "_feishuAttempts": int(attempts or 0),
                "_feishuLeaseOwner": str(lease_owner or ""),
                "_feishuLeaseUntil": lease_until,
            }
        )
    return notification


def pending_feishu_notifications(
    connection,
    config: Mapping[str, Any],
    limit: int = 50,
) -> List[Dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT safety_code, payload, card_sent_at, sent_image_paths
            FROM {table}
            WHERE target_table = %s
              AND sent_at IS NULL
              AND next_attempt_at <= CURRENT_TIMESTAMP
              AND (lease_until IS NULL OR lease_until <= CURRENT_TIMESTAMP)
            ORDER BY next_attempt_at, created_at, safety_code
            LIMIT %s
            """.format(table=feishu_outbox_table(config)),
            (migration_target_table(config), limit),
        )
        return [_notification_from_record(record) for record in cursor.fetchall()]


def claim_feishu_notifications(
    connection,
    config: Mapping[str, Any],
    lease_owner: str,
    limit: int = 50,
    lease_seconds: float = 120.0,
    now: Optional[datetime] = None,
    commit: bool = True,
) -> List[Dict[str, Any]]:
    owner = str(lease_owner or "").strip()
    if not owner:
        raise ValueError("lease_owner must not be empty")
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be greater than zero")
    claimed_at = now or datetime.now()
    lease_until = claimed_at + timedelta(seconds=lease_seconds)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidates AS (
                    SELECT target_table, safety_code
                    FROM {table}
                    WHERE target_table = %s
                      AND sent_at IS NULL
                      AND next_attempt_at <= %s
                      AND (lease_until IS NULL OR lease_until <= %s)
                    ORDER BY next_attempt_at, created_at, safety_code
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE {table} AS outbox
                SET lease_owner = %s,
                    lease_until = %s,
                    updated_at = CURRENT_TIMESTAMP
                FROM candidates
                WHERE outbox.target_table = candidates.target_table
                  AND outbox.safety_code = candidates.safety_code
                RETURNING outbox.safety_code, outbox.payload, outbox.card_sent_at,
                          outbox.sent_image_paths, outbox.attempts,
                          outbox.lease_owner, outbox.lease_until
                """.format(table=feishu_outbox_table(config)),
                (
                    migration_target_table(config),
                    claimed_at,
                    claimed_at,
                    limit,
                    owner,
                    lease_until,
                ),
            )
            claimed = [_notification_from_record(record) for record in cursor.fetchall()]
        if commit:
            connection.commit()
        return claimed
    except Exception:
        if commit:
            connection.rollback()
        raise


def _require_outbox_lease_update(cursor, safety_code: str, lease_owner: Optional[str]) -> None:
    rowcount = getattr(cursor, "rowcount", None)
    if lease_owner is not None and isinstance(rowcount, int) and rowcount != 1:
        raise RuntimeError(f"Feishu outbox lease was lost before progress update: {safety_code}")


def mark_feishu_notification_sent(
    connection,
    config: Mapping[str, Any],
    safety_code: str,
    lease_owner: Optional[str] = None,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE {table}
            SET sent_at = CURRENT_TIMESTAMP,
                attempts = attempts + 1,
                last_error = NULL,
                error_kind = NULL,
                lease_owner = NULL,
                lease_until = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE target_table = %s AND safety_code = %s
              AND (%s IS NULL OR lease_owner = %s)
            """.format(table=feishu_outbox_table(config)),
            (migration_target_table(config), safety_code, lease_owner, lease_owner),
        )
        _require_outbox_lease_update(cursor, safety_code, lease_owner)


def mark_feishu_notification_card_sent(
    connection,
    config: Mapping[str, Any],
    safety_code: str,
    lease_owner: Optional[str] = None,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE {table}
            SET card_sent_at = COALESCE(card_sent_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            WHERE target_table = %s AND safety_code = %s
              AND (%s IS NULL OR lease_owner = %s)
            """.format(table=feishu_outbox_table(config)),
            (migration_target_table(config), safety_code, lease_owner, lease_owner),
        )
        _require_outbox_lease_update(cursor, safety_code, lease_owner)


def mark_feishu_notification_image_sent(
    connection,
    config: Mapping[str, Any],
    safety_code: str,
    saved_path: str,
    lease_owner: Optional[str] = None,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE {table}
            SET sent_image_paths = CASE
                    WHEN sent_image_paths ? %s THEN sent_image_paths
                    ELSE sent_image_paths || jsonb_build_array(%s::text)
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE target_table = %s AND safety_code = %s
              AND (%s IS NULL OR lease_owner = %s)
            """.format(table=feishu_outbox_table(config)),
            (
                saved_path,
                saved_path,
                migration_target_table(config),
                safety_code,
                lease_owner,
                lease_owner,
            ),
        )
        _require_outbox_lease_update(cursor, safety_code, lease_owner)


def sanitize_error(error: object, limit: int = 2000) -> str:
    message = diagnostic_error(str(error or "").replace("\x00", ""))
    message = _URL_CREDENTIALS.sub(rf"\1{REDACTED}\2", message)
    return message[: max(0, limit)]


def classify_delivery_error(error: object) -> str:
    message = str(error or "").casefold()
    if "timeout" in message or "超时" in message:
        return "timeout"
    if any(marker in message for marker in ("429", "rate limit", "限流")):
        return "rate_limited"
    if any(marker in message for marker in ("401", "403", "unauthorized", "forbidden", "凭证")):
        return "authentication"
    if any(marker in message for marker in ("connection", "network", "temporar", "网络", "连接")):
        return "transient"
    return "external"


def record_feishu_notification_failure(
    connection,
    config: Mapping[str, Any],
    safety_code: str,
    error: object,
    error_kind: Optional[str] = None,
    lease_owner: Optional[str] = None,
    base_delay_seconds: float = 60.0,
    max_delay_seconds: float = 3600.0,
) -> None:
    if base_delay_seconds <= 0 or max_delay_seconds < base_delay_seconds:
        raise ValueError("invalid Feishu retry delay configuration")
    safe_error = sanitize_error(error)
    requested_kind = str(error_kind or classify_delivery_error(error)).casefold()
    kind = re.sub(r"[^a-z0-9_.-]+", "_", requested_kind).strip("_")[:100] or "external"
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE {table}
            SET attempts = attempts + 1,
                last_error = %s,
                error_kind = %s,
                next_attempt_at = CURRENT_TIMESTAMP + (
                    LEAST(
                        %s::double precision,
                        %s::double precision * POWER(2, LEAST(attempts, 16))
                    ) * INTERVAL '1 second'
                ),
                lease_owner = NULL,
                lease_until = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE target_table = %s AND safety_code = %s
              AND (%s IS NULL OR lease_owner = %s)
            """.format(table=feishu_outbox_table(config)),
            (
                safe_error,
                kind,
                max_delay_seconds,
                base_delay_seconds,
                migration_target_table(config),
                safety_code,
                lease_owner,
                lease_owner,
            ),
        )
        _require_outbox_lease_update(cursor, safety_code, lease_owner)


def insert_new_rows(connection, config: Mapping[str, Any], rows: Iterable[Dict[str, Any]]) -> int:
    """Backward-compatible alias; writes are now upserts."""
    return upsert_rows(connection, config, rows)


def write_existing_codes_file(data_dir: Path, config_path: Path = DEFAULT_CONFIG_PATH) -> Path:
    config = load_config(config_path)
    output_path = data_dir / "database-existing-safety-codes.json"
    if not is_enabled(config):
        output_path.write_text(
            json.dumps({"enabled": False, "codes": []}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return output_path

    rows, _ = load_detail_batch(data_dir)
    codes = [row.get("safetyCode") for row in rows]
    connection = connect(config)
    try:
        ensure_table(connection, config)
        existing = sorted(
            existing_safety_codes(connection, config, codes, complete_only=True)
        )
    finally:
        connection.close()

    output_path.write_text(
        json.dumps(
            {"enabled": True, "count": len(existing), "codes": existing},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    LOGGER.info("数据库已有单据清单已生成: %s 条", len(existing))
    return output_path


def import_data_dir(data_dir: Path, config_path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, int]:
    config = load_config(config_path)
    if not is_enabled(config):
        LOGGER.info("PostgreSQL 入库未启用，跳过数据库写入")
        return {"captured": 0, "existing": 0, "inserted": 0}

    complete_rows, failed_rows = load_detail_batch(data_dir)
    valid_rows = [row for row in complete_rows if str(row.get("safetyCode") or "").strip()]
    codes = [str(row.get("safetyCode") or "").strip() for row in valid_rows]
    connection = connect(config)
    try:
        ensure_table(connection, config)
        existing = existing_safety_codes(connection, config, codes)
        upserted = upsert_rows(connection, config, valid_rows)
    finally:
        connection.close()

    inserted = sum(1 for code in set(codes) if code not in existing)
    updated = max(0, upserted - inserted)
    result = {
        "captured": len(complete_rows) + len(failed_rows),
        "failed": len(failed_rows),
        "missing_code": len(complete_rows) - len(valid_rows),
        "existing": len(existing),
        "inserted": inserted,
        "updated": updated,
    }
    LOGGER.info(
        "数据库写入完成: 抓取 %s 条，失败隔离 %s 条，新增 %s 条，更新 %s 条",
        result["captured"],
        result["failed"],
        result["inserted"],
        result["updated"],
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将日常管理详情数据去重写入 PostgreSQL。")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--write-existing-codes", action="store_true")
    return parser.parse_args()


def main() -> None:
    from common.logging_utils import setup_logging

    setup_logging()
    args = parse_args()
    data_dir = project_path(args.data_dir).resolve()
    if args.write_existing_codes:
        path = write_existing_codes_file(data_dir, args.config)
        LOGGER.info("数据库已有单据清单: %s", path)
    else:
        import_data_dir(data_dir, args.config)


if __name__ == "__main__":
    main()
