"""MOTD publish job — build the weekend poster grid and set the channel MOTD.

Resolves the current weekend's line-up from the schedule workbook, downloads
poster art, renders whichever master template (plus zone fragments) the MOTD
schedules make live right now, and pushes the whole document to CyTube through
api-gate's ``PUT /admin/motd``. Templates are the source of truth: the live
MOTD is backed up and then replaced (docs/MOTD_TEMPLATES_SPEC.md).
Unresolvable or not-yet-scheduled positions become mystery boxes, so a run
always produces a complete grid and self-heals on the next run once a curator
fixes a title.

Admin-supplied per-slot overrides (custom art, hand-picked title, non-IMDb
link) are stored per workbook week and always win over the lookup.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import secrets
from concurrent.futures import Future
from pathlib import Path

from ..motd.builder import build_slots, mystery_pool
from ..motd.composer import render_composition, resolve_composition
from ..motd.render import motd_context
from ..motd.templating import MOTDTemplateError, js_length
from .manager import JobError

logger = logging.getLogger(__name__)

_TRIGGERS = {"motd_automation": "schedule", "scheduler": "weekly"}

MOTD_PUBLISH_SCHEMA = [
    {
        "name": "publish",
        "label": "Publish to channel",
        "type": "bool",
        "default": True,
        "required": False,
        "help": "Back up the live MOTD, then replace it with the scheduled template. Turn off to write a generated HTML file only.",
    },
    {
        "name": "dry_run",
        "label": "Dry run (render only)",
        "type": "bool",
        "default": False,
        "required": False,
        "help": "Resolve titles and render the snippet without downloading art, writing files, or touching the channel.",
    },
    {
        "name": "refresh_art",
        "label": "Re-download poster art",
        "type": "bool",
        "default": False,
        "required": False,
        "help": "Fetch poster art again even when the file already exists on disk.",
    },
    {
        "name": "week",
        "label": "Which weekend",
        "type": "enum",
        "default": "current",
        "required": False,
        "options": [
            {"value": "current", "label": "This weekend (rolls over Monday)"},
            {
                "value": "next",
                "label": "Next weekend — debut early, while weekend traffic is still here",
            },
        ],
        "help": "The sheet rolls forward on Monday; pick 'next' to publish the coming weekend's grid on Sunday.",
    },
    {
        "name": "workbook_path",
        "label": "Local workbook path (override SharePoint)",
        "type": "string",
        "default": None,
        "required": False,
    },
]


def _humanize_delta(seconds: float) -> str:
    if seconds < 90:
        return "starting now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"in {minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"in {hours}h {minutes:02d}m" if minutes else f"in {hours}h"
    days = hours // 24
    return f"in {days} day{'s' if days != 1 else ''}"


async def _next_event(db) -> dict | None:
    """Summarise the next armed playlist schedule for the MOTD context."""
    row = await db.get_next_schedule()
    if not row:
        return None
    fire_at = row.get("fire_at")
    starts_in = ""
    try:
        when = datetime.datetime.fromisoformat(str(fire_at).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        delta = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        if delta > 0:
            starts_in = _humanize_delta(delta)
    except (TypeError, ValueError):
        logger.debug("motd_publish: unparseable fire_at %r", fire_at)
    return {
        "schedule_id": row.get("id"),
        "playlist_id": row.get("playlist_id"),
        "label": row.get("label") or "Scheduled event",
        "fire_at": (
            fire_at.isoformat() if isinstance(fire_at, datetime.datetime) else fire_at
        ),
        "starts_in": starts_in,
    }


async def _write_backup(out_dir: Path, html: str) -> str:
    """Save the live MOTD before replacing it; failure aborts the publish."""
    backups = out_dir / "backups"
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()[:8]
    target = backups / f"motd-{stamp}-{digest}-{secrets.token_hex(2)}.html"

    def _write() -> None:
        backups.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")

    try:
        await asyncio.to_thread(_write)
    except OSError as exc:
        raise JobError(
            f"Could not back up the live MOTD to {backups}; nothing published: {exc}"
        ) from exc
    return str(target)


async def motd_publish_job(params: dict, ctx) -> dict:
    """Build, render, and publish the weekend MOTD."""
    dry_run = bool(params.get("dry_run", False))
    publish = bool(params.get("publish", True)) and not dry_run
    refresh_art = bool(params.get("refresh_art", False))
    workbook_path = params.get("workbook_path") or ""
    week_offset = 1 if params.get("week") == "next" else 0

    from ..motd.builder import week_context

    week_key, _ = week_context(week_offset=week_offset)
    overrides = {
        row["slot_key"]: row for row in await ctx.db.list_motd_overrides(week_key)
    }

    loop = asyncio.get_running_loop()
    progress_updates: list[Future[None]] = []

    def _emit(detail: dict) -> None:
        progress_updates.append(
            asyncio.run_coroutine_threadsafe(ctx.progress(detail), loop)
        )

    try:
        week = await asyncio.to_thread(
            build_slots,
            ctx.config,
            overrides=overrides,
            workbook_path=workbook_path,
            refresh_art=refresh_art,
            dry_run=dry_run,
            week_offset=week_offset,
            mystery_urls=mystery_pool(ctx.config, getattr(ctx, "cover_art", None)),
            emit=_emit,
        )
    except RuntimeError as exc:
        raise JobError(str(exc)) from exc
    finally:
        await asyncio.gather(
            *(asyncio.wrap_future(update) for update in progress_updates)
        )

    next_event = await _next_event(ctx.db)
    context = motd_context(ctx.config, week, next_event=next_event)
    try:
        composition = await resolve_composition(ctx.db)
        rendered = await render_composition(ctx.db, ctx.config, context, composition)
    except MOTDTemplateError as exc:
        raise JobError(f"MOTD not published: {exc}") from exc
    html = rendered.html

    out_dir = Path(ctx.config.motd.output_dir).expanduser()
    backup_path: str | None = None
    if publish:
        try:
            current = await ctx.api_gate.get_motd()
        except Exception as exc:
            raise JobError(
                f"Could not read the live MOTD to back it up; nothing published: {exc}"
            ) from exc
        backup_path = await _write_backup(out_dir, current or "")

    output_path: str | None = None
    if not dry_run:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise JobError(
                f"motd.output_dir {out_dir} is not writable by this process "
                f"({type(exc).__name__}: {exc.strerror or exc}). Point it at a "
                "directory the service can create and write to."
            ) from exc
        target = out_dir / f"motd-{week.week_key}.html"
        try:
            await asyncio.to_thread(target.write_text, html, encoding="utf-8")
        except OSError as exc:
            raise JobError(f"Could not write {target}: {exc}") from exc
        output_path = str(target)
        logger.info("motd_publish: wrote snippet → %s", target)

    published = False
    publication_id: int | None = None
    if publish:
        try:
            await ctx.api_gate.set_motd(html)
            published = True
            logger.info(
                "motd_publish: MOTD updated for %s (%s)",
                week.week_key,
                composition.master["name"],
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a job failure message
            raise JobError(f"Failed to set channel MOTD: {exc}") from exc
        triggered_by = getattr(ctx, "triggered_by", None) or "system"
        publication_id = await ctx.db.add_motd_publication(
            trigger=_TRIGGERS.get(triggered_by, "manual"),
            composition_key=composition.key,
            master_revision_id=composition.master["current_revision_id"],
            fragment_revisions=composition.fragment_revisions,
            week_key=week.week_key,
            html_sha256=hashlib.sha256(html.encode("utf-8")).hexdigest(),
            html_chars=js_length(html),
            backup_path=backup_path,
            job_run_id=getattr(ctx, "run_id", None),
            published_by=triggered_by,
        )

    resolved = sum(1 for s in week.slots if s.resolved)
    overridden = sum(1 for s in week.slots if s.source == "override")
    mystery = [s.slot_key for s in week.slots if not s.resolved]

    logger.info(
        "motd_publish: %s — %d/%d slots resolved (%d overridden, %d mystery)",
        week.week_key,
        resolved,
        len(week.slots),
        overridden,
        len(mystery),
    )

    result = {
        "week_key": week.week_key,
        "slots": len(week.slots),
        "resolved": resolved,
        "overridden": overridden,
        "mystery": len(mystery),
        "mystery_slots": mystery,
        "workbook_found": week.workbook_found,
        "warnings": week.warnings,
        "next_event": next_event,
        "published": published,
        "publication_id": publication_id,
        "composition": composition.summary(),
        "warnings_html": rendered.warnings,
        "backup_path": backup_path,
        "output_path": output_path,
        "dry_run": dry_run,
        "html_chars": js_length(html),
    }
    await ctx.progress({"phase": "complete", **result})
    return result
