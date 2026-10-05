"""Admin routes for MOTD templates, revisions, schedules, timeline, and history.

Templates are the source of truth for the channel MOTD (docs/MOTD_TEMPLATES_SPEC.md).
Nothing here publishes directly: saves and schedule edits change what the
reconcile loop resolves, and ``POST /admin/motd/publish`` remains the manual
trigger. Every mutation writes an audit row with the session's username.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth.session import require_admin
from ..catalog.db._motd_templates import MOTDTemplateConflict
from ..motd import schedule as sched
from ..motd.builder import build_slots, mystery_pool, sample_week, week_context
from ..motd.composer import (
    fragments_at,
    live_schedules,
    render_fragment_html,
    render_html,
    resolve_composition,
)
from ..motd.render import motd_context
from ..motd.templating import (
    TEMPLATE_NAME_RE,
    ZONE_NAME_RE,
    MOTDTemplateError,
    analyze,
    body_sha256,
    js_length,
)

router = APIRouter(prefix="/admin/motd", tags=["admin"])
audit_router = APIRouter(prefix="/admin/audit", tags=["admin"])

UTC = datetime.timezone.utc
MAX_TIMELINE_DAYS = 92


class TemplateCreate(BaseModel):
    name: str
    kind: Literal["master", "fragment"]
    display_name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    body: str
    note: str | None = Field(default=None, max_length=500)


class TemplateSave(BaseModel):
    body: str
    note: str = Field(min_length=1, max_length=500)
    expected_revision_id: int


class TemplateMeta(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    is_default: bool | None = None


class PreviewRequest(BaseModel):
    body: str | None = None
    name: str | None = None
    kind: Literal["master", "fragment"] = "master"
    week: Literal["current", "next"] = "current"
    grid: Literal["sample", "live"] = "sample"
    as_of: datetime.datetime | None = None
    host_master: str | None = None
    zone: str | None = None
    zone_overrides: dict[str, str | None] | None = None


class ScheduleIn(BaseModel):
    template: str
    zone: str | None = None
    label: str = Field(min_length=1, max_length=120)
    starts_at: datetime.datetime
    ends_at: datetime.datetime | None = None
    rrule: str | None = Field(default=None, max_length=500)
    duration_minutes: int | None = None
    tz: str | None = None
    priority: int = Field(default=0, ge=-1000, le=1000)
    is_active: bool = True


class AutomationIn(BaseModel):
    enabled: bool


# --- helpers -----------------------------------------------------------------


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _local(value: datetime.datetime | None, tz: str) -> str | None:
    return value.astimezone(ZoneInfo(tz)).isoformat() if value else None


def _template_out(row: dict, *, include_body: bool = False) -> dict:
    out = {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "display_name": row["display_name"],
        "description": row.get("description"),
        "is_default": bool(row.get("is_default")),
        "archived_at": _iso(row.get("archived_at")),
        "current_revision_id": row.get("current_revision_id"),
        "revision_no": row.get("revision_no"),
        "zones": row.get("zones") or [],
        "updated_at": _iso(row.get("updated_at")),
        "revision_by": row.get("revision_by"),
        "revision_at": _iso(row.get("revision_at")),
        "created_by": row.get("created_by"),
    }
    if include_body:
        out["body"] = row.get("body") or ""
        out["note"] = row.get("note")
    return out


async def _template_or_404(db, name: str) -> dict:
    template = await db.get_motd_template(name)
    if template is None:
        raise HTTPException(404, f"No template named {name!r}")
    return template


def _template_error(exc: MOTDTemplateError) -> HTTPException:
    return HTTPException(422, {"message": str(exc), "line": exc.line})


def _week_offset(week: str) -> int:
    return 1 if week == "next" else 0


async def _context(request: Request, week: str = "current", grid: str = "sample"):
    config = request.app.state.config
    if grid == "sample":
        built = sample_week(config, week_offset=_week_offset(week))
    else:
        db = request.app.state.db
        week_key, _ = week_context(week_offset=_week_offset(week))
        overrides = {
            row["slot_key"]: row for row in await db.list_motd_overrides(week_key)
        }
        built = await asyncio.to_thread(
            build_slots,
            config,
            overrides=overrides,
            dry_run=True,
            week_offset=_week_offset(week),
            mystery_urls=mystery_pool(
                config, getattr(request.app.state, "cover_art", None)
            ),
        )
    return motd_context(config, built)


async def _validate(request: Request, body: str, kind: str):
    """Static analysis plus a full sample render; returns (analysis, warnings)."""
    db = request.app.state.db
    config = request.app.state.config
    analysis = analyze(body, kind)
    context = await _context(request)
    if kind == "master":
        rendered = await render_html(
            db,
            config,
            context,
            master_body=body,
            fragments={zone: None for zone in analysis.zones},
        )
    else:
        rendered = await render_fragment_html(db, config, context, body)
    return analysis, rendered.warnings


async def _live_ids(db) -> set[int]:
    try:
        composition = await resolve_composition(db)
    except MOTDTemplateError:
        return set()
    ids = {composition.master["id"]}
    ids.update(f["id"] for f in composition.fragments.values() if f)
    return ids


async def _audit(request: Request, user: dict, **fields) -> None:
    await request.app.state.db.add_admin_audit(actor=user["username"], **fields)


# --- templates ---------------------------------------------------------------


@router.get("/templates")
async def list_templates(
    request: Request,
    kind: Literal["master", "fragment"] | None = None,
    archived: bool = False,
    user: dict = Depends(require_admin),
):
    db = request.app.state.db
    rows = await db.list_motd_templates(kind=kind, include_archived=archived)
    live = await _live_ids(db)
    return {"templates": [{**_template_out(r), "live": r["id"] in live} for r in rows]}


@router.post("/templates", status_code=201)
async def create_template(
    body: TemplateCreate, request: Request, user: dict = Depends(require_admin)
):
    db = request.app.state.db
    if not TEMPLATE_NAME_RE.match(body.name):
        raise HTTPException(
            422,
            "Name must be 2-64 lowercase letters, digits, - or _, starting with a letter or digit",
        )
    if await db.get_motd_template(body.name):
        raise HTTPException(409, f"A template named {body.name!r} already exists")
    try:
        analysis, warnings = await _validate(request, body.body, body.kind)
    except MOTDTemplateError as exc:
        raise _template_error(exc) from exc
    template = await db.create_motd_template(
        name=body.name,
        kind=body.kind,
        display_name=body.display_name,
        description=body.description,
        body=body.body,
        body_sha256=body_sha256(body.body),
        zones=analysis.zones,
        note=body.note or "Created",
        created_by=user["username"],
    )
    await _audit(
        request,
        user,
        action="motd.template.create",
        entity_type="motd_template",
        entity_id=template["id"],
        summary=f"Created {body.kind} {body.name}",
        after={"revision_id": template["current_revision_id"]},
    )
    return {**_template_out(template, include_body=True), "warnings": warnings}


@router.get("/templates/{name}")
async def get_template(
    name: str, request: Request, user: dict = Depends(require_admin)
):
    db = request.app.state.db
    template = await _template_or_404(db, name)
    return {
        **_template_out(template, include_body=True),
        "live": template["id"] in await _live_ids(db),
    }


@router.put("/templates/{name}")
async def save_template(
    name: str, body: TemplateSave, request: Request, user: dict = Depends(require_admin)
):
    db = request.app.state.db
    template = await _template_or_404(db, name)
    if template.get("archived_at"):
        raise HTTPException(409, "Archived templates cannot be edited")
    if template["current_revision_id"] != body.expected_revision_id:
        raise HTTPException(
            409, "This template was saved by someone else since you opened it"
        )
    try:
        analysis, warnings = await _validate(request, body.body, template["kind"])
    except MOTDTemplateError as exc:
        raise _template_error(exc) from exc
    try:
        saved = await db.save_motd_template_revision(
            template["id"],
            body=body.body,
            body_sha256=body_sha256(body.body),
            zones=analysis.zones,
            note=body.note,
            created_by=user["username"],
            expected_revision_id=body.expected_revision_id,
        )
    except MOTDTemplateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    await _audit(
        request,
        user,
        action="motd.template.save",
        entity_type="motd_template",
        entity_id=template["id"],
        summary=f"Saved {name} r{saved['revision_no']}: {body.note}",
        before={"revision_id": template["current_revision_id"]},
        after={"revision_id": saved["current_revision_id"]},
    )
    live = saved["id"] in await _live_ids(db)
    return {
        **_template_out(saved, include_body=True),
        "live": live,
        "warnings": warnings,
    }


@router.patch("/templates/{name}")
async def update_template_meta(
    name: str, body: TemplateMeta, request: Request, user: dict = Depends(require_admin)
):
    db = request.app.state.db
    template = await _template_or_404(db, name)
    if template.get("archived_at"):
        raise HTTPException(409, "Archived templates cannot be edited")
    if body.is_default and template["kind"] != "master":
        raise HTTPException(422, "Only master templates can be the default")
    if body.is_default is False and template.get("is_default"):
        raise HTTPException(
            422, "Make another master the default instead of clearing this one"
        )
    await db.update_motd_template_meta(
        template["id"], display_name=body.display_name, description=body.description
    )
    if body.is_default and not template.get("is_default"):
        await db.set_default_motd_template(template["id"])
    updated = await db.get_motd_template_by_id(template["id"])
    await _audit(
        request,
        user,
        action="motd.template.meta",
        entity_type="motd_template",
        entity_id=template["id"],
        summary=f"Updated {name}",
        before={
            "display_name": template["display_name"],
            "description": template.get("description"),
            "is_default": bool(template.get("is_default")),
        },
        after={
            "display_name": updated["display_name"],
            "description": updated.get("description"),
            "is_default": bool(updated.get("is_default")),
        },
    )
    return _template_out(updated, include_body=True)


@router.post("/templates/{name}/archive")
async def archive_template(
    name: str, request: Request, user: dict = Depends(require_admin)
):
    db = request.app.state.db
    template = await _template_or_404(db, name)
    if template.get("archived_at"):
        return _template_out(template)
    if template.get("is_default"):
        raise HTTPException(409, "Make another master the default before archiving")
    live = await db.count_live_motd_schedules_for_template(
        template["id"], datetime.datetime.now(UTC)
    )
    if live:
        raise HTTPException(
            409, f"{live} active schedule(s) still use this template; remove them first"
        )
    await db.archive_motd_template(template["id"])
    await _audit(
        request,
        user,
        action="motd.template.archive",
        entity_type="motd_template",
        entity_id=template["id"],
        summary=f"Archived {name}",
    )
    return _template_out(await db.get_motd_template_by_id(template["id"]))


@router.get("/templates/{name}/revisions")
async def list_revisions(
    name: str, request: Request, user: dict = Depends(require_admin)
):
    db = request.app.state.db
    template = await _template_or_404(db, name)
    rows = await db.list_motd_template_revisions(template["id"])
    return {
        "revisions": [
            {
                "id": r["id"],
                "revision_no": r["revision_no"],
                "note": r.get("note"),
                "zones": r.get("zones") or [],
                "created_by": r["created_by"],
                "created_at": _iso(r["created_at"]),
                "current": r["id"] == template["current_revision_id"],
            }
            for r in rows
        ]
    }


@router.get("/templates/{name}/revisions/{revision_no}")
async def get_revision(
    name: str, revision_no: int, request: Request, user: dict = Depends(require_admin)
):
    db = request.app.state.db
    template = await _template_or_404(db, name)
    revision = await db.get_motd_template_revision(template["id"], revision_no)
    if revision is None:
        raise HTTPException(404, "No such revision")
    return {
        "revision_no": revision["revision_no"],
        "body": revision["body"],
        "note": revision.get("note"),
        "created_by": revision["created_by"],
        "created_at": _iso(revision["created_at"]),
    }


@router.post("/templates/{name}/revisions/{revision_no}/restore")
async def restore_revision(
    name: str, revision_no: int, request: Request, user: dict = Depends(require_admin)
):
    db = request.app.state.db
    template = await _template_or_404(db, name)
    if template.get("archived_at"):
        raise HTTPException(409, "Archived templates cannot be edited")
    revision = await db.get_motd_template_revision(template["id"], revision_no)
    if revision is None:
        raise HTTPException(404, "No such revision")
    try:
        analysis, warnings = await _validate(
            request, revision["body"], template["kind"]
        )
    except MOTDTemplateError as exc:
        raise _template_error(exc) from exc
    try:
        saved = await db.save_motd_template_revision(
            template["id"],
            body=revision["body"],
            body_sha256=revision["body_sha256"],
            zones=analysis.zones,
            note=f"Restored revision {revision_no}",
            created_by=user["username"],
            expected_revision_id=template["current_revision_id"],
        )
    except MOTDTemplateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    await _audit(
        request,
        user,
        action="motd.template.restore",
        entity_type="motd_template",
        entity_id=template["id"],
        summary=f"Restored {name} r{revision_no} as r{saved['revision_no']}",
        before={"revision_id": template["current_revision_id"]},
        after={"revision_id": saved["current_revision_id"]},
    )
    return {**_template_out(saved, include_body=True), "warnings": warnings}


# --- preview -----------------------------------------------------------------


@router.post("/templates/preview")
async def preview(
    body: PreviewRequest, request: Request, user: dict = Depends(require_admin)
):
    """Render unsaved or saved template source; template faults return ok=false."""
    db = request.app.state.db
    config = request.app.state.config
    source, kind = body.body, body.kind
    if source is None:
        if not body.name:
            raise HTTPException(422, "Provide body or name")
        saved = await _template_or_404(db, body.name)
        source, kind = saved["body"], saved["kind"]
    at = datetime.datetime.now(UTC)
    if body.as_of:
        as_of = body.as_of
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=ZoneInfo(config.motd.timezone))
        at = as_of.astimezone(UTC)
    context = await _context(request, body.week, body.grid)
    notes: list[str] = []
    try:
        if kind == "master":
            zones = analyze(source, "master").zones
            fragments = await _preview_fragments(db, zones, at, body.zone_overrides)
            rendered = await render_html(
                db, config, context, master_body=source, fragments=fragments, now=at
            )
        else:
            analyze(source, "fragment")
            host = await _host_master(db, body.host_master, at)
            zone = body.zone or (host.get("zones") or [None])[0] if host else None
            if host is None or zone is None or zone not in (host.get("zones") or []):
                notes.append("Shown on its own: the host master has no matching zone")
                rendered = await render_fragment_html(
                    db, config, context, source, now=at
                )
            else:
                fragments = await _preview_fragments(
                    db, host.get("zones") or [], at, body.zone_overrides
                )
                fragments[zone] = source
                rendered = await render_html(
                    db,
                    config,
                    context,
                    master_body=host["body"],
                    fragments=fragments,
                    now=at,
                )
                notes.append(f"Shown in zone {zone!r} of master {host['name']!r}")
    except MOTDTemplateError as exc:
        return {"ok": False, "error": str(exc), "line": exc.line}

    return {
        "ok": True,
        "html": rendered.html,
        "chars": js_length(rendered.html),
        "max_chars": config.motd.max_html_chars,
        "warnings": rendered.warnings,
        "notes": notes,
        "as_of": at.isoformat(),
    }


async def _preview_fragments(
    db, zones: list[str], at: datetime.datetime, overrides: dict[str, str | None] | None
) -> dict[str, str | None]:
    scheduled = await fragments_at(db, zones, at)
    fragments = {z: (f["body"] if f else None) for z, f in scheduled.items()}
    for zone, name in (overrides or {}).items():
        if zone not in fragments:
            continue
        if not name:
            fragments[zone] = None
            continue
        fragment = await db.get_motd_template(name)
        if fragment is None or fragment["kind"] != "fragment":
            raise MOTDTemplateError(f"No fragment template named {name!r}")
        fragments[zone] = fragment["body"]
    return fragments


async def _host_master(db, name: str | None, at: datetime.datetime) -> dict | None:
    if name:
        host = await db.get_motd_template(name)
        if host is None or host["kind"] != "master":
            raise MOTDTemplateError(f"No master template named {name!r}")
        return host
    return (await resolve_composition(db, at)).master


# --- schedules ---------------------------------------------------------------


def _schedule_out(row: dict, tz_default: str) -> dict:
    tz = row.get("tz") or tz_default
    return {
        "id": row["id"],
        "template_id": row["template_id"],
        "template": row.get("template_name"),
        "template_kind": row.get("template_kind"),
        "template_display_name": row.get("template_display_name"),
        "zone": row.get("zone"),
        "label": row["label"],
        "starts_at": _iso(row["starts_at"]),
        "ends_at": _iso(row.get("ends_at")),
        "starts_at_local": _local(row["starts_at"], tz),
        "ends_at_local": _local(row.get("ends_at"), tz),
        "rrule": row.get("rrule"),
        "duration_minutes": row.get("duration_minutes"),
        "tz": tz,
        "priority": row.get("priority") or 0,
        "is_active": bool(row.get("is_active")),
        "created_by": row.get("created_by"),
        "updated_at": _iso(row.get("updated_at")),
    }


async def _schedule_fields(request: Request, body: ScheduleIn) -> dict:
    db = request.app.state.db
    config = request.app.state.config
    tz = body.tz or config.motd.timezone
    try:
        zone_info = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(422, f"Unknown timezone {tz!r}") from exc
    template = await db.get_motd_template(body.template)
    if template is None or template.get("archived_at"):
        raise HTTPException(422, f"No active template named {body.template!r}")
    zone = (body.zone or "").strip() or None
    if template["kind"] == "master" and zone:
        raise HTTPException(422, "Master schedules do not take a zone")
    if template["kind"] == "fragment":
        if not zone or not ZONE_NAME_RE.match(zone):
            raise HTTPException(422, "Fragment schedules need a valid zone name")

    def localize(value: datetime.datetime | None) -> datetime.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone_info)
        return value.astimezone(UTC)

    rrule = (body.rrule or "").strip() or None
    fields = {
        "template_id": template["id"],
        "zone": zone,
        "label": body.label.strip(),
        "starts_at": localize(body.starts_at),
        "ends_at": localize(body.ends_at),
        "rrule": rrule,
        "duration_minutes": body.duration_minutes if rrule else None,
        "tz": tz,
        "priority": body.priority,
        "is_active": body.is_active,
    }
    try:
        sched.validate(fields)
    except sched.ScheduleError as exc:
        raise HTTPException(422, str(exc)) from exc
    return fields


def _audit_schedule(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {
        k: (_iso(v) if isinstance(v, datetime.datetime) else v) for k, v in row.items()
    }


@router.get("/schedules")
async def list_schedules(request: Request, user: dict = Depends(require_admin)):
    tz = request.app.state.config.motd.timezone
    rows = await request.app.state.db.list_motd_schedules()
    return {"timezone": tz, "schedules": [_schedule_out(r, tz) for r in rows]}


@router.post("/schedules", status_code=201)
async def create_schedule(
    body: ScheduleIn, request: Request, user: dict = Depends(require_admin)
):
    db = request.app.state.db
    fields = await _schedule_fields(request, body)
    schedule_id = await db.create_motd_schedule(created_by=user["username"], **fields)
    row = await db.get_motd_schedule(schedule_id)
    await _audit(
        request,
        user,
        action="motd.schedule.create",
        entity_type="motd_schedule",
        entity_id=schedule_id,
        summary=f"Scheduled {body.template}: {fields['label']}",
        after=_audit_schedule(row),
    )
    return {"id": schedule_id}


@router.put("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: int,
    body: ScheduleIn,
    request: Request,
    user: dict = Depends(require_admin),
):
    db = request.app.state.db
    before = await db.get_motd_schedule(schedule_id)
    if before is None:
        raise HTTPException(404, "No such schedule")
    fields = await _schedule_fields(request, body)
    await db.update_motd_schedule(schedule_id, **fields)
    after = await db.get_motd_schedule(schedule_id)
    await _audit(
        request,
        user,
        action="motd.schedule.update",
        entity_type="motd_schedule",
        entity_id=schedule_id,
        summary=f"Updated schedule {fields['label']}",
        before=_audit_schedule(before),
        after=_audit_schedule(after),
    )
    return {"id": schedule_id}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: int, request: Request, user: dict = Depends(require_admin)
):
    db = request.app.state.db
    before = await db.get_motd_schedule(schedule_id)
    if before is None:
        raise HTTPException(404, "No such schedule")
    await db.delete_motd_schedule(schedule_id)
    await _audit(
        request,
        user,
        action="motd.schedule.delete",
        entity_type="motd_schedule",
        entity_id=schedule_id,
        summary=f"Deleted schedule {before['label']}",
        before=_audit_schedule(before),
    )
    return {"deleted": schedule_id}


@router.get("/timeline")
async def timeline(
    request: Request,
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    user: dict = Depends(require_admin),
):
    db = request.app.state.db
    tz = request.app.state.config.motd.timezone
    zone_info = ZoneInfo(tz)

    def norm(value: datetime.datetime | None, default: datetime.datetime):
        if value is None:
            return default
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone_info)
        return value.astimezone(UTC)

    now = datetime.datetime.now(UTC)
    start_at = norm(start, now)
    end_at = norm(end, start_at + datetime.timedelta(days=30))
    if end_at <= start_at:
        raise HTTPException(422, "end must be after start")
    if end_at - start_at > datetime.timedelta(days=MAX_TIMELINE_DAYS):
        raise HTTPException(
            422, f"Timeline range is limited to {MAX_TIMELINE_DAYS} days"
        )

    schedules = await live_schedules(db)
    by_id = {s["id"]: s for s in schedules}
    segments, shadowed = sched.timeline(schedules, start_at, end_at)
    default = await db.get_default_motd_template()
    templates: dict[int, dict | None] = {}

    async def template(template_id: int) -> dict | None:
        if template_id not in templates:
            templates[template_id] = await db.get_motd_template_by_id(template_id)
        return templates[template_id]

    out, warnings = [], []
    for segment in segments:
        master_sid = segment.winners.get(None)
        master = (
            await template(by_id[master_sid]["template_id"]) if master_sid else default
        )
        master_zones = set((master or {}).get("zones") or [])
        zones = {}
        for scope, sid in segment.winners.items():
            if scope is None:
                continue
            fragment = await template(by_id[sid]["template_id"])
            hidden = scope not in master_zones
            zones[scope] = {
                "template": fragment["name"] if fragment else None,
                "schedule_id": sid,
                "hidden": hidden,
            }
            if hidden:
                warnings.append(
                    f"{by_id[sid]['label']}: zone {scope!r} is not in master "
                    f"{(master or {}).get('name')!r} from {_local(segment.start, tz)}"
                )
        out.append(
            {
                "start": _iso(segment.start),
                "end": _iso(segment.end),
                "start_local": _local(segment.start, tz),
                "end_local": _local(segment.end, tz),
                "master": (master or {}).get("name"),
                "master_schedule_id": master_sid,
                "zones": zones,
            }
        )
    for sid in sorted(shadowed):
        warnings.append(
            f"{by_id[sid]['label']} never shows in this range: always outranked"
        )
    return {"timezone": tz, "segments": out, "warnings": warnings}


# --- history & automation ----------------------------------------------------


@router.get("/publications")
async def list_publications(
    request: Request, limit: int = 50, user: dict = Depends(require_admin)
):
    db = request.app.state.db
    rows = await db.list_motd_publications(min(max(limit, 1), 200))
    names: dict[int, str | None] = {}

    async def revision_name(revision_id: int | None) -> str | None:
        if revision_id is None:
            return None
        if revision_id not in names:
            revision = await db.get_motd_revision(revision_id)
            template = (
                await db.get_motd_template_by_id(revision["template_id"])
                if revision
                else None
            )
            names[revision_id] = (
                f"{template['name']} r{revision['revision_no']}"
                if template and revision
                else None
            )
        return names[revision_id]

    out = []
    for row in rows:
        fragments = row.get("fragment_revisions") or {}
        out.append(
            {
                "id": row["id"],
                "published_at": _iso(row["published_at"]),
                "trigger": row["trigger"],
                "published_by": row["published_by"],
                "week_key": row["week_key"],
                "master": await revision_name(row["master_revision_id"]),
                "zones": {z: await revision_name(r) for z, r in fragments.items()},
                "html_chars": row["html_chars"],
                "job_run_id": row.get("job_run_id"),
                "backup_path": row.get("backup_path"),
            }
        )
    return {"publications": out}


@router.get("/automation")
async def get_automation(request: Request, user: dict = Depends(require_admin)):
    db = request.app.state.db
    config = request.app.state.config
    latest = await db.get_latest_motd_publication()
    try:
        composition = (await resolve_composition(db)).summary()
        error = None
    except MOTDTemplateError as exc:
        composition, error = None, str(exc)
    return {
        "enabled": config.motd.automation_enabled,
        "interval_seconds": config.motd.reconcile_interval_seconds,
        "timezone": config.motd.timezone,
        "desired": composition,
        "error": error,
        "in_sync": bool(
            latest
            and composition
            and latest["composition_key"] == composition["composition_key"]
        ),
        "latest_publication_at": _iso(latest["published_at"]) if latest else None,
    }


@router.put("/automation")
async def set_automation(
    body: AutomationIn, request: Request, user: dict = Depends(require_admin)
):
    config = request.app.state.config
    previous = config.motd.automation_enabled
    config.motd.automation_enabled = body.enabled
    try:
        config.save()
    except (RuntimeError, OSError) as exc:
        config.motd.automation_enabled = previous
        raise HTTPException(
            500, f"Could not persist the setting; automation unchanged: {exc}"
        ) from exc
    await _audit(
        request,
        user,
        action="motd.automation.set",
        entity_type="motd_automation",
        entity_id="enabled",
        summary=f"Automation {'enabled' if body.enabled else 'disabled'}",
        before={"enabled": previous},
        after={"enabled": body.enabled},
    )
    return {"enabled": body.enabled}


# --- audit -------------------------------------------------------------------


@audit_router.get("")
async def list_audit(
    request: Request,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 100,
    user: dict = Depends(require_admin),
):
    rows = await request.app.state.db.list_admin_audit(
        entity_type, entity_id, min(max(limit, 1), 500)
    )
    return {
        "entries": [
            {
                "id": r["id"],
                "at": _iso(r["at"]),
                "actor": r["actor"],
                "action": r["action"],
                "entity_type": r["entity_type"],
                "entity_id": r["entity_id"],
                "summary": r.get("summary"),
                "before": r.get("before"),
                "after": r.get("after"),
            }
            for r in rows
        ]
    }
