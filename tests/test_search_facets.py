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
    await db.insert_catalog(
        {
            "friendly_token": token,
            "title": title,
            "description": "",
            "duration_sec": 600,
            "manifest_url": f"{MEDIACMS}/api/v1/media/cytube/{token}.json?format=json",
            "thumbnail_url": "",
            "synced_at": "2026-01-01T00:00:00+00:00",
        }
    )


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
    action_slug = next(
        c["slug"] for c in await db.get_categories() if c["name"] == "Action"
    )

    # Plain query: all three match.
    assert {r["friendly_token"] for r in await db.search("Dragon")} == {
        "tok_action",
        "tok_comedy",
        "tok_plain",
    }
    assert await db.search_count("Dragon") == 3

    # Query + category: only the Action item.
    cat_results = {
        r["friendly_token"] for r in await db.search("Dragon", category=action_slug)
    }
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
    comedy_slug = next(
        c["slug"] for c in await db.get_categories() if c["name"] == "Comedy"
    )

    # The category matches the item, but the query does not -> no results.
    assert await db.search("Horror", category=comedy_slug) == []
    assert await db.search_count("Horror", category=comedy_slug) == 0


async def _add_catalog_with_duration(db, token, title, duration_sec):
    await db.insert_catalog(
        {
            "friendly_token": token,
            "title": title,
            "description": "",
            "duration_sec": duration_sec,
            "manifest_url": f"{MEDIACMS}/api/v1/media/cytube/{token}.json?format=json",
            "thumbnail_url": "",
            "synced_at": "2026-01-01T00:00:00+00:00",
        }
    )


async def test_search_special_chars_do_not_raise(db):
    """FTS5 metacharacters in the query must not raise a 500."""
    await _add_catalog(db, "chud", "C.H.U.D.")
    await _add_catalog(db, "se7en", "Se7en (1995)")

    # "C.H.U.D." sanitizes to "C H U D"; FTS5 also tokenizes the title as
    # c h u d so the AND-match succeeds.
    results = await db.search("C.H.U.D.")
    tokens = {r["friendly_token"] for r in results}
    assert "chud" in tokens

    # With a year that's not in the title, the AND fails to match — but it must
    # not raise an exception (was crashing on the bare parentheses before fix).
    results_with_year = await db.search("C.H.U.D. (1984)")
    assert isinstance(results_with_year, list)

    # Colon (FTS5 column-filter syntax) must not raise.
    results_colon = await db.search("se7en: a film")
    assert isinstance(results_colon, list)

    # All-punctuation sanitizes to empty -> graceful empty return, no exception.
    assert await db.search("!!!") == []
    assert await db.search_count("!!!") == 0


async def test_browse_and_search_min_duration_filter(db):
    """Items shorter than min_duration_sec are excluded from browse and search."""
    await _add_catalog_with_duration(db, "short", "Short Sketch", 300)  # 5 min
    await _add_catalog_with_duration(db, "feature", "Feature Film", 5400)  # 90 min

    # Without filter: both items appear.
    browse_all = {r["friendly_token"] for r in await db.browse()}
    assert {"short", "feature"} == browse_all

    # With 10-minute minimum: only the feature.
    browse_filtered = {
        r["friendly_token"] for r in await db.browse(min_duration_sec=600)
    }
    assert browse_filtered == {"feature"}
    assert await db.browse_count(min_duration_sec=600) == 1

    # Search without filter: each item is findable by a unique word in its title.
    short_results = {r["friendly_token"] for r in await db.search("Sketch")}
    assert "short" in short_results
    feature_results = {r["friendly_token"] for r in await db.search("Feature")}
    assert "feature" in feature_results

    # Search with filter: short item is excluded.
    filtered_sketch = await db.search("Sketch", min_duration_sec=600)
    assert filtered_sketch == []
    assert await db.search_count("Sketch", min_duration_sec=600) == 0

    filtered_feature = {
        r["friendly_token"] for r in await db.search("Feature", min_duration_sec=600)
    }
    assert filtered_feature == {"feature"}
    assert await db.search_count("Feature", min_duration_sec=600) == 1
