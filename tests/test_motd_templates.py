"""MOTD templates: sandbox, zones, schedules, composition, automation, retention.

Runs against the real SQLite layers (monolith and partitioned) so native value
types are exercised end to end instead of mocked away.
"""

import datetime
import os
import time
from types import SimpleNamespace

import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.catalog.db._motd_templates import MOTDTemplateConflict
from kryten_webqueue.config import DatabaseConfig, MediaConfig, MOTDConfig
from kryten_webqueue.motd import builder, composer, render, schedule, templating
from kryten_webqueue.motd.automation import MOTDAutomation, motd_retention_prune_job
from kryten_webqueue.motd.templating import MOTDTemplateError

UTC = datetime.timezone.utc
ET = "America/New_York"


@pytest.fixture(params=["monolith", "partitioned"])
async def db(request, tmp_path):
    database = Database(
        DatabaseConfig(
            backend="sqlite",
            layout=request.param,
            data_dir=str(tmp_path / "data"),
            db_path=str(tmp_path / "mono.db"),
        )
    )
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


def _config(tmp_path, **motd_kw):
    return SimpleNamespace(
        motd=MOTDConfig(
            poster_dir=str(tmp_path / "boxes"),
            output_dir=str(tmp_path / "out"),
            mystery_box_url="https://cdn.example/mystery.jpg",
            slots=4,
            **motd_kw,
        ),
        media=MediaConfig(
            dir=str(tmp_path / "media"), base_url="https://q.example/media"
        ),
    )


def _context(config):
    return render.motd_context(config, builder.sample_week(config))


GRID_MASTER = '<h1>{{ headline }}</h1>{{ movie_grid() }}{{ zone("promo") }}'


# --- sandbox & static analysis -------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        '{% include "channel_z.html" %}',
        '{% extends "x" %}',
        '{% import "x" as y %}',
        '{% from "x" import y %}',
    ],
)
def test_filesystem_composition_is_rejected(body):
    with pytest.raises(MOTDTemplateError, match="zone"):
        templating.analyze(body, "master")


@pytest.mark.parametrize(
    "body",
    [
        "{{ ''.__class__.__mro__ }}",
        "{{ banner_url.__class__ }}",
        "{{ slots.append(1) }}",
        "{{ config }}",
    ],
)
def test_sandbox_blocks_escapes_and_unknown_names(tmp_path, body):
    config = _config(tmp_path)
    with pytest.raises(MOTDTemplateError):
        templating.render_template(
            body + "{{ movie_grid() }}", {}, _context(config), media_urls={}
        )


def test_zones_are_static_unique_literals():
    assert templating.analyze(GRID_MASTER, "master").zones == ["promo"]
    with pytest.raises(MOTDTemplateError, match="more than once"):
        templating.analyze('{{ zone("a") }}{{ zone("a") }}', "master")
    with pytest.raises(MOTDTemplateError, match="quoted name"):
        templating.analyze("{{ zone(week_key) }}", "master")
    with pytest.raises(MOTDTemplateError, match="Invalid zone"):
        templating.analyze('{{ zone("Bad Zone") }}', "master")


@pytest.mark.parametrize("func", ["zone", "zone_active", "movie_grid"])
def test_fragments_cannot_compose(func):
    with pytest.raises(MOTDTemplateError, match="only available in master"):
        templating.analyze(f'{{{{ {func}("x") }}}}', "fragment")


def test_syntax_errors_carry_the_line_number():
    with pytest.raises(MOTDTemplateError) as info:
        templating.analyze("ok\n{% if %}", "master")
    assert info.value.line == 2


def test_zone_renders_wrapper_only_when_filled(tmp_path):
    context = _context(_config(tmp_path))
    empty = templating.render_template(
        GRID_MASTER, {"promo": None}, context, media_urls={}
    )
    assert "kryten-motd-zone" not in empty
    filled = templating.render_template(
        GRID_MASTER + '{% if zone_active("promo") %}<i>on</i>{% endif %}',
        {"promo": '<b>{{ week_key }} & {{ "<x>" }}</b>'},
        context,
        media_urls={},
    )
    assert 'data-kryten-motd-zone="promo"' in filled
    assert f"<b>{context['week_key']} & &lt;x&gt;</b>" in filled
    assert "<i>on</i>" in filled


