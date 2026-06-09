"""Phase 3 (SPEC_JOBS_AND_BROWSE) reimplemented-jobs tests.

Covers the headless vendored-tool wiring that does NOT require network or the
optional download deps: the fetchurls upcoming-weekend computation, manifest
token extraction, the dependency-missing guard, the fetch add-to-playlist
wiring, and idempotent section→saved-playlist import.
"""

import datetime
from types import SimpleNamespace

import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.jobs.manager import JobContext, validate_params
from kryten_webqueue.jobs import tasks
from kryten_webqueue.integrations.cmsutils import fetchurls


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "phase3.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


def _config():
    return SimpleNamespace(
        mediacms_url="https://cms.example",
        mediacms_token="tok",
        tmdb_api_key="", omdb_api_key="",
        image_dir="/tmp/kqimg",
        fetch_cookies_path="",
        fetchurls=SimpleNamespace(workbook_path=""),
    )


def _ctx(db, **kw):
    return JobContext(db=db, api_gate=None, config=_config(), run_id=1,
                      triggered_by=kw.get("triggered_by", "admin"))


# --- upcoming-weekend sheet computation (OQ-3) ---

@pytest.mark.parametrize("today,expected", [
    (datetime.date(2026, 3, 4), "3.6-3.7"),    # Wed → upcoming Fri
    (datetime.date(2026, 3, 5), "3.6-3.7"),    # Thu → upcoming Fri
    (datetime.date(2026, 3, 6), "3.6-3.7"),    # Fri → TODAY (per OQ-3)
    (datetime.date(2026, 3, 7), "3.13-3.14"),  # Sat → next Fri
    (datetime.date(2026, 3, 8), "3.13-3.14"),  # Sun → next Fri
])
def test_upcoming_weekend_sheet(today, expected):
    sheet, friday, saturday = fetchurls.upcoming_weekend_sheet(today)
    assert sheet == expected
    assert saturday == friday + datetime.timedelta(days=1)


def test_extract_manifest_token():
    assert fetchurls._extract_manifest_token(
        "https://cms/api/v1/media/cytube/AbC123.json?format=json") == "AbC123"
    assert fetchurls._extract_manifest_token("https://cms/view?m=XyZ9") == "XyZ9"
    assert fetchurls._extract_manifest_token("https://example.com/nope") is None


# --- dependency guard ---

async def test_fetch_job_missing_ytdlp_fails_fast(db, monkeypatch):
    # Simulate yt_dlp absent regardless of the host environment.
    import importlib
    real_import = importlib.import_module

    def fake_import(name, *a, **k):
        if name == "yt_dlp":
            raise ImportError("no yt_dlp")
        return real_import(name, *a, **k)

    monkeypatch.setattr(tasks.importlib, "import_module", fake_import)
    with pytest.raises(RuntimeError, match="yt_dlp"):
        await tasks.fetch_job({"url": "http://x"}, _ctx(db))


# --- fetch add-to-playlist wiring ---

async def test_fetch_job_appends_to_playlist(db, monkeypatch):
    async def fake_run_vendored(module_path, params, ctx, *, deps):
        return {"downloaded": 1, "uploaded": 1, "tokens": ["TOK1", "TOK2"], "errors": []}

    monkeypatch.setattr(tasks, "_run_vendored", fake_run_vendored)
    pid = await db.create_saved_playlist(name="P", description=None, is_immutable=False, created_by="admin")

    result = await tasks.fetch_job({"url": "http://x", "add_to_playlist": pid}, _ctx(db))
    assert result["added_to_playlist"] == pid
    items = await db.get_saved_playlist_items(pid)
    assert len(items) == 2
    assert items[0]["media_type"] == "cm"
    assert "cytube/TOK1.json" in items[0]["media_id"]


async def test_fetch_job_no_tokens_records_warning(db, monkeypatch):
    async def fake_run_vendored(module_path, params, ctx, *, deps):
        return {"downloaded": 0, "uploaded": 0, "tokens": [], "errors": []}

    monkeypatch.setattr(tasks, "_run_vendored", fake_run_vendored)
    pid = await db.create_saved_playlist(name="P", description=None, is_immutable=False, created_by="admin")
    result = await tasks.fetch_job({"url": "http://x", "add_to_playlist": pid}, _ctx(db))
    assert any("nothing uploaded" in e for e in result["errors"])


# --- fetchurls section → saved playlist import (idempotent) ---

async def _add_catalog(db, token, title):
    await db.insert_catalog({
        "friendly_token": token, "title": title, "description": "",
        "duration_sec": 600, "manifest_url": f"https://cms/api/v1/media/cytube/{token}.json",
        "thumbnail_url": "", "synced_at": "2026-01-01T00:00:00+00:00",
    })


async def test_import_section_idempotent(db):
    await _add_catalog(db, "t1", "One")
    await _add_catalog(db, "t2", "Two")
    ctx = _ctx(db)

    info1 = await tasks._import_section_as_playlist(ctx, "3.6-3.7-friday", ["cm:t1", "cm:t2"], "admin")
    assert info1["count"] == 2
    # Re-run with one item: same playlist, items replaced (no duplicate playlist).
    info2 = await tasks._import_section_as_playlist(ctx, "3.6-3.7-friday", ["cm:t1"], "admin")
    assert info2["id"] == info1["id"]
    playlists = [p for p in await db.get_saved_playlists() if p["name"] == "3.6-3.7-friday"]
    assert len(playlists) == 1
    items = await db.get_saved_playlist_items(info1["id"])
    assert len(items) == 1


async def test_import_section_empty_returns_none(db):
    assert await tasks._import_section_as_playlist(_ctx(db), "x", [], "admin") is None


# --- schema validation for the download jobs ---

def test_fetch_schema_requires_url():
    with pytest.raises(ValueError, match="Source URL is required"):
        validate_params(tasks.FETCH_SCHEMA, {})


def test_fetchurls_schema_defaults():
    out = validate_params(tasks.FETCHURLS_SCHEMA, {})
    assert out["section"] == "all"
    assert out["dry_run"] is False
    assert out["validate"] is True
