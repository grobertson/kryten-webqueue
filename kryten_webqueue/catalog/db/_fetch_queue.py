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
    ) -> None:
        await self._db.execute(
            """
            UPDATE fetch_queue
            SET status = ?, finished_at = datetime('now'), result_json = ?, error = ?
            WHERE id = ?
            """,
            [status, result_json, error, item_id],
        )
        await self._db.commit()

    async def get_fetch_queue(
        self, *, limit: int = 100, status: str | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM fetch_queue"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY added_at DESC LIMIT ?"
        params.append(limit)
        return await self._fetch_all(sql, params)

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
