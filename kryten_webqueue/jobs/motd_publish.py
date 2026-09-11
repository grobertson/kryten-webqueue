"""MOTD publish job — build the weekend poster grid and set the channel MOTD.

Resolves the current weekend's line-up from the schedule workbook, downloads
poster art, renders the Jinja snippet, and pushes it to CyTube through
api-gate's ``PUT /admin/motd``. Unresolvable or not-yet-scheduled positions
become mystery boxes, so a run always produces a complete grid and self-heals
on the next run once a curator fixes a title.

Admin-supplied per-slot overrides (custom art, hand-picked title, non-IMDb
link) are stored per workbook week and always win over the lookup.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from pathlib import Path

from ..motd.builder import build_slots, mystery_pool
from ..motd.render import render_motd
from .manager import JobError

logger = logging.getLogger(__name__)

MOTD_PUBLISH_SCHEMA = [
    {
        "name": "publish",
        "label": "Publish to channel",
        "type": "bool",
        "default": True,
        "required": False,
        "help": "Push the rendered snippet to CyTube's MOTD. Turn off to only write the HTML file.",
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
        "fire_at": fire_at,
        "starts_in": starts_in,
    }


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

    def _emit(detail: dict) -> None:
        asyncio.run_coroutine_threadsafe(ctx.progress(detail), loop)

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

    next_event = await _next_event(ctx.db)
    html = render_motd(ctx.config, week, next_event=next_event)

    output_path: str | None = None
    if not dry_run:
        out_dir = Path(ctx.config.motd.output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"motd-{week.week_key}.html"
        await asyncio.to_thread(target.write_text, html, encoding="utf-8")
        output_path = str(target)
        logger.info("motd_publish: wrote snippet → %s", target)

    published = False
    if publish:
        try:
            await ctx.api_gate.set_motd(html)
            published = True
            logger.info("motd_publish: MOTD updated for %s", week.week_key)
        except Exception as exc:  # noqa: BLE001 - surfaced as a job failure message
            raise JobError(f"Failed to set channel MOTD: {exc}") from exc

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
        "output_path": output_path,
        "dry_run": dry_run,
        "html_bytes": len(html),
    }
    await ctx.progress({"phase": "complete", **result})
    return result
