"""Postgres-backed jobs domain: job runs, run logs, schedules, and fetch queue."""

from datetime import datetime, timedelta, timezone

from ._pg_base_domain import _PgDomainDB, parse_dt


class _PgJobsDB(_PgDomainDB):
    """Postgres implementation of the jobs domain (schema ``jobs``)."""

    # --- Job runs ---

    async def start_job_run(
        self, job_name: str, triggered_by: str | None = None, params: str | None = None
    ) -> int:
        return await self._execute_returning_id(
            "INSERT INTO job_runs (job_name, started_at, status, triggered_by, params) "
            "VALUES (?, ?, 'running', ?, ?) RETURNING id",
            [job_name, datetime.now(timezone.utc), triggered_by, params],
        )

    async def finish_job_run(self, run_id: int, status: str, detail: str | None = None):
        await self._execute(
            "UPDATE job_runs SET ended_at=?, status=?, detail=? WHERE id=?",
            [datetime.now(timezone.utc), status, detail, run_id],
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
            (run_id, seq, parse_dt(logged_at), level, logger, message)
            for seq, (logged_at, level, logger, message) in enumerate(records)
        ]
        await self._executemany(
            "INSERT INTO job_run_logs (run_id, seq, logged_at, level, logger, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

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
        result = await self._execute(
            "UPDATE job_runs SET status='interrupted', "
            "ended_at = COALESCE(ended_at, ?) WHERE status='running'",
            [datetime.now(timezone.utc)],
        )
        return result.rowcount or 0

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
        await self._execute(
            """
            INSERT INTO job_schedules
                (job_name, label, cron_expression, params_json, is_active, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_name) DO UPDATE SET
                label            = excluded.label,
                cron_expression  = excluded.cron_expression,
                params_json      = excluded.params_json,
                is_active        = excluded.is_active,
                updated_at       = now()
            """,
            [
                job_name,
                label,
                cron_expression,
                params_json,
                bool(is_active),
                created_by,
            ],
        )

    async def delete_job_schedule(self, job_name: str) -> None:
        await self._execute("DELETE FROM job_schedules WHERE job_name=?", [job_name])

    async def prune_job_run_logs(
        self, retention_days: int = 30, batch_size: int = 5000
    ) -> int:
        """Prune job_run_logs older than retention_days in bounded batches.

        Strictly touches ONLY job_run_logs.
        """
        total_deleted = 0
        cutoff = timedelta(days=-int(retention_days))
        while True:
            result = await self._execute(
                """
                DELETE FROM job_run_logs
                WHERE id IN (
                    SELECT id FROM job_run_logs
                    WHERE logged_at < (now() + (?)::interval)
                    LIMIT ?
                )
                """,
                [cutoff, batch_size],
            )
            count = result.rowcount or 0
            total_deleted += count
            if count < batch_size:
                break
        return total_deleted

    # --- Fetch queue ---

    async def enqueue_fetch(
        self,
        *,
        url: str,
        quality: str = "medium",
        max_videos: int = 50,
        add_to_playlist: int | None = None,
        added_by: str | None = None,
    ) -> int:
        return await self._execute_returning_id(
            """
            INSERT INTO fetch_queue (url, quality, max_videos, add_to_playlist, added_by)
            VALUES (?, ?, ?, ?, ?) RETURNING id
            """,
            [url, quality, max_videos, add_to_playlist, added_by],
        )

    async def claim_next_fetch_item(self) -> dict | None:
        """Atomically claim the oldest pending item (sets status to running)."""
        row = await self._fetch_one(
            "SELECT * FROM fetch_queue WHERE status = 'pending' ORDER BY added_at ASC LIMIT 1"
        )
        if not row:
            return None
        result = await self._execute(
            "UPDATE fetch_queue SET status = 'running', started_at = now()"
            " WHERE id = ? AND status = 'pending'",
            [row["id"]],
        )
        if (result.rowcount or 0) == 0:
            return None  # lost the claim (shouldn't happen; drain is single-flight)
        row = dict(row)
        row["status"] = "running"
        return row

    async def finish_fetch_item(
        self,
        item_id: int,
        *,
        status: str,
        result_json: str | None = None,
        error: str | None = None,
        bump_attempts: bool = False,
    ) -> None:
        attempts_sql = ", attempts = attempts + 1" if bump_attempts else ""
        await self._execute(
            f"""
            UPDATE fetch_queue
            SET status = ?, finished_at = now(), result_json = ?, error = ?{attempts_sql}
            WHERE id = ?
            """,
            [status, result_json, error, item_id],
        )

    async def requeue_fetch_item(self, item_id: int) -> None:
        """Return a single item to 'pending' (e.g. interrupted by shutdown).

        Clears the run timestamps and any prior error so the drain re-attempts it
        cleanly on the next pass.
        """
        await self._execute(
            "UPDATE fetch_queue"
            " SET status = 'pending', started_at = NULL, finished_at = NULL, error = NULL"
            " WHERE id = ?",
            [item_id],
        )

    async def requeue_fetch_item_for_retry(
        self, item_id: int, *, error: str | None = None
    ) -> int:
        """Re-queue an item to the *back* of the queue for another upload attempt.

        Increments ``attempts``, sets ``added_at`` strictly after every existing
        row so it sorts last (retry after everything else currently pending), and
        clears the run timestamps. Keeps ``error`` as a breadcrumb of why it was
        re-queued. Returns the new ``attempts`` count.
        """
        await self._execute(
            "UPDATE fetch_queue"
            " SET status = 'pending', attempts = attempts + 1,"
            " added_at = (SELECT MAX(added_at) + interval '1 second' FROM fetch_queue),"
            " started_at = NULL, finished_at = NULL, error = ?"
            " WHERE id = ?",
            [error, item_id],
        )
        row = await self._fetch_one(
            "SELECT attempts FROM fetch_queue WHERE id = ?", [item_id]
        )
        return int(row["attempts"]) if row else 0

    async def reset_running_fetch_items(self) -> int:
        """Reset any 'running' items to 'pending'; return how many were reset.

        Called at startup to recover from a crash or hard kill that left the
        in-flight item marked 'running' forever (the running flag is process
        state, not durable). Flipping it back to 'pending' lets the drain
        re-attempt it — a fresh download, though a yt-dlp ``.part`` file left in
        the work dir may let yt-dlp resume where it left off.
        """
        result = await self._execute(
            "UPDATE fetch_queue SET status = 'pending', started_at = NULL"
            " WHERE status = 'running'"
        )
        return result.rowcount or 0

    async def get_fetch_queue(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        hide_expired_done: bool = False,
    ) -> list[dict]:
        sql = "SELECT * FROM fetch_queue"
        where, params = self._fetch_queue_filter(status, hide_expired_done)
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY added_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return await self._fetch_all(sql, params)

    async def count_fetch_queue(
        self, *, status: str | None = None, hide_expired_done: bool = False
    ) -> int:
        """Count rows matching the same visibility filter as ``get_fetch_queue``."""
        sql = "SELECT COUNT(*) AS c FROM fetch_queue"
        where, params = self._fetch_queue_filter(status, hide_expired_done)
        if where:
            sql += " WHERE " + where
        row = await self._fetch_one(sql, params)
        return int(row["c"]) if row else 0

    @staticmethod
    def _fetch_queue_filter(
        status: str | None, hide_expired_done: bool
    ) -> tuple[str, list]:
        """Build the shared WHERE clause for fetch-queue list/count queries.

        When ``hide_expired_done`` is set, successful downloads finished more than
        24h ago are excluded from view (rows are retained for audit — this is a
        visibility filter only). Failed / pending / running items are never
        hidden regardless of age.
        """
        clauses: list[str] = []
        params: list = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if hide_expired_done:
            clauses.append(
                "NOT (status = 'done' AND finished_at IS NOT NULL "
                "AND finished_at < (now() - interval '24 hours'))"
            )
        return " AND ".join(clauses), params

    async def count_fetch_queue_pending(self) -> int:
        row = await self._fetch_one(
            "SELECT COUNT(*) AS c FROM fetch_queue WHERE status = 'pending'"
        )
        return int(row["c"]) if row else 0

    async def delete_fetch_queue_item(self, item_id: int) -> bool:
        """Remove a non-running item. Returns False if not found or currently running."""
        result = await self._execute(
            "DELETE FROM fetch_queue WHERE id = ? AND status != 'running'",
            [item_id],
        )
        return (result.rowcount or 0) > 0