def test_media_url_resolves_known_slugs_only(tmp_path):
    context = _context(_config(tmp_path))
    body = '{{ movie_grid() }}<img src="{{ media_url("pumpkin") }}">'
    html = templating.render_template(
        body, {}, context, media_urls={"pumpkin": "https://q.example/media/pumpkin.gif"}
    )
    assert 'src="https://q.example/media/pumpkin.gif"' in html
    with pytest.raises(MOTDTemplateError, match="Unknown or deleted media"):
        templating.render_template(body, {}, context, media_urls={})


def test_required_grid_and_length_limit(tmp_path):
    config = _config(tmp_path)
    context = _context(config)
    slots = composer.expected_slots(context)
    html = templating.render_template(GRID_MASTER, {}, context, media_urls={})
    templating.validate_master_output(html, slots, 20000)

    with pytest.raises(MOTDTemplateError, match="missing night1-slot1"):
        templating.validate_master_output("<p>none</p>", slots, 20000)
    with pytest.raises(MOTDTemplateError, match="unexpected night9-slot1"):
        templating.validate_master_output(
            html
            + '<a data-kryten-motd-slot="night9-slot1"><img data-kryten-motd-slot="night9-slot1"></a>',
            slots,
            20000,
        )
    # Hand-written markers pass as long as the slot set matches.
    manual = "".join(
        f'<a data-kryten-motd-slot="{s}"><img data-kryten-motd-slot="{s}"></a>'
        for s in sorted(slots)
    )
    templating.validate_master_output(manual, slots, 20000)


def test_length_counts_utf16_code_units_like_cytube():
    # Each square emoji is one Python char but two JavaScript code units.
    assert templating.js_length("🟥" * 3) == 6
    templating.check_length("🟥" * 10000, 20000)
    with pytest.raises(MOTDTemplateError, match="silently truncates"):
        templating.check_length("🟥" * 10000 + "a", 20000)


def test_lint_flags_stripped_markup():
    warnings = templating.lint(
        '<!-- c --><script></script><img onerror="x"><a href="javascript:x">'
        '<video></video><img src="http://x">',
        20000,
    )
    assert len(warnings) == 6


def test_seed_master_is_valid_and_has_a_promo_zone(tmp_path):
    config = _config(tmp_path)
    body = composer.seed_body()
    assert templating.analyze(body, "master").zones == ["promo"]
    html = templating.render_template(
        body, {"promo": None}, _context(config), media_urls={}
    )
    templating.validate_master_output(
        html, composer.expected_slots(_context(config)), 20000
    )


# --- schedules ----------------------------------------------------------------


def _sched(sid, *, template_id=1, zone=None, priority=0, updated=None, **kw):
    return {
        "id": sid,
        "template_id": template_id,
        "zone": zone,
        "priority": priority,
        "tz": ET,
        "updated_at": updated or datetime.datetime(2026, 1, 1, tzinfo=UTC),
        **kw,
    }


def _et(*args):
    return datetime.datetime(*args, tzinfo=schedule._zone(ET)).astimezone(UTC)


def test_one_off_windows_are_half_open():
    s = _sched(1, starts_at=_et(2026, 10, 1), ends_at=_et(2026, 11, 1))
    assert schedule.active_at([s], _et(2026, 10, 1))[None].schedule_id == 1
    assert schedule.active_at([s], _et(2026, 11, 1)) == {}


def test_priority_then_narrower_window_then_recency_wins():
    october = _sched(1, starts_at=_et(2026, 10, 1), ends_at=_et(2026, 11, 1))
    halloween = _sched(2, starts_at=_et(2026, 10, 31), ends_at=_et(2026, 11, 1))
    at = _et(2026, 10, 31, 20)
    assert schedule.active_at([october, halloween], at)[None].schedule_id == 2
    loud = {**october, "priority": 5}
    assert schedule.active_at([loud, halloween], at)[None].schedule_id == 1
    twin = {
        **halloween,
        "id": 3,
        "updated_at": datetime.datetime(2026, 2, 1, tzinfo=UTC),
    }
    assert schedule.active_at([halloween, twin], at)[None].schedule_id == 3


