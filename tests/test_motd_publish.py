"""MOTD publish pipeline tests.

Covers the grid layout, override precedence, mystery-box fallbacks for
unverifiable/missing titles, per-week override CRUD, and the rendered snippet
(including URL sanitisation of untrusted curator input). No network: the OMDB
lookup and poster download are stubbed.
"""

import datetime
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.config import MediaConfig, MOTDConfig
from kryten_webqueue.jobs.manager import JobError
from kryten_webqueue.jobs.motd_publish import _next_event, motd_publish_job
from kryten_webqueue.motd import builder, render


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "motd.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


async def test_next_event_serializes_native_postgres_timestamp():
    fire_at = datetime.datetime(2026, 10, 9, 22, tzinfo=datetime.timezone.utc)
    database = SimpleNamespace(
        get_next_schedule=AsyncMock(
            return_value={
                "id": 1,
                "playlist_id": 2,
                "label": "Friday",
                "fire_at": fire_at,
            }
        )
    )
    event = await _next_event(database)
    assert event["fire_at"] == fire_at.isoformat()
    assert json.loads(json.dumps(event))["fire_at"] == fire_at.isoformat()


async def test_late_builder_progress_is_drained_before_final_summary(
    tmp_path, monkeypatch, stub_lookup, db
):
    _stub_workbook(monkeypatch, {})
    config = _config(tmp_path, slots=8)
    week = builder.build_slots(config, dry_run=True)
    phases = []

    def build(*args, **kwargs):
        kwargs["emit"]({"phase": "late-slot"})
        return week

    async def progress(detail):
        if detail.get("phase") == "late-slot":
            await asyncio.sleep(0.02)
        phases.append(detail.get("phase"))

    monkeypatch.setattr("kryten_webqueue.jobs.motd_publish.build_slots", build)
    api = SimpleNamespace(
        get_motd=AsyncMock(return_value=render.render_motd(config, week)),
        set_motd=AsyncMock(),
    )
    ctx = SimpleNamespace(
        config=config, api_gate=api, db=db, progress=progress, triggered_by="admin"
    )
    await motd_publish_job({"publish": True}, ctx)
    assert phases == ["late-slot", "complete"]


def _config(tmp_path, **motd_kw):
    motd = MOTDConfig(
        poster_dir=str(tmp_path / "boxes"),
        poster_base_url="https://cdn.example/boxes",
        output_dir=str(tmp_path / "out"),
        mystery_box_base_url="https://queue.example",
        mystery_box_url="https://cdn.example/boxes/mystery.jpg",
        mystery_box_href="https://queue.example/",
        **motd_kw,
    )
    return SimpleNamespace(
        motd=motd,
        media=MediaConfig(
            dir=str(tmp_path / "media"), base_url="https://cdn.example/media"
        ),
        omdb_api_key="key",
        fetchurls=SimpleNamespace(workbook_path=""),
    )


@pytest.fixture
def stub_lookup(monkeypatch):
    """Resolve any title containing a year; download always succeeds."""

    def _resolve(title, *, api_key):
        if "(" not in title:
            return None
        return {"imdb_id": "tt0000001", "poster_url": "https://omdb/img.jpg"}

    monkeypatch.setattr(builder, "_resolve_omdb", _resolve)
    monkeypatch.setattr(builder, "_download_image", lambda url: b"jpegbytes")


def _stub_workbook(monkeypatch, titles_by_night):
    monkeypatch.setattr(builder, "load_workbook_bytes", lambda config, **kw: b"xlsx")
    monkeypatch.setattr(builder, "resolve_weekend_sheet", lambda b, s: None)
    monkeypatch.setattr(
        builder,
        "extract_movies_by_section",
        lambda b, s: {
            "friday": titles_by_night.get(1, []),
            "saturday-night": titles_by_night.get(2, []),
            "sunday-daytime": titles_by_night.get(3, []),
        },
    )


# --- grid layout ---


def test_grid_pads_a_thin_weekend_to_the_target_size(
    tmp_path, monkeypatch, stub_lookup
):
    _stub_workbook(monkeypatch, {})
    week = builder.build_slots(_config(tmp_path), today=datetime.date(2026, 3, 4))
    assert len(week.slots) == 12
    assert [s.slot_key for s in week.slots[:2]] == ["night1-slot1", "night1-slot2"]
    # Sunday has no schedule yet, so the grid is Friday + Saturday.
    assert {s.night for s in week.slots} == {1, 2}
    assert len([s for s in week.slots if s.night == 1]) == 6
    assert len([s for s in week.slots if s.night == 2]) == 6


