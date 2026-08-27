class _FetchQueueMixin:
    """Persistent download queue for the fetch_queue_add / fetch_queue_drain jobs."""

    async def enqueue_fetch(
        self,
        *,
        url: str,
        quality: str = "medium",
        max_videos: int = 50,
        add_to_playlist: int | None = None,
        added_by: str | None = None,
    ) -> int:
        cursor = await self._db.execute(
            """
            INSERT INTO fetch_queue (url, quality, max_videos, add_to_playlist, added_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            [url, quality, max_videos, add_to_playlist, added_by],
        )
        await self._db.commit()
        return cursor.lastrowid

    async def claim_next_fetch_item(self) -> dict | None:
        """Atomically claim the oldest pending item (sets status to running)."""
        row = await self._fetch_one(
            "SELECT * FROM fetch_queue WHERE status = 'pending' ORDER BY added_at ASC LIMIT 1"
        )
        if not row:
            return None
        cursor = await self._db.execute(
            "UPDATE fetch_queue SET status = 'running', started_at = datetime('now')"
            " WHERE id = ? AND status = 'pending'",
            [row["id"]],
        )
        await self._db.commit()
        if (cursor.rowcount or 0) == 0:
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
        await self._db.execute(
            f"""
            UPDATE fetch_queue
            SET status = ?, finished_at = datetime('now'), result_json = ?, error = ?{attempts_sql}
            WHERE id = ?
            """,
            [status, result_json, error, item_id],
        )
        await self._db.commit()

    async def requeue_fetch_item(self, item_id: int) -> None:
        """Return a single item to 'pending' (e.g. interrupted by shutdown).

        Clears the run timestamps and any prior error so the drain re-attempts it
        cleanly on the next pass.
        """
        await self._db.execute(
            "UPDATE fetch_queue"
            " SET status = 'pending', started_at = NULL, finished_at = NULL, error = NULL"
            " WHERE id = ?",
            [item_id],
        )
        await self._db.commit()

    async def requeue_fetch_item_for_retry(
        self, item_id: int, *, error: str | None = None
    ) -> int:
        """Re-queue an item to the *back* of the queue for another upload attempt.

        Increments ``attempts``, sets ``added_at`` strictly after every existing
        row so it sorts last (retry after everything else currently pending), and
        clears the run timestamps. Keeps ``error`` as a breadcrumb of why it was
        re-queued. Returns the new ``attempts`` count.
        """
        await self._db.execute(
            "UPDATE fetch_queue"
            " SET status = 'pending', attempts = attempts + 1,"
            " added_at = (SELECT datetime(MAX(added_at), '+1 second') FROM fetch_queue),"
            " started_at = NULL, finished_at = NULL, error = ?"
            " WHERE id = ?",
            [error, item_id],
        )
        await self._db.commit()
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
        cursor = await self._db.execute(
            "UPDATE fetch_queue SET status = 'pending', started_at = NULL"
            " WHERE status = 'running'"
        )
        await self._db.commit()
        return cursor.rowcount or 0

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
                "AND finished_at < datetime('now', '-24 hours'))"
            )
        return " AND ".join(clauses), params

    async def count_fetch_queue_pending(self) -> int:
        row = await self._fetch_one(
            "SELECT COUNT(*) AS c FROM fetch_queue WHERE status = 'pending'"
        )
        return int(row["c"]) if row else 0

    async def delete_fetch_queue_item(self, item_id: int) -> bool:
        """Remove a non-running item. Returns False if not found or currently running."""
        cursor = await self._db.execute(
            "DELETE FROM fetch_queue WHERE id = ? AND status != 'running'",
            [item_id],
        )
        await self._db.commit()
        return (cursor.rowcount or 0) > 0