def test_recurring_window_stays_on_the_wall_clock_across_dst():
    thursday = _sched(
        1,
        zone="promo",
        starts_at=_et(2026, 10, 1, 18),
        ends_at=None,
        rrule="FREQ=WEEKLY;BYDAY=TH",
        duration_minutes=300,
    )
    schedule.validate(thursday)
    # Before (EDT) and after (EST) the 2026-11-01 change: 18:00 local both times.
    for day in (datetime.date(2026, 10, 29), datetime.date(2026, 11, 5)):
        assert schedule.active_at([thursday], _et(day.year, day.month, day.day, 18))
        assert schedule.active_at([thursday], _et(day.year, day.month, day.day, 22, 59))
        assert not schedule.active_at([thursday], _et(day.year, day.month, day.day, 23))
        assert not schedule.active_at(
            [thursday], _et(day.year, day.month, day.day, 17, 59)
        )
    assert not schedule.active_at([thursday], _et(2026, 10, 30, 19))


def test_series_bounds_clip_recurring_occurrences():
    s = _sched(
        1,
        starts_at=_et(2026, 10, 1, 18),
        ends_at=_et(2026, 10, 8, 20),
        rrule="FREQ=DAILY",
        duration_minutes=240,
    )
    assert schedule.active_at([s], _et(2026, 10, 8, 19))
    assert not schedule.active_at([s], _et(2026, 10, 8, 20, 30))
    assert not schedule.active_at([s], _et(2026, 10, 9, 19))


@pytest.mark.parametrize(
    "fields,match",
    [
        (
            {"rrule": "FREQ=DAILY;COUNT=3", "duration_minutes": 60},
            "DTSTART, UNTIL, or COUNT",
        ),
        ({"rrule": "FREQ=DAILY", "duration_minutes": 0}, "duration"),
        ({"rrule": "NOT A RULE", "duration_minutes": 60}, "Invalid recurrence"),
        ({"rrule": None, "ends_at": None}, "need an end"),
        (
            {
                "rrule": "FREQ=YEARLY;BYMONTH=12",
                "duration_minutes": 60,
                "ends_at": _et(2026, 11, 1),
            },
            "no occurrence",
        ),
    ],
)
def test_invalid_schedules_are_rejected(fields, match):
    base = _sched(1, starts_at=_et(2026, 10, 1), ends_at=_et(2026, 12, 31))
    with pytest.raises(schedule.ScheduleError, match=match):
        schedule.validate({**base, **fields})


def test_timeline_segments_and_shadowed_schedules():
    october = _sched(1, starts_at=_et(2026, 10, 1), ends_at=_et(2026, 11, 1))
    halloween = _sched(2, starts_at=_et(2026, 10, 31), ends_at=_et(2026, 11, 1))
    buried = _sched(
        3, starts_at=_et(2026, 10, 10), ends_at=_et(2026, 10, 11), priority=-1
    )
    segments, shadowed = schedule.timeline(
        [october, halloween, buried], _et(2026, 10, 1), _et(2026, 11, 2)
    )
    assert [s.winners.get(None) for s in segments] == [1, 2, None]
    assert shadowed == {3}


# --- database & composition ---------------------------------------------------


async def _make(db, name, kind, body, default=False):
    return await db.create_motd_template(
        name=name,
        kind=kind,
        display_name=name,
        description=None,
        body=body,
        body_sha256=templating.body_sha256(body),
        zones=templating.analyze(body, kind).zones,
        note="t",
        created_by="tester",
        is_default=default,
    )


async def test_revisions_are_append_only_and_refuse_stale_editors(db):
    t = await _make(db, "m", "master", GRID_MASTER)
    first = t["current_revision_id"]
    saved = await db.save_motd_template_revision(
        t["id"],
        body="v2",
        body_sha256="h",
        zones=[],
        note="n",
        created_by="a",
        expected_revision_id=first,
    )
    assert saved["revision_no"] == 2 and saved["body"] == "v2"
    with pytest.raises(MOTDTemplateConflict):
        await db.save_motd_template_revision(
            t["id"],
            body="v3",
            body_sha256="h",
            zones=[],
            note="n",
            created_by="b",
            expected_revision_id=first,
        )
    revisions = await db.list_motd_template_revisions(t["id"])
    assert [r["revision_no"] for r in revisions] == [2, 1]
    assert isinstance(revisions[0]["created_at"], datetime.datetime)


