"""Postgres-backed domain connection base class using asyncpg.

Each domain gets its own connection pool scoped to its schema via
``search_path``, so the existing unqualified table names (``catalog``,
``queue_shadow``, ``job_runs``, ``otps`` etc.) resolve to the correct
``<schema>.<table>`` without rewriting every query to be schema-qualified.
"""

import asyncio
import logging
import re
import time
from datetime import datetime

import asyncpg

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\?")


def parse_dt(value: str | datetime | None) -> datetime | None:
    """Convert an ISO-8601 string to a ``datetime`` for asyncpg's ``timestamptz`` codec.

    asyncpg requires an actual ``datetime`` object for timestamptz parameters (unlike
    aiosqlite, which stores/binds them as plain text) — this normalizes the ISO strings
    produced by existing call sites across the app. Already-``datetime`` values and
    ``None`` pass through unchanged.
    """
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def to_pg_sql(sql: str) -> str:
    """Convert SQLite-style ``?`` positional placeholders to asyncpg ``$n``."""
    counter = 0

    def _repl(_match: re.Match) -> str:
        nonlocal counter
        counter += 1
        return f"${counter}"

    return _PLACEHOLDER_RE.sub(_repl, sql)


class _PgResult:
    """Minimal aiosqlite-cursor-compatible result wrapper for asyncpg command tags."""

    def __init__(self, status: str, lastrowid: int | None = None):
        self.rowcount = self._parse_rowcount(status)
        self.lastrowid = lastrowid

    @staticmethod
    def _parse_rowcount(status: str) -> int:
        parts = status.split()
        if not parts:
            return 0
        try:
            return int(parts[-1])
        except (ValueError, IndexError):
            return 0


class _PgDomainDB:
    """Independent asyncpg connection pool for a single Postgres schema/domain."""

    def __init__(self, dsn: str, schema: str, domain_name: str):
        self._dsn = dsn
        self._schema = schema
        self._domain_name = domain_name
        self._pool: asyncpg.Pool | None = None
        self._lock_retries = 0

    @property
    def domain_name(self) -> str:
        return self._domain_name

    @property
    def is_connected(self) -> bool:
        return self._pool is not None

    async def connect(self):
        schema = self._schema

        # NOTE: asyncpg's pool resets session-level `SET` state (including
        # search_path) whenever a connection is released back to the pool, so an
        # `init=` callback that runs `SET search_path` only sticks for the very
        # first query on each connection. `server_settings` sets it as a startup
        # parameter instead, which the pool's reset does NOT clear.
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=2,
            max_size=10,
            server_settings={"search_path": f"{schema},public"},
        )
        logger.debug(
            f"Connected Postgres domain '{self._domain_name}' (schema={schema})"
        )

    async def close(self):
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def run_migrations(self):
        """No-op: Postgres schema is applied out-of-band via sql/001_initial_schema.sql."""
        return

    async def _execute(self, sql: str, params: list | tuple | None = None) -> _PgResult:
        pg_sql = to_pg_sql(sql)
        start = time.monotonic()
        try:
            async with self._pool.acquire() as conn:
                status = await conn.execute(pg_sql, *(params or []))
            return _PgResult(status)
        except (asyncpg.exceptions.DeadlockDetectedError, asyncio.TimeoutError) as exc:
            self._lock_retries += 1
            logger.warning(
                f"[{self._domain_name}] Postgres lock contention: {exc} "
                f"(retries={self._lock_retries}, elapsed={time.monotonic() - start:.3f}s)"
            )
            raise

    async def _execute_returning_id(
        self, sql: str, params: list | tuple | None = None
    ) -> int:
        """Execute an INSERT ending in ``RETURNING id`` and return the new id."""
        pg_sql = to_pg_sql(sql)
        async with self._pool.acquire() as conn:
            return await conn.fetchval(pg_sql, *(params or []))

    async def _executemany(self, sql: str, params_list: list[tuple]) -> None:
        pg_sql = to_pg_sql(sql)
        async with self._pool.acquire() as conn:
            await conn.executemany(pg_sql, params_list)

    async def _fetch_one(
        self, sql: str, params: list | tuple | None = None
    ) -> dict | None:
        pg_sql = to_pg_sql(sql)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(pg_sql, *(params or []))
            return dict(row) if row else None

    async def _fetch_all(
        self, sql: str, params: list | tuple | None = None
    ) -> list[dict]:
        pg_sql = to_pg_sql(sql)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(pg_sql, *(params or []))
            return [dict(r) for r in rows]

    async def _fetch_val(self, sql: str, params: list | tuple | None = None):
        pg_sql = to_pg_sql(sql)
        async with self._pool.acquire() as conn:
            return await conn.fetchval(pg_sql, *(params or []))
