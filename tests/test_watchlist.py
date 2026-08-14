"""Tests for the per-user watchlist ("My List") feature."""

import pytest

from kryten_webqueue.catalog.db import Database

MEDIACMS = "https://www.dropsugar.com"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "watchlist.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


async def _add_item(db: Database, token: str, title: str) -> None:
    await db.insert_catalog(
        {
            "friendly_token": token,
            "title": title,
            "description": "",
            "duration_sec": 7200,
            "manifest_url": f"{MEDIACMS}/api/v1/media/cytube/{token}.json?format=json",
            "thumbnail_url": "",
            "synced_at": "2026-01-01T00:00:00+00:00",
        }
    )


async def test_add_and_retrieve(db):
    await _add_item(db, "tok1", "Film One")
    await _add_item(db, "tok2", "Film Two")

    assert await db.watchlist_add("alice", "tok1") is True
    assert await db.watchlist_add("alice", "tok2") is True

    tokens = await db.watchlist_tokens("alice")
    assert set(tokens) == {"tok1", "tok2"}
    assert await db.watchlist_count("alice") == 2


async def test_add_duplicate_returns_false(db):
    await _add_item(db, "tok1", "Film One")
    assert await db.watchlist_add("alice", "tok1") is True
    assert await db.watchlist_add("alice", "tok1") is False  # already present
    assert await db.watchlist_count("alice") == 1


async def test_remove(db):
    await _add_item(db, "tok1", "Film One")
    await db.watchlist_add("alice", "tok1")

    assert await db.watchlist_remove("alice", "tok1") is True
    assert await db.watchlist_count("alice") == 0


async def test_remove_absent_returns_false(db):
    assert await db.watchlist_remove("alice", "nonexistent") is False


async def test_lists_are_per_user(db):
    await _add_item(db, "tok1", "Film One")
    await _add_item(db, "tok2", "Film Two")

    await db.watchlist_add("alice", "tok1")
    await db.watchlist_add("bob", "tok2")

    assert await db.watchlist_tokens("alice") == ["tok1"]
    assert await db.watchlist_tokens("bob") == ["tok2"]


async def test_watchlist_get_returns_catalog_rows(db):
    await _add_item(db, "tok1", "Film One")
    await _add_item(db, "tok2", "Film Two")
    await db.watchlist_add("alice", "tok1")
    await db.watchlist_add("alice", "tok2")

    items = await db.watchlist_get("alice")
    assert len(items) == 2
    titles = {i["title"] for i in items}
    assert titles == {"Film One", "Film Two"}
    # Catalog fields are present
    assert all("friendly_token" in i for i in items)
    assert all("duration_sec" in i for i in items)


async def test_watchlist_get_pagination(db):
    for i in range(5):
        await _add_item(db, f"tok{i}", f"Film {i}")
        await db.watchlist_add("alice", f"tok{i}")

    page1 = await db.watchlist_get("alice", page=1, per_page=3)
    page2 = await db.watchlist_get("alice", page=2, per_page=3)
    assert len(page1) == 3
    assert len(page2) == 2
    # No overlap between pages
    p1_tokens = {r["friendly_token"] for r in page1}
    p2_tokens = {r["friendly_token"] for r in page2}
    assert not p1_tokens & p2_tokens


async def test_watchlist_newest_first(db):
    """Items are returned newest-added first."""
    for token in ("oldest", "middle", "newest"):
        await _add_item(db, token, token.capitalize())
        await db.watchlist_add("alice", token)

    tokens = await db.watchlist_tokens("alice")
    assert tokens[0] == "newest"
    assert tokens[-1] == "oldest"


async def test_empty_watchlist(db):
    assert await db.watchlist_tokens("nobody") == []
    assert await db.watchlist_count("nobody") == 0
    assert await db.watchlist_get("nobody") == []
