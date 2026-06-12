"""fetchurls SharePoint + fixed-name playlist behaviour (v0.9.8).

Covers the silent-token guard, source selection / clear error when the MSAL
cache is unseeded, the fixed section-label playlist names, immutable-preserve on
re-import, and the importer ``cm:`` manifest-URL fallback.
"""

from types import SimpleNamespace

import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.jobs.manager import JobContext
from kryten_webqueue.jobs import tasks
from kryten_webqueue.integrations.cmsutils import fetchurls
from kryten_webqueue.playlists.importer import import_playlist_text


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "fu.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


def _config(**fu):
    return SimpleNamespace(
        mediacms_url="https://cms.example",
        mediacms_token="tok",
        tmdb_api_key="", omdb_api_key="",
        image_dir="/tmp/kqimg",
        fetch_cookies_path="",
        fetchurls=SimpleNamespace(
            workbook_path=fu.get("workbook_path", ""),
            sharepoint_tenant_id=fu.get("sharepoint_tenant_id", ""),
            sharepoint_client_id=fu.get("sharepoint_client_id", ""),
            sharepoint_sharing_url=fu.get("sharepoint_sharing_url", ""),
            token_cache_path=fu.get("token_cache_path", ""),
        ),
    )


def _ctx(db, config=None):
    return JobContext(db=db, api_gate=None, config=config or _config(), run_id=1, triggered_by="admin")


# --- silent token guard ---

def test_silent_token_none_without_cache(tmp_path):
    missing = str(tmp_path / "nope.bin")
    assert fetchurls.acquire_graph_token_silent("tenant", "client", missing) is None
    assert fetchurls.acquire_graph_token_silent("tenant", "client", "") is None


# --- run() source selection / clear error ---

def test_run_sharepoint_without_token_raises_clear_error(tmp_path):
    config = _config(
        sharepoint_tenant_id="t", sharepoint_client_id="c",
        sharepoint_sharing_url="https://sp/share",
        token_cache_path=str(tmp_path / "absent.bin"),
    )
    with pytest.raises(RuntimeError, match="fetchurls_auth"):
        fetchurls.run({}, config=config, progress=None)


def test_run_no_source_raises(tmp_path):
    with pytest.raises(RuntimeError, match="workbook source"):
        fetchurls.run({}, config=_config(), progress=None)


# --- fixed section-label playlist names + immutable preserve ---

async def _add_catalog(db, token, title="T"):
    await db.insert_catalog({
        "friendly_token": token, "title": title, "description": "",
        "duration_sec": 600, "manifest_url": f"https://cms/api/v1/media/cytube/{token}.json",
        "thumbnail_url": "", "synced_at": "2026-01-01T00:00:00+00:00",
    })


async def test_fetchurls_job_uses_fixed_label_names(db, monkeypatch):
    await _add_catalog(db, "f1")
    await _add_catalog(db, "s1")

    async def fake_run_vendored(module_path, params, ctx, *, deps):
        return {
            "sheet": "3.6-3.7", "dry_run": False, "resolved": 2, "failures": 0,
            "section_lines": {"friday": ["cm:f1"], "saturday-night": ["cm:s1"]},
            "section_labels": {"friday": "Friday Night", "saturday-night": "Saturday Night"},
        }

    monkeypatch.setattr(tasks, "_run_vendored", fake_run_vendored)
    result = await tasks.fetchurls_job({}, _ctx(db))

    assert sorted(result["imported_playlists"]) == ["Friday Night", "Saturday Night"]
    names = {p["name"] for p in await db.get_saved_playlists()}
    assert {"Friday Night", "Saturday Night"} <= names
    # Created playlists are immutable (reserved).
    fri = await db.get_playlist_by_name_any("Friday Night")
    assert fri["is_immutable"] == 1


async def test_fetchurls_import_preserves_existing_immutability(db, monkeypatch):
    await _add_catalog(db, "f1")
    await _add_catalog(db, "f2")
    # Pre-existing MUTABLE playlist with the fixed name, created by someone else.
    pid = await db.create_saved_playlist(
        name="Friday Night", description=None, is_immutable=False, created_by="other-admin",
    )

    info = await tasks._import_section_as_playlist(_ctx(db), "Friday Night", ["cm:f1", "cm:f2"], "admin")
    assert info["id"] == pid                      # reused, not duplicated
    pl = await db.get_playlist_by_name_any("Friday Night")
    assert pl["is_immutable"] == 0                # existing flag preserved (not forced)
    assert pl["created_by"] == "other-admin"      # original owner kept
    items = await db.get_saved_playlist_items(pid)
    assert len(items) == 2


# --- importer cm: manifest fallback for not-yet-synced items ---

async def test_importer_cm_fallback_builds_manifest(db):
    out = await import_playlist_text(db, "cm:freshtoken", mediacms_url="https://cms.example")
    assert len(out["items"]) == 1
    assert out["items"][0]["media_id"] == "https://cms.example/api/v1/media/cytube/freshtoken.json?format=json"


async def test_importer_cm_uses_catalog_when_present(db):
    await _add_catalog(db, "known", "Known Title")
    out = await import_playlist_text(db, "cm:known", mediacms_url="https://cms.example")
    assert out["items"][0]["media_id"] == "https://cms/api/v1/media/cytube/known.json"
    assert out["items"][0]["title"] == "Known Title"
