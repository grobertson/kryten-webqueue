"""Play-completion recorder.

Watches the now-playing item across poll cycles and records a completion only
when the now-playing pointer moves off an item that had progressed past a
threshold (default 50%) of its duration. This is the signal that drives hiding
recently-played catalog items from regular users.

Why threshold-on-advance rather than "item left the queue":

* An item queued and then **refunded/removed** by an admin is pulled before it
  ever reaches the now-playing slot, so it is never tracked here and can't be
  mistaken for "played".
* An admin **skipping the currently-playing item** early leaves its observed
  progress below the threshold, so it also doesn't count.
* A natural finish leaves progress near the end (well past the threshold), so it
  is recorded exactly once when playback advances.
"""

import logging

logger = logging.getLogger(__name__)


def _to_seconds(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class CompletionRecorder:
    """Records genuine play-completions from the poll loop."""

    def __init__(self, *, db, completion_threshold: float = 0.5):
        self._db = db
        self._threshold = completion_threshold
        # State of the item currently occupying the now-playing slot.
        self._cur_key: tuple | None = None  # (media_type, media_id)
        self._cur_token: str | None = None  # resolved catalog friendly_token
        self._cur_duration: float = 0.0
        self._max_ct: float = 0.0  # furthest observed currentTime

    async def on_poll(self, now_playing: dict | None) -> None:
        """Called once per poll cycle with the latest now-playing payload."""
        np = now_playing or {}
        media_id = np.get("id")
        media_type = np.get("type")
        key = (media_type, media_id) if media_id else None

        if key == self._cur_key:
            # Same item still playing — track its furthest progress and fill in a
            # duration if it wasn't known when it started.
            ct = _to_seconds(np.get("currentTime"))
            if ct > self._max_ct:
                self._max_ct = ct
            if not self._cur_duration:
                self._cur_duration = _to_seconds(
                    np.get("seconds") or np.get("duration")
                )
            return

        # Now-playing advanced (or stopped): finalize the item that just left.
        if self._cur_key is not None:
            await self._finalize(self._cur_token, self._cur_duration, self._max_ct)

        # Begin tracking the newly-playing item.
        self._cur_key = key
        self._cur_token = None
        self._max_ct = _to_seconds(np.get("currentTime"))
        self._cur_duration = _to_seconds(np.get("seconds") or np.get("duration"))
        if key is not None:
            try:
                meta = await self._db.resolve_media(media_id)
            except Exception:
                logger.debug(
                    "Failed to resolve now-playing media %r", media_id, exc_info=True
                )
                meta = None
            if meta:
                self._cur_token = meta.get("friendly_token")
                if not self._cur_duration:
                    self._cur_duration = _to_seconds(meta.get("duration_sec"))

    async def _finalize(
        self, token: str | None, duration: float, max_ct: float
    ) -> None:
        if not token:
            return  # not a resolvable catalog ('cm') item
        if duration <= 0:
            return  # unknown duration — can't judge the threshold; don't hide
        if max_ct < self._threshold * duration:
            return  # skipped/removed before playing enough to count as played
        try:
            await self._db.record_play_completion(
                friendly_token=token,
                duration_sec=int(duration),
            )
        except Exception:
            logger.warning(
                "Failed to record play completion for %s", token, exc_info=True
            )
        try:
            await self._db.rotate_playlist_item_to_bottom(token)
        except Exception:
            logger.warning(
                "Failed to rotate playlist item for %s", token, exc_info=True
            )