async def test_only_one_default_master(db):
    a = await _make(db, "a", "master", GRID_MASTER, default=True)
    b = await _make(db, "b", "master", GRID_MASTER)
    await db.set_default_motd_template(b["id"])
    assert (await db.get_default_motd_template())["id"] == b["id"]
    assert (await db.get_motd_template_by_id(a["id"]))["is_default"] is False


async def test_seeding_happens_once(db):
    assert (await composer.ensure_seeded(db))["name"] == composer.SEED_TEMPLATE_NAME
    assert await composer.ensure_seeded(db) is None
    seed = await db.get_default_motd_template()
    assert seed["zones"] == ["promo"]
    audit = await db.list_admin_audit("motd_template")
    assert audit[0]["action"] == "motd.template.seed"


async def test_composition_follows_master_and_independent_zone_schedules(db, tmp_path):
    await composer.ensure_seeded(db)
    october = await _make(db, "october", "master", GRID_MASTER)
    promo = await _make(db, "thursday-promo", "fragment", "<b>Thursday!</b>")
    await db.create_motd_schedule(
        created_by="t",
        template_id=october["id"],
        label="October",
        starts_at=_et(2026, 10, 1),
        ends_at=_et(2026, 11, 1),
        tz=ET,
    )
    await db.create_motd_schedule(
        created_by="t",
        template_id=promo["id"],
        zone="promo",
        label="Thu nights",
        starts_at=_et(2026, 10, 1, 18),
        rrule="FREQ=WEEKLY;BYDAY=TH",
        duration_minutes=300,
        tz=ET,
    )
    rows = await db.list_motd_schedules()
    assert rows[0]["is_active"] is True
    assert rows[0]["starts_at"].tzinfo is not None

    thursday = await composer.resolve_composition(db, _et(2026, 10, 15, 19))
    assert thursday.master["name"] == "october"
    assert thursday.fragments["promo"]["name"] == "thursday-promo"
    friday = await composer.resolve_composition(db, _et(2026, 10, 16, 19))
    assert friday.fragments["promo"] is None
    assert thursday.key != friday.key
    november = await composer.resolve_composition(db, _et(2026, 11, 5, 19))
    assert november.master["name"] == composer.SEED_TEMPLATE_NAME
    assert november.fragments["promo"]["name"] == "thursday-promo"

    config = _config(tmp_path)
    rendered = await composer.render_composition(db, config, _context(config), thursday)
    assert "<b>Thursday!</b>" in rendered.html


async def test_archived_templates_drop_out_of_composition(db):
    await composer.ensure_seeded(db)
    promo = await _make(db, "p", "fragment", "<b>x</b>")
    await db.create_motd_schedule(
        created_by="t",
        template_id=promo["id"],
        zone="promo",
        label="p",
        starts_at=_et(2026, 1, 1),
        ends_at=_et(2027, 1, 1),
        tz=ET,
    )
    at = _et(2026, 6, 1)
    assert (await composer.resolve_composition(db, at)).fragments["promo"]
    await db.archive_motd_template(promo["id"])
    assert (await composer.resolve_composition(db, at)).fragments["promo"] is None


# --- automation ----------------------------------------------------------------


class _Jobs:
    def __init__(self):
        self.runs = []
        self.running = False

    def is_running(self, name):
        return self.running

    async def run(self, name, **kw):
        self.runs.append((name, kw))
        return {"started": True, "run_id": len(self.runs)}


async def test_automation_publishes_only_on_composition_change(db, tmp_path):
    config = _config(tmp_path, automation_enabled=True)
    jobs = _Jobs()
    automation = MOTDAutomation(db, jobs, config)

    first = await automation.tick()
    assert first["action"] == "publish"
    assert jobs.runs[0][1]["triggered_by"] == "motd_automation"
    composition = await composer.resolve_composition(db)
    week_key, _ = builder.week_context()
    await db.add_motd_publication(
        trigger="schedule",
        composition_key=composition.key,
        master_revision_id=composition.master["current_revision_id"],
        fragment_revisions=composition.fragment_revisions,
        week_key=week_key,
        html_sha256="h",
        html_chars=1,
        backup_path=None,
        job_run_id=1,
        published_by="motd_automation",
    )
    assert (await automation.tick())["action"] == "none"

    jobs.running = True
    assert (await automation.tick())["reason"] == "publish_running"
    config.motd.automation_enabled = False
    assert (await automation.tick())["reason"] == "disabled"
    assert len(jobs.runs) == 1


