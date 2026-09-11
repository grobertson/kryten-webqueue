"""Admin MOTD routes — preview the weekend grid and pin individual slots.

The ``motd_publish`` job resolves the line-up automatically; these endpoints let
an admin inspect what it found and override any slot for the current workbook
week (a hand-picked title, curator-supplied art, or a non-IMDb link). Overrides
are keyed to the week, so they retire on their own when the week rolls over.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from ..auth.session import require_admin
from ..motd.builder import SLOT_KEY_RE, build_slots, mystery_pool, week_context
from ..motd.render import render_motd

router = APIRouter(prefix="/admin/motd", tags=["admin"])

# Uploaded art is served as a static file, so restrict to formats the browser
# renders inertly — never trust the client-supplied filename or extension.
_ALLOWED_UPLOAD_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_WEEK_KEY_RE = re.compile(r"^\d{1,2}\.\d{1,2}-\d{1,2}\.\d{1,2}$")


class SlotOverride(BaseModel):
    title: str | None = None
    poster_url: str | None = None
    href: str | None = None


def _check_slot_key(slot_key: str) -> str:
    if not SLOT_KEY_RE.match(slot_key):
        raise HTTPException(400, "Invalid slot key")
    return slot_key


def _check_url(url: str | None, field: str) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, f"{field} must be an http(s) URL")
    return url


def _week_offset(week: str) -> int:
    if week not in ("current", "next"):
        raise HTTPException(400, "week must be 'current' or 'next'")
    return 1 if week == "next" else 0


async def _build_current(request: Request, week: str = "current"):
    config = request.app.state.config
    db = request.app.state.db
    week_key, _ = week_context(week_offset=_week_offset(week))
    overrides = {row["slot_key"]: row for row in await db.list_motd_overrides(week_key)}
    return await asyncio.to_thread(
        build_slots,
        config,
        overrides=overrides,
        dry_run=True,
        week_offset=_week_offset(week),
        mystery_urls=mystery_pool(
            config, getattr(request.app.state, "cover_art", None)
        ),
    )


@router.get("/grid")
async def motd_preview(
    request: Request, week: str = "current", user: dict = Depends(require_admin)
):
    """Weekend grid as resolved right now, plus the live channel MOTD."""
    built = await _build_current(request, week)
    try:
        current = await request.app.state.api_gate.get_motd()
    except Exception:  # noqa: BLE001 - preview must work even if api-gate is down
        current = None
    return {**built.to_dict(), "current_motd": current}


@router.get("/render")
async def motd_render(
    request: Request, week: str = "current", user: dict = Depends(require_admin)
):
    """Render the snippet for the weekend without publishing it."""
    built = await _build_current(request, week)
    html = render_motd(request.app.state.config, built)
    return {"week_key": built.week_key, "html": html}


@router.put("/overrides/{slot_key}")
async def set_override(
    slot_key: str,
    body: SlotOverride,
    request: Request,
    week: str = "current",
    user: dict = Depends(require_admin),
):
    """Pin a title, poster URL, and/or link for one slot of the target week."""
    _check_slot_key(slot_key)
    week_key, _ = week_context(week_offset=_week_offset(week))
    await request.app.state.db.upsert_motd_override(
        week_key,
        slot_key,
        title=body.title or None,
        poster_url=_check_url(body.poster_url, "poster_url"),
        href=_check_url(body.href, "href"),
        created_by=user["username"],
    )
    return {"week_key": week_key, "slot_key": slot_key}


@router.post("/overrides/{slot_key}/art")
async def upload_override_art(
    slot_key: str,
    request: Request,
    file: UploadFile = File(...),
    href: str | None = Form(None),
    title: str | None = Form(None),
    week: str = Form("current"),
    user: dict = Depends(require_admin),
):
    """Upload alternate art for a slot and pin it as that slot's poster."""
    _check_slot_key(slot_key)
    config = request.app.state.config
    ext = _ALLOWED_UPLOAD_TYPES.get((file.content_type or "").lower())
    if not ext:
        raise HTTPException(400, "Unsupported image type; use JPEG, PNG, WebP, or GIF")

    max_bytes = int(getattr(config.motd, "upload_max_bytes", 5 * 1024 * 1024))
    data = await file.read(max_bytes + 1)
    if not data:
        raise HTTPException(400, "Empty upload")
    if len(data) > max_bytes:
        raise HTTPException(413, f"Image exceeds {max_bytes // (1024 * 1024)} MiB")

    week_key, _ = week_context(week_offset=_week_offset(week))
    if not _WEEK_KEY_RE.match(week_key):
        raise HTTPException(500, "Unexpected week key")

    poster_dir = Path(config.motd.poster_dir).expanduser()
    poster_dir.mkdir(parents=True, exist_ok=True)
    # Random suffix busts the CDN/browser cache when a slot's art is replaced.
    filename = f"custom-{week_key}-{slot_key}-{secrets.token_hex(4)}{ext}"
    await asyncio.to_thread((poster_dir / filename).write_bytes, data)

    poster_url = f"{config.motd.poster_base_url.rstrip('/')}/{filename}"
    await request.app.state.db.upsert_motd_override(
        week_key,
        slot_key,
        title=title or None,
        poster_url=poster_url,
        href=_check_url(href, "href"),
        created_by=user["username"],
    )
    return {"week_key": week_key, "slot_key": slot_key, "poster_url": poster_url}


@router.delete("/overrides/{slot_key}")
async def clear_override(
    slot_key: str,
    request: Request,
    week: str = "current",
    user: dict = Depends(require_admin),
):
    """Drop one slot's override so it reverts to the auto-resolved value."""
    _check_slot_key(slot_key)
    week_key, _ = week_context(week_offset=_week_offset(week))
    removed = await request.app.state.db.delete_motd_override(week_key, slot_key)
    if not removed:
        raise HTTPException(404, "No override for that slot")
    return {"week_key": week_key, "slot_key": slot_key, "removed": removed}


@router.post("/publish")
async def publish_now(
    request: Request, week: str = "current", user: dict = Depends(require_admin)
):
    """Kick off the motd_publish job (single-flight, same as the jobs tab)."""
    _week_offset(week)
    return await request.app.state.job_manager.run(
        "motd_publish",
        triggered_by=user["username"],
        params={"publish": True, "week": week},
    )
