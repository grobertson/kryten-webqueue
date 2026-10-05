"""Resolve which master and fragments are live, and render them for publishing.

This is the only place that combines stored templates, schedules, media, and
the weekend context. The publish job, the reconcile loop, and the admin
preview all go through it so they can never disagree about what "live" means.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import schedule as sched
from .templating import (
    MOTDTemplateError,
    analyze,
    body_sha256,
    check_length,
    lint,
    render_fragment,
    render_template,
    validate_master_output,
)

UTC = datetime.timezone.utc
SEED_TEMPLATE_NAME = "channel-z-weekend"
_SEED_PATH = (
    Path(__file__).resolve().parent.parent
    / "templates"
    / "motd"
    / "seed_default_master.html"
)


@dataclass
class Composition:
    master: dict
    fragments: dict[str, dict | None] = field(default_factory=dict)
    master_schedule_id: int | None = None
    fragment_schedule_ids: dict[str, int | None] = field(default_factory=dict)

    @property
    def fragment_revisions(self) -> dict[str, int | None]:
        return {
            zone: (fragment["current_revision_id"] if fragment else None)
            for zone, fragment in sorted(self.fragments.items())
        }

    @property
    def key(self) -> str:
        """Identity of what is displayed; excludes the weekend (that is the job's input)."""
        payload = json.dumps(
            {
                "master": self.master["current_revision_id"],
                "fragments": self.fragment_revisions,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def summary(self) -> dict:
        return {
            "master": self.master["name"],
            "master_revision_id": self.master["current_revision_id"],
            "master_schedule_id": self.master_schedule_id,
            "zones": {
                zone: (
                    {
                        "template": fragment["name"],
                        "revision_id": fragment["current_revision_id"],
                        "schedule_id": self.fragment_schedule_ids.get(zone),
                    }
                    if fragment
                    else None
                )
                for zone, fragment in sorted(self.fragments.items())
            },
            "composition_key": self.key,
        }


def seed_body() -> str:
    return _SEED_PATH.read_text(encoding="utf-8")


async def ensure_seeded(db) -> dict | None:
    """Create the default master on first use; return it if created."""
    if await db.count_motd_templates():
        return None
    body = seed_body()
    analysis = analyze(body, "master")
    template = await db.create_motd_template(
        name=SEED_TEMPLATE_NAME,
        kind="master",
        display_name="Channel Z Weekend",
        description="Seeded from the built-in weekend layout.",
        body=body,
        body_sha256=body_sha256(body),
        zones=analysis.zones,
        note="Initial seed",
        created_by="system",
        is_default=True,
    )
    await db.add_admin_audit(
        actor="system",
        action="motd.template.seed",
        entity_type="motd_template",
        entity_id=template["id"],
        summary=f"Seeded default master {SEED_TEMPLATE_NAME}",
        after={"revision_id": template["current_revision_id"]},
    )
    return template


async def live_schedules(db) -> list[dict]:
    return [
        s
        for s in await db.list_motd_schedules(include_inactive=False)
        if not s.get("template_archived_at")
    ]


async def resolve_composition(db, at: datetime.datetime | None = None) -> Composition:
    """What should be on the channel at ``at`` (default now)."""
    await ensure_seeded(db)
    at = at or datetime.datetime.now(UTC)
    winners = sched.active_at(await live_schedules(db), at)

    master_occ = winners.get(None)
    master = None
    if master_occ is not None:
        master = await db.get_motd_template_by_id(master_occ.template_id)
    if master is None or master.get("archived_at") or master["kind"] != "master":
        master_occ = None
        master = await db.get_default_motd_template()
    if master is None:
        raise MOTDTemplateError("No default master template is set")

    composition = Composition(
        master=master,
        master_schedule_id=master_occ.schedule_id if master_occ else None,
    )
    fragments, ids = await _fragments_for(db, master.get("zones") or [], winners)
    composition.fragments = fragments
    composition.fragment_schedule_ids = ids
    return composition


async def _fragments_for(
    db, zones: list[str], winners: dict
) -> tuple[dict[str, dict | None], dict[str, int | None]]:
    fragments: dict[str, dict | None] = {}
    ids: dict[str, int | None] = {}
    for zone in zones:
        occ = winners.get(zone)
        fragment = await db.get_motd_template_by_id(occ.template_id) if occ else None
        if fragment and (fragment.get("archived_at") or fragment["kind"] != "fragment"):
            fragment = None
        fragments[zone] = fragment
        ids[zone] = occ.schedule_id if fragment and occ else None
    return fragments, ids


async def fragments_at(
    db, zones: list[str], at: datetime.datetime
) -> dict[str, dict | None]:
    """Scheduled fragment per zone at ``at``, for previewing an arbitrary master."""
    winners = sched.active_at(await live_schedules(db), at)
    fragments, _ = await _fragments_for(db, zones, winners)
    return fragments


async def media_urls_for(db, config, bodies: list[str], kinds: list[str]) -> dict:
    slugs: set[str] = set()
    for body, kind in zip(bodies, kinds):
        slugs |= analyze(body, kind).media_slugs
    base = config.media.base_url.rstrip("/")
    urls = {}
    for slug in sorted(slugs):
        asset = await db.get_media_asset(slug)
        if asset:
            urls[slug] = f"{base}/{asset['filename']}"
    return urls


def expected_slots(context: dict) -> set[str]:
    return {slot.slot_key for slot in context["slots"]}


@dataclass
class Rendered:
    html: str
    warnings: list[str]


async def render_html(
    db,
    config,
    context: dict,
    *,
    master_body: str,
    fragments: dict[str, str | None],
    now: datetime.datetime | None = None,
) -> Rendered:
    """Render, enforce the movie grid and CyTube's length limit, and lint."""
    bodies = [master_body, *[b for b in fragments.values() if b]]
    kinds = ["master", *["fragment" for b in fragments.values() if b]]
    html = render_template(
        master_body,
        fragments,
        context,
        media_urls=await media_urls_for(db, config, bodies, kinds),
        timezone=config.motd.timezone,
        now=now,
    )
    validate_master_output(html, expected_slots(context), config.motd.max_html_chars)
    return Rendered(html=html, warnings=lint(html, config.motd.max_html_chars))


async def render_composition(
    db, config, context: dict, composition: Composition, *, now=None
) -> Rendered:
    return await render_html(
        db,
        config,
        context,
        master_body=composition.master["body"],
        fragments={
            zone: (fragment["body"] if fragment else None)
            for zone, fragment in composition.fragments.items()
        },
        now=now,
    )


async def render_fragment_html(
    db, config, context: dict, body: str, *, now=None
) -> Rendered:
    """A fragment on its own (no host master); length-checked and linted."""
    html = render_fragment(
        body,
        context,
        media_urls=await media_urls_for(db, config, [body], ["fragment"]),
        timezone=config.motd.timezone,
        now=now,
    )
    check_length(html, config.motd.max_html_chars)
    return Rendered(html=html, warnings=lint(html, config.motd.max_html_chars))
