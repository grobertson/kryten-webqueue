"""Phase 1 (SPEC_JOBS_AND_BROWSE) database-level tests.

Covers the job-run reconciliation fix (A1.2), browse sort orderings incl.
``added_at`` (B1), the ``kryten-hidden`` exclusion (B6), and the playlist
append / most-recent helpers (B4/B5).
"""

import pytest

from kryten_webqueue.catalog.db import Database, HIDDEN_ITEM_TAG


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


async def _add_item(db: Database, token: str, title: str, *, synced_at: str):
    await db.insert_catalog({
        "friendly_token": token,
        "title": title,
        "description": "",
        "duration_sec": 600,
        "manifest_url": f"https://cms/api/v1/media/cytube/{token}.json",
        "thumbnail_url": "",
        "synced_at": synced_at,
    })


# --- A1.2 job-run reconciliation ---

async def test_reconcile_orphaned_job_runs(db):
    run_id = await db.start_job_run("catalog_sync", triggered_by="tester")
    # Simulate a crash: the row is left at 'running'.
    reconciled = await db.reconcile_orphaned_job_runs()
    assert reconciled == 1
    runs = await db.get_job_runs(job_name="catalog_sync")
    assert runs[0]["status"] == "interrupted"
    assert runs[0]["ended_at"] is not None
    # A finished run is untouched on a subsequent reconcile.
    assert await db.reconcile_orphaned_job_runs() == 0


# --- B1 insert populates added_at + sort orderings ---

async def test_insert_populates_added_at(db):
    await _add_item(db, "a1", "Alpha", synced_at="2026-01-01T00:00:00+00:00")
    item = await db.get_item_admin("a1")
    assert item["added_at"] == "2026-01-01T00:00:00+00:00"


async def test_browse_sort_orderings(db):
    await _add_item(db, "a", "Apple", synced_at="2026-01-01T00:00:00+00:00")
    await _add_item(db, "b", "Cherry", synced_at="2026-03-01T00:00:00+00:00")
    await _add_item(db, "c", "Banana", synced_at="2026-02-01T00:00:00+00:00")

    title_asc = [i["title"] for i in await db.browse(sort="title_asc")]
    assert title_asc == ["Apple", "Banana", "Cherry"]

    title_desc = [i["title"] for i in await db.browse(sort="title_desc")]
    assert title_desc == ["Cherry", "Banana", "Apple"]

    newest = [i["title"] for i in await db.browse(sort="newest")]
    assert newest == ["Cherry", "Banana", "Apple"]

    oldest = [i["title"] for i in await db.browse(sort="oldest")]
    assert oldest == ["Apple", "Banana", "Cherry"]

    # Unknown sort key falls back to default (no crash).
    assert len(await db.browse(sort="bogus")) == 3


# --- B6 kryten-hidden exclusion ---

async def test_hide_tag_excludes_from_browse(db):
    await _add_item(db, "h", "Hidden Me", synced_at="2026-01-01T00:00:00+00:00")
    assert any(i["friendly_token"] == "h" for i in await db.browse())

    await db.add_catalog_tag("h", HIDDEN_ITEM_TAG)
    assert not any(i["friendly_token"] == "h" for i in await db.browse())
    # Admins can still see it with show_hidden.
    assert any(i["friendly_token"] == "h" for i in await db.browse(show_hidden=True))

    await db.remove_catalog_tag("h", HIDDEN_ITEM_TAG)
    assert any(i["friendly_token"] == "h" for i in await db.browse())


# --- B4/B5 playlist append + most-recent ---

async def test_append_and_most_recent_playlist(db):
    await _add_item(db, "p1", "One", synced_at="2026-01-01T00:00:00+00:00")
    first = await db.create_saved_playlist(name="First", description=None, is_immutable=False, created_by="admin")
    second = await db.create_saved_playlist(name="Second", description=None, is_immutable=False, created_by="admin")

    recent = await db.get_most_recent_playlist("admin")
    assert recent["id"] == second

    count = await db.append_playlist_item(first, {
        "media_type": "cm", "media_id": "m1", "title": "One", "duration_sec": 600,
    })
    assert count == 1
    count = await db.append_playlist_item(first, {
        "media_type": "cm", "media_id": "m2", "title": "Two", "duration_sec": 600,
    })
    assert count == 2
    items = await db.get_saved_playlist_items(first)
    assert [it["position"] for it in items] == [0, 1]

    assert await db.get_most_recent_playlist("nobody") is None