def test_grid_shape_is_an_even_split(tmp_path, monkeypatch, stub_lookup):
    _stub_workbook(
        monkeypatch,
        {
            1: [f"Friday {n} (1980)" for n in range(6)],
            2: [f"Saturday {n} (1980)" for n in range(6)],
        },
    )
    week = builder.build_slots(_config(tmp_path), today=datetime.date(2026, 3, 4))
    assert len([s for s in week.slots if s.night == 1]) == 6
    assert len([s for s in week.slots if s.night == 2]) == 6
    assert all(s.resolved for s in week.slots)
    assert not week.warnings


def test_overfull_night_warns_instead_of_silently_dropping(
    tmp_path, monkeypatch, stub_lookup
):
    _stub_workbook(
        monkeypatch,
        {
            1: [f"Friday {n} (1980)" for n in range(8)],
            2: [f"Saturday {n} (1980)" for n in range(6)],
        },
    )
    week = builder.build_slots(_config(tmp_path), today=datetime.date(2026, 3, 4))
    assert len(week.slots) == 12
    assert any("2 scheduled title(s)" in w for w in week.warnings)
    assert "Friday 6 (1980)" not in [s.title for s in week.slots]


def test_sunday_titles_are_ignored(tmp_path, monkeypatch, stub_lookup):
    _stub_workbook(
        monkeypatch,
        {
            1: ["Friday One (1976)"],
            2: ["Saturday One (1980)"],
            3: ["Sunday One (1990)"],
        },
    )
    week = builder.build_slots(_config(tmp_path), today=datetime.date(2026, 3, 4))
    titles = [s.title for s in week.slots if s.title]
    assert "Friday One (1976)" in titles
    assert "Saturday One (1980)" in titles
    assert "Sunday One (1990)" not in titles


def test_nights_config_can_re_enable_sunday(tmp_path, monkeypatch, stub_lookup):
    _stub_workbook(monkeypatch, {3: ["Sunday One (1990)"]})
    week = builder.build_slots(
        _config(tmp_path, nights=[1, 2, 3]), today=datetime.date(2026, 3, 4)
    )
    assert {s.night for s in week.slots} == {1, 2, 3}
    assert "Sunday One (1990)" in [s.title for s in week.slots if s.title]


def test_missing_workbook_yields_all_mystery(tmp_path, monkeypatch, stub_lookup):
    def _boom(config, **kw):
        raise RuntimeError("no sheet")

    monkeypatch.setattr(builder, "load_workbook_bytes", _boom)
    week = builder.build_slots(_config(tmp_path), today=datetime.date(2026, 3, 4))
    assert week.workbook_found is False
    assert all(not s.resolved for s in week.slots)
    assert all(s.poster_url.endswith("mystery.jpg") for s in week.slots)
    assert week.warnings


def test_week_offset_debuts_next_weekend(tmp_path, monkeypatch, stub_lookup):
    _stub_workbook(monkeypatch, {})
    sunday = datetime.date(2026, 3, 8)
    assert builder.week_context(sunday)[0] == "3.6-3.7"
    assert builder.week_context(sunday, week_offset=1)[0] == "3.13-3.14"

    week = builder.build_slots(_config(tmp_path), today=sunday, week_offset=1)
    assert week.week_key == "3.13-3.14"
    assert week.friday == datetime.date(2026, 3, 13)


# --- resolution ---


def test_resolved_title_gets_imdb_link_and_local_art(
    tmp_path, monkeypatch, stub_lookup
):
    _stub_workbook(monkeypatch, {1: ["The Big Bus (1976)"]})
    week = builder.build_slots(_config(tmp_path), today=datetime.date(2026, 3, 4))
    slot = week.slots[0]
    assert slot.source == "omdb"
    assert slot.href == "https://www.imdb.com/title/tt0000001/"
    assert slot.poster_url.startswith("https://cdn.example/boxes/art-2026-03-06-")
    assert (tmp_path / "boxes" / slot.poster_url.rsplit("/", 1)[1]).exists()


