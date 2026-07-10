import asyncio
import logging
from contextlib import nullcontext
from datetime import datetime, timedelta, UTC

from ..queue.ordering import refund_item
from .bulk_add import add_item_throttled

logger = logging.getLogger(__name__)

_queue_lock = asyncio.Lock()


async def fire_schedule(
    *,
    schedule_id: int,
    api_gate,
    db,
    shadow,
    ws_manager,
    add_delay_sec: float = 0.0,
    add_max_retries: int = 0,
    promo_director=None,
):
    """Fire a scheduled playlist: clear queue, refund displaced pay items, load playlist.

    Promo insertion is suppressed for the whole load (via ``promo_director``):
    promos must never be slotted between items while the playlist is still being
    built. Suppression spans through ``set_active_schedule`` so that, for an
    immutable event, the persistent event lock is already recorded by the time
    suppression lifts — a clean handoff with no window for a stray insertion.
    """
    async with _queue_lock:
        schedule = await db.get_schedule(schedule_id)
        if not schedule:
            logger.error(f"Schedule {schedule_id} not found")
            return

        playlist_id = schedule["playlist_id"]
        playlist = await db.get_saved_playlist(playlist_id)
        if not playlist:
            logger.error(f"Playlist {playlist_id} not found for schedule {schedule_id}")
            return

        suppress_ctx = (
            promo_director.suppressed(f"schedule fire {schedule_id}")
            if promo_director is not None
            else nullcontext()
        )
        with suppress_ctx:
            # Refund all pay items currently in queue
            pay_items = await db.get_pay_items()
            for item in pay_items:
                await refund_item(api_gate=api_gate, db=db, uid=item["uid"], reason="schedule_displaced")

            # Clear the CyTube playlist
            await api_gate.playlist_clear()

            # Load scheduled playlist items
            items = await db.get_saved_playlist_items(playlist_id)
            # For a mutable (TV-show) playlist, skip episodes already played in the
            # current pass so a re-fire continues where it left off instead of
            # replaying season 1. Immutable/promo playlists never have played rows,
            # so this is a no-op for them.
            played = await db.get_playlist_played_media_ids(playlist_id)
            if played:
                before = len(items)
                items = [it for it in items if it["media_id"] not in played]
                if before != len(items):
                    logger.info(
                        f"Schedule {schedule_id}: skipped {before - len(items)} already-played "
                        f"episode(s) from mutable playlist {playlist_id}"
                    )
            total_duration = 0
            last_item_uid = None
            for index, item in enumerate(items):
                # Throttle consecutive adds so CyTube can validate each item before
                # the next arrives (avoids transient queueFail/422 under load).
                if index and add_delay_sec:
                    await asyncio.sleep(add_delay_sec)
                try:
                    add_result = await add_item_throttled(
                        api_gate,
                        media_type=item["media_type"],
                        media_id=item["media_id"],
                        position="end",
                        max_retries=add_max_retries,
                        retry_delay_sec=add_delay_sec or 0.5,
                    )
                    if isinstance(add_result, dict) and add_result.get("uid") is not None:
                        last_item_uid = add_result["uid"]
                    total_duration += item.get("duration_sec", 0) or 0
                except Exception as e:
                    logger.warning(f"Schedule fire: failed to add {item['media_id']}: {e}")

            # Append the optional fallback (mutable) playlist AFTER the event items so
            # the live queue isn't left empty once the event is exhausted. The
            # fallback items are not part of the "scheduled event", so they do not
            # change last_item_uid (the event lock still lifts when the last EVENT
            # item begins) and they remain available for pay-to-play/search.
            fallback_id = schedule.get("fallback_playlist_id")
            if fallback_id:
                fallback_items = await db.get_saved_playlist_items(fallback_id)
                # Same continuation logic as the main playlist: skip already-played
                # episodes of the (mutable) fallback so it advances across fires.
                fb_played = await db.get_playlist_played_media_ids(fallback_id)
                if fb_played:
                    fallback_items = [it for it in fallback_items if it["media_id"] not in fb_played]
                for index, item in enumerate(fallback_items):
                    if index and add_delay_sec:
                        await asyncio.sleep(add_delay_sec)
                    try:
                        await add_item_throttled(
                            api_gate,
                            media_type=item["media_type"],
                            media_id=item["media_id"],
                            position="end",
                            max_retries=add_max_retries,
                            retry_delay_sec=add_delay_sec or 0.5,
                        )
                    except Exception as e:
                        logger.warning(f"Schedule fire: failed to add fallback {item['media_id']}: {e}")
                if fallback_items:
                    logger.info(
                        f"Schedule {schedule_id}: appended {len(fallback_items)} fallback item(s) "
                        f"from playlist {fallback_id}"
                    )

            # Update active schedule (recorded *inside* the suppression window so
            # the event lock is live before promos resume).
            now = datetime.now(UTC)
            await db.set_active_schedule(
                schedule_id=schedule_id,
                playlist_id=playlist_id,
                is_immutable=playlist.get("is_immutable", False),
                started_at=now.isoformat(),
                estimated_end_at=(now + timedelta(seconds=total_duration)).isoformat(),
                last_item_uid=last_item_uid,
            )

            # Mark schedule as fired
            await db.mark_schedule_fired(schedule_id, now.isoformat())

        # Notify WS clients
        await ws_manager.broadcast({
            "type": "schedule_fired",
            "data": {
                "schedule_id": schedule_id,
                "playlist_name": playlist["name"],
                "is_immutable": playlist.get("is_immutable", False),
            },
        })

        logger.info(f"Schedule {schedule_id} fired: playlist '{playlist['name']}' ({len(items)} items)")
