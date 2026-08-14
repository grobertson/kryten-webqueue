"""Tests for cast/crew/studio facets (DB layer and browse/search filtering)."""

import pytest

from kryten_webqueue.catalog.db import Database

MEDIACMS = "https://www.dropsugar.com"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "people.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


async def _add_item(db: Database, token: str, title: str, duration: int = 7200):
    await db.insert_catalog(
        {
            "friendly_token": token,
            "title": title,
            "description": "",
            "duration_sec": duration,
            "manifest_url": f"{MEDIACMS}/api/v1/media/cytube/{token}.json?format=json",
            "thumbnail_url": "",
            "synced_at": "2026-01-01T00:00:00+00:00",
        }
    )


# --- upsert / idempotency ---


async def test_upsert_person_idempotent(db):
    id1 = await db.upsert_person("James Cameron")
    id2 = await db.upsert_person("James Cameron")
    assert id1 == id2


async def test_upsert_studio_idempotent(db):
    id1 = await db.upsert_studio("Universal Pictures")
    id2 = await db.upsert_studio("Universal Pictures")
    assert id1 == id2


# --- set / get people ---


async def test_set_and_get_item_people(db):
    await _add_item(db, "tok1", "Aliens")
    await db.set_catalog_people(
        "tok1",
        [
            {"name": "James Cameron", "role": "director", "position": 0},
            {"name": "Sigourney Weaver", "role": "cast", "position": 0},
            {"name": "Michael Biehn", "role": "cast", "position": 1},
            {"name": "Gale Anne Hurd", "role": "producer", "position": 0},
        ],
    )
    people = await db.get_item_people("tok1")
    assert people["director"] == ["James Cameron"]
    assert "Sigourney Weaver" in people["cast"]
    assert "Michael Biehn" in people["cast"]
    assert people["producer"] == ["Gale Anne Hurd"]
    assert people["writer"] == []


async def test_set_catalog_people_replaces_existing(db):
    await _add_item(db, "tok1", "Film")
    await db.set_catalog_people("tok1", [{"name": "Old Director", "role": "director"}])
    await db.set_catalog_people("tok1", [{"name": "New Director", "role": "director"}])
    people = await db.get_item_people("tok1")
    assert people["director"] == ["New Director"]
    assert "Old Director" not in people["director"]


async def test_invalid_role_is_skipped(db):
    await _add_item(db, "tok1", "Film")
    await db.set_catalog_people("tok1", [{"name": "Someone", "role": "grip"}])
    people = await db.get_item_people("tok1")
    assert all(len(v) == 0 for v in people.values())


# --- set / get studios ---


async def test_set_and_get_studios(db):
    await _add_item(db, "tok1", "Film")
    await db.set_catalog_studios("tok1", ["Universal Pictures", "Amblin Entertainment"])
    studios = await db.get_item_studios("tok1")
    assert set(studios) == {"Universal Pictures", "Amblin Entertainment"}


async def test_set_catalog_studios_replaces(db):
    await _add_item(db, "tok1", "Film")
    await db.set_catalog_studios("tok1", ["Old Studio"])
    await db.set_catalog_studios("tok1", ["New Studio"])
    studios = await db.get_item_studios("tok1")
    assert studios == ["New Studio"]


# --- browse/search by person ---


async def test_browse_filter_by_person(db):
    await _add_item(db, "tok_aliens", "Aliens")
    await _add_item(db, "tok_titanic", "Titanic")
    await _add_item(db, "tok_other", "Other Film")

    await db.set_catalog_people(
        "tok_aliens", [{"name": "James Cameron", "role": "director"}]
    )
    await db.set_catalog_people(
        "tok_titanic", [{"name": "James Cameron", "role": "director"}]
    )

    results = await db.browse(person="James Cameron")
    tokens = {r["friendly_token"] for r in results}
    assert tokens == {"tok_aliens", "tok_titanic"}
    assert await db.browse_count(person="James Cameron") == 2


async def test_browse_filter_by_person_matches_any_role(db):
    await _add_item(db, "tok1", "Film A")
    await _add_item(db, "tok2", "Film B")
    await db.set_catalog_people("tok1", [{"name": "Alice", "role": "cast"}])
    await db.set_catalog_people("tok2", [{"name": "Alice", "role": "producer"}])

    results = await db.browse(person="Alice")
    tokens = {r["friendly_token"] for r in results}
    assert tokens == {"tok1", "tok2"}


async def test_browse_filter_by_studio(db):
    await _add_item(db, "tok1", "Film A")
    await _add_item(db, "tok2", "Film B")
    await _add_item(db, "tok3", "Film C")
    await db.set_catalog_studios("tok1", ["Universal"])
    await db.set_catalog_studios("tok2", ["Universal"])

    results = await db.browse(studio="Universal")
    tokens = {r["friendly_token"] for r in results}
    assert tokens == {"tok1", "tok2"}
    assert await db.browse_count(studio="Universal") == 2


async def test_search_filter_by_person(db):
    await _add_item(db, "tok1", "Aliens")
    await _add_item(db, "tok2", "Titanic")
    await _add_item(db, "tok3", "Avatar")
    await db.set_catalog_people("tok1", [{"name": "James Cameron", "role": "director"}])
    await db.set_catalog_people("tok2", [{"name": "James Cameron", "role": "director"}])
    await db.set_catalog_people("tok3", [{"name": "James Cameron", "role": "director"}])

    # Search narrows to Avatar only; person filter selects all Cameron films
    results = await db.search("Avatar", person="James Cameron")
    tokens = {r["friendly_token"] for r in results}
    assert tokens == {"tok3"}


# --- get_item_facets includes people/studios ---


async def test_get_item_facets_includes_people_and_studios(db):
    await _add_item(db, "tok1", "Aliens")
    await db.set_catalog_people("tok1", [{"name": "James Cameron", "role": "director"}])
    await db.set_catalog_studios("tok1", ["Brandywine Productions"])

    facets = await db.get_item_facets("tok1")
    assert facets["people"]["director"] == ["James Cameron"]
    assert facets["studios"] == ["Brandywine Productions"]
    assert "cast" in facets["people"]  # all roles present even if empty