def test_unverifiable_title_falls_back_to_mystery(tmp_path, monkeypatch, stub_lookup):
    _stub_workbook(monkeypatch, {1: ["Some Untitled Thing"]})
    week = builder.build_slots(_config(tmp_path), today=datetime.date(2026, 3, 4))
    slot = week.slots[0]
    assert slot.source == "mystery"
    assert slot.title == "Some Untitled Thing"
    assert "could not be verified" in slot.note


# --- mystery art comes from the browse view's branded placeholder pool ---


def test_mystery_pool_absolutizes_browse_placeholders(tmp_path):
    config = _config(tmp_path)
    cover_art = SimpleNamespace(
        list_placeholder_urls=lambda: ["/images/placeholders/a.webp"]
    )
    assert builder.mystery_pool(config, cover_art) == [
        "https://queue.example/images/placeholders/a.webp"
    ]
    assert builder.mystery_pool(config, None) == []


def test_mystery_slots_use_the_placeholder_pool(tmp_path, monkeypatch, stub_lookup):
    _stub_workbook(monkeypatch, {})
    week = builder.build_slots(
        _config(tmp_path),
        today=datetime.date(2026, 3, 4),
        mystery_urls=["https://queue.example/images/placeholders/a.webp"],
    )
    assert all(
        s.poster_url == "https://queue.example/images/placeholders/a.webp"
        for s in week.slots
    )


def test_mystery_falls_back_when_no_placeholders_installed(
    tmp_path, monkeypatch, stub_lookup
):
    _stub_workbook(monkeypatch, {})
    week = builder.build_slots(
        _config(tmp_path), today=datetime.date(2026, 3, 4), mystery_urls=[]
    )
    assert all(s.poster_url.endswith("mystery.jpg") for s in week.slots)


def test_dry_run_writes_nothing(tmp_path, monkeypatch, stub_lookup):
    _stub_workbook(monkeypatch, {1: ["The Big Bus (1976)"]})
    builder.build_slots(
        _config(tmp_path), dry_run=True, today=datetime.date(2026, 3, 4)
    )
    assert not (tmp_path / "boxes").exists()


# --- overrides ---


def test_override_art_and_href_win_over_lookup(tmp_path, monkeypatch, stub_lookup):
    _stub_workbook(monkeypatch, {1: ["The Big Bus (1976)"]})
    week = builder.build_slots(
        _config(tmp_path),
        overrides={
            "night1-slot1": {
                "title": "Hand Picked",
                "poster_url": "https://cdn.example/boxes/custom.png",
                "href": "https://example.com/notes",
            }
        },
        today=datetime.date(2026, 3, 4),
    )
    slot = week.slots[0]
    assert slot.source == "override"
    assert slot.title == "Hand Picked"
    assert slot.poster_url == "https://cdn.example/boxes/custom.png"
    assert slot.href == "https://example.com/notes"


def test_override_title_alone_still_resolves_art(tmp_path, monkeypatch, stub_lookup):
    _stub_workbook(monkeypatch, {1: ["garbled name"]})
    week = builder.build_slots(
        _config(tmp_path),
        overrides={"night1-slot1": {"title": "Corrected (1976)"}},
        today=datetime.date(2026, 3, 4),
    )
    assert week.slots[0].source == "omdb"
    assert week.slots[0].title == "Corrected (1976)"


async def test_override_crud_is_per_week_and_merges(db):
    await db.upsert_motd_override(
        "3.6-3.7", "night1-slot1", title="A", created_by="admin"
    )
    await db.upsert_motd_override(
        "3.6-3.7", "night1-slot1", poster_url="https://x/y.png", created_by="admin"
    )
    rows = await db.list_motd_overrides("3.6-3.7")
    assert len(rows) == 1
    assert rows[0]["title"] == "A"
    assert rows[0]["poster_url"] == "https://x/y.png"

    assert await db.list_motd_overrides("3.13-3.14") == []
    assert await db.delete_motd_override("3.6-3.7", "night1-slot1") == 1
    assert await db.list_motd_overrides("3.6-3.7") == []


# --- rendering ---


def test_render_groups_by_night_and_includes_links(tmp_path, monkeypatch, stub_lookup):
    _stub_workbook(monkeypatch, {1: ["The Big Bus (1976)"]})
    config = _config(tmp_path)
    week = builder.build_slots(config, today=datetime.date(2026, 3, 4))
    html = render.render_motd(config, week)

    assert '<div class="poster-grid">' in html
    assert "<!-- Friday Night -->" in html
    assert "<!-- Saturday Night -->" in html
    assert html.count("<img src=") == 12
    assert "Join us on Reddit!" in html
    # The headline goes through autoescape like every other context value.
    assert "3/6 @ 6pm ET &amp; 3/7 @ 6pm ET" in html


