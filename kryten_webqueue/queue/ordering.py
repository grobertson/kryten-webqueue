import asyncio
import uuid
import logging

import httpx

logger = logging.getLogger(__name__)

# Module-level lock for queue ordering
_queue_lock = asyncio.Lock()


def _add_failure_reason(
    add_result: dict | None, exc: httpx.HTTPStatusError | None
) -> str:
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
    """Position of the item for chat announcement (deprecated; kept for tests).

    Counting starts at the currently-playing item (position 0), so the next
    item to play is position 1. The shadow mirrors the full CyTube playlist
    (including the active item at index 0), so the item's shadow index is the
    announcement number.
    """
    for it in shadow.items:
        if it.get("uid") == uid:
            return it.get("position")
    return None


_ONES_ORDINAL = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}
_ONES = [
    "",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = [
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
]


def _cardinal_words(n: int) -> str:
    """English cardinal words for 1..999 (queue positions never exceed this)."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    hundreds, rem = divmod(n, 100)
    head = f"{_ONES[hundreds]} hundred"
    return f"{head} {_cardinal_words(rem)}" if rem else head


def _to_ordinal_word(word: str) -> str:
    """Convert a single cardinal word to its ordinal form."""
    if word in _ONES_ORDINAL:
        return _ONES_ORDINAL[word]
    if word.endswith("y"):
        return word[:-1] + "ieth"
    return word + "th"


def _ordinal_words(n: int) -> str:
    """English ordinal words for n (e.g. 3 -> 'third', 42 -> 'forty-second')."""
    cardinal = _cardinal_words(n)
    # Convert only the final word (handles 'forty-two'->'forty-second',
    # 'one hundred seven'->'one hundred seventh').
    if "-" in cardinal:
        head, last = cardinal.rsplit("-", 1)
        return f"{head}-{_to_ordinal_word(last)}"
    parts = cardinal.rsplit(" ", 1)
    if len(parts) == 2:
        return f"{parts[0]} {_to_ordinal_word(parts[1])}"
    return _to_ordinal_word(cardinal)


async def _announce_paid_queued(
    api_gate, shadow, *, uid: int, title: str, username: str
) -> None:
    """Announce a paid queue placement to the channel chat.

    Position is counted from the currently-playing item, wrapping around the
    playlist (CyTube loops). The item immediately after now-playing reads
    "next"; everything else uses an English ordinal counting the now-playing
    item as first (so the item two slots away is "third").
    """
    items = shadow.items
    np_uid = await _now_playing_uid(api_gate, shadow)
    np_index = None
    item_index = None
    for i, it in enumerate(items):
        if it.get("uid") == np_uid:
            np_index = i
        if it.get("uid") == uid:
            item_index = i
    if item_index is None:
        return
    n = len(items)
    offset = item_index if np_index is None else (item_index - np_index) % n
    if offset <= 0:
        return
    position = "next" if offset == 1 else _ordinal_words(offset + 1)
    try:
        await api_gate.send_chat(
            f"{title} added to the queue with Zcoin by {username} and is now {position}."
        )
    except Exception:
        logger.warning("Failed to send queue announcement", exc_info=True)


async def _now_playing_uid(api_gate, shadow) -> int | None:
    """UID of the currently-playing item, preferring fresh state over the cache.

    CyTube's now-playing payload (``changeMedia``) carries the media
    ``{id, type, title, seconds}`` but no playlist ``uid``. When the uid is
    absent we recover it by matching the now-playing media id/type against the
    shadow playlist (whose items do carry uids).
    """
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
    if uid is not None:
        try:
            return int(uid)
        except (TypeError, ValueError):
            pass
    # Fall back to matching the now-playing media against the shadow playlist.
    np_id = np.get("id")
    np_type = np.get("type")
    if np_id is not None:
        for it in shadow.items:
            if it.get("media_id") == np_id and (
                np_type is None or it.get("media_type") == np_type
            ):
                it_uid = it.get("uid")
                try:
                    return int(it_uid) if it_uid is not None else None
                except (TypeError, ValueError):
                    return None
    return None


def _shadow_index_after_uid(shadow, target_uid: int | None) -> int:
    """Shadow list index immediately after target_uid (end of list if not found)."""
    if target_uid is not None:
        for idx, it in enumerate(shadow.items):
            if it.get("uid") == target_uid:
                return idx + 1
    return len(shadow.items)


def _last_pay_uid(shadow) -> int | None:
    """UID of the last paid item in true play order, read from the in-memory shadow.

    The in-memory shadow is rebuilt in CyTube playlist order on every poll, so
    it is the authoritative source for FIFO positioning. (The persisted
    ``queue_shadow.position`` column can lag between polls because reconciliation
    re-indexes positions in memory only, which previously caused new paid items
    to be anchored against a stale uid and land directly after the now-playing
    item instead of at the tail of the pay queue.)
    """
    last = None
    for it in shadow.items:
        if it.get("is_pay"):
            last = it.get("uid")
    return last


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
    promo_director=None,
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
            return {
                "success": False,
                "error": f"Spend failed: {exc.response.status_code}",
            }

        # Target position: immediately after the LAST item in the persistent
        # pay-queue list, or after the currently-playing item when none exist.
        last_pay_uid = _last_pay_uid(shadow)
        if last_pay_uid:
            target_uid = last_pay_uid
        else:
            target_uid = await _now_playing_uid(api_gate, shadow)
            if target_uid is None:
                # No anchor to position against (robot KV not initialised).
                # Cancel and refund rather than dumping the item at the end.
                try:
                    await api_gate.queue_refund(
                        username=username,
                        request_id=request_id,
                        reason="no_now_playing",
                    )
                except Exception:
                    pass
                return {
                    "success": False,
                    "error": "Queue position unavailable (now-playing unknown); refunded",
                }

        # Add to CyTube playlist (always appended; repositioned below)
        try:
            add_result = await api_gate.playlist_add(
                media_type=media_type,
                media_id=media_id,
                position="end",
            )
        except httpx.HTTPStatusError as exc:
            try:
                await api_gate.queue_refund(
                    username=username,
                    request_id=request_id,
                    reason="playlist_add_failed",
                )
            except Exception:
                pass
            return {"success": False, "error": _add_failure_reason(None, exc)}
        if not add_result.get("success"):
            # Refund on failure
            try:
                await api_gate.queue_refund(
                    username=username, request_id=request_id, reason="add_failed"
                )
            except Exception:
                pass
            return {"success": False, "error": _add_failure_reason(add_result, None)}

        uid = add_result["uid"]

        # Move after the target UID; refund + remove if positioning fails
        try:
            await _move_after(api_gate, uid=uid, target_uid=target_uid)
        except httpx.HTTPStatusError:
            try:
                await api_gate.queue_refund(
                    username=username, request_id=request_id, reason="move_failed"
                )
            except Exception:
                pass
            try:
                await api_gate.playlist_delete(uid)
            except Exception:
                pass
            return {"success": False, "error": "Failed to position item in queue"}

        # Record spend
        _ft = (
            friendly_token
            if friendly_token is not None
            else (media_id if media_type == "cm" else None)
        )
        await db.save_spend_request(
            request_id,
            username=username,
            uid=uid,
            friendly_token=_ft,
            tier=tier,
            z_cost=z_cost,
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
            username=username,
            friendly_token=_ft,
            title=title,
            tier=tier,
            z_cost=z_cost,
        )

        # Announce placement to the channel
        await _announce_paid_queued(
            api_gate, shadow, uid=uid, title=title, username=username
        )

        # Insert a Viewer's Choice lead-in immediately before this paid item.
        if promo_director is not None:
            try:
                await promo_director.insert_viewers_choice(uid)
            except Exception:
                logger.warning(
                    "Viewer's Choice lead-in insert failed for uid=%s",
                    uid,
                    exc_info=True,
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
    friendly_token: str | None = None,
    title: str,
    duration_sec: int,
    tier: str,
    z_cost: int,
    promo_director=None,
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
            return {
                "success": False,
                "error": f"Spend failed: {exc.response.status_code}",
            }

        # Target position: immediately after the currently-playing item.
        target_uid = await _now_playing_uid(api_gate, shadow)
        if target_uid is None:
            # Cannot place "play next" without knowing the active item.
            try:
                await api_gate.queue_refund(
                    username=username, request_id=request_id, reason="no_now_playing"
                )
            except Exception:
                pass
            return {
                "success": False,
                "error": "Play-next unavailable (now-playing unknown); refunded",
            }

        # Add to CyTube playlist (always appended; repositioned below)
        try:
            add_result = await api_gate.playlist_add(
                media_type=media_type,
                media_id=media_id,
                position="end",
            )
        except httpx.HTTPStatusError as exc:
            try:
                await api_gate.queue_refund(
                    username=username,
                    request_id=request_id,
                    reason="playlist_add_failed",
                )
            except Exception:
                pass
            return {"success": False, "error": _add_failure_reason(None, exc)}
        if not add_result.get("success"):
            try:
                await api_gate.queue_refund(
                    username=username, request_id=request_id, reason="add_failed"
                )
            except Exception:
                pass
            return {"success": False, "error": _add_failure_reason(add_result, None)}

        uid = add_result["uid"]

        # Move to immediately after the now-playing item; refund + remove on failure
        try:
            await _move_after(api_gate, uid=uid, target_uid=target_uid)
        except httpx.HTTPStatusError:
            try:
                await api_gate.queue_refund(
                    username=username, request_id=request_id, reason="move_failed"
                )
            except Exception:
                pass
            try:
                await api_gate.playlist_delete(uid)
            except Exception:
                pass
            return {"success": False, "error": "Failed to position item in queue"}

        # Record spend
        _ft = (
            friendly_token
            if friendly_token is not None
            else (media_id if media_type == "cm" else None)
        )
        await db.save_spend_request(
            request_id,
            username=username,
            uid=uid,
            friendly_token=_ft,
            tier=tier,
            z_cost=z_cost,
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
            username=username,
            friendly_token=_ft,
            title=title,
            tier=tier,
            z_cost=z_cost,
        )

        # Announce placement to the channel
        await _announce_paid_queued(
            api_gate, shadow, uid=uid, title=title, username=username
        )

        # Insert a Viewer's Choice lead-in immediately before this paid item.
        if promo_director is not None:
            try:
                await promo_director.insert_viewers_choice(uid)
            except Exception:
                logger.warning(
                    "Viewer's Choice lead-in insert failed for uid=%s",
                    uid,
                    exc_info=True,
                )

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
            await refund_item(
                api_gate=api_gate, db=db, uid=uid, reason="admin_playnext_refund"
            )
        except Exception:
            logger.warning(
                "Refund failed for uid %s during admin override", uid, exc_info=True
            )
        try:
            await api_gate.playlist_delete(uid)
        except Exception:
            logger.warning(
                "Delete failed for uid %s during admin override", uid, exc_info=True
            )
        try:
            await shadow.remove(uid)
        except Exception:
            pass
        try:
            from ..promos.director import remove_lead_in_for

            await remove_lead_in_for(api_gate=api_gate, shadow=shadow, uid=uid)
        except Exception:
            logger.debug("Lead-in cleanup failed for uid %s", uid, exc_info=True)
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
                return {
                    "success": False,
                    "error": "Play-next unavailable (now-playing unknown)",
                }
        else:
            # after_purchased: immediately after the LAST persistent pay item,
            # or after the currently-playing item when none exist.
            removed = 0
            last_pay_uid = _last_pay_uid(shadow)
            if last_pay_uid:
                target_uid = last_pay_uid
            else:
                target_uid = await _now_playing_uid(api_gate, shadow)
                if target_uid is None:
                    return {
                        "success": False,
                        "error": "Queue position unavailable (now-playing unknown)",
                    }

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

        _ft = (
            friendly_token
            if friendly_token is not None
            else (media_id if media_type == "cm" else None)
        )

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
            username=username,
            friendly_token=_ft,
            title=title,
            tier="admin",
            z_cost=0,
        )

        # Admin queueing is intentionally NOT announced in the channel.

        return {"success": True, "uid": uid, "refunded": removed}


async def refund_item(*, api_gate, db, uid: int, reason: str) -> bool:
    """Refund a paid queue item."""
    request_id = await db.get_request_id_for_uid(uid)
    if not request_id:
        return False

    # Look up the spend to find username
    row = await db._fetch_one(
        "SELECT username FROM spend_requests WHERE request_id=?", [request_id]
    )
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
