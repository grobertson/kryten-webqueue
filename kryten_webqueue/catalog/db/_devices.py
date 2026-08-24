class _DevicesMixin:
    """Device-linking DB methods for the public API (see migration v27).

    Two tables back the smart-TV / tablet linking flow:

    * ``device_link_codes`` — single-use one-time pads (5-char code, 10-min TTL)
      that a device exchanges for an API key.
    * ``device_api_keys`` — issued keys stored as an irreversible SHA-256 hash
      plus a non-secret display prefix and the user-chosen device name.
    """

    # ── one-time link codes ──────────────────────────────────────────────────

    async def create_link_code(
        self, code: str, username: str, device_name: str, expires_at: str
    ) -> None:
        """Store a fresh one-time pad. ``code`` must already be normalized."""
        await self._db.execute(
            "INSERT INTO device_link_codes (code, username, device_name, expires_at) "
            "VALUES (?, ?, ?, ?)",
            [code, username, device_name, expires_at],
        )
        await self._db.commit()

    async def get_valid_link_code(self, code: str) -> dict | None:
        """Return an unexpired link-code row, or ``None`` if missing/expired."""
        row = await self._fetch_one(
            "SELECT code, username, device_name, created_at, expires_at "
            "FROM device_link_codes "
            "WHERE code = ? AND datetime(expires_at) > datetime('now') LIMIT 1",
            [code],
        )
        return dict(row) if row else None

    async def delete_link_code(self, code: str) -> None:
        await self._db.execute("DELETE FROM device_link_codes WHERE code = ?", [code])
        await self._db.commit()

    async def link_code_exists(self, code: str) -> bool:
        """True when the code is currently allocated (used to avoid collisions)."""
        row = await self._fetch_one(
            "SELECT 1 FROM device_link_codes WHERE code = ? LIMIT 1", [code]
        )
        return row is not None

    async def purge_expired_link_codes(self) -> int:
        """Delete link codes whose TTL has elapsed. Returns count removed."""
        cursor = await self._db.execute(
            "DELETE FROM device_link_codes WHERE datetime(expires_at) <= datetime('now')"
        )
        await self._db.commit()
        return cursor.rowcount or 0

    # ── issued API keys ──────────────────────────────────────────────────────

    async def create_device_key(
        self, username: str, device_name: str, key_prefix: str, key_hash: str
    ) -> int:
        """Persist a new device key (hash only). Returns the new row id."""
        cursor = await self._db.execute(
            "INSERT INTO device_api_keys (username, device_name, key_prefix, key_hash) "
            "VALUES (?, ?, ?, ?)",
            [username, device_name, key_prefix, key_hash],
        )
        await self._db.commit()
        return cursor.lastrowid or 0

    async def get_device_key_by_hash(self, key_hash: str) -> dict | None:
        """Resolve an API key hash to its owning device, or ``None``."""
        row = await self._fetch_one(
            "SELECT id, username, device_name, key_prefix, created_at, last_used_at "
            "FROM device_api_keys WHERE key_hash = ? LIMIT 1",
            [key_hash],
        )
        return dict(row) if row else None

    async def touch_device_key(self, key_id: int) -> None:
        """Record that a key was just used (best-effort last-seen tracking)."""
        await self._db.execute(
            "UPDATE device_api_keys SET last_used_at = datetime('now') WHERE id = ?",
            [key_id],
        )
        await self._db.commit()

    async def list_device_keys(self, username: str) -> list[dict]:
        """All of a user's linked devices, newest first. Never exposes hashes."""
        rows = await self._fetch_all(
            "SELECT id, device_name, key_prefix, created_at, last_used_at "
            "FROM device_api_keys WHERE username = ? "
            "ORDER BY created_at DESC, id DESC",
            [username],
        )
        return [dict(r) for r in rows]

    async def delete_device_key(self, key_id: int, username: str) -> bool:
        """Revoke one device key, scoped to its owner. True if a row was removed."""
        cursor = await self._db.execute(
            "DELETE FROM device_api_keys WHERE id = ? AND username = ?",
            [key_id, username],
        )
        await self._db.commit()
        return (cursor.rowcount or 0) > 0

    async def revoke_user_device_keys(self, username: str) -> int:
        """Revoke every key for a user (e.g. on ban). Returns count removed."""
        cursor = await self._db.execute(
            "DELETE FROM device_api_keys WHERE username = ?", [username]
        )
        await self._db.commit()
        return cursor.rowcount or 0

    async def device_key_usernames(self) -> list[str]:
        """Distinct usernames that currently hold at least one device key.

        Used by the ban-reconciliation job to intersect key holders against the
        moderator's ban list without scanning every key.
        """
        rows = await self._fetch_all("SELECT DISTINCT username FROM device_api_keys")
        return [r["username"] for r in rows]
