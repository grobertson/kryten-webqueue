"""Tests for Sorties 2 & 3: Partitioned Multi-Database Connection Layer & Cross-Domain Decoupling."""

import pytest

from kryten_webqueue.config import DatabaseConfig
from kryten_webqueue.catalog.db import Database


@pytest.fixture
async def partitioned_db(tmp_path):
    data_dir = tmp_path / "partitioned_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = DatabaseConfig(layout="partitioned", data_dir=str(data_dir))
    db = Database(cfg)
    await db.connect()
    await db.run_migrations()
    yield db
    await db.close()


async def test_partitioned_databases_created_and_migrated(partitioned_db, tmp_path):
    """Verify that all 4 discrete SQLite databases exist and have schemas."""
    data_dir = tmp_path / "partitioned_data"
    catalog_path = data_dir / "catalog.sqlite3"
    queue_path = data_dir / "queue.sqlite3"
    jobs_path = data_dir / "jobs.sqlite3"
    users_path = data_dir / "users.sqlite3"

    assert catalog_path.is_file()
    assert queue_path.is_file()
    assert jobs_path.is_file()
    assert users_path.is_file()

    assert partitioned_db.catalog.is_connected
    assert partitioned_db.queue.is_connected
    assert partitioned_db.jobs.is_connected
    assert partitioned_db.users.is_connected


async def test_partitioned_jobs_and_catalog_independence(partitioned_db):
    """Verify jobs writes only to jobs.db while catalog writes to catalog.db."""
    run_id = await partitioned_db.jobs.start_job_run(
        "catalog_enrich", triggered_by="test"
    )
    await partitioned_db.jobs.add_job_run_logs(
        run_id,
        [("2026-09-23T12:00:00Z", "INFO", "test", "Enrichment started")],
    )

    await partitioned_db.catalog.insert_catalog(
        {
            "friendly_token": "tok_independent",
            "title": "Independent Movie",
            "manifest_url": "https://dropsugar.com/cytube/tok_independent.json",
            "synced_at": "2026-01-01T00:00:00Z",
        }
    )

    logs = await partitioned_db.jobs.get_job_run_logs(run_id)
    assert len(logs) == 1
    assert logs[0]["message"] == "Enrichment started"

    item = await partitioned_db.catalog.get_item("tok_independent")
    assert item is not None
    assert item["title"] == "Independent Movie"


async def test_partitioned_decoupled_watchlist(partitioned_db):
    """Verify decoupled watchlist: ordered in users.db, hydrated from catalog.db."""
    # Add catalog items
    for i in range(1, 4):
        await partitioned_db.catalog.insert_catalog(
            {
                "friendly_token": f"movie_{i}",
                "title": f"Movie {i}",
                "manifest_url": f"https://dropsugar.com/cytube/movie_{i}.json",
                "synced_at": "2026-01-01T00:00:00Z",
            }
        )

    # Add to user watchlist in order
    await partitioned_db.watchlist_add("user1", "movie_1")
    await partitioned_db.watchlist_add("user1", "movie_2")
    await partitioned_db.watchlist_add("user1", "movie_3")

    # Get watchlist through facade
    items = await partitioned_db.watchlist_get("user1", page=1, per_page=10)
    assert len(items) == 3
    # Newest added is movie_3, then movie_2, then movie_1
    assert [item["friendly_token"] for item in items] == [
        "movie_3",
        "movie_2",
        "movie_1",
    ]
    assert items[0]["title"] == "Movie 3"


async def test_partitioned_watchlist_handles_deleted_catalog_item(partitioned_db):
    """If a catalog item was removed, watchlist_get omits it gracefully while preserving remaining items."""
    await partitioned_db.catalog.insert_catalog(
        {
            "friendly_token": "active_movie",
            "title": "Active Movie",
            "manifest_url": "https://dropsugar.com/cytube/active.json",
            "synced_at": "2026-01-01T00:00:00Z",
        }
    )
    # Add both to user's watchlist
    await partitioned_db.watchlist_add("user2", "deleted_movie")
    await partitioned_db.watchlist_add("user2", "active_movie")

    items = await partitioned_db.watchlist_get("user2")
    assert len(items) == 1
    assert items[0]["friendly_token"] == "active_movie"


