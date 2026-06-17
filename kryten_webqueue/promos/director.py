"""PromoDirector — just-in-time promo insertion driven by the poll cycle.

Inserts short promo clips between mutable content as playback advances:

* **General** promos (``channel_identity``, ``event``, ``mod_shoutout``) are
  inserted on a cadence (every N content items or every M minutes) between
  content, with weighted type selection and per-type random/sequential clip
  ordering.
* **Lead-ins** are attached immediately before a specific upcoming item:
  ``feature_presentation`` before a mutable-playlist movie
  (``duration_sec >= movie_threshold_seconds``) and ``viewers_choice`` before a
  pay-to-play item (any length). A paid movie is "paid" first, so it gets
  Viewer's Choice only, never Feature Presentation.

When both a general promo and a lead-in are due before the same item the order
is ``[general][lead-in][content]``.

Promos are added as CyTube **temp** items (via the throttled add helper) so they
auto-remove after playing and never accumulate across playlist loops. The
director is a no-op while an immutable scheduled event is locking the queue.
"""

import logging
import random
from datetime import datetime, UTC

from ..queue.ordering import _now_playing_uid
from ..playlists.bulk_add import add_item_throttled
from . import GENERAL_PROMO_TYPES

logger = logging.getLogger(__name__)


def _duration_seconds(item: dict) -> float:
    try:
        return float(item.get("duration_sec") or 0)
    except (TypeError, ValueError):
        return 0.0


async def remove_lead_in_for(*, api_gate, shadow, uid: int) -> int:
    """Remove any lead-in promo(s) attached to content ``uid``.

    Deletes the orphaned Feature-Presentation / Viewer's-Choice promo from CyTube
    and the shadow when its target content item is cancelled/refunded. Returns
    the number of lead-ins removed.
    """
    removed = 0
    for it in shadow.items:
        if it.get("is_promo") and it.get("lead_in_for_uid") == uid:
            promo_uid = it.get("uid")
            if promo_uid is None:
                continue
            try:
                await api_gate.playlist_delete(promo_uid)
            except Exception:
                logger.warning("Failed to delete lead-in promo uid=%s from CyTube", promo_uid, exc_info=True)
            try:
                await shadow.remove(promo_uid)
            except Exception:
                logger.warning("Failed to remove lead-in promo uid=%s from shadow", promo_uid, exc_info=True)
            removed += 1
    return removed


