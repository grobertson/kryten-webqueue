import asyncio
import logging
from datetime import datetime, timedelta, UTC

logger = logging.getLogger(__name__)


def _to_seconds(value) -> float:
    """Convert a duration value to seconds. Handles int/float and 'HH:MM:SS' strings."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    parts = s.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(s)
    except (ValueError, IndexError):
        return 0.0


class QueueShadow:
    """Local mirror of the CyTube playlist state."""

    def __init__(self, db, now_playing: dict | None = None):
        self._db = db
        self._items: list[dict] = []
        self._now_playing: dict | None = now_playing
        self._lock = asyncio.Lock()

    @property
    def items(self) -> list[dict]:
        return self._items.copy()

    @property
    def now_playing(self) -> dict | None:
        return self._now_playing

    async def load_from_db(self):
        """Load initial state from database."""
        self._items = await self._db.get_shadow_items()

    async def apply_poll_result(self, playlist_items: list[dict], now_playing: dict | None):
        """Reconcile polled state with local shadow."""
        async with self._lock:
            self._now_playing = now_playing

            polled_uids = {item["uid"] for item in playlist_items}
            local_uids = {item["uid"] for item in self._items}

            # Removed items
            removed = local_uids - polled_uids
            if removed:
                await self._db.remove_shadow_items(removed)

            # Rebuild item list from poll result, preserving local metadata
            local_map = {item["uid"]: item for item in self._items}
            new_items = []

            for pos, polled in enumerate(playlist_items):
                uid = polled["uid"]
                # CyTube playlist items nest the media metadata under a "media"
                # key ({uid, temp, queueby, media: {id, title, seconds, type}}).
                # Fall back to flat keys for forward/backward compatibility.
                media = polled.get("media") if isinstance(polled.get("media"), dict) else polled
                title = media.get("title") or polled.get("title") or ""
                media_type = media.get("type") or polled.get("type") or "unknown"
                media_id = media.get("id") or polled.get("id") or ""
                duration_sec = _to_seconds(media.get("seconds", media.get("duration")))
                queueby = polled.get("queueby") or None

                if uid in local_map:
                    # Preserve local metadata, update position; backfill any
                    # fields we never captured locally (e.g. title/duration for
                    # items first added by an external client or a prior run).
                    merged = {**local_map[uid], "position": pos}
                    if not merged.get("title"):
                        merged["title"] = title
                    if not merged.get("duration_sec"):
                        merged["duration_sec"] = duration_sec
                    if not merged.get("media_id"):
                        merged["media_id"] = media_id
                    if not merged.get("media_type") or merged.get("media_type") == "unknown":
                        merged["media_type"] = media_type
                else:
                    # New item from external source
                    merged = {
                        "uid": uid,
                        "position": pos,
                        "title": title,
                        "media_type": media_type,
                        "media_id": media_id,
                        "duration_sec": duration_sec,
                        "is_pay": False,
                        "paid_by": queueby,
                        "tier": None,
                        "z_cost": None,
                        "schedule_id": None,
                        "added_at": datetime.now(UTC).isoformat(),
                    }
                    await self._db.upsert_shadow_item(merged)

                new_items.append(merged)

            self._items = new_items
            await self._recalculate_estimated_starts()

    async def _recalculate_estimated_starts(self):
        """Recalculate estimated start times based on position and now-playing.

        Emits BOTH an absolute UTC timestamp (``estimated_start_at``, kept for
        compatibility) and a clock-independent relative offset
        (``estimated_start_in_sec``) = seconds from "now" until the item plays.

        The relative offset is the authoritative value the UI should use: it is
        immune to server clock skew / timezone misconfiguration, which is the
        usual cause of ETAs appearing shifted by a whole UTC offset. The browser
        computes the wall-clock time from its own clock (Date.now() + offset).
        """
        if not self._items:
            return

        # Offset (seconds from now) until the head of the queue starts playing.
        offset = 0.0
        if self._now_playing:
            np_total = _to_seconds(self._now_playing.get("seconds", self._now_playing.get("duration")))
            remaining = np_total - _to_seconds(self._now_playing.get("currentTime"))
            offset = max(0.0, remaining)

        now = datetime.now(UTC)
        for item in self._items:
            item["estimated_start_in_sec"] = round(offset)
            # Absolute timestamp retained for compatibility; relative offset is
            # what the UI renders.
            item["estimated_start_at"] = (now + timedelta(seconds=offset)).isoformat()
            offset += _to_seconds(item.get("duration_sec"))

    async def insert_at(self, item: dict, position: int):
        """Insert a new item at given position in local shadow."""
        async with self._lock:
            item["position"] = position
            self._items.insert(position, item)
            # Re-index
            for i, it in enumerate(self._items):
                it["position"] = i
            await self._db.upsert_shadow_item(item)
            await self._recalculate_estimated_starts()

    async def remove(self, uid: int):
        """Remove item from shadow."""
        async with self._lock:
            self._items = [it for it in self._items if it["uid"] != uid]
            for i, it in enumerate(self._items):
                it["position"] = i
            await self._db.remove_shadow_items({uid})
            await self._recalculate_estimated_starts()

    def get_queue_state(self) -> dict:
        """Return serializable queue state for WebSocket broadcast."""
        return {
            "items": self._items,
            "now_playing": self._now_playing,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    async def get_enriched_state(self, db) -> dict:
        """Queue state augmented with catalog metadata (cover art, etc.)."""
        state = self.get_queue_state()
        items = state.get("items") or []
        now_playing = state.get("now_playing")

        tokens, manifests = [], []
        for it in items:
            if it.get("friendly_token"):
                tokens.append(it["friendly_token"])
            if it.get("media_id"):
                manifests.append(it["media_id"])
        if now_playing:
            if now_playing.get("friendly_token"):
                tokens.append(now_playing["friendly_token"])
            if now_playing.get("id"):
                manifests.append(now_playing["id"])

        try:
            lookup = await db.get_catalog_brief(tokens, manifests)
        except Exception:
            logger.warning("Failed to enrich queue state with catalog metadata", exc_info=True)
            return state

        def _meta_for(obj: dict, id_key: str) -> dict | None:
            return lookup.get(obj.get("friendly_token") or "") or lookup.get(obj.get(id_key) or "")

        enriched_items = []
        for it in items:
            meta = _meta_for(it, "media_id")
            merged = dict(it)
            if meta:
                merged.setdefault("cover_art_path", meta.get("cover_art_path"))
                merged.setdefault("thumbnail_url", meta.get("thumbnail_url"))
                if not merged.get("title") or merged.get("title") == "Unknown":
                    merged["title"] = meta.get("title") or merged.get("title")
                if not merged.get("friendly_token"):
                    merged["friendly_token"] = meta.get("friendly_token")
            enriched_items.append(merged)
        state["items"] = enriched_items

        if now_playing:
            meta = _meta_for(now_playing, "id")
            np = dict(now_playing)
            if meta:
                np.setdefault("cover_art_path", meta.get("cover_art_path"))
                np.setdefault("thumbnail_url", meta.get("thumbnail_url"))
                if not np.get("friendly_token"):
                    np["friendly_token"] = meta.get("friendly_token")
            # Ensure now-playing carries a playlist uid so the frontend can
            # highlight the matching queue item. CyTube's changeMedia payload
            # lacks a uid; recover it by matching media id/type against the
            # shadow playlist (whose items do carry uids).
            if np.get("uid") is None:
                np_id = np.get("id")
                np_type = np.get("type")
                if np_id is not None:
                    for it in enriched_items:
                        if it.get("media_id") == np_id and (
                            np_type is None or it.get("media_type") == np_type
                        ):
                            np["uid"] = it.get("uid")
                            break
            # Attach description + category/tag names for the now-playing card.
            if np.get("friendly_token"):
                try:
                    facets = await db.get_item_facets(np["friendly_token"])
                    np.setdefault("description", facets.get("description"))
                    np["categories"] = [c["name"] for c in (facets.get("categories") or [])]
                    np["tags"] = facets.get("tags") or []
                except Exception:
                    logger.debug("Failed to enrich now-playing facets", exc_info=True)
            state["now_playing"] = np

        return state

