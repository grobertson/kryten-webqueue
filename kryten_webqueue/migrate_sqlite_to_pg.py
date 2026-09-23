"""One-shot ETL migration script from partitioned SQLite databases into PostgreSQL webqueue database."""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import asyncpg
from dateutil import parser as date_parser

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("migrate_sqlite_to_pg")

# Ordered tables per schema (dependency-safe order)
MIGRATION_TABLES = [
    # catalog
    ("catalog", "catalog.sqlite3", "catalog", None),
    ("catalog", "catalog.sqlite3", "categories", "id"),
    ("catalog", "catalog.sqlite3", "catalog_categories", None),
    ("catalog", "catalog.sqlite3", "tags", "id"),
    ("catalog", "catalog.sqlite3", "catalog_tags", None),
    ("catalog", "catalog.sqlite3", "people", "id"),
    ("catalog", "catalog.sqlite3", "catalog_people", None),
    ("catalog", "catalog.sqlite3", "studios", "id"),
    ("catalog", "catalog.sqlite3", "catalog_studios", None),
    ("catalog", "catalog.sqlite3", "item_enrichment_state", None),
    ("catalog", "catalog.sqlite3", "item_edit_log", "id"),
    ("catalog", "catalog.sqlite3", "sync_log", "id"),
    ("catalog", "catalog.sqlite3", "motd_overrides", None),
    # queue
    ("queue", "queue.sqlite3", "saved_playlists", "id"),
    ("queue", "queue.sqlite3", "saved_playlist_items", "id"),
    ("queue", "queue.sqlite3", "playlist_schedules", "id"),
    ("queue", "queue.sqlite3", "active_schedule", None),
    ("queue", "queue.sqlite3", "queue_shadow", None),
    ("queue", "queue.sqlite3", "spend_requests", None),
    ("queue", "queue.sqlite3", "queue_history", "id"),
    ("queue", "queue.sqlite3", "play_completions", "id"),
    ("queue", "queue.sqlite3", "playlist_item_played", None),
    ("queue", "queue.sqlite3", "catalog_blackouts", None),
    # jobs
    ("jobs", "jobs.sqlite3", "job_runs", "id"),
    ("jobs", "jobs.sqlite3", "job_run_logs", "id"),
    ("jobs", "jobs.sqlite3", "job_schedules", "id"),
    ("jobs", "jobs.sqlite3", "fetch_queue", "id"),
    # users
    ("users", "users.sqlite3", "otps", None),
    ("users", "users.sqlite3", "device_link_codes", None),
    ("users", "users.sqlite3", "device_api_keys", "id"),
    ("users", "users.sqlite3", "user_watchlist", "id"),
    ("users", "users.sqlite3", "feedback", "id"),
    ("users", "users.sqlite3", "title_suggestions", "id"),
]


def convert_val(val: Any, pg_type: str) -> Any:
    """Convert SQLite value to appropriate Python type for asyncpg/Postgres."""
    if val is None:
        return None
    pg_t = pg_type.lower()
    if "bool" in pg_t:
        return bool(val)
    if "timestamp" in pg_t or "timestamptz" in pg_t:
        if isinstance(val, (int, float)):
            return datetime.fromtimestamp(val, tz=timezone.utc)
        if isinstance(val, str):
            val_clean = val.strip()
            if not val_clean:
                return None
            try:
                dt = date_parser.parse(val_clean)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                return None
        return val
    if "json" in pg_t or "jsonb" in pg_t:
        if isinstance(val, str):
            try:
                json.loads(val)
                return val
            except Exception:
                return json.dumps(val)
        return json.dumps(val)
    return val


async def copy_table_to_pg(
    sqlite_conn: sqlite3.Connection,
    pg_conn: asyncpg.Connection,
    schema: str,
    table: str,
    identity_col: str | None,
    chunk_size: int = 1000,
) -> int:
    """Stream rows from SQLite table into Postgres schema.table."""
    target_table = f"{schema}.{table}"
    # Fetch PostgreSQL column names and data types
    pg_cols_info = await pg_conn.fetch(
        """
        SELECT column_name, data_type, is_generated
        FROM information_schema.columns
        WHERE table_schema = $1 AND table_name = $2
        ORDER BY ordinal_position
        """,
        schema,
        table,
    )
    if not pg_cols_info:
        logger.warning(f"PostgreSQL target {target_table} does not exist; skipping")
        return 0

    # Filter out generated stored columns like search_vector
    insertable_pg_cols = {
        r["column_name"]: r["data_type"]
        for r in pg_cols_info
        if r.get("is_generated") != "ALWAYS"
    }

    # Fetch SQLite column names
    src_cursor = sqlite_conn.cursor()
    src_cursor.execute(f'PRAGMA table_info("{table}")')
    src_cols = [r[1] for r in src_cursor.fetchall()]

    common_cols = [c for c in src_cols if c in insertable_pg_cols]
    if not common_cols:
        logger.warning(f"No common columns for {target_table}; skipping")
        return 0

    col_list_pg = ", ".join(f'"{c}"' for c in common_cols)
    placeholders = ", ".join(f"${i+1}" for i in range(len(common_cols)))

    overriding_clause = " OVERRIDING SYSTEM VALUE" if identity_col else ""
    insert_sql = f"INSERT INTO {target_table} ({col_list_pg}){overriding_clause} VALUES ({placeholders})"

    select_sql = f'SELECT {", ".join(f"{c}" for c in common_cols)} FROM "{table}" ORDER BY rowid ASC'
    src_cursor.execute(select_sql)

    total_copied = 0
    while True:
        rows = src_cursor.fetchmany(chunk_size)
        if not rows:
            break

        converted_batch = []
        for row in rows:
            converted_row = [
                convert_val(val, insertable_pg_cols[col])
                for col, val in zip(common_cols, row)
            ]
            converted_batch.append(converted_row)

        await pg_conn.executemany(insert_sql, converted_batch)
        total_copied += len(rows)

    # Reset identity sequence if applicable
    if identity_col:
        seq_reset_sql = f"""
            SELECT setval(
                pg_get_serial_sequence('{schema}.{table}', '{identity_col}'),
                coalesce(max({identity_col}), 1),
                max({identity_col}) IS NOT NULL
            )
            FROM {target_table};
        """
        try:
            await pg_conn.execute(seq_reset_sql)
        except Exception as e:
            logger.warning(
                f"Could not reset sequence for {target_table}.{identity_col}: {e}"
            )

    return total_copied