def test_next_event_is_carried_but_hidden_by_default(
    tmp_path, monkeypatch, stub_lookup
):
    _stub_workbook(monkeypatch, {})
    config = _config(tmp_path)
    week = builder.build_slots(config, today=datetime.date(2026, 3, 4))
    html = render.render_motd(
        config, week, next_event={"label": "Saturday Night", "starts_in": "in 2h"}
    )
    assert "Up next" not in html


def test_render_includes_next_event_when_enabled(tmp_path, monkeypatch, stub_lookup):
    _stub_workbook(monkeypatch, {})
    config = _config(tmp_path, show_next_event=True)
    week = builder.build_slots(config, today=datetime.date(2026, 3, 4))
    html = render.render_motd(
        config, week, next_event={"label": "Saturday Night", "starts_in": "in 2h"}
    )
    assert "Up next: Saturday Night — in 2h" in html


def test_render_rejects_unsafe_override_urls(tmp_path, monkeypatch, stub_lookup):
    _stub_workbook(monkeypatch, {})
    config = _config(tmp_path)
    week = builder.build_slots(
        config,
        overrides={
            "night1-slot1": {
                "poster_url": "javascript:alert(1)",
                "href": "javascript:alert(2)",
            }
        },
        today=datetime.date(2026, 3, 4),
    )
    html = render.render_motd(config, week)
    assert "javascript:" not in html


def test_render_escapes_curator_titles(tmp_path, monkeypatch, stub_lookup):
    _stub_workbook(monkeypatch, {1: ["<script>x</script> (1976)"]})
    config = _config(tmp_path)
    week = builder.build_slots(config, today=datetime.date(2026, 3, 4))
    html = render.render_motd(config, week)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# --- slot key validation ---


def test_render_marks_both_tags_for_each_of_eight_slots(
    tmp_path, monkeypatch, stub_lookup
):
    _stub_workbook(monkeypatch, {})
    config = _config(tmp_path, slots=8)
    week = builder.build_slots(config, today=datetime.date(2026, 3, 4))
    html = render.render_motd(config, week)
    assert len(week.slots) == 8
    for slot in week.slots:
        assert html.count(f'data-kryten-motd-slot="{slot.slot_key}"') == 2


def test_managed_slot_update_preserves_manual_html_and_tag_styling():
    current = '<h1>Hand edited announcement</h1>\n<a data-kryten-motd-slot="night1-slot1" class="custom" style="color:red" href="https://old"><img data-kryten-motd-slot="night1-slot1" src="https://old/img" width="140" /></a>\n<footer>Manual footer &amp; links</footer>'
    generated = '<a data-kryten-motd-slot="night1-slot1" href="https://new/movie" title="New Movie"><img data-kryten-motd-slot="night1-slot1" src="https://new/poster" alt="New Movie" /></a>'
    updated = render.update_motd_slots(current, generated)
    assert updated.startswith("<h1>Hand edited announcement</h1>\n")
    assert updated.endswith("\n<footer>Manual footer &amp; links</footer>")
    assert 'class="custom"' in updated and 'style="color:red"' in updated
    assert 'width="140"' in updated
    assert 'href="https://new/movie"' in updated
    assert 'src="https://new/poster"' in updated


def test_legacy_grid_migration_keeps_banner_and_footer():
    current = '<img src="https://custom/banner"><div class="poster-grid"><a href="https://old"><img src="https://old/poster"></a></div><p>Hand edited footer</p>'
    generated = '<a data-kryten-motd-slot="night1-slot1" href="https://new"><img data-kryten-motd-slot="night1-slot1" src="https://new/poster"></a>'
    updated = render.update_motd_slots(current, generated)
    assert updated.startswith('<img src="https://custom/banner">')
    assert updated.endswith("<p>Hand edited footer</p>")
    assert updated.count('data-kryten-motd-slot="night1-slot1"') == 2


