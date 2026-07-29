"""Admin proxy routes for kryten-moderator service.

All routes are admin-only and proxy to kryten-api-gate, which in turn routes
requests to kryten-moderator over NATS. The CyTube channel name is read from
app config so the frontend does not need to specify it.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from ..auth.session import require_admin

router = APIRouter(prefix="/admin/moderation", tags=["admin"])


# ── request bodies ─────────────────────────────────────────────────────────────


class AddEntryRequest(BaseModel):
    username: str
    action: Literal["ban", "smute", "mute"]
    reason: str | None = None


class AddPatternRequest(BaseModel):
    pattern: str
    is_regex: bool = False
    action: Literal["ban", "smute", "mute"] = "ban"
    description: str | None = None


# ── helpers ────────────────────────────────────────────────────────────────────


def _api(request: Request):
    return request.app.state.api_gate


def _channel(request: Request) -> str:
    return request.app.state.config.channel


# ── service status ─────────────────────────────────────────────────────────────


@router.get("/status")
async def mod_status(request: Request, user: dict = Depends(require_admin)):
    """Combined ping / health / stats from the kryten-moderator service.

    Each key is fetched independently so a partial failure still returns the
    available data rather than a blanket error.
    """
    api = _api(request)
    result: dict = {}
    for key, coro in [
        ("ping", api.moderator_ping()),
        ("health", api.moderator_health()),
        ("stats", api.moderator_stats()),
    ]:
        try:
            result[key] = await coro
        except Exception as exc:
            result[key] = {"error": str(exc)}
    return result


# ── moderation entries ─────────────────────────────────────────────────────────


@router.get("/entries")
async def list_entries(
    request: Request,
    action_filter: str | None = Query(alias="filter", default=None),
    user: dict = Depends(require_admin),
):
    """List moderation entries for the configured channel."""
    return await _api(request).mod_list_entries(
        _channel(request), action_filter=action_filter
    )


@router.post("/entries", status_code=201)
async def add_entry(
    request: Request,
    body: AddEntryRequest,
    user: dict = Depends(require_admin),
):
    """Add a moderation entry. The logged-in admin is recorded as moderator."""
    return await _api(request).mod_add_entry(
        _channel(request),
        username=body.username,
        action=body.action,
        reason=body.reason,
        moderator=user["username"],
    )


@router.delete("/entries/{username}")
async def remove_entry(
    request: Request,
    username: str,
    user: dict = Depends(require_admin),
):
    """Remove a moderation entry by username."""
    return await _api(request).mod_remove_entry(_channel(request), username)


# ── patterns ───────────────────────────────────────────────────────────────────


@router.get("/patterns")
async def list_patterns(request: Request, user: dict = Depends(require_admin)):
    """List banned username patterns for the configured channel."""
    return await _api(request).mod_list_patterns(_channel(request))


@router.post("/patterns", status_code=201)
async def add_pattern(
    request: Request,
    body: AddPatternRequest,
    user: dict = Depends(require_admin),
):
    """Register a banned username pattern."""
    return await _api(request).mod_add_pattern(
        _channel(request),
        pattern=body.pattern,
        is_regex=body.is_regex,
        action=body.action,
        description=body.description,
        added_by=user["username"],
    )


@router.delete("/patterns/{pattern:path}")
async def remove_pattern(
    request: Request,
    pattern: str,
    user: dict = Depends(require_admin),
):
    """Remove a pattern by its exact string (URL-encoded in the path)."""
    return await _api(request).mod_remove_pattern(_channel(request), pattern)


# ── recent users ───────────────────────────────────────────────────────────────


@router.get("/recent")
async def recent_users(
    request: Request,
    window_minutes: float = Query(default=60.0, gt=0),
    user: dict = Depends(require_admin),
):
    """List users seen in the channel within a rolling time window."""
    return await _api(request).mod_recent_users(
        _channel(request), window_minutes=window_minutes
    )
