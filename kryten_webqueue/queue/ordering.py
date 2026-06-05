import asyncio
import uuid
import logging

import httpx

logger = logging.getLogger(__name__)

# Module-level lock for queue ordering
_queue_lock = asyncio.Lock()


def _add_failure_reason(add_result: dict | None, exc: httpx.HTTPStatusError | None) -> str:
    """Extract a human-readable reason from a failed playlist add."""
    if exc is not None:
        try:
            detail = exc.response.json().get("detail")
            if detail:
                return str(detail)
        except Exception:
            pass
        return f"playlist add failed ({exc.response.status_code})"
    if add_result is not None:
        return str(add_result.get("error", "Failed to add to playlist"))
    return "Failed to add to playlist"


def _announcement_position(shadow, uid: int) -> int | None:
    """Position of the item for chat announcement.

    Counting starts at the currently-playing item (position 0), so the next
    item to play is position 1. The shadow mirrors the full CyTube playlist
    (including the active item at index 0), so the item's shadow index is the
    announcement number.
    """
    for it in shadow.items:
        if it.get("uid") == uid:
            return it.get("position")
    return None


async def _announce_queued(api_gate, shadow, *, uid: int, title: str, username: str) -> None:
    """Announce a successful queue placement to the channel chat."""
    pos = _announcement_position(shadow, uid)
    if pos is None:
        return
    try:
        await api_gate.send_chat(f"{title} has been queued in position {pos} by {username}")
    except Exception:
        logger.warning("Failed to send queue announcement", exc_info=True)


async def _now_playing_uid(api_gate, shadow) -> int | None:
    """UID of the currently-playing item, preferring fresh state over the cache."""
    np = None
    try:
        np = await api_gate.get_now_playing()
    except Exception:
        np = None
    if not np:
        np = shadow.now_playing
    if not np:
        return None
    uid = np.get("uid")
    try:
        return int(uid) if uid is not None else None
    except (TypeError, ValueError):
        return None


def _shadow_index_after_uid(shadow, target_uid: int | None) -> int:
    """Shadow list index immediately after target_uid (end of list if not found)."""
    if target_uid is not None:
        for idx, it in enumerate(shadow.items):
            if it.get("uid") == target_uid:
                return idx + 1
    return len(shadow.items)


async def _move_after(api_gate, *, uid: int, target_uid: int | None) -> None:
    """Move uid to immediately after target_uid. No-op when target is None."""
    if target_uid is not None:
        await api_gate.playlist_move(uid, target_uid)


async def insert_pay_queue(
    *,
    api_gate,
    shadow,
    db,
    username: str,
    media_type: str,
    media_id: str,
    friendly_token: str | None = None,
    title: str,
    duration_sec: int,
    tier: str,
    z_cost: int,
) -> dict:
    """Insert a paid item at the end of the pay-queue section (FIFO)."""
    async with _queue_lock:
        request_id = str(uuid.uuid4())

        # Spend currency (api-gate _unwrap strips the success envelope;
        # raise_for_status propagates failures as httpx.HTTPStatusError)
        try:
            await api_gate.queue_spend(
                username=username,
                duration_sec=duration_sec,
                tier=tier,
                request_id=request_id,
            )
        except httpx.HTTPStatusError as exc:
            return {"success": False, "error": f"Spend failed: {exc.response.status_code}"}

        # Target position: immediately after the LAST item in the persistent
        # pay-queue list, or after the currently-playing item when none exist.
        last_pay_uid = await db.get_last_pay_uid()
        if last_pay_uid:
            target_uid = last_pay_uid
        else:
            target_uid = await _now_playing_uid(api_gate, shadow)
            if target_uid is None:
                # No anchor to position against (robot KV not initialised).
                # Cancel and refund rather than dumping the item at the end.
                try:
                    await api_gate.queue_refund(username=username, request_id=request_id, reason="no_now_playing")
                except Exception:
                    pass
                return {"success": False, "error": "Queue position unavailable (now-playing unknown); refunded"}

        # Add to CyTube playlist (always appended; repositioned below)
        try:
            add_result = await api_gate.playlist_add(
                media_type=media_type,
                media_id=media_id,
                position="end",
            )
        except httpx.HTTPStatusError as exc:
            try:
                await api_gate.queue_refund(username=username, request_id=request_id, reason="playlist_add_failed")
            except Exception:
                pass
            return {"success": False, "error": _add_failure_reason(None, exc)}
        if not add_result.get("success"):
            # Refund on failure
            try:
                await api_gate.queue_refund(username=username, request_id=request_id, reason="add_failed")
            except Exception:
                pass
            return {"success": False, "error": _add_failure_reason(add_result, None)}

        uid = add_result["uid"]

        # Move after the target UID; refund + remove if positioning fails
        try:
            await _move_after(api_gate, uid=uid, target_uid=target_uid)
        except httpx.HTTPStatusError:
            try:
                await api_gate.queue_refund(username=username, request_id=request_id, reason="move_failed")
            except Exception:
                pass
            try:
                await api_gate.playlist_delete(uid)
            except Exception:
                pass
            return {"success": False, "error": "Failed to position item in queue"}

        # Record spend
        _ft = friendly_token if friendly_token is not None else (media_id if media_type == "cm" else None)
        await db.save_spend_request(
            request_id, username=username, uid=uid,
            friendly_token=_ft,
            tier=tier, z_cost=z_cost,
        )

        # Update local shadow
        item = {
            "uid": uid,
            "title": title,
            "friendly_token": _ft,
            "media_type": media_type,
            "media_id": media_id,
            "duration_sec": duration_sec,
            "is_pay": True,
            "paid_by": username,
            "tier": tier,
            "z_cost": z_cost,
            "schedule_id": None,
        }
        # Position immediately after the target UID
        pos = _shadow_index_after_uid(shadow, target_uid)
        await shadow.insert_at(item, pos)

        # Queue history
        await db.add_queue_history(
            username=username, friendly_token=_ft,
            title=title, tier=tier, z_cost=z_cost,
        )

        # Announce placement to the channel
        await _announce_queued(api_gate, shadow, uid=uid, title=title, username=username)

        return {"success": True, "uid": uid, "request_id": request_id}


