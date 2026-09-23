"""Postgres-backed users domain: OTPs, device keys, watchlists, feedback, suggestions."""

from ._pg_base_domain import _PgDomainDB, parse_dt


class _PgUsersDB(_PgDomainDB):
    """Postgres implementation of the users domain (schema ``users``)."""

    # --- Watchlist ---

    async def get_user_watchlist_tokens(
        self, username: str, limit: int = 24, offset: int = 0
    ) -> list[dict]:
        sql = """
            SELECT friendly_token, added_at, id
            FROM user_watchlist
            WHERE username = ?
            ORDER BY added_at DESC, id DESC
            LIMIT ? OFFSET ?
        """
        return await self._fetch_all(sql, [username, limit, offset])

    async def watchlist_add(self, username: str, token: str) -> bool:
        result = await self._execute(
            "INSERT INTO user_watchlist (username, friendly_token) VALUES (?, ?) "
            "ON CONFLICT DO NOTHING",
            [username, token],
        )
        return result.rowcount > 0

    async def watchlist_remove(self, username: str, token: str) -> bool:
        result = await self._execute(
            "DELETE FROM user_watchlist WHERE username = ? AND friendly_token = ?",
            [username, token],
        )
        return result.rowcount > 0

    async def watchlist_tokens(self, username: str) -> list[str]:
        rows = await self._fetch_all(
            "SELECT friendly_token FROM user_watchlist WHERE username = ? "
            "ORDER BY added_at DESC, id DESC",
            [username],
        )
        return [r["friendly_token"] for r in rows]

    async def watchlist_count(self, username: str) -> int:
        row = await self._fetch_one(
            "SELECT COUNT(*) as cnt FROM user_watchlist WHERE username = ?", [username]
        )
        return row["cnt"] if row else 0

    # --- OTP ---

    async def store_otp(self, username: str, code: str, expires_at: str):
        await self._execute(
            "INSERT INTO otps (username, code, expires_at) VALUES (?, ?, ?)",
            [username, code, parse_dt(expires_at)],
        )

    async def verify_otp(self, username: str, code: str) -> bool:
        row = await self._fetch_one(
            "SELECT id FROM otps WHERE username=? AND code=? AND used=false AND expires_at > now()",
            [username, code],
        )
        if row:
            await self._execute("UPDATE otps SET used=true WHERE id=?", [row["id"]])
            return True
        return False

    async def cleanup_expired_otps(self):
        await self._execute("DELETE FROM otps WHERE expires_at < now() OR used=true")

    # --- Device link codes ---

    async def create_link_code(
        self, code: str, username: str, device_name: str, expires_at: str
    ) -> None:
        await self._execute(
            "INSERT INTO device_link_codes (code, username, device_name, expires_at) "
            "VALUES (?, ?, ?, ?)",
            [code, username, device_name, parse_dt(expires_at)],
        )

    async def get_valid_link_code(self, code: str) -> dict | None:
        row = await self._fetch_one(
            "SELECT code, username, device_name, created_at, expires_at "
            "FROM device_link_codes "
            "WHERE code = ? AND expires_at > now() LIMIT 1",
            [code],
        )
        return dict(row) if row else None

    async def delete_link_code(self, code: str) -> None:
        await self._execute("DELETE FROM device_link_codes WHERE code = ?", [code])

    async def link_code_exists(self, code: str) -> bool:
        row = await self._fetch_one(
            "SELECT 1 FROM device_link_codes WHERE code = ? LIMIT 1", [code]
        )
        return row is not None

    async def purge_expired_link_codes(self) -> int:
        result = await self._execute(
            "DELETE FROM device_link_codes WHERE expires_at <= now()"
        )
        return result.rowcount or 0

    # --- Device API keys ---

    async def create_device_key(
        self, username: str, device_name: str, key_prefix: str, key_hash: str
    ) -> int:
        return await self._execute_returning_id(
            "INSERT INTO device_api_keys (username, device_name, key_prefix, key_hash) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            [username, device_name, key_prefix, key_hash],
        )

    async def get_device_key_by_hash(self, key_hash: str) -> dict | None:
        row = await self._fetch_one(
            "SELECT id, username, device_name, key_prefix, created_at, last_used_at "
            "FROM device_api_keys WHERE key_hash = ? LIMIT 1",
            [key_hash],
        )
        return dict(row) if row else None

    async def touch_device_key(self, key_id: int) -> None:
        await self._execute(
            "UPDATE device_api_keys SET last_used_at = now() WHERE id = ?",
            [key_id],
        )

    async def list_device_keys(self, username: str) -> list[dict]:
        rows = await self._fetch_all(
            "SELECT id, device_name, key_prefix, created_at, last_used_at "
            "FROM device_api_keys WHERE username = ? "
            "ORDER BY created_at DESC, id DESC",
            [username],
        )
        return [dict(r) for r in rows]

    async def delete_device_key(self, key_id: int, username: str) -> bool:
        result = await self._execute(
            "DELETE FROM device_api_keys WHERE id = ? AND username = ?",
            [key_id, username],
        )
        return (result.rowcount or 0) > 0

    async def revoke_user_device_keys(self, username: str) -> int:
        result = await self._execute(
            "DELETE FROM device_api_keys WHERE username = ?", [username]
        )
        return result.rowcount or 0

    async def device_key_usernames(self) -> list[str]:
        rows = await self._fetch_all("SELECT DISTINCT username FROM device_api_keys")
        return [r["username"] for r in rows]

    # --- Feedback ---

    async def add_feedback(self, *, username: str, body: str) -> int:
        return await self._execute_returning_id(
            "INSERT INTO feedback (username, body) VALUES (?, ?) RETURNING id",
            [username, body],
        )

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
        result = await self._execute(
            "UPDATE feedback SET status = ? WHERE id = ?", [status, feedback_id]
        )
        return (result.rowcount or 0) > 0

    async def delete_feedback(self, feedback_id: int) -> bool:
        result = await self._execute("DELETE FROM feedback WHERE id = ?", [feedback_id])
        return (result.rowcount or 0) > 0

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
        return await self._execute_returning_id(
            "INSERT INTO title_suggestions "
            "(username, query, resolved_title, resolved_year, resolved_source, "
            " resolved_id, poster_url, resolution, catalog_token) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
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
        result = await self._execute(
            "UPDATE title_suggestions SET status = ? WHERE id = ?",
            [status, suggestion_id],
        )
        return (result.rowcount or 0) > 0

    async def delete_title_suggestion(self, suggestion_id: int) -> bool:
        result = await self._execute(
            "DELETE FROM title_suggestions WHERE id = ?", [suggestion_id]
        )
        return (result.rowcount or 0) > 0
