class _FeedbackMixin:
    """Viewer feedback and title suggestion methods."""

    # --- Feedback ---

    async def add_feedback(self, *, username: str, body: str) -> int:
        cursor = await self._db.execute(
            "INSERT INTO feedback (username, body) VALUES (?, ?)",
            [username, body],
        )
        await self._db.commit()
        return cursor.lastrowid

    async def list_feedback(
        self, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        sql = "SELECT * FROM feedback"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return await self._fetch_all(sql, params)

    async def count_feedback(self, *, status: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM feedback"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        row = await self._fetch_one(sql, params)
        return int(row["c"]) if row else 0

    async def set_feedback_status(self, feedback_id: int, status: str) -> bool:
        cursor = await self._db.execute(
            "UPDATE feedback SET status = ? WHERE id = ?", [status, feedback_id]
        )
        await self._db.commit()
        return (cursor.rowcount or 0) > 0

    async def delete_feedback(self, feedback_id: int) -> bool:
        cursor = await self._db.execute(
            "DELETE FROM feedback WHERE id = ?", [feedback_id]
        )
        await self._db.commit()
        return (cursor.rowcount or 0) > 0

    # --- Title suggestions ---

    async def add_title_suggestion(
        self,
        *,
        username: str,
        query: str,
        resolved_title: str | None = None,
        resolved_year: str | None = None,
        resolved_source: str | None = None,
        resolved_id: str | None = None,
        poster_url: str | None = None,
        resolution: str = "unresolved",
        catalog_token: str | None = None,
    ) -> int:
        cursor = await self._db.execute(
            "INSERT INTO title_suggestions "
            "(username, query, resolved_title, resolved_year, resolved_source, "
            " resolved_id, poster_url, resolution, catalog_token) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                username,
                query,
                resolved_title,
                resolved_year,
                resolved_source,
                resolved_id,
                poster_url,
                resolution,
                catalog_token,
            ],
        )
        await self._db.commit()
        return cursor.lastrowid

    async def list_title_suggestions(
        self, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        sql = "SELECT * FROM title_suggestions"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return await self._fetch_all(sql, params)

    async def count_title_suggestions(self, *, status: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM title_suggestions"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        row = await self._fetch_one(sql, params)
        return int(row["c"]) if row else 0

    async def set_title_suggestion_status(
        self, suggestion_id: int, status: str
    ) -> bool:
        cursor = await self._db.execute(
            "UPDATE title_suggestions SET status = ? WHERE id = ?",
            [status, suggestion_id],
        )
        await self._db.commit()
        return (cursor.rowcount or 0) > 0

    async def delete_title_suggestion(self, suggestion_id: int) -> bool:
        cursor = await self._db.execute(
            "DELETE FROM title_suggestions WHERE id = ?", [suggestion_id]
        )
        await self._db.commit()
        return (cursor.rowcount or 0) > 0