async def verify_table_counts(
    sqlite_conn: sqlite3.Connection,
    pg_conn: asyncpg.Connection,
    schema: str,
    table: str,
) -> tuple[int, int, bool]:
    """Verify that row count matches between SQLite and Postgres."""
    src_cursor = sqlite_conn.cursor()
    src_cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
    src_count = src_cursor.fetchone()[0]

    pg_count = await pg_conn.fetchval(f"SELECT COUNT(*) FROM {schema}.{table}")
    return src_count, pg_count, (src_count == pg_count)


async def migrate_all(
    data_dir: Path,
    pg_dsn: str,
    dry_run: bool = False,
    verify_only: bool = False,
) -> bool:
    """Migrate all domain tables from data_dir to PostgreSQL."""
    logger.info(
        f"Connecting to PostgreSQL: {pg_dsn.split('@')[-1] if '@' in pg_dsn else pg_dsn}"
    )
    pg_conn = await asyncpg.connect(pg_dsn)
    try:
        report: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data_dir": str(data_dir.resolve()),
            "tables": {},
            "errors": 0,
        }

        # Check existing data in destination
        if not verify_only and not dry_run:
            total_dest_rows = await pg_conn.fetchval(
                "SELECT count(*) FROM catalog.catalog"
            )
            if total_dest_rows > 0:
                logger.info(
                    "Target database already contains catalog rows; truncating target tables before ETL..."
                )
                # Truncate tables with CASCADE
                for schema, _, table, _ in reversed(MIGRATION_TABLES):
                    await pg_conn.execute(f"TRUNCATE TABLE {schema}.{table} CASCADE")

        for schema, db_filename, table, id_col in MIGRATION_TABLES:
            db_path = data_dir / db_filename
            if not db_path.is_file():
                logger.warning(
                    f"SQLite file '{db_filename}' not found in {data_dir}; skipping {schema}.{table}"
                )
                continue

            src_conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
            try:
                # Check if table exists in source
                c = src_conn.cursor()
                c.execute(
                    f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
                )
                if not c.fetchone():
                    logger.debug(
                        f"Table '{table}' not present in {db_filename}; skipping"
                    )
                    continue

                if dry_run:
                    c.execute(f'SELECT COUNT(*) FROM "{table}"')
                    cnt = c.fetchone()[0]
                    logger.info(
                        f"[DRY RUN] {schema}.{table} from {db_filename}: {cnt} rows"
                    )
                    continue

                if not verify_only:
                    logger.info(f"Streaming {table} -> {schema}.{table}...")
                    copied = await copy_table_to_pg(
                        src_conn, pg_conn, schema, table, id_col
                    )
                    logger.info(f"Copied {copied} rows into {schema}.{table}")

                # Verification
                src_cnt, pg_cnt, match = await verify_table_counts(
                    src_conn, pg_conn, schema, table
                )
                report["tables"][f"{schema}.{table}"] = {
                    "source_rows": src_cnt,
                    "postgres_rows": pg_cnt,
                    "match": match,
                }
                if not match:
                    logger.error(
                        f"Mismatch on {schema}.{table}: source={src_cnt}, pg={pg_cnt}"
                    )
                    report["errors"] += 1
                else:
                    logger.info(f"Verified {schema}.{table}: {pg_cnt} rows match")

            finally:
                src_conn.close()

        report_file = data_dir / "postgres_migration_report.json"
        report["status"] = "success" if report["errors"] == 0 else "failed"
        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        logger.info(f"Migration report saved to: {report_file}")

        if report["errors"] > 0:
            logger.error(f"ETL completed with {report['errors']} verification errors!")
            return False

        logger.info(
            "SUCCESS: All domain databases migrated to PostgreSQL with 100% row count parity."
        )
        return True

    finally:
        await pg_conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="ETL migration from partitioned SQLite to PostgreSQL"
    )
    parser.add_argument(
        "--data-dir",
        "-d",
        type=Path,
        default=Path("./data"),
        help="Path to directory containing partitioned SQLite databases",
    )
    parser.add_argument(
        "--pg-dsn",
        type=str,
        default=os.getenv(
            "KRYTEN_WEBQUEUE_PG_DSN",
            "postgresql://kryten:kryten_secret_password@host.containers.internal:5432/webqueue",
        ),
        help="PostgreSQL connection DSN",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preflight check without modifying PostgreSQL",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Run verification on existing data without copying",
    )
    args = parser.parse_args()

    success = asyncio.run(
        migrate_all(
            data_dir=args.data_dir,
            pg_dsn=args.pg_dsn,
            dry_run=args.dry_run,
            verify_only=args.verify_only,
        )
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
