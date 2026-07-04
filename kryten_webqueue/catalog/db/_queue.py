from datetime import datetime, UTC


class _QueueMixin:
    """Queue shadow, spend requests, and queue history methods."""

    # --- Queue shadow ---

    async def get_shadow_items(self) -> list[dict]:
        return await self._fetch_all("SELECT * FROM queue_shadow ORDER BY position ASC")

    async def upsert_shadow_item(self, item: dict):
        sql = """
            INSERT OR REPLACE INTO queue_shadow
                (uid, position, title, media_type, media_id, duration_sec, is_pay, paid_by, tier, z_cost, schedule_id,
                 is_promo, promo_type, lead_in_for_uid, added_at)
            VALUES (:uid, :position, :title, :media_type, :media_id, :duration_sec, :is_pay,
                    :paid_by, :tier, :z_cost, :schedule_id,
                    :is_promo, :promo_type, :lead_in_for_uid, :added_at)
        """
        defaults = {"paid_by": None, "tier": None, "z_cost": None, "schedule_id": None,
                    "friendly_token": None, "is_promo": 0, "promo_type": None,
                    "lead_in_for_uid": None, "added_at": datetime.now(UTC).isoformat()}
        row = {**defaults, **item}
        row["is_promo"] = int(bool(row.get("is_promo")))
        await self._db.execute(sql, row)
        await self._db.commit()

    async def remove_shadow_items(self, uids: set[int]):
        placeholders = ",".join("?" * len(uids))
        await self._execute(f"DELETE FROM queue_shadow WHERE uid IN ({placeholders})", list(uids))

    async def update_shadow_position(self, uid: int, position: int):
        await self._db.execute("UPDATE queue_shadow SET position=? WHERE uid=?", [position, uid])
        await self._db.commit()

    async def update_shadow_estimated_start(self, uid: int, estimated: str):
        await self._db.execute(
            "UPDATE queue_shadow SET estimated_start_at=? WHERE uid=?", [estimated, uid]
        )
        await self._db.commit()

    async def get_last_pay_uid(self) -> int | None:
        row = await self._fetch_one(
            "SELECT uid FROM queue_shadow WHERE is_pay = 1 ORDER BY position DESC LIMIT 1"
        )
        return row["uid"] if row else None

    async def get_shadow_position_after(self, after_uid: int) -> int:
        row = await self._fetch_one("SELECT position FROM queue_shadow WHERE uid = ?", [after_uid])
        return (row["position"] + 1) if row else 0

    async def get_pay_items(self) -> list[dict]:
        return await self._fetch_all("SELECT * FROM queue_shadow WHERE is_pay = 1 ORDER BY position ASC")

    # --- Spend requests ---

    async def save_spend_request(self, request_id: str, *, username: str, uid: int | None,
                                 friendly_token: str | None = None, tier: str | None = None,
                                 z_cost: int | None = None):
        sql = """
            INSERT OR IGNORE INTO spend_requests (request_id, username, uid, friendly_token, tier, z_cost)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        await self._execute(sql, [request_id, username, uid, friendly_token, tier, z_cost])

    async def get_request_id_for_uid(self, uid: int) -> str | None:
        row = await self._fetch_one(
            "SELECT request_id FROM spend_requests WHERE uid = ? AND refunded = 0 LIMIT 1", [uid]
        )
        return row["request_id"] if row else None

    async def mark_spend_refunded(self, request_id: str):
        await self._execute(
            "UPDATE spend_requests SET refunded=1, refunded_at=datetime('now') WHERE request_id=?",
            [request_id],
        )

    # --- Queue history ---

    async def add_queue_history(self, *, username: str, friendly_token: str | None,
                                title: str | None, tier: str, z_cost: int):
        await self._execute(
            "INSERT INTO queue_history (username, friendly_token, title, tier, z_cost) VALUES (?, ?, ?, ?, ?)",
            [username, friendly_token, title, tier, z_cost],
        )

    async def get_user_queue_history(self, username: str, limit: int = 50, offset: int = 0) -> list[dict]:
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
