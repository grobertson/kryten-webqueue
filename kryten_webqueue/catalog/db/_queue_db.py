"""Queue domain database connection for queue.sqlite3."""

import logging
from ._base_domain import _DomainDB
from ._queue import _QueueMixin
from ._playlists import _PlaylistsMixin
from ._blackouts import _BlackoutMixin
from .schemas.queue_schema import QUEUE_MIGRATIONS

logger = logging.getLogger(__name__)


class _QueueDB(_QueueMixin, _PlaylistsMixin, _BlackoutMixin, _DomainDB):
    """Queue domain database handling queue shadow, playlists, schedules, completions, and blackouts."""

    def __init__(self, db_path: str):
        super().__init__(db_path, QUEUE_MIGRATIONS, domain_name="queue")

    async def get_active_hidden_media_ids(self, window_days: int = 7) -> set[str]:
        """Fetch media_ids that should be hidden from regular users due to recent play."""
        if window_days <= 0:
            return set()

        sql_pc = """
            SELECT media_id FROM play_completions
            WHERE media_type = 'cm' AND completed_at >= datetime('now', ?)
        """
        rows_pc = await self._fetch_all(sql_pc, [f"-{int(window_days)} days"])
        hidden = {r["media_id"] for r in rows_pc if r.get("media_id")}

        sql_pip = """
            SELECT spi.media_id FROM playlist_item_played pip
            JOIN saved_playlists sp ON sp.id = pip.playlist_id
            JOIN saved_playlist_items spi ON spi.playlist_id = sp.id AND spi.position = pip.position
            WHERE sp.is_immutable = 0 AND spi.media_type = 'cm'
        """
        rows_pip = await self._fetch_all(sql_pip)
        hidden.update(r["media_id"] for r in rows_pip if r.get("media_id"))
        return hidden

    async def get_reserved_media_ids(self) -> set[str]:
        """Fetch media_ids belonging to immutable or promo playlists (excluded from public catalog)."""
        sql = """
            SELECT spi.media_id FROM saved_playlist_items spi
            JOIN saved_playlists sp ON spi.playlist_id = sp.id
            WHERE (sp.is_immutable = 1 OR sp.promo_type IS NOT NULL)
              AND spi.media_type = 'cm'
        """
        rows = await self._fetch_all(sql)
        return {r["media_id"] for r in rows if r.get("media_id")}

    async def get_active_blackout_tokens(self) -> set[str]:
        """Tokens currently hidden under an active weekend blackout."""
        sql = "SELECT friendly_token FROM catalog_blackouts WHERE expires_at > datetime('now')"
        rows = await self._fetch_all(sql)
        return {r["friendly_token"] for r in rows if r.get("friendly_token")}

    async def is_media_restricted(self, identifiers: list[str]) -> bool:
        """Check whether any of the given tokens or manifest URLs belong to reserved playlists."""
        ids = [i for i in identifiers if i]
        if not ids:
            return False
        ph = ",".join("?" * len(ids))
        sql = f"""
            SELECT 1 FROM saved_playlist_items spi
            JOIN saved_playlists sp ON spi.playlist_id = sp.id
            WHERE (sp.is_immutable = 1 OR sp.promo_type IS NOT NULL)
              AND spi.media_type = 'cm'
              AND spi.media_id IN ({ph})
            LIMIT 1
        """
        row = await self._fetch_one(sql, ids)
        return row is not None

    async def get_played_at_for_tokens(self, tokens: list[str]) -> dict[str, str]:
        """Return {media_id: completed_at} for the most recent completion of each token."""
        if not tokens:
            return {}
        ph = ",".join("?" * len(tokens))
        sql = f"""
            SELECT media_id, MAX(completed_at) AS played_at
            FROM play_completions
            WHERE media_type = 'cm' AND media_id IN ({ph})
            GROUP BY media_id
        """
        rows = await self._fetch_all(sql, tokens)
        return {r["media_id"]: r["played_at"] for r in rows if r.get("media_id")}

    async def get_promo_pool_media_ids(self) -> set[str]:
        """Media IDs of promo items in promo playlists."""
        sql = """
            SELECT spi.media_id FROM saved_playlist_items spi
            JOIN saved_playlists sp ON sp.id = spi.playlist_id
            WHERE sp.promo_type IS NOT NULL AND spi.media_type = 'cm'
        """
        rows = await self._fetch_all(sql)
        return {r["media_id"] for r in rows if r.get("media_id")}

    async def purge_promo_completions(self, media_ids: set[str]) -> int:
        """Purge completions and played state for promo items."""
        if not media_ids:
            return 0
        ids_list = list(media_ids)
        total_deleted = 0
        for i in range(0, len(ids_list), 500):
            chunk = ids_list[i : i + 500]
            ph = ",".join("?" * len(chunk))
            row = await self._fetch_one(
                f"SELECT COUNT(*) AS c FROM play_completions WHERE media_id IN ({ph})",
                chunk,
            )
            count = row["c"] if row else 0
            if count > 0:
                await self._execute(
                    f"DELETE FROM play_completions WHERE media_id IN ({ph})",
                    chunk,
                )
                total_deleted += count
            await self._execute(
                f"DELETE FROM playlist_item_played WHERE media_id IN ({ph})",
                chunk,
            )
        return total_deleted

    async def record_play_completion(
        self,
        media_type: str = "cm",
        media_id: str | None = None,
        playlist_id: int | None = None,
        position: int | None = None,
        *,
        friendly_token: str | None = None,
        duration_sec: int | None = None,
    ) -> None:
        """Record that an item was played."""
        target_id = friendly_token or media_id or ""
        # Short episodes of mutable playlists track position until last item plays
        if playlist_id is not None and position is not None:
            sp = await self._fetch_one(
                "SELECT is_immutable FROM saved_playlists WHERE id = ?", [playlist_id]
            )
            if sp and not sp["is_immutable"]:
                await self._execute(
                    "INSERT OR REPLACE INTO playlist_item_played "
                    "(playlist_id, position, media_type, media_id, played_at) "
                    "VALUES (?, ?, ?, ?, datetime('now'))",
                    [playlist_id, position, media_type, target_id],
                )
                max_row = await self._fetch_one(
                    "SELECT MAX(position) AS max_pos FROM saved_playlist_items WHERE playlist_id = ?",
                    [playlist_id],
                )
                if max_row and position >= (max_row["max_pos"] or 0):
                    await self._execute(
                        "DELETE FROM playlist_item_played WHERE playlist_id = ?",
                        [playlist_id],
                    )
                return

        await self._execute(
            "INSERT INTO play_completions (media_type, media_id) VALUES (?, ?)",
            [media_type, target_id],
        )

    async def unrecord_play_completion(self, media_type: str, media_id: str) -> int:
        """Delete completions for a media item so it reappears in browse."""
        row = await self._fetch_one(
            "SELECT COUNT(*) AS c FROM play_completions WHERE media_id = ? AND media_type = ?",
            [media_id, media_type],
        )
        count = row["c"] if row else 0
        if count > 0:
            await self._execute(
                "DELETE FROM play_completions WHERE media_id = ? AND media_type = ?",
                [media_id, media_type],
            )
        return count

    async def clear_play_state(self, media_id: str, media_type: str = "cm") -> None:
        """Clear all play completion and playlist played records for a media item."""
        await self._execute(
            "DELETE FROM play_completions WHERE media_id = ? AND media_type = ?",
            [media_id, media_type],
        )
        await self._execute(
            "DELETE FROM playlist_item_played WHERE media_id = ? AND media_type = ?",
            [media_id, media_type],
        )

    async def get_recently_played_completions(self, days: int) -> list[dict]:
        """Return raw play_completions rows within the window."""
        sql = """
            SELECT pc.media_id, MAX(pc.completed_at) AS last_completed
            FROM play_completions pc
            WHERE pc.media_type = 'cm' AND pc.completed_at >= datetime('now', ?)
            GROUP BY pc.media_id ORDER BY last_completed DESC
        """
        return await self._fetch_all(sql, [f"-{int(days)} days"])
