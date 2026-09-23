"""Base domain database connection for partitioned SQLite databases."""

import logging
import sqlite3
import time
from pathlib import Path
import aiosqlite

logger = logging.getLogger(__name__)


class _DomainDB:
    """Independent SQLite connection and migration runner for a specific domain."""

    def __init__(
        self, db_path: str, migrations: list[str], domain_name: str = "domain"
    ):
        self._db_path = db_path
        self._migrations = migrations
        self._domain_name = domain_name
        self._db: aiosqlite.Connection | None = None
        self._lock_retries: int = 0
        self._query_count: int = 0

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def domain_name(self) -> str:
        return self._domain_name

    @property
    def is_connected(self) -> bool:
        return self._db is not None

    async def connect(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=10000")
        await self._db.execute("PRAGMA wal_autocheckpoint=100")
        await self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        logger.debug(
            f"Connected domain database '{self._domain_name}' at {self._db_path}"
        )

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None
            logger.debug(f"Closed domain database '{self._domain_name}'")

    async def run_migrations(self):
        """Apply pending migrations sequentially."""
        if not self._db:
            raise RuntimeError(
                f"Cannot run migrations: domain '{self._domain_name}' is not connected"
            )
        await self._executescript(self._migrations[0])
        row = await self._fetch_one("SELECT MAX(version) as v FROM _migrations")
        current_version = (row["v"] or 0) if row else 0

        for version, sql in enumerate(self._migrations[1:], start=1):
            if version > current_version:
                logger.info(
                    f"Applying migration v{version} to {self._domain_name} ({self._db_path})"
                )
                await self._executescript(sql)
                await self._execute(
                    "INSERT INTO _migrations (version) VALUES (?)", [version]
                )

    async def _execute(self, sql: str, params: list | tuple | None = None):
        if not self._db:
            raise RuntimeError(
                f"Domain database '{self._domain_name}' is not connected"
            )
        start = time.monotonic()
        try:
            cursor = await self._db.execute(sql, params or [])
            await self._db.commit()
            self._query_count += 1
            return cursor
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                self._lock_retries += 1
                logger.warning(
                    f"[{self._domain_name}] SQLite lock contention: {exc} "
                    f"(retries={self._lock_retries}, elapsed={time.monotonic() - start:.3f}s)"
                )
            raise

    async def _executescript(self, sql: str):
        if not self._db:
            raise RuntimeError(
                f"Domain database '{self._domain_name}' is not connected"
            )
        start = time.monotonic()
        try:
            await self._db.executescript(sql)
            await self._db.commit()
            self._query_count += 1
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                self._lock_retries += 1
                logger.warning(
                    f"[{self._domain_name}] SQLite lock contention during script: {exc} "
                    f"(retries={self._lock_retries}, elapsed={time.monotonic() - start:.3f}s)"
                )
            raise

    async def _fetch_one(
        self, sql: str, params: list | tuple | None = None
    ) -> dict | None:
        if not self._db:
            raise RuntimeError(
                f"Domain database '{self._domain_name}' is not connected"
            )
        cursor = await self._db.execute(sql, params or [])
        row = await cursor.fetchone()
        self._query_count += 1
        return dict(row) if row else None

    async def _fetch_all(
        self, sql: str, params: list | tuple | None = None
    ) -> list[dict]:
        if not self._db:
            raise RuntimeError(
                f"Domain database '{self._domain_name}' is not connected"
            )
        cursor = await self._db.execute(sql, params or [])
        rows = await cursor.fetchall()
        self._query_count += 1
        return [dict(r) for r in rows]
