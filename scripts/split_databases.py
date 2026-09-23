"""ETL script to split a monolithic webqueue.db into partitioned domain databases:
- catalog.sqlite3
- queue.sqlite3
- jobs.sqlite3
- users.sqlite3
"""

import argparse
import asyncio
import hashlib
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from kryten_webqueue.catalog.db.schemas import (
    CATALOG_MIGRATIONS,
    QUEUE_MIGRATIONS,
    JOBS_MIGRATIONS,
    USERS_MIGRATIONS,
)
from kryten_webqueue.catalog.db._base_domain import _DomainDB

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("split_databases")

DOMAIN_TABLE_MAP = {
    "catalog": [
        "catalog",
        "categories",
        "catalog_categories",
        "tags",
        "catalog_tags",
        "people",
        "catalog_people",
        "studios",
        "catalog_studios",
        "item_enrichment_state",
        "item_edit_log",
        "sync_log",
        "motd_overrides",
    ],
    "queue": [
        "queue_shadow",
        "spend_requests",
        "queue_history",
        "saved_playlists",
        "saved_playlist_items",
        "playlist_schedules",
        "active_schedule",
        "play_completions",
        "playlist_item_played",
        "catalog_blackouts",
    ],
    "jobs": [
        "job_runs",
        "job_run_logs",
        "job_schedules",
        "fetch_queue",
    ],
    "users": [
        "otps",
        "device_link_codes",
        "device_api_keys",
        "user_watchlist",
        "feedback",
        "title_suggestions",
    ],
}

DOMAIN_MIGRATIONS = {
    "catalog": CATALOG_MIGRATIONS,
    "queue": QUEUE_MIGRATIONS,
    "jobs": JOBS_MIGRATIONS,
    "users": USERS_MIGRATIONS,
}


def compute_table_hash(
    conn: sqlite3.Connection, table_name: str, columns: list[str]
) -> tuple[int, str]:
    """Compute row count and deterministic hash for a table ordered by rowid/pk."""
    cursor = conn.cursor()
    col_str = ", ".join(f'"{c}"' for c in columns)
    try:
        cursor.execute(f'SELECT COUNT(*) FROM "{table_name}"')
        row_count = cursor.fetchone()[0]
        if row_count == 0:
            return 0, "empty"

        cursor.execute(f'SELECT {col_str} FROM "{table_name}" ORDER BY rowid ASC')
        hasher = hashlib.sha256()
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                row_repr = "|".join("" if v is None else str(v) for v in row)
                hasher.update(row_repr.encode("utf-8"))
        return row_count, hasher.hexdigest()
    except sqlite3.OperationalError:
        return 0, "missing"


