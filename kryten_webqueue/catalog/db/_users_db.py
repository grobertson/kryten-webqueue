"""Users domain database connection for users.sqlite3."""

import logging
from ._base_domain import _DomainDB
from ._watchlist import _WatchlistMixin
from ._devices import _DevicesMixin
from ._feedback import _FeedbackMixin
from .schemas.users_schema import USERS_MIGRATIONS

logger = logging.getLogger(__name__)


class _UsersDB(_WatchlistMixin, _DevicesMixin, _FeedbackMixin, _DomainDB):
    """Users domain database handling OTPs, device keys, watchlists, feedback, and suggestions."""

    def __init__(self, db_path: str):
        super().__init__(db_path, USERS_MIGRATIONS, domain_name="users")

    async def get_user_watchlist_tokens(
        self, username: str, limit: int = 24, offset: int = 0
    ) -> list[dict]:
        """Fetch ordered slice of user watchlist entries."""
        sql = """
            SELECT friendly_token, added_at, id
            FROM user_watchlist
            WHERE username = ?
            ORDER BY added_at DESC, id DESC
            LIMIT ? OFFSET ?
        """
        return await self._fetch_all(sql, [username, limit, offset])

    # --- OTP ---

    async def store_otp(self, username: str, code: str, expires_at: str):
        await self._execute(
            "INSERT INTO otps (username, code, expires_at) VALUES (?, ?, ?)",
            [username, code, expires_at],
        )

    async def verify_otp(self, username: str, code: str) -> bool:
        row = await self._fetch_one(
            "SELECT rowid FROM otps WHERE username=? AND code=? AND used=0 AND expires_at > datetime('now')",
            [username, code],
        )
        if row:
            await self._execute("UPDATE otps SET used=1 WHERE rowid=?", [row["rowid"]])
            return True
        return False

    async def cleanup_expired_otps(self):
        await self._execute(
            "DELETE FROM otps WHERE expires_at < datetime('now') OR used=1"
        )
