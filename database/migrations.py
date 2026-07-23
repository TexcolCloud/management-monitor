from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MIGRATION_FILE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

# Migration 001 was changed twice before the immutability rule was enforced.
# These are the only historical checksums known to have been shipped.
KNOWN_CHECKSUM_ALIASES: Mapping[int, frozenset[str]] = {
    1: frozenset(
        {
            "89417bba4487f40eb8e0c7d6bc933151ae9fb52bb178d98cb54cc4472ef3e676",
            "f8e4bb6a62be918429805cc2134eff7b04b5cfdb5a9d6333699a3305bafcf016",
        }
    )
}
LEGACY_TABLE_RECONCILIATION_VERSION = 6


def quote_identifier(value: str) -> str:
    if not value or not value.replace("_", "").isalnum() or value[0].isdigit():
        raise ValueError(f"Invalid PostgreSQL identifier: {value!r}")
    return f'"{value}"'


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    sql: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_FILE.fullmatch(path.name)
        if not match:
            raise ValueError(f"Invalid migration filename: {path.name}")
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=int(match.group(1)),
                name=path.name,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                sql=sql,
            )
        )
    versions = [item.version for item in migrations]
    if versions != sorted(set(versions)):
        raise ValueError("Database migration versions must be unique")
    return migrations


def checksum_is_compatible(migration: Migration, applied_checksum: str) -> bool:
    return applied_checksum == migration.checksum or applied_checksum in KNOWN_CHECKSUM_ALIASES.get(
        migration.version,
        frozenset(),
    )


def apply_migrations(connection, config: Mapping[str, Any]) -> list[int]:
    schema_name = str(config.get("schema", "public"))
    table_name = str(config.get("table", "work_orders"))
    schema = quote_identifier(schema_name)
    table = f"{schema}.{quote_identifier(table_name)}"
    migration_table = f'{schema}."workorder_schema_migrations"'
    target_table = f"{schema_name}.{table_name}"
    context = {
        "schema": schema,
        "table": table,
        "unique_constraint": quote_identifier(f"uk_{table_name}_safety_code"),
        "create_time_index": quote_identifier(f"idx_{table_name}_create_time"),
    }

    migrations = load_migrations()
    applied_now: list[int] = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                ("workorder_schema_migrations", schema_name),
            )
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {migration_table} (
                    target_table VARCHAR(300) NOT NULL,
                    version INTEGER NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    checksum CHAR(64) NOT NULL,
                    applied_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (target_table, version)
                )
                """
            )
            cursor.execute(
                f"SELECT version, checksum FROM {migration_table} WHERE target_table = %s",
                (target_table,),
            )
            applied = {int(version): str(checksum).strip() for version, checksum in cursor.fetchall()}

            # Validate the complete ledger before making any schema change.
            for migration in migrations:
                previous_checksum = applied.get(migration.version)
                if previous_checksum:
                    if not checksum_is_compatible(migration, previous_checksum):
                        raise RuntimeError(
                            f"Migration checksum changed after application: {migration.name}"
                        )

            # A pre-migration table may lack create_time, which migration 004 indexes.
            # Run the additive reconciliation first, then record version 006 in order.
            preapplied: set[int] = set()
            reconciliation = next(
                (
                    migration
                    for migration in migrations
                    if migration.version == LEGACY_TABLE_RECONCILIATION_VERSION
                ),
                None,
            )
            if reconciliation and reconciliation.version not in applied:
                cursor.execute("SELECT to_regclass(%s)", (table,))
                existing_table = cursor.fetchone()
                if existing_table and existing_table[0] is not None:
                    cursor.execute(reconciliation.sql.format_map(context))
                    preapplied.add(reconciliation.version)

            for migration in migrations:
                previous_checksum = applied.get(migration.version)
                if previous_checksum:
                    continue
                if migration.version not in preapplied:
                    cursor.execute(migration.sql.format_map(context))
                cursor.execute(
                    f"""
                    INSERT INTO {migration_table} (target_table, version, name, checksum)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (target_table, migration.version, migration.name, migration.checksum),
                )
                applied_now.append(migration.version)
        connection.commit()
        return applied_now
    except Exception:
        connection.rollback()
        raise