async def initialize_domain_db(
    db_path: Path, migrations: list[str], domain_name: str
) -> None:
    """Initialize a domain database and run its baseline migrations."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    domain_db = _DomainDB(str(db_path), migrations, domain_name=domain_name)
    await domain_db.connect()
    try:
        await domain_db.run_migrations()
    finally:
        await domain_db.close()


def copy_table_data(
    source_conn: sqlite3.Connection,
    target_conn: sqlite3.Connection,
    table_name: str,
    chunk_size: int = 1000,
) -> int:
    """Copy all rows from source table to target table."""
    src_cursor = source_conn.cursor()
    tgt_cursor = target_conn.cursor()

    # Get target columns
    tgt_cursor.execute(f'PRAGMA table_info("{table_name}")')
    tgt_cols = [row[1] for row in tgt_cursor.fetchall()]
    if not tgt_cols:
        logger.warning(f"Target table '{table_name}' has no columns; skipping")
        return 0

    # Get source columns that match target
    src_cursor.execute(f'PRAGMA table_info("{table_name}")')
    src_cols_all = {row[1] for row in src_cursor.fetchall()}
    common_cols = [c for c in tgt_cols if c in src_cols_all]

    if not common_cols:
        logger.warning(f"No common columns for '{table_name}'; skipping")
        return 0

    col_names = ", ".join(f'"{c}"' for c in common_cols)
    placeholders = ", ".join("?" * len(common_cols))
    select_sql = f'SELECT {col_names} FROM "{table_name}" ORDER BY rowid ASC'
    insert_sql = f'INSERT INTO "{table_name}" ({col_names}) VALUES ({placeholders})'

    src_cursor.execute(select_sql)
    total_copied = 0
    while True:
        rows = src_cursor.fetchmany(chunk_size)
        if not rows:
            break
        tgt_cursor.executemany(insert_sql, rows)
        total_copied += len(rows)

    target_conn.commit()
    return total_copied


def rebuild_fts(catalog_conn: sqlite3.Connection) -> int:
    """Populate catalog_fts from catalog rows using FTS5 rebuild."""
    cursor = catalog_conn.cursor()
    cursor.execute("INSERT INTO catalog_fts(catalog_fts) VALUES('rebuild')")
    catalog_conn.commit()
    cursor.execute("SELECT COUNT(*) FROM catalog_fts")
    return cursor.fetchone()[0]


async def split_database(
    source_path: Path,
    data_dir: Path,
    dry_run: bool = False,
    verify_only: bool = False,
) -> bool:
    """Main ETL workflow to split source database into partitioned databases."""
    if not source_path.is_file():
        logger.error(f"Source database file does not exist: {source_path}")
        return False

    logger.info(f"Checking source database: {source_path}")
    source_uri = f"file:{source_path.resolve()}?mode=ro"
    src_conn = sqlite3.connect(source_uri, uri=True)

    # Integrity check on source
    cursor = src_conn.cursor()
    cursor.execute("PRAGMA integrity_check")
    res = cursor.fetchone()[0]
    if res != "ok":
        logger.error(f"Source database integrity check failed: {res}")
        src_conn.close()
        return False
    logger.info("Source database integrity check: OK")

    # Discover existing tables in source
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    source_tables = {row[0] for row in cursor.fetchall()}

    target_paths = {
        "catalog": data_dir / "catalog.sqlite3",
        "queue": data_dir / "queue.sqlite3",
        "jobs": data_dir / "jobs.sqlite3",
        "users": data_dir / "users.sqlite3",
    }

    if dry_run:
        logger.info("[DRY RUN] Source database tables found:")
        for domain, tables in DOMAIN_TABLE_MAP.items():
            logger.info(f"--- Domain '{domain}' (-> {target_paths[domain].name}) ---")
            for t in tables:
                if t in source_tables:
                    cursor.execute(f'SELECT COUNT(*) FROM "{t}"')
                    cnt = cursor.fetchone()[0]
                    logger.info(f"  {t}: {cnt} rows")
                else:
                    logger.info(f"  {t}: (not present in source)")
        src_conn.close()
        return True

    if not verify_only:
        data_dir.mkdir(parents=True, exist_ok=True)
        # Step 1: Initialize all target databases with baseline migrations
        for domain, target_path in target_paths.items():
            logger.info(f"Initializing target database: {domain} -> {target_path}")
            await initialize_domain_db(target_path, DOMAIN_MIGRATIONS[domain], domain)

        # Step 2: Copy data per domain
        for domain, tables in DOMAIN_TABLE_MAP.items():
            tgt_path = target_paths[domain]
            tgt_conn = sqlite3.connect(str(tgt_path))
            tgt_conn.execute("PRAGMA foreign_keys=OFF")  # Disable FKs during bulk copy
            try:
                for table in tables:
                    if table in source_tables:
                        copied = copy_table_data(src_conn, tgt_conn, table)
                        logger.info(f"[{domain}] Copied {copied} rows into '{table}'")
                    else:
                        logger.debug(
                            f"[{domain}] Table '{table}' not present in source database; skipping"
                        )
            finally:
                tgt_conn.execute("PRAGMA foreign_keys=ON")
                tgt_conn.close()

        # Step 3: Rebuild FTS5 virtual table in catalog.db
        logger.info("Rebuilding catalog_fts index in catalog.sqlite3...")
        cat_conn = sqlite3.connect(str(target_paths["catalog"]))
        try:
            fts_count = rebuild_fts(cat_conn)
            logger.info(f"catalog_fts populated with {fts_count} entries")
        finally:
            cat_conn.close()

    # Step 4: Verification
    logger.info("Verifying data parity across all tables...")
    parity_errors = 0
    report_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": str(source_path.resolve()),
        "data_dir": str(data_dir.resolve()),
        "tables": {},
    }

    for domain, tables in DOMAIN_TABLE_MAP.items():
        tgt_conn = sqlite3.connect(
            f"file:{target_paths[domain].resolve()}?mode=ro", uri=True
        )
        tgt_cursor = tgt_conn.cursor()

        # Check FK integrity on target
        tgt_cursor.execute("PRAGMA foreign_key_check")
        fk_issues = tgt_cursor.fetchall()
        if fk_issues:
            logger.error(f"[{domain}] Foreign key violations found: {fk_issues}")
            parity_errors += len(fk_issues)
        else:
            logger.info(f"[{domain}] PRAGMA foreign_key_check: OK")

        try:
            for table in tables:
                if table not in source_tables:
                    continue
                tgt_cursor.execute(f'PRAGMA table_info("{table}")')
                common_cols = [r[1] for r in tgt_cursor.fetchall()]

                src_count, src_hash = compute_table_hash(src_conn, table, common_cols)
                tgt_count, tgt_hash = compute_table_hash(tgt_conn, table, common_cols)

                report_data["tables"][table] = {
                    "domain": domain,
                    "source_rows": src_count,
                    "target_rows": tgt_count,
                    "source_hash": src_hash,
                    "target_hash": tgt_hash,
                    "match": (src_count == tgt_count and src_hash == tgt_hash),
                }

                if src_count != tgt_count:
                    logger.error(
                        f"Row count mismatch on '{table}': source={src_count}, target={tgt_count}"
                    )
                    parity_errors += 1
                elif src_hash != tgt_hash:
                    logger.error(f"Checksum hash mismatch on '{table}'! Data differs.")
                    parity_errors += 1
                else:
                    logger.info(
                        f"Verified '{table}': {src_count} rows, checksum matches"
                    )
        finally:
            tgt_conn.close()

    src_conn.close()

    report_path = data_dir / "split_report.json"
    report_data["status"] = "success" if parity_errors == 0 else "failed"
    report_data["errors"] = parity_errors
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)
    logger.info(f"Split report written to: {report_path}")

    if parity_errors > 0:
        logger.error(f"Split verification completed with {parity_errors} error(s)!")
        return False

    logger.info(
        "SUCCESS: All domain databases partitioned and verified with 100% data parity."
    )
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Split monolithic webqueue.db into 4 partitioned SQLite databases"
    )
    parser.add_argument(
        "--source",
        "-s",
        type=Path,
        default=Path("./data/webqueue.db"),
        help="Path to source monolithic SQLite database",
    )
    parser.add_argument(
        "--data-dir",
        "-d",
        type=Path,
        default=Path("./data"),
        help="Destination directory for partitioned databases",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect source database without modifying or creating targets",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Run verification against existing target databases without copying data",
    )
    args = parser.parse_args()

    success = asyncio.run(
        split_database(
            source_path=args.source,
            data_dir=args.data_dir,
            dry_run=args.dry_run,
            verify_only=args.verify_only,
        )
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
