"""Jobs domain database connection for jobs.sqlite3."""

import logging
from datetime import datetime, timezone
from ._base_domain import _DomainDB
from ._fetch_queue import _FetchQueueMixin
from .schemas.jobs_schema import JOBS_MIGRATIONS

logger = logging.getLogger(__name__)


class _JobsDB(_FetchQueueMixin, _DomainDB):
    """Jobs domain database handling background jobs, run logs, schedules, and fetch queue."""

    def __init__(self, db_path: str):
        super().__init__(db_path, JOBS_MIGRATIONS, domain_name="jobs")

    async def start_job_run(
        self, job_name: str, triggered_by: str | None = None, params: str | None = None
    ) -> int:
        cursor = await self._db.execute(
            "INSERT INTO job_runs (job_name, started_at, status, triggered_by, params) "
            "VALUES (?, ?, 'running', ?, ?)",
            [job_name, datetime.now(timezone.utc).isoformat(), triggered_by, params],
        )
        await self._db.commit()
        return cursor.lastrowid

    async def finish_job_run(self, run_id: int, status: str, detail: str | None = None):
        await self._execute(
            "UPDATE job_runs SET ended_at=?, status=?, detail=? WHERE id=?",
            [datetime.now(timezone.utc).isoformat(), status, detail, run_id],
        )

    async def update_job_run_detail(self, run_id: int, detail: str | None):
        """Update only a running job's detail column (used for live progress)."""
        await self._execute("UPDATE job_runs SET detail=? WHERE id=?", [detail, run_id])

    async def add_job_run_logs(
        self, run_id: int, records: list[tuple[str, str, str, str]]
    ) -> None:
        """Bulk-append captured log lines for a run."""
        if not records:
            return
        rows = [
            (run_id, seq, logged_at, level, logger, message)
            for seq, (logged_at, level, logger, message) in enumerate(records)
        ]
        await self._db.executemany(
            "INSERT INTO job_run_logs (run_id, seq, logged_at, level, logger, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        await self._db.commit()

    async def get_job_run_logs(self, run_id: int) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM job_run_logs WHERE run_id=? ORDER BY seq ASC", [run_id]
        )

    async def get_job_run(self, run_id: int) -> dict | None:
        return await self._fetch_one("SELECT * FROM job_runs WHERE id=?", [run_id])

    async def get_job_runs(
        self, job_name: str | None = None, limit: int = 10
    ) -> list[dict]:
        if job_name:
            return await self._fetch_all(
                "SELECT * FROM job_runs WHERE job_name=? ORDER BY id DESC LIMIT ?",
                [job_name, limit],
            )
        return await self._fetch_all(
            "SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", [limit]
        )

    async def reconcile_orphaned_job_runs(self) -> int:
        """Mark any job run still flagged 'running' as 'interrupted'."""
        cursor = await self._db.execute(
            "UPDATE job_runs SET status='interrupted', "
            "ended_at = COALESCE(ended_at, ?) WHERE status='running'",
            [datetime.now(timezone.utc).isoformat()],
        )
        await self._db.commit()
        return cursor.rowcount or 0

    # --- Job schedules ---

    async def get_job_schedules(self) -> list[dict]:
        return await self._fetch_all("SELECT * FROM job_schedules ORDER BY job_name")

    async def get_job_schedule(self, job_name: str) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM job_schedules WHERE job_name=?", [job_name]
        )

    async def upsert_job_schedule(
        self,
        job_name: str,
        cron_expression: str,
        *,
        label: str | None = None,
        params_json: str | None = None,
        is_active: bool = True,
        created_by: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO job_schedules
                (job_name, label, cron_expression, params_json, is_active, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_name) DO UPDATE SET
                label            = excluded.label,
                cron_expression  = excluded.cron_expression,
                params_json      = excluded.params_json,
                is_active        = excluded.is_active,
                updated_at       = datetime('now')
            """,
            [job_name, label, cron_expression, params_json, int(is_active), created_by],
        )
        await self._db.commit()

    async def delete_job_schedule(self, job_name: str) -> None:
        await self._execute("DELETE FROM job_schedules WHERE job_name=?", [job_name])
