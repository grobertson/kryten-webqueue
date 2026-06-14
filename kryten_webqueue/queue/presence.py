import asyncio
import logging
from datetime import datetime, UTC

from .ordering import refund_item, _now_playing_uid

logger = logging.getLogger(__name__)


class PresenceRefundMonitor:
    """Cancel & refund pending paid items when their owner leaves or goes AFK.

    Runs its own loop (decoupled from the 3s state poll) on
    ``check_interval_seconds``. Each cycle it looks at the pending paid items in
    the shadow (``is_pay`` and not the currently-playing item), groups them by
    owner, and asks api-gate for each owner's presence. An owner who is gone
    (``online is False``) or AFK (``meta.afk``) starts a grace clock; if they are
    still gone/AFK after ``grace_seconds`` all of their pending paid items are
    refunded and removed. If they return before grace elapses the items are kept.

    Inconclusive presence lookups (api-gate / robot hiccups) never start the
    grace clock and never cancel — they are ignored for that cycle.
    """

    def __init__(self, *, api_gate, shadow, db, ws_manager, config):
        self._api_gate = api_gate
        self._shadow = shadow
        self._db = db
        self._ws_manager = ws_manager
        self._config = config
        self._task: asyncio.Task | None = None
        # owner username -> (first_seen_missing, reason)
        self._missing_since: dict[str, tuple[datetime, str]] = {}

    async def start(self):
        if not self._config.enabled:
            logger.info("PresenceRefundMonitor disabled by config")
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "PresenceRefundMonitor started "
            f"(interval={self._config.check_interval_seconds}s, "
            f"grace={self._config.grace_seconds}s, "
            f"on_leave={self._config.on_leave}, on_afk={self._config.on_afk})"
        )

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("PresenceRefundMonitor stopped")

    async def _loop(self):
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Presence check error: {e}")
            await asyncio.sleep(self._config.check_interval_seconds)

    def _pending_paid_by_owner(self, np_uid: int | None) -> dict[str, list[dict]]:
        """Map owner username -> their pending paid shadow items (excludes now-playing)."""
        owners: dict[str, list[dict]] = {}
        for item in self._shadow.items:
            if not item.get("is_pay"):
                continue
            uid = item.get("uid")
            if np_uid is not None and uid == np_uid:
                continue  # never cancel the currently-playing item
            owner = item.get("paid_by")
            if not owner:
                continue
            owners.setdefault(owner, []).append(item)
        return owners

    @staticmethod
    def _classify(data: dict | None) -> str:
        """Classify an owner's presence: 'gone', 'afk', or 'present'."""
        if not data or data.get("online") is False:
            return "gone"
        meta = data.get("meta") or {}
        if meta.get("afk"):
            return "afk"
        return "present"

    async def check_once(self) -> int:
        """Evaluate owners once; return the number of items cancelled this cycle."""
        np_uid = await _now_playing_uid(self._api_gate, self._shadow)
        owners = self._pending_paid_by_owner(np_uid)

        # Drop grace entries for owners with no pending paid items anymore.
        for tracked in list(self._missing_since):
            if tracked not in owners:
                self._missing_since.pop(tracked, None)

        now = datetime.now(UTC)
        cancelled = 0

        for owner, items in owners.items():
            try:
                data = await self._api_gate.get_user(owner)
            except Exception:
                # Inconclusive (robot/NATS hiccup): do not start the grace clock
                # and do not clear an existing one — just skip this cycle.
                logger.debug("Presence lookup failed for %s; skipping", owner, exc_info=True)
                continue

            status = self._classify(data)
            reason = "owner_left" if status == "gone" else "owner_afk"
            actionable = (
                (status == "gone" and self._config.on_leave)
                or (status == "afk" and self._config.on_afk)
            )

            if not actionable:
                # Present, or the relevant trigger is disabled: keep the items.
                self._missing_since.pop(owner, None)
                continue

            first = self._missing_since.get(owner)
            if first is None:
                self._missing_since[owner] = (now, reason)
                continue

            since, _first_reason = first
            if (now - since).total_seconds() < self._config.grace_seconds:
                continue  # still within grace

            # Grace elapsed and still gone/AFK: cancel all pending paid items.
            self._missing_since.pop(owner, None)
            for item in items:
                if await self._cancel_item(item, reason):
                    cancelled += 1

        if cancelled:
            await self._broadcast_state()
        return cancelled

    async def _cancel_item(self, item: dict, reason: str) -> bool:
        """Refund + remove a single pending paid item. Returns True on success."""
        uid = item.get("uid")
        if uid is None:
            return False
        refunded = await refund_item(
            api_gate=self._api_gate, db=self._db, uid=uid, reason=reason
        )
        if not refunded:
            logger.warning(
                "Presence cancel: no refundable spend for uid=%s (owner=%s); skipping",
                uid, item.get("paid_by"),
            )
            return False
        try:
            await self._api_gate.playlist_delete(uid)
        except Exception:
            logger.warning("Presence cancel: failed to delete uid=%s from CyTube", uid, exc_info=True)
        try:
            await self._shadow.remove(uid)
        except Exception:
            logger.warning("Presence cancel: failed to remove uid=%s from shadow", uid, exc_info=True)
        try:
            from ..promos.director import remove_lead_in_for
            await remove_lead_in_for(api_gate=self._api_gate, shadow=self._shadow, uid=uid)
        except Exception:
            logger.debug("Presence cancel: lead-in cleanup failed for uid=%s", uid, exc_info=True)
        await self._notify_owner(item, reason)
        logger.info(
            "Presence cancel: refunded & removed uid=%s (%s) owner=%s reason=%s",
            uid, item.get("title"), item.get("paid_by"), reason,
        )
        return True

    async def _notify_owner(self, item: dict, reason: str):
        """PM the owner that their paid item was cancelled & refunded.

        Best-effort: a failed PM never blocks the cancel/refund itself.
        """
        if not getattr(self._config, "notify_user", False):
            return
        owner = item.get("paid_by")
        if not owner:
            return
        why = "you left the channel" if reason == "owner_left" else "you went AFK"
        title = item.get("title") or "your queued item"
        try:
            await self._api_gate.send_pm(
                owner,
                f"Your queued item \"{title}\" was cancelled and refunded because {why}.",
            )
        except Exception:
            logger.debug("Presence cancel: failed to PM %s", owner, exc_info=True)

    async def _broadcast_state(self):
        try:
            state = await self._shadow.get_enriched_state(self._db)
            await self._ws_manager.broadcast({"type": "queue_state", "data": state})
        except Exception:
            logger.debug("Failed to broadcast queue state after presence cancel", exc_info=True)