async def insert_pay_playnext(
    *,
    api_gate,
    shadow,
    db,
    username: str,
    media_type: str,
    media_id: str,
    friendly_token: str | None = None,
    title: str,
    duration_sec: int,
    tier: str,
    z_cost: int,
) -> dict:
    """Insert a paid item at position 0 (play next)."""
    async with _queue_lock:
        request_id = str(uuid.uuid4())

        # Spend currency (api-gate _unwrap strips the success envelope;
        # raise_for_status propagates failures as httpx.HTTPStatusError)
        try:
            await api_gate.queue_spend(
                username=username,
                duration_sec=duration_sec,
                tier=tier,
                request_id=request_id,
            )
        except httpx.HTTPStatusError as exc:
            return {"success": False, "error": f"Spend failed: {exc.response.status_code}"}

        # Target position: immediately after the currently-playing item.
        target_uid = await _now_playing_uid(api_gate, shadow)
        if target_uid is None:
            # Cannot place "play next" without knowing the active item.
            try:
                await api_gate.queue_refund(username=username, request_id=request_id, reason="no_now_playing")
            except Exception:
                pass
            return {"success": False, "error": "Play-next unavailable (now-playing unknown); refunded"}

        # Add to CyTube playlist (always appended; repositioned below)
        try:
            add_result = await api_gate.playlist_add(
                media_type=media_type,
                media_id=media_id,
                position="end",
            )
        except httpx.HTTPStatusError as exc:
            try:
                await api_gate.queue_refund(username=username, request_id=request_id, reason="playlist_add_failed")
            except Exception:
                pass
            return {"success": False, "error": _add_failure_reason(None, exc)}
        if not add_result.get("success"):
            try:
                await api_gate.queue_refund(username=username, request_id=request_id, reason="add_failed")
            except Exception:
                pass
            return {"success": False, "error": _add_failure_reason(add_result, None)}

        uid = add_result["uid"]

        # Move to immediately after the now-playing item; refund + remove on failure
        try:
            await _move_after(api_gate, uid=uid, target_uid=target_uid)
        except httpx.HTTPStatusError:
            try:
                await api_gate.queue_refund(username=username, request_id=request_id, reason="move_failed")
            except Exception:
                pass
            try:
                await api_gate.playlist_delete(uid)
            except Exception:
                pass
            return {"success": False, "error": "Failed to position item in queue"}

        # Record spend
        _ft = friendly_token if friendly_token is not None else (media_id if media_type == "cm" else None)
        await db.save_spend_request(
            request_id, username=username, uid=uid,
            friendly_token=_ft,
            tier=tier, z_cost=z_cost,
        )

        # Update local shadow immediately after now-playing. Existing pay items
        # shift down one position as insert_at re-indexes the list.
        item = {
            "uid": uid,
            "title": title,
            "friendly_token": _ft,
            "media_type": media_type,
            "media_id": media_id,
            "duration_sec": duration_sec,
            "is_pay": True,
            "paid_by": username,
            "tier": tier,
            "z_cost": z_cost,
            "schedule_id": None,
        }
        pos = _shadow_index_after_uid(shadow, target_uid)
        await shadow.insert_at(item, pos)

        await db.add_queue_history(
            username=username, friendly_token=_ft,
            title=title, tier=tier, z_cost=z_cost,
        )

        # Announce placement to the channel
        await _announce_queued(api_gate, shadow, uid=uid, title=title, username=username)

        return {"success": True, "uid": uid, "request_id": request_id}


