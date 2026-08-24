class _BlackoutMixin:
    """Weekend blackout window CRUD (see migration v26).

    A blackout hides a rehosted catalog item from regular users until
    ``expires_at`` (the end of the *following* weekend). Keyed on the bare
    friendly_token. Populated by the standalone ``catalog_blackout`` job; the
    catalog browse/search filters consult it via ``_blackout_exclusion``.
    """

    async def upsert_blackout(
        self, friendly_token: str, *, reason: str, expires_at: str
    ) -> None:
        """Insert or extend a blackout. Keeps the latest (max) expiry on conflict."""
        await self._db.execute(
            """
            INSERT INTO catalog_blackouts (friendly_token, reason, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(friendly_token) DO UPDATE SET
                reason = excluded.reason,
                expires_at = MAX(catalog_blackouts.expires_at, excluded.expires_at)
            """,
            [friendly_token, reason, expires_at],
        )
        await self._db.commit()

    async def prune_expired_blackouts(self) -> int:
        """Delete blackout rows whose window has fully elapsed. Returns count removed."""
        cursor = await self._db.execute(
            "DELETE FROM catalog_blackouts WHERE expires_at <= datetime('now')"
        )
        await self._db.commit()
        return cursor.rowcount or 0

    async def is_blackout(self, friendly_token: str) -> bool:
        """True when the token is under an active (unexpired) blackout."""
        row = await self._fetch_one(
            "SELECT 1 FROM catalog_blackouts "
            "WHERE friendly_token = ? AND expires_at > datetime('now') LIMIT 1",
            [friendly_token],
        )
        return row is not None

    async def count_active_blackouts(self) -> int:
        row = await self._fetch_one(
            "SELECT COUNT(*) AS cnt FROM catalog_blackouts "
            "WHERE expires_at > datetime('now')"
        )
        return row["cnt"] if row else 0

    async def list_active_blackouts(self) -> list[dict]:
        return await self._fetch_all(
            "SELECT friendly_token, reason, expires_at, created_at "
            "FROM catalog_blackouts WHERE expires_at > datetime('now') "
            "ORDER BY expires_at"
        )
