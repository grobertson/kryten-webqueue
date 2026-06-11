import asyncio
import logging
from datetime import datetime, timedelta, UTC

from ..queue.ordering import refund_item

logger = logging.getLogger(__name__)

_queue_lock = asyncio.Lock()


async def fire_schedule(*, schedule_id: int, api_gate, db, shadow, ws_manager):
    """Fire a scheduled playlist: clear queue, refund displaced pay items, load playlist."""
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

        # Refund all pay items currently in queue
        pay_items = await db.get_pay_items()
        for item in pay_items:
            await refund_item(api_gate=api_gate, db=db, uid=item["uid"], reason="schedule_displaced")

        # Clear the CyTube playlist
        await api_gate.playlist_clear()

        # Load scheduled playlist items
        items = await db.get_saved_playlist_items(playlist_id)
        total_duration = 0
        last_item_uid = None
        for item in items:
            try:
                add_result = await api_gate.playlist_add(
                    media_type=item["media_type"],
                    media_id=item["media_id"],
                    position="end",
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
            for item in fallback_items:
                try:
                    await api_gate.playlist_add(
                        media_type=item["media_type"],
                        media_id=item["media_id"],
                        position="end",
                    )
                except Exception as e:
                    logger.warning(f"Schedule fire: failed to add fallback {item['media_id']}: {e}")
            if fallback_items:
                logger.info(
                    f"Schedule {schedule_id}: appended {len(fallback_items)} fallback item(s) "
                    f"from playlist {fallback_id}"
                )

        # Update active schedule
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
