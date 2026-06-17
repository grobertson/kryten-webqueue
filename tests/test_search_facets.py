"""Search combines with category/tag facets (Item 4 of the UX polish plan).

A free-text search now ANDs with the selected category and/or tag, mirroring how
browse() already filters. These tests pin that behavior at the DB layer.
"""

import pytest

from kryten_webqueue.catalog.db import Database

MEDIACMS = "https://www.dropsugar.com"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "search_facets.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


async def _add_catalog(db, token, title):
    await db.insert_catalog({
        "friendly_token": token,
        "title": title,
        "description": "",
        "duration_sec": 600,
        "manifest_url": f"{MEDIACMS}/api/v1/media/cytube/{token}.json?format=json",
        "thumbnail_url": "",
        "synced_at": "2026-01-01T00:00:00+00:00",
    })


async def test_search_ands_with_category_and_tag(db):
    # Three items all matching the query "Dragon", differentiated by facets.
    await _add_catalog(db, "tok_action", "Dragon Action")
    await _add_catalog(db, "tok_comedy", "Dragon Comedy")
    await _add_catalog(db, "tok_plain", "Dragon Plain")

    action_id = await db.upsert_category("Action")
    comedy_id = await db.upsert_category("Comedy")
    await db.set_catalog_categories("tok_action", [action_id])
    await db.set_catalog_categories("tok_comedy", [comedy_id])

    epic_tag = await db.upsert_tag("Epic")
    await db.set_catalog_tags("tok_action", [epic_tag])

    # Resolve the Action slug (upsert derives it).
    action_slug = next(c["slug"] for c in await db.get_categories() if c["name"] == "Action")

    # Plain query: all three match.
    assert {r["friendly_token"] for r in await db.search("Dragon")} == {
        "tok_action", "tok_comedy", "tok_plain",
    }
    assert await db.search_count("Dragon") == 3

    # Query + category: only the Action item.
    cat_results = {r["friendly_token"] for r in await db.search("Dragon", category=action_slug)}
    assert cat_results == {"tok_action"}
    assert await db.search_count("Dragon", category=action_slug) == 1

    # Query + tag: only the Epic-tagged item.
    tag_results = {r["friendly_token"] for r in await db.search("Dragon", tag="Epic")}
    assert tag_results == {"tok_action"}
    assert await db.search_count("Dragon", tag="Epic") == 1

    # Query + category + tag that don't co-occur: empty (true intersection).
    assert await db.search("Dragon", category=action_slug, tag="Nonexistent") == []
    assert await db.search_count("Dragon", category=action_slug, tag="Nonexistent") == 0


async def test_search_facet_with_no_text_match_is_empty(db):
    await _add_catalog(db, "tok1", "Comedy Night")
    comedy_id = await db.upsert_category("Comedy")
    await db.set_catalog_categories("tok1", [comedy_id])
    comedy_slug = next(c["slug"] for c in await db.get_categories() if c["name"] == "Comedy")

    # The category matches the item, but the query does not -> no results.
    assert await db.search("Horror", category=comedy_slug) == []
    assert await db.search_count("Horror", category=comedy_slug) == 0