def test_reordered_boxes_follow_markers_not_display_position():
    def box(slot, url):
        return f'<a data-kryten-motd-slot="{slot}" href="https://{url}"><img data-kryten-motd-slot="{slot}" src="https://{url}/poster"></a>'

    current = box("night1-slot2", "old2") + box("night1-slot1", "old1")
    generated = box("night1-slot1", "new1") + box("night1-slot2", "new2")
    updated = render.update_motd_slots(current, generated)
    assert updated.index("https://new2") < updated.index("https://new1")


@pytest.mark.parametrize("separator", ["\r", "\r\n", "\u2028"])
def test_managed_updates_preserve_nonstandard_line_separators(separator):
    before = f"<p>Manual{separator}announcement</p>"
    current = (
        before
        + '<a data-kryten-motd-slot="night1-slot1"><img data-kryten-motd-slot="night1-slot1"></a>'
    )
    generated = '<a data-kryten-motd-slot="night1-slot1" href="https://new"><img data-kryten-motd-slot="night1-slot1" src="https://new/poster"></a>'
    assert render.update_motd_slots(current, generated).startswith(before)


@pytest.mark.parametrize(
    "current",
    [
        "<p>Unrecognised hand-built MOTD</p>",
        '<div class="poster-grid"></div>',
        '<a data-kryten-motd-slot="night1-slot1"><img></a>',
        '<a data-kryten-motd-slot="night1-slot1"><img data-kryten-motd-slot="night1-slot1"></a><a data-kryten-motd-slot="night1-slot1"></a>',
    ],
)
def test_unsafe_or_incomplete_slot_markup_is_rejected(current):
    generated = '<a data-kryten-motd-slot="night1-slot1" href="https://new"><img data-kryten-motd-slot="night1-slot1" src="https://new/poster"></a>'
    with pytest.raises(render.MOTDMergeError):
        render.update_motd_slots(current, generated)


async def test_publish_job_backs_up_live_motd_then_replaces_it_with_the_template(
    tmp_path, monkeypatch, stub_lookup, db
):
    _stub_workbook(monkeypatch, {})
    config = _config(tmp_path, slots=8)
    live = "<p>Unrecognised hand-built MOTD</p>"
    api = SimpleNamespace(get_motd=AsyncMock(return_value=live), set_motd=AsyncMock())
    ctx = SimpleNamespace(
        config=config,
        api_gate=api,
        db=db,
        progress=AsyncMock(),
        triggered_by="admin",
        run_id=42,
    )
    result = await motd_publish_job({"publish": True}, ctx)

    sent = api.set_motd.await_args.args[0]
    assert "Unrecognised hand-built MOTD" not in sent
    assert render.collect_motd_slots(sent) == {
        f"night{n}-slot{p}" for n in (1, 2) for p in range(1, 5)
    }
    assert result["published"] is True
    assert result["composition"]["master"] == "channel-z-weekend"
    assert open(result["backup_path"], encoding="utf-8").read() == live
    week_key = result["week_key"]
    assert (tmp_path / "out" / f"motd-{week_key}.html").read_text(
        encoding="utf-8"
    ) == sent

    publication = await db.get_latest_motd_publication()
    assert publication["trigger"] == "manual"
    assert publication["published_by"] == "admin"
    assert publication["job_run_id"] == 42
    assert publication["composition_key"] == result["composition"]["composition_key"]
    assert publication["week_key"] == week_key
    assert isinstance(publication["published_at"], datetime.datetime)
    assert json.loads(json.dumps(result, default=str))["published"] is True


async def test_publish_job_never_sends_when_the_live_read_fails(
    tmp_path, monkeypatch, stub_lookup, db
):
    _stub_workbook(monkeypatch, {})
    api = SimpleNamespace(
        get_motd=AsyncMock(side_effect=RuntimeError("api offline")),
        set_motd=AsyncMock(),
    )
    ctx = SimpleNamespace(
        config=_config(tmp_path), api_gate=api, db=db, progress=AsyncMock()
    )
    with pytest.raises(JobError, match="back it up"):
        await motd_publish_job({"publish": True}, ctx)
    api.set_motd.assert_not_awaited()
    assert not (tmp_path / "out").exists()
    assert await db.get_latest_motd_publication() is None


