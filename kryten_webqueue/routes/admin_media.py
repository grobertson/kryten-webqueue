"""Admin media library: upload art once, get a stable public dropsugar URL.

Serves both MOTD templates (via ``media_url("slug")``) and general one-off use.
Files are served by this app's ``/media`` mount; slugs and filenames are never
reused, so a published URL can only ever point at the image it was minted for.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ..auth.session import require_admin
from ..motd import media
from ..motd.templating import MEDIA_SLUG_RE

router = APIRouter(prefix="/admin/media", tags=["admin"])

_MAX_SLUG_ATTEMPTS = 200


class MediaMeta(BaseModel):
    description: str | None = Field(default=None, max_length=500)


def _asset_out(row: dict, base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/{row['filename']}"
    return {
        "slug": row["slug"],
        "filename": row["filename"],
        "url": url,
        "snippet": f'{{{{ media_url("{row["slug"]}") }}}}',
        "mime": row["mime"],
        "bytes": row["bytes"],
        "width": row.get("width"),
        "height": row.get("height"),
        "animated": bool(row.get("animated")),
        "frame_count": row.get("frame_count"),
        "original_name": row.get("original_name"),
        "description": row.get("description"),
        "uploaded_by": row["uploaded_by"],
        "uploaded_at": (
            row["uploaded_at"].isoformat() if row.get("uploaded_at") else None
        ),
    }


async def _free_slug(db, base: str) -> str:
    base = base[:72].strip("-") or "image"
    for attempt in range(1, _MAX_SLUG_ATTEMPTS + 1):
        candidate = base if attempt == 1 else f"{base}-{attempt}"
        if not await db.media_slug_exists(candidate):
            return candidate
    raise HTTPException(409, "Too many uploads share that name; choose another slug")


@router.get("/assets")
async def list_assets(
    request: Request,
    q: str | None = None,
    limit: int = 60,
    offset: int = 0,
    user: dict = Depends(require_admin),
):
    config = request.app.state.config
    rows = await request.app.state.db.list_media_assets(
        (q or "").strip() or None, min(max(limit, 1), 200), max(offset, 0)
    )
    return {"assets": [_asset_out(r, config.media.base_url) for r in rows]}


@router.post("/assets", status_code=201)
async def upload_asset(
    request: Request,
    file: UploadFile = File(...),
    slug: str | None = Form(None),
    description: str | None = Form(None),
    user: dict = Depends(require_admin),
):
    config = request.app.state.config
    db = request.app.state.db
    cfg = config.media
    data = await file.read(cfg.max_image_bytes + 1)
    if not data:
        raise HTTPException(400, "Empty upload")
    if len(data) > cfg.max_image_bytes:
        raise HTTPException(
            413, f"File exceeds {cfg.max_image_bytes // (1024 * 1024)} MiB"
        )
    if description and len(description) > 500:
        raise HTTPException(422, "Description is limited to 500 characters")

    requested = media.slugify(slug) if slug and slug.strip() else None
    if requested is not None and not MEDIA_SLUG_RE.match(requested):
        raise HTTPException(422, "Slug must use lowercase letters, digits, and dashes")
    try:
        image = await asyncio.to_thread(
            media.process_image,
            data,
            max_pixels=cfg.max_image_pixels,
            max_frames=cfg.max_frames,
        )
        public_dir = media.ensure_dir(cfg.dir, "media.dir")
    except media.MediaError as exc:
        raise HTTPException(422, str(exc)) from exc

    final_slug = await _free_slug(db, requested or media.slugify(file.filename))
    filename = f"{final_slug}{image.ext}"
    # Claim the slug in the database first: the UNIQUE constraint, not the file
    # system, decides which of two racing uploads owns the filename.
    try:
        await db.create_media_asset(
            slug=final_slug,
            filename=filename,
            kind="image",
            mime=image.mime,
            bytes=len(image.data),
            width=image.width,
            height=image.height,
            animated=image.animated,
            frame_count=image.frame_count,
            sha256=image.sha256,
            original_name=(file.filename or "")[:255] or None,
            description=(description or "").strip() or None,
            uploaded_by=user["username"],
        )
    except Exception as exc:
        if "unique" in str(exc).lower():
            raise HTTPException(409, "That name was just taken; try again") from exc
        raise
    try:
        await asyncio.to_thread(media.write_atomic, public_dir, filename, image.data)
    except OSError as exc:
        await db.soft_delete_media_asset(final_slug, "system")
        raise HTTPException(500, f"Could not store the upload: {exc}") from exc
    await db.add_admin_audit(
        actor=user["username"],
        action="media.upload",
        entity_type="media_asset",
        entity_id=final_slug,
        summary=f"Uploaded {filename} ({len(image.data)} bytes)",
        after={"filename": filename, "sha256": image.sha256},
    )
    row = await db.get_media_asset(final_slug)
    return _asset_out(row, cfg.base_url)


@router.patch("/assets/{slug}")
async def update_asset(
    slug: str, body: MediaMeta, request: Request, user: dict = Depends(require_admin)
):
    db = request.app.state.db
    before = await db.get_media_asset(slug)
    if before is None:
        raise HTTPException(404, "No such media asset")
    description = (body.description or "").strip() or None
    await db.update_media_asset_description(slug, description)
    await db.add_admin_audit(
        actor=user["username"],
        action="media.update",
        entity_type="media_asset",
        entity_id=slug,
        summary=f"Updated description of {slug}",
        before={"description": before.get("description")},
        after={"description": description},
    )
    return _asset_out(
        await db.get_media_asset(slug), request.app.state.config.media.base_url
    )


@router.delete("/assets/{slug}")
async def delete_asset(
    slug: str, request: Request, user: dict = Depends(require_admin)
):
    db = request.app.state.db
    cfg = request.app.state.config.media
    asset = await db.get_media_asset(slug)
    if asset is None:
        raise HTTPException(404, "No such media asset")
    blocking = [
        t["name"]
        for t in await db.list_motd_templates()
        if media.references(t.get("body") or "", slug, asset["filename"])
    ]
    if blocking:
        raise HTTPException(
            409,
            {
                "message": "Still used by the current version of these templates",
                "templates": blocking,
            },
        )
    try:
        trash = media.ensure_dir(cfg.trash_dir, "media.trash_dir")
        await asyncio.to_thread(
            media.move_to_trash, Path(cfg.dir).expanduser(), trash, asset["filename"]
        )
    except (media.MediaError, OSError) as exc:
        raise HTTPException(500, f"Could not remove the file: {exc}") from exc
    await db.soft_delete_media_asset(slug, user["username"])
    await db.add_admin_audit(
        actor=user["username"],
        action="media.delete",
        entity_type="media_asset",
        entity_id=slug,
        summary=f"Deleted {asset['filename']}",
        before={"filename": asset["filename"], "sha256": asset["sha256"]},
    )
    return {"deleted": slug}