async def test_automation_backs_off_a_failing_composition(db, tmp_path):
    automation = MOTDAutomation(db, _Jobs(), _config(tmp_path, automation_enabled=True))
    assert (await automation.tick())["action"] == "publish"
    assert (await automation.tick())["reason"] == "recent_attempt"


async def test_automation_keeps_an_early_next_weekend_debut(db, tmp_path):
    config = _config(tmp_path, automation_enabled=True)
    jobs = _Jobs()
    await composer.ensure_seeded(db)
    next_key, _ = builder.week_context(week_offset=1)
    await db.add_motd_publication(
        trigger="manual",
        composition_key="stale",
        master_revision_id=1,
        fragment_revisions={},
        week_key=next_key,
        html_sha256="h",
        html_chars=1,
        backup_path=None,
        job_run_id=None,
        published_by="admin",
    )
    await MOTDAutomation(db, jobs, config).tick()
    assert jobs.runs[0][1]["params"]["week"] == "next"


# --- retention -----------------------------------------------------------------


async def test_retention_prunes_history_but_keeps_the_latest_publication(db, tmp_path):
    config = _config(tmp_path, retention_days=30)
    backups = tmp_path / "out" / "backups"
    backups.mkdir(parents=True)
    old_file, kept_file = backups / "motd-old.html", backups / "motd-kept.html"
    for f in (old_file, kept_file):
        f.write_text("x")
        stale = time.time() - 40 * 86400
        os.utime(f, (stale, stale))

    for path in (str(old_file), str(kept_file)):
        await db.add_motd_publication(
            trigger="manual",
            composition_key="k",
            master_revision_id=1,
            fragment_revisions={},
            week_key="w",
            html_sha256="h",
            html_chars=1,
            backup_path=path,
            job_run_id=None,
            published_by="a",
        )
    await db.add_admin_audit(actor="a", action="x", entity_type="t", entity_id=1)
    # Age every row past the window (SQLite stores fixed-width UTC strings).
    old = "2000-01-01T00:00:00.000000Z"
    await db.catalog._db.execute("UPDATE motd_publications SET published_at = ?", [old])
    await db.catalog._db.execute("UPDATE admin_audit_log SET at = ?", [old])
    await db.catalog._db.commit()

    ctx = SimpleNamespace(config=config, db=db)
    result = await motd_retention_prune_job({}, ctx)
    assert result["publications_removed"] == 1
    assert result["audit_removed"] == 1
    assert result["backups_removed"] == 1
    assert not old_file.exists() and kept_file.exists()
    assert (await db.get_latest_motd_publication())["backup_path"] == str(kept_file)


async def test_media_references_render_and_break_once_deleted(db, tmp_path):
    await composer.ensure_seeded(db)
    await db.create_media_asset(
        slug="pumpkin",
        filename="pumpkin.gif",
        kind="image",
        mime="image/gif",
        bytes=1,
        width=1,
        height=1,
        animated=False,
        frame_count=1,
        sha256="s",
        uploaded_by="a",
    )
    fragment = await _make(
        db, "p", "fragment", '<img src="{{ media_url("pumpkin") }}">'
    )
    await db.create_motd_schedule(
        created_by="t",
        template_id=fragment["id"],
        zone="promo",
        label="p",
        starts_at=_et(2026, 1, 1),
        ends_at=_et(2030, 1, 1),
        tz=ET,
    )
    config = _config(tmp_path)
    composition = await composer.resolve_composition(db, _et(2026, 6, 1))
    rendered = await composer.render_composition(
        db, config, _context(config), composition
    )
    assert 'src="https://q.example/media/pumpkin.gif"' in rendered.html
    await db.soft_delete_media_asset("pumpkin", "a")
    with pytest.raises(MOTDTemplateError, match="Unknown or deleted media"):
        await composer.render_composition(db, config, _context(config), composition)