async def test_publish_job_never_sends_an_invalid_default_master(
    tmp_path, monkeypatch, stub_lookup, db
):
    _stub_workbook(monkeypatch, {})
    body = "<p>No movie grid here</p>"
    template = await db.create_motd_template(
        name="broken",
        kind="master",
        display_name="Broken",
        description=None,
        body=body,
        body_sha256="x",
        zones=[],
        note=None,
        created_by="t",
    )
    await db.set_default_motd_template(template["id"])
    api = SimpleNamespace(get_motd=AsyncMock(return_value=""), set_motd=AsyncMock())
    ctx = SimpleNamespace(
        config=_config(tmp_path), api_gate=api, db=db, progress=AsyncMock()
    )
    with pytest.raises(JobError, match="movie grid"):
        await motd_publish_job({"publish": True}, ctx)
    api.get_motd.assert_not_awaited()
    api.set_motd.assert_not_awaited()


@pytest.mark.parametrize(
    "key,ok",
    [
        ("night1-slot1", True),
        ("night3-slot12", True),
        ("night4-slot1", False),
        ("night1-slot0", False),
        ("../../etc/passwd", False),
        ("night1-slot1; rm -rf /", False),
    ],
)
def test_slot_key_regex(key, ok):
    assert bool(builder.SLOT_KEY_RE.match(key)) is ok


# --- poster directory availability ---
#
# Regression coverage for the containerization failure: with
# `motd.poster_dir` left pointing at the pre-container MediaCMS path
# (/home/mediacms.io/mediacms/static/motd_boxes), the admin art-upload endpoint
# and the scheduled motd_publish job both died with a bare
# "PermissionError: [Errno 13] Permission denied: '/home/mediacms.io'".


def test_ensure_poster_dir_creates_a_missing_directory(tmp_path):
    motd = SimpleNamespace(poster_dir=str(tmp_path / "boxes" / "motd"))
    created = builder.ensure_poster_dir(motd)
    assert created.is_dir()


def test_ensure_poster_dir_is_idempotent(tmp_path):
    motd = SimpleNamespace(poster_dir=str(tmp_path / "boxes"))
    builder.ensure_poster_dir(motd)
    assert builder.ensure_poster_dir(motd).is_dir()


def test_ensure_poster_dir_reports_an_unusable_path_actionably(tmp_path, monkeypatch):
    """A bare OSError becomes a PosterDirUnavailable naming the config key."""

    def _deny(self, *a, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("pathlib.Path.mkdir", _deny)
    motd = SimpleNamespace(poster_dir="/home/mediacms.io/mediacms/static/motd_boxes")
    with pytest.raises(builder.PosterDirUnavailable) as exc:
        builder.ensure_poster_dir(motd)
    message = str(exc.value)
    assert "motd.poster_dir" in message
    # Path separators differ per platform; assert on the path parts.
    assert "mediacms" in message
    # Must name the remedy, not just restate errno 13.
    assert "/var/lib/kryten-webqueue" in message


def test_ensure_poster_dir_rejects_an_unset_path():
    with pytest.raises(builder.PosterDirUnavailable):
        builder.ensure_poster_dir(SimpleNamespace(poster_dir=""))


def test_default_poster_dir_lives_on_the_service_volume():
    """The shipped default must not point at a foreign host path again."""
    motd = MOTDConfig()
    assert motd.poster_dir.startswith("/var/lib/kryten-webqueue/")
    assert "mediacms" not in motd.poster_dir
    assert motd.output_dir.startswith("/var/lib/kryten-webqueue/")


def test_build_slots_surfaces_an_unusable_poster_dir(
    tmp_path, monkeypatch, stub_lookup
):
    """A non-dry run fails once, up front, with an actionable message."""
    _stub_workbook(monkeypatch, {1: ["The Big Bus (1976)"]})
    config = _config(tmp_path)

    def _deny(self, *a, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("pathlib.Path.mkdir", _deny)
    with pytest.raises(builder.PosterDirUnavailable):
        builder.build_slots(config, today=datetime.date(2026, 3, 4))


def test_dry_run_does_not_need_a_writable_poster_dir(
    tmp_path, monkeypatch, stub_lookup
):
    """A dry run touches no filesystem, so it must not fail on an unusable one."""
    _stub_workbook(monkeypatch, {1: ["The Big Bus (1976)"]})

    def _deny(self, *a, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("pathlib.Path.mkdir", _deny)
    week = builder.build_slots(
        _config(tmp_path), dry_run=True, today=datetime.date(2026, 3, 4)
    )
    assert week.slots[0].source == "omdb"
