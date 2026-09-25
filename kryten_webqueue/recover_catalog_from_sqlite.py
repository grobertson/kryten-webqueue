"""Restore only Webqueue's catalog domain from a monolithic SQLite backup.

This is intentionally separate from the full SQLite-to-PostgreSQL migration:
queue, jobs, and users are live operational domains and must not be overwritten
while recovering a catalog that was accidentally pruned.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

import asyncpg

from .config import Config
from .migrate_sqlite_to_pg import copy_table_to_pg, verify_table_counts

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("recover_catalog_from_sqlite")


# Dependency-safe order.  ``sync_log`` remains in Postgres so the incident
# audit trail is preserved instead of being replaced by historical SQLite rows.
CATALOG_TABLES = [
    ("catalog", None),
    ("categories", "id"),
    ("catalog_categories", None),
    ("tags", "id"),
    ("catalog_tags", None),
    ("people", "id"),
    ("catalog_people", None),
    ("studios", "id"),
    ("catalog_studios", None),
    ("item_enrichment_state", None),
    ("item_edit_log", "id"),
    ("motd_overrides", None),
]


async def recover(sqlite_path: Path, config_path: Path, *, dry_run: bool) -> bool:
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"SQLite backup not found: {sqlite_path}")

    config = Config.from_file(config_path)
    pg_conn = await asyncpg.connect(config.database.postgres.get_asyncpg_dsn())
    # The copied backup may retain SQLite journal metadata.  It has already
    # been integrity-checked, so immutable mode prevents SQLite from trying to
    # perform journal recovery or create side files during a read-only restore.
    sqlite_conn = sqlite3.connect(
        f"file:{sqlite_path.resolve()}?mode=ro&immutable=1", uri=True
    )
    try:
        source_count = sqlite_conn.execute("SELECT count(*) FROM catalog").fetchone()[0]
        if source_count == 0:
            raise RuntimeError("Refusing recovery from an empty SQLite catalog")
        logger.info("SQLite recovery source contains %d catalog rows", source_count)

        if dry_run:
            logger.info("Dry run complete; PostgreSQL was not modified")
            return True

        # The target catalog may be partially populated from a failed sync.
        # CASCADE is limited to catalog-domain dependents; it cannot touch the
        # queue/jobs/users schemas because those tables have no FK to catalog.
        await pg_conn.execute(
            "TRUNCATE TABLE "
            "catalog.catalog, catalog.categories, catalog.catalog_categories, "
            "catalog.tags, catalog.catalog_tags, catalog.people, "
            "catalog.catalog_people, catalog.studios, catalog.catalog_studios, "
            "catalog.item_enrichment_state, catalog.item_edit_log, "
            "catalog.motd_overrides CASCADE"
        )

        for table, identity_col in CATALOG_TABLES:
            exists = sqlite_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                logger.warning("Source table %s is absent; skipping", table)
                continue
            copied = await copy_table_to_pg(
                sqlite_conn, pg_conn, "catalog", table, identity_col
            )
            source_rows, target_rows, matches = await verify_table_counts(
                sqlite_conn, pg_conn, "catalog", table
            )
            if not matches:
                raise RuntimeError(
                    f"Count mismatch for catalog.{table}: "
                    f"source={source_rows}, target={target_rows}"
                )
            logger.info("Restored catalog.%s (%d rows)", table, copied)

        restored = await pg_conn.fetchval("SELECT count(*) FROM catalog.catalog")
        if restored != source_count:
            raise RuntimeError(
                f"Catalog count mismatch: source={source_count}, target={restored}"
            )
        logger.info("Catalog recovery complete: %d rows", restored)
        return True
    finally:
        sqlite_conn.close()
        await pg_conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore catalog domain from SQLite")
    parser.add_argument("--sqlite-db", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, default=Path("/etc/kryten-webqueue/config.json")
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    success = asyncio.run(recover(args.sqlite_db, args.config, dry_run=args.dry_run))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
