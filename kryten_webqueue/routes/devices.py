"""Session-authenticated device management (the browser side of linking).

A logged-in user names a device and requests a one-time pad here, lists their
linked devices, and revokes access. The machine-facing exchange and data
endpoints live in :mod:`kryten_webqueue.routes.public_api` under
``/api/public/v1``.
"""

from datetime import datetime, timedelta, UTC

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from ..auth.session import get_current_user
from ..auth.device_keys import (
    LINK_CODE_TTL_MINUTES,
    generate_link_code,
)

router = APIRouter(prefix="/user/devices", tags=["devices"])

_MAX_DEVICES_PER_USER = 20
_LINK_CODE_COLLISION_RETRIES = 8


class LinkCodeRequest(BaseModel):
    device_name: str

    @field_validator("device_name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Please name the device you're about to link.")
        if len(v) > 60:
            raise ValueError("Device name too long (max 60 characters).")
        return v


@router.get("")
async def list_devices(request: Request, user: dict = Depends(get_current_user)):
    """List the current user's linked devices (never exposes key material)."""
    db = request.app.state.db
    return {"devices": await db.list_device_keys(user["username"])}


@router.post("/link-code", status_code=201)
async def create_link_code(
    body: LinkCodeRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Issue a one-time pad the user types into the device to link it.

    The code is bound to ``(username, device_name)`` now; the eventual API key
    inherits that name when the device redeems the code.
    """
    db = request.app.state.db
    username = user["username"]

    limiter = getattr(request.app.state, "device_link_rate_limiter", None)
    if limiter is not None and not limiter.is_allowed(username):
        raise HTTPException(429, "Too many link codes requested. Try again shortly.")

    existing = await db.list_device_keys(username)
    if len(existing) >= _MAX_DEVICES_PER_USER:
        raise HTTPException(
            409,
            f"Device limit reached ({_MAX_DEVICES_PER_USER}). "
            "Revoke a device before linking another.",
        )

    await db.purge_expired_link_codes()

    expires_at = datetime.now(UTC) + timedelta(minutes=LINK_CODE_TTL_MINUTES)
    code = None
    for _ in range(_LINK_CODE_COLLISION_RETRIES):
        candidate = generate_link_code()
        if not await db.link_code_exists(candidate):
            code = candidate
            break
    if code is None:
        raise HTTPException(503, "Could not allocate a link code. Try again.")

    await db.create_link_code(
        code=code,
        username=username,
        device_name=body.device_name,
        expires_at=expires_at.isoformat(),
    )

    return {
        "code": code,
        "device_name": body.device_name,
        "expires_at": expires_at.isoformat(),
        "expires_in_sec": LINK_CODE_TTL_MINUTES * 60,
    }


@router.delete("/{key_id}")
async def revoke_device(
    key_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Revoke (permanently disable) one of the user's linked devices."""
    db = request.app.state.db
    removed = await db.delete_device_key(key_id, user["username"])
    if not removed:
        raise HTTPException(404, "Device not found.")
    return {"success": True}