async def test_partitioned_browse_and_search_decoupled(partitioned_db):
    """Verify decoupled browse and search with recently-played filtering and blackouts."""
    await partitioned_db.catalog.insert_catalog(
        {
            "friendly_token": "tok_regular",
            "title": "Blade Runner",
            "manifest_url": "https://dropsugar.com/cytube/regular.json",
            "synced_at": "2026-01-01T00:00:00Z",
        }
    )
    await partitioned_db.catalog.insert_catalog(
        {
            "friendly_token": "tok_recently_played",
            "title": "The Terminator",
            "manifest_url": "https://dropsugar.com/cytube/recent.json",
            "synced_at": "2026-01-01T00:00:00Z",
        }
    )
    await partitioned_db.catalog.insert_catalog(
        {
            "friendly_token": "tok_blackout",
            "title": "Weekend Feature",
            "manifest_url": "https://dropsugar.com/cytube/blackout.json",
            "synced_at": "2026-01-01T00:00:00Z",
        }
    )

    # Record completion in queue.db
    await partitioned_db.queue.record_play_completion("cm", "tok_recently_played")
    # Record blackout in queue.db
    await partitioned_db.queue.upsert_blackout(
        "tok_blackout", reason="Weekend event", expires_at="2099-01-01T00:00:00Z"
    )

    # Regular user browse: hides recently played and blackouts
    regular_browse = await partitioned_db.browse(
        recently_played_days=7, show_hidden=False
    )
    reg_tokens = [i["friendly_token"] for i in regular_browse]
    assert "tok_regular" in reg_tokens
    assert "tok_recently_played" not in reg_tokens
    assert "tok_blackout" not in reg_tokens

    # Admin browse: sees all, decorates blackout_active and played_at
    admin_browse = await partitioned_db.browse(recently_played_days=0, show_hidden=True)
    admin_tokens = [i["friendly_token"] for i in admin_browse]
    assert "tok_regular" in admin_tokens
    assert "tok_recently_played" in admin_tokens
    assert "tok_blackout" in admin_tokens

    item_map = {i["friendly_token"]: i for i in admin_browse}
    assert item_map["tok_blackout"]["blackout_active"] == 1
    assert item_map["tok_recently_played"]["played_at"] is not None


async def test_partitioned_is_restricted_decoupling(partitioned_db):
    """Verify is_restricted checks queue.db reserved playlists via token and manifest_url."""
    manifest_url = "https://dropsugar.com/cytube/restricted.json"
    await partitioned_db.catalog.insert_catalog(
        {
            "friendly_token": "tok_restricted",
            "title": "Restricted Film",
            "manifest_url": manifest_url,
            "synced_at": "2026-01-01T00:00:00Z",
        }
    )

    # Not restricted initially
    assert await partitioned_db.is_restricted("tok_restricted") is False

    # Create immutable playlist in queue.db
    pl_id = await partitioned_db.queue.create_saved_playlist(
        name="Event", description=None, is_immutable=True, created_by="admin"
    )
    await partitioned_db.queue.append_playlist_items(
        pl_id,
        [{"media_type": "cm", "media_id": manifest_url, "title": "Restricted"}],
    )

    # Now is_restricted is True
    assert await partitioned_db.is_restricted("tok_restricted") is True


async def test_partitioned_get_tags_decoupled(partitioned_db):
    """Regression test: get_tags must not reference saved_playlist_items in catalog.db.

    Previously crashed with 'sqlite3.OperationalError: no such table:
    saved_playlist_items' because get_tags ran the monolith reserved-item
    exclusion subquery unconditionally, even in partitioned mode.
    """
    for i in range(1, 4):
        token = f"tag_movie_{i}"
        await partitioned_db.catalog.insert_catalog(
            {
                "friendly_token": token,
                "title": f"Tag Movie {i}",
                "manifest_url": f"https://dropsugar.com/cytube/{token}.json",
                "synced_at": "2026-01-01T00:00:00Z",
            }
        )
        tag_id = await partitioned_db.upsert_tag("actionpacked")
        await partitioned_db.set_catalog_tags(token, [tag_id])

    tags = await partitioned_db.get_tags()
    assert any(t["name"] == "actionpacked" for t in tags)


async def test_partitioned_reserved_exclusion_resolves_manifest_url(partitioned_db):
    """Regression test: reserved-item exclusion must resolve queue.db media_id
    (usually a manifest URL) back to friendly_token before filtering catalog.db,
    or immutable/promo-pool items leak into public browse/search/tags.
    """
    manifest_url = "https://dropsugar.com/cytube/reserved_by_url.json"
    await partitioned_db.catalog.insert_catalog(
        {
            "friendly_token": "tok_reserved_by_url",
            "title": "Reserved By URL",
            "manifest_url": manifest_url,
            "synced_at": "2026-01-01T00:00:00Z",
        }
    )
    await partitioned_db.catalog.insert_catalog(
        {
            "friendly_token": "tok_public",
            "title": "Public Item",
            "manifest_url": "https://dropsugar.com/cytube/public.json",
            "synced_at": "2026-01-01T00:00:00Z",
        }
    )

    pl_id = await partitioned_db.queue.create_saved_playlist(
        name="Immutable Event", description=None, is_immutable=True, created_by="admin"
    )
    # media_id stored as the manifest URL, the common real-world shape.
    await partitioned_db.queue.append_playlist_items(
        pl_id,
        [{"media_type": "cm", "media_id": manifest_url, "title": "Reserved By URL"}],
    )

    items = await partitioned_db.browse(show_hidden=False)
    tokens = [i["friendly_token"] for i in items]
    assert "tok_public" in tokens
    assert "tok_reserved_by_url" not in tokens
