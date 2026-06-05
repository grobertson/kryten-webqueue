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

        # Find position: after last pay item, or prepend if none
        last_pay_uid = await db.get_last_pay_uid()
        position = "end" if not last_pay_uid else str(last_pay_uid)

        # Add to CyTube playlist
        try:
            add_result = await api_gate.playlist_add(
                media_type=media_type,
                media_id=media_id,
                position=position,
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

        # Move after last pay UID if needed; refund + remove if positioning fails
        if last_pay_uid:
            try:
                await api_gate.playlist_move(uid, last_pay_uid)
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
            "media_type": media_type,
            "media_id": media_id,
            "duration_sec": duration_sec,
            "is_pay": True,
            "paid_by": username,
            "tier": tier,
            "z_cost": z_cost,
            "schedule_id": None,
        }
        # Position after last pay
        if last_pay_uid:
            pos = await db.get_shadow_position_after(last_pay_uid)
        else:
            pos = len(shadow.items)
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

        # Add to CyTube playlist at prepend position
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

        # Move to front; refund + remove if positioning fails
        try:
            await api_gate.playlist_move(uid, "prepend")
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

        # Update local shadow at position 0
        item = {
            "uid": uid,
            "title": title,
            "media_type": media_type,
            "media_id": media_id,
            "duration_sec": duration_sec,
            "is_pay": True,
            "paid_by": username,
            "tier": tier,
            "z_cost": z_cost,
            "schedule_id": None,
        }
        await shadow.insert_at(item, 0)

        await db.add_queue_history(
            username=username, friendly_token=_ft,
            title=title, tier=tier, z_cost=z_cost,
        )

        # Announce placement to the channel
        await _announce_queued(api_gate, shadow, uid=uid, title=title, username=username)

        return {"success": True, "uid": uid, "request_id": request_id}


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
) -> dict:
    """Insert a zero-cost admin item in the first available non-pay slot.

    Treated exactly like a non-paid item (no economy interaction). It is placed
    immediately after the last paid item, i.e. at the top of the free section.
    """
    async with _queue_lock:
        # First available non-pay slot is right after the last pay item.
        last_pay_uid = await db.get_last_pay_uid()
        position = "end" if not last_pay_uid else str(last_pay_uid)

        # Add to CyTube playlist
        try:
            add_result = await api_gate.playlist_add(
                media_type=media_type,
                media_id=media_id,
                position=position,
            )
        except httpx.HTTPStatusError as exc:
            return {"success": False, "error": _add_failure_reason(None, exc)}
        if not add_result.get("success"):
            return {"success": False, "error": _add_failure_reason(add_result, None)}

        uid = add_result["uid"]

        # Move to the top of the free section if there are pay items above
        if last_pay_uid:
            try:
                await api_gate.playlist_move(uid, last_pay_uid)
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
            "media_type": media_type,
            "media_id": media_id,
            "duration_sec": duration_sec,
            "is_pay": False,
            "paid_by": None,
            "tier": None,
            "z_cost": None,
            "schedule_id": None,
        }
        if last_pay_uid:
            pos = await db.get_shadow_position_after(last_pay_uid)
        else:
            pos = len(shadow.items)
        await shadow.insert_at(item, pos)

        # Queue history (zero cost, admin tier)
        await db.add_queue_history(
            username=username, friendly_token=_ft,
            title=title, tier="admin", z_cost=0,
        )

        # Announce placement to the channel
        await _announce_queued(api_gate, shadow, uid=uid, title=title, username=username)

        return {"success": True, "uid": uid}


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