async def _refund_and_remove_pending_pay(api_gate, shadow, db) -> int:
    """Refund and remove every pending (up-next) paid item from the queue.

    Returns the number of items removed. The currently-playing item is never
    touched (it is not present in the pay shadow as an up-next item).
    """
    pending = await db.get_pay_items()
    np_uid = await _now_playing_uid(api_gate, shadow)
    removed = 0
    for it in pending:
        uid = it.get("uid")
        if uid is None or uid == np_uid:
            continue
        try:
            await refund_item(api_gate=api_gate, db=db, uid=uid, reason="admin_playnext_refund")
        except Exception:
            logger.warning("Refund failed for uid %s during admin override", uid, exc_info=True)
        try:
            await api_gate.playlist_delete(uid)
        except Exception:
            logger.warning("Delete failed for uid %s during admin override", uid, exc_info=True)
        try:
            await shadow.remove(uid)
        except Exception:
            pass
        removed += 1
    return removed


async def insert_admin_queue(
    *,
    api_gate,
    shadow,
    db,
    username: str,
    media_type: str,
    media_id: str,
    friendly_token: str | None = None,
    title: str,
    duration_sec: int,
    mode: str = "after_purchased",
) -> dict:
    """Insert a zero-cost admin item (no economy interaction).

    ``mode`` selects how the item is positioned:

    - ``"after_purchased"`` (default): placed immediately after the last item in
      the persistent pay-queue list, i.e. at the top of the free section.
    - ``"playnext_refund"``: every pending (up-next) paid item is refunded and
      removed, then the admin item is placed immediately after the now-playing
      item.
    - ``"cancel"``: no-op.
    """
    if mode == "cancel":
        return {"success": False, "error": "cancelled", "cancelled": True}

    async with _queue_lock:
        if mode == "playnext_refund":
            removed = await _refund_and_remove_pending_pay(api_gate, shadow, db)
            target_uid = await _now_playing_uid(api_gate, shadow)
            if target_uid is None:
                return {"success": False, "error": "Play-next unavailable (now-playing unknown)"}
        else:
            # after_purchased: immediately after the LAST persistent pay item,
            # or after the currently-playing item when none exist.
            removed = 0
            last_pay_uid = await db.get_last_pay_uid()
            if last_pay_uid:
                target_uid = last_pay_uid
            else:
                target_uid = await _now_playing_uid(api_gate, shadow)
                if target_uid is None:
                    return {"success": False, "error": "Queue position unavailable (now-playing unknown)"}

        # Add to CyTube playlist (always appended; repositioned below)
        try:
            add_result = await api_gate.playlist_add(
                media_type=media_type,
                media_id=media_id,
                position="end",
            )
        except httpx.HTTPStatusError as exc:
            return {"success": False, "error": _add_failure_reason(None, exc)}
        if not add_result.get("success"):
            return {"success": False, "error": _add_failure_reason(add_result, None)}

        uid = add_result["uid"]

        # Move after the target UID; remove the orphan if positioning fails
        try:
            await _move_after(api_gate, uid=uid, target_uid=target_uid)
        except httpx.HTTPStatusError:
            try:
                await api_gate.playlist_delete(uid)
            except Exception:
                pass
            return {"success": False, "error": "Failed to position item in queue"}

        _ft = friendly_token if friendly_token is not None else (media_id if media_type == "cm" else None)

        # Update local shadow as a non-paid item
        item = {
            "uid": uid,
            "title": title,
            "friendly_token": _ft,
            "media_type": media_type,
            "media_id": media_id,
            "duration_sec": duration_sec,
            "is_pay": False,
            "paid_by": None,
            "tier": None,
            "z_cost": None,
            "schedule_id": None,
        }
        pos = _shadow_index_after_uid(shadow, target_uid)
        await shadow.insert_at(item, pos)

        # Queue history (zero cost, admin tier)
        await db.add_queue_history(
            username=username, friendly_token=_ft,
            title=title, tier="admin", z_cost=0,
        )

        # Announce placement to the channel
        await _announce_queued(api_gate, shadow, uid=uid, title=title, username=username)

        return {"success": True, "uid": uid, "refunded": removed}



async def refund_item(*, api_gate, db, uid: int, reason: str) -> bool:
    """Refund a paid queue item."""
    request_id = await db.get_request_id_for_uid(uid)
    if not request_id:
        return False

    # Look up the spend to find username
    row = await db._fetch_one("SELECT username FROM spend_requests WHERE request_id=?", [request_id])
    if not row:
        return False

    result = await api_gate.queue_refund(
        username=row["username"],
        request_id=request_id,
        reason=reason,
    )
    if result.get("success"):
        await db.mark_spend_refunded(request_id)
        return True
    return False
