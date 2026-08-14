class _WatchlistMixin:
    """Per-user watchlist ("My List") DB methods."""

    async def watchlist_add(self, username: str, token: str) -> bool:
        """Insert item; returns True if added, False if already present."""
        cursor = await self._db.execute(
            "INSERT OR IGNORE INTO user_watchlist (username, friendly_token) VALUES (?, ?)",
            [username, token],
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def watchlist_remove(self, username: str, token: str) -> bool:
        """Remove item; returns True if it existed."""
        cursor = await self._db.execute(
            "DELETE FROM user_watchlist WHERE username = ? AND friendly_token = ?",
            [username, token],
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def watchlist_tokens(self, username: str) -> list[str]:
        """All friendly_tokens in the user's watchlist, newest-added first."""
        rows = await self._fetch_all(
            "SELECT friendly_token FROM user_watchlist WHERE username = ? ORDER BY added_at DESC, id DESC",
            [username],
        )
        return [r["friendly_token"] for r in rows]

    async def watchlist_get(
        self, username: str, *, page: int = 1, per_page: int = 24
    ) -> list[dict]:
        """Catalog rows for the user's watchlist, newest-added first."""
        sql = """
            SELECT c.friendly_token, c.title, c.duration_sec, c.cover_art_path,
                   c.cover_art_source, c.thumbnail_url, c.manifest_url
            FROM user_watchlist wl
            JOIN catalog c ON c.friendly_token = wl.friendly_token
            WHERE wl.username = ?
            ORDER BY wl.added_at DESC, wl.id DESC
            LIMIT ? OFFSET ?
        """
        return await self._fetch_all(sql, [username, per_page, (page - 1) * per_page])

    async def watchlist_count(self, username: str) -> int:
        row = await self._fetch_one(
            "SELECT COUNT(*) as cnt FROM user_watchlist WHERE username = ?", [username]
        )
        return row["cnt"] if row else 0
