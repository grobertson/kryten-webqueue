class _MOTDMixin:
    """Per-week MOTD slot override CRUD (see migration v30).

    An override pins one position of the weekend poster grid for one workbook
    week. Any of ``title`` / ``poster_url`` / ``href`` may be NULL, in which
    case the auto-resolved value for that field is kept.
    """

    async def upsert_motd_override(
        self,
        week_key: str,
        slot_key: str,
        *,
        title: str | None = None,
        poster_url: str | None = None,
        href: str | None = None,
        created_by: str | None = None,
    ) -> None:
        """Insert or update one slot override, merging with any existing row."""
        await self._db.execute(
            """
            INSERT INTO motd_overrides
                (week_key, slot_key, title, poster_url, href, created_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(week_key, slot_key) DO UPDATE SET
                title      = COALESCE(excluded.title, motd_overrides.title),
                poster_url = COALESCE(excluded.poster_url, motd_overrides.poster_url),
                href       = COALESCE(excluded.href, motd_overrides.href),
                created_by = excluded.created_by,
                updated_at = CURRENT_TIMESTAMP
            """,
            [week_key, slot_key, title, poster_url, href, created_by],
        )
        await self._db.commit()

    async def list_motd_overrides(self, week_key: str) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM motd_overrides WHERE week_key = ? ORDER BY slot_key",
            [week_key],
        )

    async def delete_motd_override(self, week_key: str, slot_key: str) -> int:
        cursor = await self._db.execute(
            "DELETE FROM motd_overrides WHERE week_key = ? AND slot_key = ?",
            [week_key, slot_key],
        )
        await self._db.commit()
        return cursor.rowcount or 0

    async def clear_motd_overrides(self, week_key: str) -> int:
        cursor = await self._db.execute(
            "DELETE FROM motd_overrides WHERE week_key = ?", [week_key]
        )
        await self._db.commit()
        return cursor.rowcount or 0
