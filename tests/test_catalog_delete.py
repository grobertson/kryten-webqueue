"""Catalog item deletion.

`delete_catalog_item` must remove the item plus its FTS row and every facet
association (tags, categories, people, studios). Regression test for the bug
where the method queried a non-existent `catalog.id` / `*.item_id` column and
500'd on every delete.
"""

import pytest

from kryten_webqueue.catalog.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "catalog_delete.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


async def _insert_item(db, token, title="Some Movie"):
    await db.insert_catalog(
        {
            "friendly_token": token,
            "title": title,
            "description": "a description",
            "duration_sec": 600,
            "manifest_url": f"https://cms/api/v1/media/{token}.json",
            "thumbnail_url": "",
            "synced_at": "2026-01-01T00:00:00+00:00",
        }
    )


async def _count(db, sql, params):
    row = await db._fetch_one(sql, params)
    return row["c"] if row else 0


async def test_delete_removes_item_and_all_facets(db):
    token = "TOK12345"
    await _insert_item(db, token)

    tag_id = await db.upsert_tag("Action")
    await db.set_catalog_tags(token, [tag_id])
    cat_id = await db.upsert_category("Movies")
    await db.set_catalog_categories(token, [cat_id])
    await db.set_catalog_people(token, [{"name": "Jane Doe", "role": "director"}])
    await db.set_catalog_studios(token, ["Acme Studios"])

    # Sanity: facets and FTS row are present before the delete.
    assert await db.get_item_admin(token) is not None
    assert (
        await _count(
            db, "SELECT COUNT(*) c FROM catalog_tags WHERE friendly_token=?", [token]
        )
        == 1
    )
    assert (
        await _count(
            db,
            "SELECT COUNT(*) c FROM catalog_categories WHERE friendly_token=?",
            [token],
        )
        == 1
    )
    assert (
        await _count(
            db, "SELECT COUNT(*) c FROM catalog_people WHERE friendly_token=?", [token]
        )
        == 1
    )
    assert (
        await _count(
            db, "SELECT COUNT(*) c FROM catalog_studios WHERE friendly_token=?", [token]
        )
        == 1
    )
    assert (
        await _count(
            db, "SELECT COUNT(*) c FROM catalog_fts WHERE friendly_token=?", [token]
        )
        == 1
    )

    assert await db.delete_catalog_item(token) is True

    # Item, FTS row, and every facet association are gone.
    assert await db.get_item_admin(token) is None
    assert (
        await _count(
            db, "SELECT COUNT(*) c FROM catalog_tags WHERE friendly_token=?", [token]
        )
        == 0
    )
    assert (
        await _count(
            db,
            "SELECT COUNT(*) c FROM catalog_categories WHERE friendly_token=?",
            [token],
        )
        == 0
    )
    assert (
        await _count(
            db, "SELECT COUNT(*) c FROM catalog_people WHERE friendly_token=?", [token]
        )
        == 0
    )
    assert (
        await _count(
            db, "SELECT COUNT(*) c FROM catalog_studios WHERE friendly_token=?", [token]
        )
        == 0
    )
    assert (
        await _count(
            db, "SELECT COUNT(*) c FROM catalog_fts WHERE friendly_token=?", [token]
        )
        == 0
    )


async def test_delete_missing_item_returns_false(db):
    assert await db.delete_catalog_item("DOES_NOT_EXIST") is False
