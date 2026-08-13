import logging

logger = logging.getLogger(__name__)


class _PlaylistsMixin:
    """Saved playlists, promo pools, schedules, and active schedule methods."""

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
        cursor = await self._db.execute(
            "INSERT INTO saved_playlists (name, description, is_immutable, created_by, promo_type) VALUES (?, ?, ?, ?, ?)",
            [name, description, int(is_immutable), created_by, promo_type],
        )
        await self._db.commit()
        return cursor.lastrowid

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
            "UPDATE saved_playlists SET name=?, description=?, is_immutable=?, promo_type=?, updated_at=datetime('now') WHERE id=?",
            [name, description, int(is_immutable), promo_type, playlist_id],
        )

    async def get_promo_pools(self) -> list[dict]:
        """All saved playlists that are designated promo pools (promo_type set)."""
        return await self._fetch_all(
            "SELECT * FROM saved_playlists WHERE promo_type IS NOT NULL ORDER BY promo_type, name"
        )

    async def get_promo_pool_items(self, promo_type: str) -> list[dict]:
        """Union of clips across every playlist tagged with ``promo_type``.

        Returns the playlist items in a stable order (playlist id, then
        position) so ``sequential`` selection is deterministic.
        """
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
        await self._db.execute(
            "DELETE FROM saved_playlist_items WHERE playlist_id=?", [playlist_id]
        )
        for i, item in enumerate(items):
            await self._db.execute(
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
        await self._db.commit()

    async def append_playlist_item(self, playlist_id: int, item: dict) -> int:
        """Append a single item to the end of a playlist. Returns new item count."""
        row = await self._fetch_one(
            "SELECT COALESCE(MAX(position), -1) AS pos, COUNT(*) AS cnt "
            "FROM saved_playlist_items WHERE playlist_id=?",
            [playlist_id],
        )
        next_pos = (row["pos"] + 1) if row else 0
        count = (row["cnt"] if row else 0) + 1
        await self._db.execute(
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
        await self._db.execute(
            "UPDATE saved_playlists SET updated_at=datetime('now') WHERE id=?",
            [playlist_id],
        )
        await self._db.commit()
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
            await self._db.execute(
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
            await self._db.execute(
                "UPDATE saved_playlists SET updated_at=datetime('now') WHERE id=?",
                [playlist_id],
            )
        await self._db.commit()
        return added

    async def rotate_playlist_item_to_bottom(
        self, media_id: str, media_type: str = "cm"
    ) -> int:
        """Move a played item to the end of every mutable playlist containing it.

        Played items sink to the bottom so the next fire always loads the
        oldest-played item last. Idempotent when the item is already at
        MAX(position). Returns the number of playlists rotated.
        """
        playlists = await self._fetch_all(
            """
            SELECT spi.playlist_id
            FROM saved_playlist_items spi
            JOIN saved_playlists sp ON sp.id = spi.playlist_id
            WHERE spi.media_id = ? AND spi.media_type = ?
              AND sp.is_immutable = 0 AND sp.promo_type IS NULL
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
            await self._db.execute(
                "DELETE FROM saved_playlist_items WHERE playlist_id=?", [playlist_id]
            )
            for new_pos, item in enumerate(reordered):
                await self._db.execute(
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
            await self._db.execute(
                "UPDATE saved_playlists SET updated_at=datetime('now') WHERE id=?",
                [playlist_id],
            )
            await self._db.commit()
            logger.info("Rotated %r to bottom of playlist %d", media_id, playlist_id)
            rotated += 1
        return rotated

    async def get_most_recent_playlist(self, created_by: str) -> dict | None:
        """The given admin's most recently *created* saved playlist, if any."""
        return await self._fetch_one(
            "SELECT * FROM saved_playlists WHERE created_by=? ORDER BY created_at DESC, id DESC LIMIT 1",
            [created_by],
        )

    async def get_playlist_by_name(self, name: str, created_by: str) -> dict | None:
        """Match an existing saved playlist by exact name + creator.

        Used for idempotent re-imports (e.g. the fetchurls job replacing a
        section playlist's items rather than creating a duplicate).
        """
        return await self._fetch_one(
            "SELECT * FROM saved_playlists WHERE name=? AND created_by=? ORDER BY id LIMIT 1",
            [name, created_by],
        )

    async def get_playlist_by_name_any(self, name: str) -> dict | None:
        """Match an existing saved playlist by exact name, any creator.

        Used for the fetchurls fixed section playlists ("Friday Night",
        "Saturday Morning", "Saturday Night") which pre-exist (possibly created
        by a different admin) and must be reused/replaced in place rather than
        duplicated.
        """
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
        cursor = await self._db.execute(
            f"INSERT INTO playlist_schedules ({keys}) VALUES ({placeholders})",
            list(kwargs.values()),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def update_schedule(self, schedule_id: int, **kwargs):
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
            [fired_at, schedule_id],
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
            "INSERT OR REPLACE INTO active_schedule "
            "(id, schedule_id, playlist_id, is_immutable, started_at, estimated_end_at, "
            "last_item_uid, last_item_media_id, lock_disabled) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, 0)",
            [
                schedule_id,
                playlist_id,
                int(is_immutable),
                started_at,
                estimated_end_at,
                last_item_uid,
                last_item_media_id,
            ],
        )

    async def clear_active_schedule(self):
        await self._execute("DELETE FROM active_schedule WHERE id=1")

    async def disable_active_lock(self):
        """Lift the in-progress scheduled-event lock without ending the event.

        Keeps the ``active_schedule`` row (so banners/state still show the event)
        and leaves the underlying schedule armed for future occurrences.
        """
        await self._execute("UPDATE active_schedule SET lock_disabled=1 WHERE id=1")

    async def is_event_lock_active(self) -> bool:
        """True while an immutable scheduled event is locking pay-to-play.

        Auto-lifts (via :meth:`disable_active_lock`, set when the last scheduled
        item begins playing) and respects an admin's manual unlock.
        """
        row = await self.get_active_schedule()
        if not row:
            return False
        if not row.get("is_immutable"):
            return False
        return not row.get("lock_disabled")

    # --- Pre-fire lock check ---

    async def is_pre_fire_lock_active(self) -> bool:
        # NOTE: fire_at must be wrapped in datetime() on BOTH sides. fire_at is
        # stored as a raw ISO string ('2026-06-21T15:00:00+00:00' or '...Z'),
        # whose 'T' separator sorts lexically AFTER the space-separated string
        # returned by datetime('now'). A bare `fire_at > datetime('now')` is a
        # string comparison that stays true from fire time until the calendar
        # day rolls over, so the lock lingered until midnight instead of
        # releasing at fire_at.
        row = await self._fetch_one(
            """
            SELECT 1 FROM playlist_schedules
            WHERE is_active = 1
              AND lock_disabled = 0
              AND datetime(fire_at, '-' || pre_fire_lock_minutes || ' minutes') <= datetime('now')
              AND datetime(fire_at) > datetime('now')
            LIMIT 1
        """
        )
        return row is not None

    async def get_active_pre_fire_lock(self) -> dict | None:
        """Return the schedule whose pre-fire lock window is currently active.

        Used to give users a specific "pay-to-play closes before [event]"
        message instead of a generic locked error.
        """
        return await self._fetch_one(
            """
            SELECT * FROM playlist_schedules
            WHERE is_active = 1
              AND lock_disabled = 0
              AND datetime(fire_at, '-' || pre_fire_lock_minutes || ' minutes') <= datetime('now')
              AND datetime(fire_at) > datetime('now')
            ORDER BY datetime(fire_at)
            LIMIT 1
        """
        )

    async def disable_active_pre_fire_locks(self) -> int:
        """Lift ALL currently-active pre-fire locks in a single operation.

        Sets ``lock_disabled = 1`` for every schedule whose pre-fire window is
        open right now, so one admin action ends the lockout even when more than
        one schedule's window overlaps. Recurring schedules reset
        ``lock_disabled`` to 0 when they re-arm, so future firings still lock.
        Returns the number of schedules affected.
        """
        cursor = await self._db.execute(
            """
            UPDATE playlist_schedules
            SET lock_disabled = 1
            WHERE is_active = 1
              AND lock_disabled = 0
              AND datetime(fire_at, '-' || pre_fire_lock_minutes || ' minutes') <= datetime('now')
              AND datetime(fire_at) > datetime('now')
        """
        )
        await self._db.commit()
        return cursor.rowcount

    async def get_next_schedule(self) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM playlist_schedules WHERE is_active=1 AND datetime(fire_at) > datetime('now') ORDER BY datetime(fire_at) LIMIT 1"
        )
