"""Postgres-backed queue domain: shadow queue, spend, history, playlists, schedules, blackouts."""

import logging
from datetime import datetime, timedelta, timezone

from ._pg_base_domain import _PgDomainDB, parse_dt

logger = logging.getLogger(__name__)


def _sqlite_timestamp_text(value: datetime | str) -> str:
    """Return a timestamp in the text shape exposed by the SQLite backend."""
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class _PgQueueDB(_PgDomainDB):
    """Postgres implementation of the queue domain (schema ``queue``)."""

    # --- Queue shadow ---

    async def get_shadow_items(self) -> list[dict]:
        return await self._fetch_all("SELECT * FROM queue_shadow ORDER BY position ASC")

    async def upsert_shadow_item(self, item: dict):
        defaults = {
            "paid_by": None,
            "tier": None,
            "z_cost": None,
            "schedule_id": None,
            "is_promo": False,
            "promo_type": None,
            "lead_in_for_uid": None,
            "added_at": datetime.now(timezone.utc),
        }
        row = {**defaults, **item}
        sql = """
            INSERT INTO queue_shadow
                (uid, position, title, media_type, media_id, duration_sec, is_pay, paid_by,
                 tier, z_cost, schedule_id, is_promo, promo_type, lead_in_for_uid, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (uid) DO UPDATE SET
                position = EXCLUDED.position,
                title = EXCLUDED.title,
                media_type = EXCLUDED.media_type,
                media_id = EXCLUDED.media_id,
                duration_sec = EXCLUDED.duration_sec,
                is_pay = EXCLUDED.is_pay,
                paid_by = EXCLUDED.paid_by,
                tier = EXCLUDED.tier,
                z_cost = EXCLUDED.z_cost,
                schedule_id = EXCLUDED.schedule_id,
                is_promo = EXCLUDED.is_promo,
                promo_type = EXCLUDED.promo_type,
                lead_in_for_uid = EXCLUDED.lead_in_for_uid,
                added_at = EXCLUDED.added_at
        """
        await self._execute(
            sql,
            [
                row["uid"],
                row["position"],
                row.get("title"),
                row["media_type"],
                row["media_id"],
                row.get("duration_sec"),
                bool(row.get("is_pay")),
                row.get("paid_by"),
                row.get("tier"),
                row.get("z_cost"),
                row.get("schedule_id"),
                bool(row.get("is_promo")),
                row.get("promo_type"),
                row.get("lead_in_for_uid"),
                parse_dt(row.get("added_at")),
            ],
        )

    async def remove_shadow_items(self, uids: set[int]):
        placeholders = ",".join("?" * len(uids))
        await self._execute(
            f"DELETE FROM queue_shadow WHERE uid IN ({placeholders})", list(uids)
        )

    async def update_shadow_position(self, uid: int, position: int):
        await self._execute(
            "UPDATE queue_shadow SET position=? WHERE uid=?", [position, uid]
        )

    async def update_shadow_estimated_start(self, uid: int, estimated: str):
        await self._execute(
            "UPDATE queue_shadow SET estimated_start_at=? WHERE uid=?",
            [parse_dt(estimated), uid],
        )

    async def get_last_pay_uid(self) -> int | None:
        row = await self._fetch_one(
            "SELECT uid FROM queue_shadow WHERE is_pay = true ORDER BY position DESC LIMIT 1"
        )
        return row["uid"] if row else None

    async def get_shadow_position_after(self, after_uid: int) -> int:
        row = await self._fetch_one(
            "SELECT position FROM queue_shadow WHERE uid = ?", [after_uid]
        )
        return (row["position"] + 1) if row else 0

    async def get_pay_items(self) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM queue_shadow WHERE is_pay = true ORDER BY position ASC"
        )

    # --- Spend requests ---

    async def save_spend_request(
        self,
        request_id: str,
        *,
        username: str,
        uid: int | None,
        friendly_token: str | None = None,
        tier: str | None = None,
        z_cost: int | None = None,
    ):
        sql = """
            INSERT INTO spend_requests (request_id, username, uid, friendly_token, tier, z_cost)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
        """
        await self._execute(
            sql, [request_id, username, uid, friendly_token, tier, z_cost]
        )

    async def get_request_id_for_uid(self, uid: int) -> str | None:
        row = await self._fetch_one(
            "SELECT request_id FROM spend_requests WHERE uid = ? AND refunded = false LIMIT 1",
            [uid],
        )
        return row["request_id"] if row else None

    async def mark_spend_refunded(self, request_id: str):
        await self._execute(
            "UPDATE spend_requests SET refunded=true, refunded_at=now() WHERE request_id=?",
            [request_id],
        )

    # --- Queue history ---

    async def add_queue_history(
        self,
        *,
        username: str,
        friendly_token: str | None,
        title: str | None,
        tier: str,
        z_cost: int,
    ):
        await self._execute(
            "INSERT INTO queue_history (username, friendly_token, title, tier, z_cost) VALUES (?, ?, ?, ?, ?)",
            [username, friendly_token, title, tier, z_cost],
        )

    async def get_user_queue_history(
        self, username: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM queue_history WHERE username=? ORDER BY id DESC LIMIT ? OFFSET ?",
            [username, limit, offset],
        )

    async def get_user_queue_history_count(self, username: str) -> int:
        row = await self._fetch_one(
            "SELECT COUNT(*) AS c FROM queue_history WHERE username=?",
            [username],
        )
        return int(row["c"]) if row else 0

    # --- Saved playlists ---

    async def get_saved_playlists(self) -> list[dict]:
        return await self._fetch_all("SELECT * FROM saved_playlists ORDER BY name")

    async def get_saved_playlist(self, playlist_id: int) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM saved_playlists WHERE id=?", [playlist_id]
        )

    async def create_saved_playlist(
        self,
        *,
        name: str,
        description: str | None,
        is_immutable: bool,
        created_by: str,
        promo_type: str | None = None,
    ) -> int:
        return await self._execute_returning_id(
            "INSERT INTO saved_playlists (name, description, is_immutable, created_by, promo_type) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id",
            [name, description, bool(is_immutable), created_by, promo_type],
        )

    async def update_saved_playlist(
        self,
        playlist_id: int,
        *,
        name: str,
        description: str | None,
        is_immutable: bool,
        promo_type: str | None = None,
    ):
        await self._execute(
            "UPDATE saved_playlists SET name=?, description=?, is_immutable=?, promo_type=?, updated_at=now() WHERE id=?",
            [name, description, bool(is_immutable), promo_type, playlist_id],
        )

    async def get_promo_pools(self) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM saved_playlists WHERE promo_type IS NOT NULL ORDER BY promo_type, name"
        )

    async def get_promo_pool_items(self, promo_type: str) -> list[dict]:
        return await self._fetch_all(
            "SELECT spi.media_type, spi.media_id, spi.title, spi.duration_sec "
            "FROM saved_playlist_items spi "
            "JOIN saved_playlists sp ON spi.playlist_id = sp.id "
            "WHERE sp.promo_type = ? "
            "ORDER BY sp.id, spi.position",
            [promo_type],
        )

    async def delete_saved_playlist(self, playlist_id: int):
        await self._execute("DELETE FROM saved_playlists WHERE id=?", [playlist_id])

    async def get_saved_playlist_items(self, playlist_id: int) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM saved_playlist_items WHERE playlist_id=? ORDER BY position",
            [playlist_id],
        )

    async def replace_playlist_items(self, playlist_id: int, items: list[dict]):
        await self._execute(
            "DELETE FROM saved_playlist_items WHERE playlist_id=?", [playlist_id]
        )
        for i, item in enumerate(items):
            await self._execute(
                "INSERT INTO saved_playlist_items (playlist_id, position, media_type, media_id, title, duration_sec) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    playlist_id,
                    i,
                    item["media_type"],
                    item["media_id"],
                    item.get("title"),
                    item.get("duration_sec"),
                ],
            )

    async def append_playlist_item(self, playlist_id: int, item: dict) -> int:
        """Append a single item to the end of a playlist. Returns new item count."""
        row = await self._fetch_one(
            "SELECT COALESCE(MAX(position), -1) AS pos, COUNT(*) AS cnt "
            "FROM saved_playlist_items WHERE playlist_id=?",
            [playlist_id],
        )
        next_pos = (row["pos"] + 1) if row else 0
        count = (row["cnt"] if row else 0) + 1
        await self._execute(
            "INSERT INTO saved_playlist_items (playlist_id, position, media_type, media_id, title, duration_sec) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                playlist_id,
                next_pos,
                item["media_type"],
                item["media_id"],
                item.get("title"),
                item.get("duration_sec"),
            ],
        )
        await self._execute(
            "UPDATE saved_playlists SET updated_at=now() WHERE id=?",
            [playlist_id],
        )
        return count

    async def append_playlist_items(self, playlist_id: int, items: list[dict]) -> int:
        """Append many items to the end of a playlist, skipping any whose
        ``media_id`` is already present. Returns the number actually added."""
        existing_rows = await self._fetch_all(
            "SELECT media_id FROM saved_playlist_items WHERE playlist_id=?",
            [playlist_id],
        )
        seen = {r["media_id"] for r in existing_rows}
        row = await self._fetch_one(
            "SELECT COALESCE(MAX(position), -1) AS pos FROM saved_playlist_items WHERE playlist_id=?",
            [playlist_id],
        )
        next_pos = (row["pos"] + 1) if row else 0
        added = 0
        for item in items:
            media_id = item.get("media_id")
            if not media_id or media_id in seen:
                continue
            seen.add(media_id)
            await self._execute(
                "INSERT INTO saved_playlist_items (playlist_id, position, media_type, media_id, title, duration_sec) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    playlist_id,
                    next_pos,
                    item.get("media_type", "cm"),
                    media_id,
                    item.get("title"),
                    item.get("duration_sec"),
                ],
            )
            next_pos += 1
            added += 1
        if added:
            await self._execute(
                "UPDATE saved_playlists SET updated_at=now() WHERE id=?",
                [playlist_id],
            )
        return added

    async def rotate_playlist_item_to_bottom(
        self, media_id: str, media_type: str = "cm", *, is_partitioned: bool = True
    ) -> int:
        """Move a played item to the end of every mutable playlist containing it.

        Postgres always runs decoupled (``is_partitioned`` defaults True here) —
        the friendly_token -> manifest_url resolution below is broken from this
        connection (catalog is a separate schema not in this pool's
        search_path) and is instead done by the ``Database`` facade before
        calling in. This mirrors the SQLite partitioned fix for the same bug.
        """
        if media_type == "cm" and not is_partitioned:
            row = await self._fetch_one(
                "SELECT manifest_url FROM catalog WHERE friendly_token = ? LIMIT 1",
                [media_id],
            )
            if row and row.get("manifest_url"):
                media_id = row["manifest_url"]
        playlists = await self._fetch_all(
            """
            SELECT spi.playlist_id
            FROM saved_playlist_items spi
            JOIN saved_playlists sp ON sp.id = spi.playlist_id
            WHERE spi.media_id = ? AND spi.media_type = ?
              AND sp.is_immutable = false AND sp.promo_type IS NULL
            """,
            [media_id, media_type],
        )
        rotated = 0
        for r in playlists:
            playlist_id = r["playlist_id"]
            items = await self._fetch_all(
                "SELECT * FROM saved_playlist_items WHERE playlist_id = ? ORDER BY position",
                [playlist_id],
            )
            target_idx = next(
                (
                    i
                    for i, it in enumerate(items)
                    if it["media_id"] == media_id and it["media_type"] == media_type
                ),
                None,
            )
            if target_idx is None or target_idx == len(items) - 1:
                continue  # not found or already at bottom
            # Move target to end; delete-then-reinsert avoids position UNIQUE conflicts.
            reordered = (
                items[:target_idx] + items[target_idx + 1 :] + [items[target_idx]]
            )
            await self._execute(
                "DELETE FROM saved_playlist_items WHERE playlist_id=?", [playlist_id]
            )
            for new_pos, item in enumerate(reordered):
                await self._execute(
                    "INSERT INTO saved_playlist_items "
                    "(playlist_id, position, media_type, media_id, title, duration_sec) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        playlist_id,
                        new_pos,
                        item["media_type"],
                        item["media_id"],
                        item.get("title"),
                        item.get("duration_sec"),
                    ],
                )
            await self._execute(
                "UPDATE saved_playlists SET updated_at=now() WHERE id=?",
                [playlist_id],
            )
            logger.info("Rotated %r to bottom of playlist %d", media_id, playlist_id)
            rotated += 1
        return rotated

    async def get_most_recent_playlist(self, created_by: str) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM saved_playlists WHERE created_by=? ORDER BY created_at DESC, id DESC LIMIT 1",
            [created_by],
        )

    async def get_playlist_by_name(self, name: str, created_by: str) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM saved_playlists WHERE name=? AND created_by=? ORDER BY id LIMIT 1",
            [name, created_by],
        )

    async def get_playlist_by_name_any(self, name: str) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM saved_playlists WHERE name=? ORDER BY id LIMIT 1",
            [name],
        )

    # --- Schedules ---

    async def get_schedules(self) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM playlist_schedules ORDER BY fire_at"
        )

    async def get_schedule(self, schedule_id: int) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM playlist_schedules WHERE id=?", [schedule_id]
        )

    async def create_schedule(self, **kwargs) -> int:
        keys = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" * len(kwargs))
        return await self._execute_returning_id(
            f"INSERT INTO playlist_schedules ({keys}) VALUES ({placeholders}) RETURNING id",
            list(kwargs.values()),
        )

    async def update_schedule(self, schedule_id: int, **kwargs):
        # The admin form sends fire_at as an ISO-8601 string.  asyncpg's
        # timestamptz codec, unlike SQLite, requires a native datetime.
        if "fire_at" in kwargs:
            kwargs["fire_at"] = parse_dt(kwargs["fire_at"])
        sets = ", ".join(f"{k}=?" for k in kwargs.keys())
        await self._execute(
            f"UPDATE playlist_schedules SET {sets} WHERE id=?",
            [*kwargs.values(), schedule_id],
        )

    async def delete_schedule(self, schedule_id: int):
        await self._execute("DELETE FROM playlist_schedules WHERE id=?", [schedule_id])

    async def mark_schedule_fired(self, schedule_id: int, fired_at: str):
        await self._execute(
            "UPDATE playlist_schedules SET fired_at=? WHERE id=?",
            [parse_dt(fired_at), schedule_id],
        )

    # --- Active schedule ---

    async def get_active_schedule(self) -> dict | None:
        return await self._fetch_one("SELECT * FROM active_schedule WHERE id=1")

    async def set_active_schedule(
        self,
        *,
        schedule_id: int,
        playlist_id: int,
        is_immutable: bool,
        started_at: str,
        estimated_end_at: str,
        last_item_uid: int | None = None,
        last_item_media_id: str | None = None,
    ):
        await self._execute(
            """
            INSERT INTO active_schedule
                (id, schedule_id, playlist_id, is_immutable, started_at, estimated_end_at,
                 last_item_uid, last_item_media_id, lock_disabled)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT (id) DO UPDATE SET
                schedule_id = EXCLUDED.schedule_id,
                playlist_id = EXCLUDED.playlist_id,
                is_immutable = EXCLUDED.is_immutable,
                started_at = EXCLUDED.started_at,
                estimated_end_at = EXCLUDED.estimated_end_at,
                last_item_uid = EXCLUDED.last_item_uid,
                last_item_media_id = EXCLUDED.last_item_media_id,
                lock_disabled = EXCLUDED.lock_disabled
            """,
            [
                schedule_id,
                playlist_id,
                bool(is_immutable),
                parse_dt(started_at),
                parse_dt(estimated_end_at),
                last_item_uid,
                last_item_media_id,
            ],
        )

    async def clear_active_schedule(self):
        await self._execute("DELETE FROM active_schedule WHERE id=1")

    async def disable_active_lock(self):
        """Lift the in-progress scheduled-event lock without ending the event."""
        await self._execute("UPDATE active_schedule SET lock_disabled=1 WHERE id=1")

    async def is_event_lock_active(self) -> bool:
        row = await self.get_active_schedule()
        if not row:
            return False
        if not row.get("is_immutable"):
            return False
        return not row.get("lock_disabled")

    # --- Pre-fire lock check ---

    async def is_pre_fire_lock_active(self) -> bool:
        # fire_at is a native timestamptz here (unlike SQLite's ISO-string column),
        # so plain `fire_at > now()` comparisons work without any datetime()-style
        # wrapping. The interval arithmetic still needs an explicit cast.
        row = await self._fetch_one("""
            SELECT 1 FROM playlist_schedules
            WHERE is_active = true
              AND lock_disabled = 0
              AND (fire_at - (pre_fire_lock_minutes::text || ' minutes')::interval) <= now()
              AND fire_at > now()
            LIMIT 1
        """)
        return row is not None

    async def get_active_pre_fire_lock(self) -> dict | None:
        return await self._fetch_one("""
            SELECT * FROM playlist_schedules
            WHERE is_active = true
              AND lock_disabled = 0
              AND (fire_at - (pre_fire_lock_minutes::text || ' minutes')::interval) <= now()
              AND fire_at > now()
            ORDER BY fire_at
            LIMIT 1
        """)

    async def disable_active_pre_fire_locks(self) -> int:
        result = await self._execute("""
            UPDATE playlist_schedules
            SET lock_disabled = 1
            WHERE is_active = true
              AND lock_disabled = 0
              AND (fire_at - (pre_fire_lock_minutes::text || ' minutes')::interval) <= now()
              AND fire_at > now()
        """)
        return result.rowcount or 0

    async def get_next_schedule(self) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM playlist_schedules WHERE is_active=true AND fire_at > now() ORDER BY fire_at LIMIT 1"
        )

    # --- Cross-domain helper queries (queue_db.py extras) ---

    async def get_active_hidden_media_ids(self, window_days: int = 7) -> set[str]:
        """Fetch media_ids that should be hidden from regular users due to recent play."""
        if window_days <= 0:
            return set()

        sql_pc = """
            SELECT media_id FROM play_completions
            WHERE media_type = 'cm' AND completed_at >= (now() + (?)::interval)
        """
        rows_pc = await self._fetch_all(sql_pc, [timedelta(days=-int(window_days))])
        hidden = {r["media_id"] for r in rows_pc if r.get("media_id")}

        sql_pip = """
            SELECT spi.media_id FROM playlist_item_played pip
            JOIN saved_playlists sp ON sp.id = pip.playlist_id
            JOIN saved_playlist_items spi ON spi.playlist_id = sp.id AND spi.position = pip.position
            WHERE sp.is_immutable = false AND spi.media_type = 'cm'
        """
        rows_pip = await self._fetch_all(sql_pip)
        hidden.update(r["media_id"] for r in rows_pip if r.get("media_id"))
        return hidden

    async def get_reserved_media_ids(self) -> set[str]:
        """Fetch media_ids belonging to immutable or promo playlists (excluded from public catalog)."""
        sql = """
            SELECT spi.media_id FROM saved_playlist_items spi
            JOIN saved_playlists sp ON spi.playlist_id = sp.id
            WHERE (sp.is_immutable = true OR sp.promo_type IS NOT NULL)
              AND spi.media_type = 'cm'
        """
        rows = await self._fetch_all(sql)
        return {r["media_id"] for r in rows if r.get("media_id")}

    async def get_active_blackout_tokens(self) -> set[str]:
        """Tokens currently hidden under an active weekend blackout."""
        sql = "SELECT friendly_token FROM catalog_blackouts WHERE expires_at > now()"
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
            WHERE (sp.is_immutable = true OR sp.promo_type IS NOT NULL)
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
        # SQLite returns its datetime() result as text, and catalog templates
        # compare it lexically to the recently-played cutoff.  Keep this
        # cross-domain contract stable instead of leaking asyncpg datetimes.
        return {
            r["media_id"]: _sqlite_timestamp_text(r["played_at"])
            for r in rows
            if r.get("media_id") and r.get("played_at") is not None
        }

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
        if playlist_id is not None and position is not None:
            sp = await self._fetch_one(
                "SELECT is_immutable FROM saved_playlists WHERE id = ?", [playlist_id]
            )
            if sp and not sp["is_immutable"]:
                await self._execute(
                    "INSERT INTO playlist_item_played "
                    "(playlist_id, position, media_type, media_id, played_at) "
                    "VALUES (?, ?, ?, ?, now()) "
                    "ON CONFLICT (playlist_id, position) DO UPDATE SET "
                    "media_type = EXCLUDED.media_type, media_id = EXCLUDED.media_id, "
                    "played_at = EXCLUDED.played_at",
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
            WHERE pc.media_type = 'cm' AND pc.completed_at >= (now() + (?)::interval)
            GROUP BY pc.media_id ORDER BY last_completed DESC
        """
        return await self._fetch_all(sql, [timedelta(days=-int(days))])

    # --- Blackouts ---

    async def upsert_blackout(
        self, friendly_token: str, *, reason: str, expires_at: str
    ) -> None:
        """Insert or extend a blackout. Keeps the latest (max) expiry on conflict."""
        await self._execute(
            """
            INSERT INTO catalog_blackouts (friendly_token, reason, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(friendly_token) DO UPDATE SET
                reason = excluded.reason,
                expires_at = GREATEST(catalog_blackouts.expires_at, excluded.expires_at)
            """,
            [friendly_token, reason, parse_dt(expires_at)],
        )

    async def prune_expired_blackouts(self) -> int:
        result = await self._execute(
            "DELETE FROM catalog_blackouts WHERE expires_at <= now()"
        )
        return result.rowcount or 0

    async def is_blackout(self, friendly_token: str) -> bool:
        row = await self._fetch_one(
            "SELECT 1 FROM catalog_blackouts "
            "WHERE friendly_token = ? AND expires_at > now() LIMIT 1",
            [friendly_token],
        )
        return row is not None

    async def count_active_blackouts(self) -> int:
        row = await self._fetch_one(
            "SELECT COUNT(*) AS cnt FROM catalog_blackouts WHERE expires_at > now()"
        )
        return row["cnt"] if row else 0

    async def list_active_blackouts(self) -> list[dict]:
        return await self._fetch_all(
            "SELECT friendly_token, reason, expires_at, created_at "
            "FROM catalog_blackouts WHERE expires_at > now() "
            "ORDER BY expires_at"
        )