class PromoDirector:
    """Inserts promos just-in-time as the poller observes playback advancing."""

    def __init__(self, *, api_gate, db, shadow, config,
                 add_delay_sec: float = 0.5, add_max_retries: int = 2):
        self._api_gate = api_gate
        self._db = db
        self._shadow = shadow
        self._config = config
        self._add_delay_sec = add_delay_sec
        self._add_max_retries = add_max_retries

        # Cadence / selection state
        self._last_np_uid: int | None = None
        self._last_np_is_promo: bool = False
        self._content_since_last_general: int = 0
        self._last_general_at: datetime | None = None
        self._last_clip_token: dict[str, str] = {}   # promo_type -> last clip media_id
        self._seq_index: dict[str, int] = {}          # promo_type -> next sequential index

        # Injectable for tests
        self._rng = random.Random()
        self._now = lambda: datetime.now(UTC)

    def update_config(self, config) -> None:
        """Hot-apply a new promo config (from the admin panel) without restart.

        Cadence/selection counters are intentionally preserved; only the
        settings change. The next poll cycle uses the new thresholds, type
        weights, and per-type ordering.
        """
        self._config = config
        logger.info("PromoDirector config updated (enabled=%s)", getattr(config, "enabled", None))

    # --- Play-order helpers -------------------------------------------------

    @staticmethod
    def _index_of(uid, items: list[dict]) -> int | None:
        if uid is None:
            return None
        for i, it in enumerate(items):
            if it.get("uid") == uid:
                return i
        return None

    def _is_promo_uid(self, uid) -> bool:
        if uid is None:
            return False
        for it in self._shadow.items:
            if it.get("uid") == uid:
                return bool(it.get("is_promo"))
        return False

    def _next_content(self, np_uid, items: list[dict]) -> dict | None:
        """First non-promo item after now-playing in play order (wrapping)."""
        n = len(items)
        if n == 0:
            return None
        np_index = self._index_of(np_uid, items)
        start = 1 if np_index is not None else 0
        base = np_index if np_index is not None else 0
        for step in range(start, n + start):
            it = items[(base + step) % n]
            if it.get("uid") == np_uid:
                continue
            if it.get("is_promo"):
                continue
            return it
        return None

    def _direct_pred_uid(self, target_uid, items: list[dict]):
        """UID of the item immediately before target in play order."""
        t = self._index_of(target_uid, items)
        if t is None:
            return None
        n = len(items)
        return items[(t - 1) % n].get("uid")

    def _content_pred_uid(self, target_uid, items: list[dict]):
        """UID of the first non-promo item before target (skipping promos)."""
        t = self._index_of(target_uid, items)
        if t is None:
            return None
        n = len(items)
        for k in range(1, n):
            it = items[(t - k) % n]
            if not it.get("is_promo"):
                return it.get("uid")
        return items[(t - 1) % n].get("uid")

    @staticmethod
    def _has_lead_in(target_uid, items: list[dict]) -> bool:
        return any(
            it.get("is_promo") and it.get("lead_in_for_uid") == target_uid
            for it in items
        )

    def _has_general_before(self, target_uid, items: list[dict]) -> bool:
        """True if a general promo already sits in the promo block before target."""
        t = self._index_of(target_uid, items)
        if t is None:
            return False
        n = len(items)
        for k in range(1, n):
            it = items[(t - k) % n]
            if not it.get("is_promo"):
                return False
            if it.get("lead_in_for_uid") is None and it.get("promo_type") in GENERAL_PROMO_TYPES:
                return True
        return False

    # --- Decisions ----------------------------------------------------------

    def _leadin_type_for(self, target: dict) -> str | None:
        if target.get("is_pay"):
            return "viewers_choice"
        if _duration_seconds(target) >= self._config.movie_threshold_seconds:
            return "feature_presentation"
        return None

    def _general_due(self, now: datetime) -> bool:
        g = self._config.general
        if self._content_since_last_general >= g.every_n_items:
            logger.debug(
                "General promo due: content_since_last=%d >= every_n_items=%d",
                self._content_since_last_general, g.every_n_items,
            )
            return True
        if self._last_general_at is not None:
            elapsed_min = (now - self._last_general_at).total_seconds() / 60.0
            if elapsed_min >= g.every_m_minutes:
                logger.debug(
                    "General promo due: elapsed=%.1fmin >= every_m_minutes=%.1f",
                    elapsed_min, g.every_m_minutes,
                )
                return True
        return False

    async def _pick_general_type(self) -> str | None:
        """Weighted choice among enabled general types with a non-empty pool."""
        candidates: list[str] = []
        weights: list[int] = []
        skipped: list[str] = []
        for t in GENERAL_PROMO_TYPES:
            tc = self._config.types.get(t)
            if not tc or not tc.enabled or tc.weight <= 0:
                skipped.append(f"{t}(disabled/weight)")
                continue
            pool = await self._db.get_promo_pool_items(t)
            if not pool:
                skipped.append(f"{t}(empty-pool)")
                continue
            candidates.append(t)
            weights.append(tc.weight)
        if not candidates:
            logger.warning(
                "Promo general-type pick found no eligible types (skipped=%s)", skipped
            )
            return None
        chosen = self._rng.choices(candidates, weights=weights, k=1)[0]
        logger.debug(
            "Promo general-type pick: chosen=%s candidates=%s weights=%s skipped=%s",
            chosen, candidates, weights, skipped,
        )
        return chosen

    def _select_clip(self, promo_type: str, pool: list[dict]) -> dict:
        tc = self._config.types.get(promo_type)
        order = tc.order if tc else "random"
        pool_size = len(pool)
        if order == "sequential":
            raw_index = self._seq_index.get(promo_type, 0)
            idx = raw_index % pool_size
            self._seq_index[promo_type] = idx + 1
            clip = pool[idx]
            logger.debug(
                "Promo clip select [%s] order=sequential pool_size=%d raw_index=%d "
                "-> idx=%d media_id=%s title=%r next_index=%d",
                promo_type, pool_size, raw_index, idx,
                clip.get("media_id"), clip.get("title"), idx + 1,
            )
        else:
            clip = self._rng.choice(pool)
            repeated = False
            if self._config.general.no_repeat and pool_size > 1:
                last = self._last_clip_token.get(promo_type)
                if clip.get("media_id") == last:
                    repeated = True
                    # Draw from the rest of the pool so the no-repeat guarantee
                    # always holds (bounded random retries could otherwise give
                    # up and return a repeat).
                    alternatives = [c for c in pool if c.get("media_id") != last]
                    if alternatives:
                        clip = self._rng.choice(alternatives)
            logger.debug(
                "Promo clip select [%s] order=random pool_size=%d no_repeat=%s "
                "avoided_repeat=%s -> media_id=%s title=%r",
                promo_type, pool_size, self._config.general.no_repeat,
                repeated, clip.get("media_id"), clip.get("title"),
            )
        if pool_size == 1:
            logger.warning(
                "Promo pool for %r has a single clip; it will repeat every time "
                "(media_id=%s). Add more clips to vary this promo type.",
                promo_type, clip.get("media_id"),
            )
        self._last_clip_token[promo_type] = clip.get("media_id")
        return clip

    # --- Insertion ----------------------------------------------------------

    def _shadow_pos_after(self, after_uid) -> int:
        items = self._shadow.items
        idx = self._index_of(after_uid, items)
        return (idx + 1) if idx is not None else len(items)

    async def _insert_promo(self, promo_type: str, *, after_uid, target_uid, lead_in: bool,
                            pool: list[dict] | None = None) -> int | None:
        tc = self._config.types.get(promo_type)
        if not tc or not tc.enabled:
            return None
        if pool is None:
            pool = await self._db.get_promo_pool_items(promo_type)
        if not pool:
            return None
        clip = self._select_clip(promo_type, pool)

        try:
            add_result = await add_item_throttled(
                self._api_gate,
                media_type=clip["media_type"],
                media_id=clip["media_id"],
                position="end",
                max_retries=self._add_max_retries,
                retry_delay_sec=self._add_delay_sec,
            )
        except Exception:
            logger.warning("Promo add failed (%s)", promo_type, exc_info=True)
            return None
        if not add_result or not add_result.get("success"):
            logger.warning(
                "Promo add rejected (%s media_id=%s): result=%r",
                promo_type, clip.get("media_id"), add_result,
            )
            return None
        uid = add_result.get("uid")
        if uid is None:
            # CyTube accepted the add but api-gate could not resolve a uid. The
            # shadow can't track this promo, so the idempotency guard will never
            # see it -> the cadence counter never resets and we'd re-add every
            # poll. Bail loudly so this shows up in logs instead of silently
            # spamming the queue.
            logger.error(
                "Promo add for %s (media_id=%s) returned success but NO uid; "
                "cannot track in shadow (result=%r). Skipping shadow insert to "
                "avoid an untracked, repeatable insertion.",
                promo_type, clip.get("media_id"), add_result,
            )
            return None

        if after_uid is not None:
            try:
                await self._api_gate.playlist_move(uid, after_uid)
                logger.debug("Promo move ok: uid=%s after_uid=%s", uid, after_uid)
            except Exception:
                logger.warning("Promo move failed (uid=%s after=%s)", uid, after_uid, exc_info=True)

        item = {
            "uid": uid,
            "title": clip.get("title") or "",
            "media_type": clip["media_type"],
            "media_id": clip["media_id"],
            "duration_sec": clip.get("duration_sec"),
            "is_pay": False,
            "paid_by": None,
            "tier": None,
            "z_cost": None,
            "schedule_id": None,
            "is_promo": True,
            "promo_type": promo_type,
            "lead_in_for_uid": target_uid if lead_in else None,
        }
        pos = self._shadow_pos_after(after_uid)
        await self._shadow.insert_at(item, pos)
        logger.info(
            "Inserted %s promo uid=%s (%s) %s",
            promo_type, uid, clip.get("title"),
            f"before content uid={target_uid}" if lead_in else "(general)",
        )
        return uid

    # --- Main entry ---------------------------------------------------------

    async def insert_viewers_choice(self, content_uid: int) -> int | None:
        """Insert a Viewer's-Choice lead-in immediately before a paid item.

        Called synchronously from the pay-insertion path so the promo is in place
        before a "play next" item can begin (which may happen before the next
        poll). Idempotent: skips if a lead-in already precedes ``content_uid``.
        """
        if not self._config.enabled:
            return None
        items = self._shadow.items
        if self._has_lead_in(content_uid, items):
            logger.debug(
                "Viewer's-Choice lead-in already present for paid uid=%s; skipping",
                content_uid,
            )
            return None
        pred = self._direct_pred_uid(content_uid, items)
        if pred is None:
            logger.debug(
                "Viewer's-Choice skipped: no predecessor for paid uid=%s", content_uid
            )
            return None
        logger.info(
            "Viewer's-Choice lead-in (pay path) for paid uid=%s after_uid=%s",
            content_uid, pred,
        )
        return await self._insert_promo(
            "viewers_choice", after_uid=pred, target_uid=content_uid, lead_in=True
        )

    async def on_poll(self) -> None:
        """Evaluate and insert promos once. Called by the poller each cycle."""
        cfg = self._config
        if not cfg.enabled:
            return

        np_uid = await _now_playing_uid(self._api_gate, self._shadow)
        np_is_promo = self._is_promo_uid(np_uid)

        # Advance detection: a finished *content* item bumps the cadence counter.
        if np_uid != self._last_np_uid:
            if self._last_np_uid is not None and not self._last_np_is_promo:
                self._content_since_last_general += 1
            logger.debug(
                "Now-playing advanced: %s -> %s (is_promo=%s) content_since_last_general=%d",
                self._last_np_uid, np_uid, np_is_promo, self._content_since_last_general,
            )
            self._last_np_uid = np_uid
            self._last_np_is_promo = np_is_promo

        now = self._now()
        if self._last_general_at is None:
            self._last_general_at = now

        # No-op during an immutable scheduled event (curated content plays as built).
        try:
            if await self._db.is_event_lock_active():
                logger.debug("Promo on_poll skipped: immutable event lock active")
                return
        except Exception:
            logger.warning(
                "Promo on_poll: is_event_lock_active() check failed; skipping this cycle",
                exc_info=True,
            )
            return

        items = self._shadow.items
        if not items:
            return
        target = self._next_content(np_uid, items)
        if target is None:
            logger.debug("Promo on_poll: no upcoming content item found")
            return
        target_uid = target.get("uid")
        direct_pred = self._direct_pred_uid(target_uid, items)
        content_pred = self._content_pred_uid(target_uid, items)

        # Lead-in first so a same-cycle general promo lands ahead of it.
        leadin_type = self._leadin_type_for(target)
        if leadin_type and not self._has_lead_in(target_uid, items):
            logger.info(
                "Promo lead-in due: type=%s target_uid=%s (is_pay=%s dur=%.0fs) after_uid=%s",
                leadin_type, target_uid, target.get("is_pay"),
                _duration_seconds(target), direct_pred,
            )
            await self._insert_promo(
                leadin_type, after_uid=direct_pred, target_uid=target_uid, lead_in=True
            )
        elif leadin_type:
            logger.debug(
                "Promo lead-in already present for target_uid=%s (type=%s)",
                target_uid, leadin_type,
            )

        # General cadence promo.
        due = self._general_due(now)
        has_general = self._has_general_before(target_uid, self._shadow.items)
        if due and not has_general:
            chosen = await self._pick_general_type()
            if chosen:
                pool = await self._db.get_promo_pool_items(chosen)
                inserted = await self._insert_promo(
                    chosen, after_uid=content_pred, target_uid=None, lead_in=False, pool=pool
                )
                if inserted is not None:
                    logger.info(
                        "Promo general inserted: type=%s uid=%s before content_uid=%s "
                        "(reset cadence counter from %d)",
                        chosen, inserted, target_uid, self._content_since_last_general,
                    )
                    self._content_since_last_general = 0
                    self._last_general_at = now
        elif due and has_general:
            logger.debug(
                "Promo general due but one already precedes target_uid=%s; "
                "skipping (idempotency guard)",
                target_uid,
            )
