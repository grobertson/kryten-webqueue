import asyncio
import uuid
import logging

import httpx

logger = logging.getLogger(__name__)

# Module-level lock for queue ordering
_queue_lock = asyncio.Lock()


async def insert_pay_queue(
    *,
    api_gate,
    shadow,
    db,
    username: str,
    media_type: str,
    media_id: str,
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
        except httpx.HTTPStatusError:
            try:
                await api_gate.queue_refund(username=username, request_id=request_id, reason="playlist_add_failed")
            except Exception:
                pass
            return {"success": False, "error": "Failed to add to playlist"}
        if not add_result.get("success"):
            # Refund on failure
            try:
                await api_gate.queue_refund(username=username, request_id=request_id, reason="add_failed")
            except Exception:
                pass
            return {"success": False, "error": "Failed to add to playlist"}

        uid = add_result["uid"]

        # Move after last pay UID if needed
        if last_pay_uid:
            await api_gate.playlist_move(uid, last_pay_uid)

        # Record spend
        await db.save_spend_request(
            request_id, username=username, uid=uid,
            friendly_token=media_id if media_type == "cm" else None,
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
            username=username, friendly_token=media_id if media_type == "cm" else None,
            title=title, tier=tier, z_cost=z_cost,
        )

        return {"success": True, "uid": uid, "request_id": request_id}


async def insert_pay_playnext(
    *,
    api_gate,
    shadow,
    db,
    username: str,
    media_type: str,
    media_id: str,
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
        except httpx.HTTPStatusError:
            try:
                await api_gate.queue_refund(username=username, request_id=request_id, reason="playlist_add_failed")
            except Exception:
                pass
            return {"success": False, "error": "Failed to add to playlist"}
        if not add_result.get("success"):
            try:
                await api_gate.queue_refund(username=username, request_id=request_id, reason="add_failed")
            except Exception:
                pass
            return {"success": False, "error": "Failed to add to playlist"}

        uid = add_result["uid"]

        # Move to front
        await api_gate.playlist_move(uid, "prepend")

        # Record spend
        await db.save_spend_request(
            request_id, username=username, uid=uid,
            friendly_token=media_id if media_type == "cm" else None,
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
            username=username, friendly_token=media_id if media_type == "cm" else None,
            title=title, tier=tier, z_cost=z_cost,
        )

        return {"success": True, "uid": uid, "request_id": request_id}


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
