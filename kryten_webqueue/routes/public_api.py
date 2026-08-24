"""Public, API-key-authenticated endpoints for third-party client apps.

Everything under ``/api/public/v1`` is callable by external applications (smart
TVs, tablets) using ``Authorization: Bearer <key>`` — the sole exception being
``POST /api/public/v1/link``, the bootstrap that exchanges a one-time pad for a
key. These endpoints mirror the data behind the live queue page in stable,
documented JSON shapes. See ``docs/PUBLIC_API.md`` for the contract.
"""

from datetime import datetime, UTC

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth.device_keys import (
    get_api_key_user,
    generate_api_key,
    api_key_display_prefix,
    hash_api_key,
    normalize_link_code,
    is_valid_link_code_format,
)

router = APIRouter(prefix="/api/public/v1", tags=["public-api"])


# ── helpers ──────────────────────────────────────────────────────────────────


def _client_ip(request: Request) -> str:
    """Best-effort client IP, honouring a single reverse proxy (nginx)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _absolute_url(request: Request, path: str | None) -> str | None:
    """Return an absolute URL for a cover-art path, passing through http(s)."""
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path
    base = str(request.base_url).rstrip("/")
    return f"{base}{path}"


def _cover_art_url(request: Request, item: dict) -> str | None:
    path = item.get("cover_art_path")
    if path:
        return _absolute_url(request, f"/images/{path}/500.webp")
    return _absolute_url(request, item.get("thumbnail_url"))


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _current_payload(request: Request, np: dict | None) -> dict:
    """Shape the now-playing item for ``/current``."""
    if not np:
        return {"playing": False, "item": None}
    duration = _to_float(np.get("duration_sec"))
    current_time = _to_float(np.get("currentTime")) or 0.0
    remaining = None
    if duration is not None:
        remaining = max(0.0, duration - current_time)
    return {
        "playing": True,
        "item": {
            "title": np.get("title"),
            "friendly_token": np.get("friendly_token"),
            "synopsis": np.get("description"),
            "duration_sec": int(duration) if duration is not None else None,
            "current_time_sec": round(current_time, 1),
            "remaining_sec": round(remaining, 1) if remaining is not None else None,
            "cover_art_url": _cover_art_url(request, np),
            "categories": np.get("categories") or [],
            "tags": np.get("tags") or [],
        },
    }


def _queue_item_payload(request: Request, item: dict) -> dict:
    duration = _to_float(item.get("duration_sec"))
    return {
        "position": item.get("position"),
        "uid": item.get("uid"),
        "title": item.get("title"),
        "friendly_token": item.get("friendly_token"),
        "duration_sec": int(duration) if duration is not None else None,
        "estimated_start_at": item.get("estimated_start_at"),
        "estimated_start_in_sec": item.get("estimated_start_in_sec"),
        "cover_art_url": _cover_art_url(request, item),
        "queued_by": item.get("queued_by") or item.get("username"),
        "tier": item.get("tier"),
    }


# ── device linking (bootstrap; no auth header) ───────────────────────────────


class LinkExchangeRequest(BaseModel):
    code: str


@router.post("/link")
async def exchange_link_code(body: LinkExchangeRequest, request: Request):
    """Exchange a one-time pad for a long-lived API key.

    The device POSTs the 5-char code a logged-in user generated. On success the
    code is consumed (single-use) and a fresh API key is returned **once** — it
    is never retrievable again.
    """
    db = request.app.state.db

    limiter = getattr(request.app.state, "device_exchange_rate_limiter", None)
    if limiter is not None and not limiter.is_allowed(_client_ip(request)):
        raise HTTPException(429, "Too many attempts. Try again shortly.")

    code = normalize_link_code(body.code)
    if not is_valid_link_code_format(code):
        raise HTTPException(400, "Invalid code format.")

    await db.purge_expired_link_codes()
    pad = await db.get_valid_link_code(code)
    if not pad:
        raise HTTPException(404, "Invalid or expired code.")

    full_key = generate_api_key()
    key_id = await db.create_device_key(
        username=pad["username"],
        device_name=pad["device_name"],
        key_prefix=api_key_display_prefix(full_key),
        key_hash=hash_api_key(full_key),
    )
    # Consume the pad so a single code yields exactly one key.
    await db.delete_link_code(code)

    return {
        "api_key": full_key,
        "token_type": "Bearer",
        "device_id": key_id,
        "device_name": pad["device_name"],
        "username": pad["username"],
    }


# ── data endpoints (Bearer key required) ─────────────────────────────────────


@router.get("/current")
async def public_current(request: Request, _auth: dict = Depends(get_api_key_user)):
    """Currently-playing item: title, synopsis, total/elapsed/remaining time."""
    state = await request.app.state.shadow.get_enriched_state(request.app.state.db)
    payload = _current_payload(request, state.get("now_playing"))
    payload["updated_at"] = state.get("updated_at") or datetime.now(UTC).isoformat()
    return payload


@router.get("/queue")
async def public_queue(request: Request, _auth: dict = Depends(get_api_key_user)):
    """Ordered queue with each item's predicted start time."""
    state = await request.app.state.shadow.get_enriched_state(request.app.state.db)
    now_playing = state.get("now_playing") or {}
    np_uid = now_playing.get("uid")
    items = []
    for it in state.get("items") or []:
        payload = _queue_item_payload(request, it)
        payload["is_now_playing"] = np_uid is not None and it.get("uid") == np_uid
        items.append(payload)
    return {
        "items": items,
        "count": len(items),
        "updated_at": state.get("updated_at") or datetime.now(UTC).isoformat(),
    }


@router.get("/events")
async def public_events(request: Request, _auth: dict = Depends(get_api_key_user)):
    """Upcoming enabled scheduled playlists (events) and their start times."""
    db = request.app.state.db
    now = datetime.now(UTC)
    events = []
    for sched in await db.get_schedules():
        if not sched.get("is_active"):
            continue
        fire_at = sched.get("fire_at")
        if not fire_at:
            continue
        try:
            dt = datetime.fromisoformat(str(fire_at))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        except ValueError:
            continue
        if dt <= now:
            continue
        events.append(
            {
                "label": sched.get("label"),
                "fire_at": dt.isoformat(),
                "is_recurring": bool(sched.get("is_recurring")),
            }
        )
    events.sort(key=lambda e: e["fire_at"])
    return {
        "events": events,
        "count": len(events),
        "updated_at": now.isoformat(),
    }
