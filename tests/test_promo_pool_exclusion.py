"""Promo pools are hidden from browse/search and rejected by pay-to-play.

Plan §2.9: clips that live in a saved playlist tagged with a ``promo_type`` must
never surface in the public catalog (browse/search) nor be pay-queueable
(``get_item`` is the pay-to-play eligibility gate — it returns ``None`` for promo
and immutable pool members). A normal catalog item is unaffected.
"""

import pytest

from kryten_webqueue.catalog.db import Database

MEDIACMS = "https://www.dropsugar.com"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "promo_excl.db"))
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


async def test_promo_pool_clips_excluded_from_browse_search_and_pay(db):
    # Two catalog items sharing a search term: one normal, one a promo clip.
    await _add_catalog(db, "normaltok", "Sparkle Normal Clip")
    await _add_catalog(db, "promotok", "Sparkle Promo Clip")

    # Tag a saved playlist as a promo pool and add the promo clip to it.
    pool_id = await db.create_saved_playlist(
        name="Channel IDs",
        description=None,
        is_immutable=False,
        created_by="admin",
        promo_type="channel_identity",
    )
    await db.append_playlist_item(
        pool_id,
        {
            "media_type": "cm",
            "media_id": "promotok",
            "title": "Sparkle Promo Clip",
            "duration_sec": 600,
        },
    )

    # Browse: normal visible, promo hidden.
    browse_tokens = {r["friendly_token"] for r in await db.browse()}
    assert "normaltok" in browse_tokens
    assert "promotok" not in browse_tokens
    assert await db.browse_count() == 1

    # Search: normal matches, promo excluded despite matching the query.
    search_tokens = {r["friendly_token"] for r in await db.search("Sparkle")}
    assert "normaltok" in search_tokens
    assert "promotok" not in search_tokens
    assert await db.search_count("Sparkle") == 1

    # Pay-to-play gate: get_item returns the normal item but None for the promo.
    assert (await db.get_item("normaltok")) is not None
    assert (await db.get_item("promotok")) is None


async def test_clearing_promo_type_restores_visibility(db):
    await _add_catalog(db, "clip1", "Reusable Clip")
    pool_id = await db.create_saved_playlist(
        name="Events",
        description=None,
        is_immutable=False,
        created_by="admin",
        promo_type="event",
    )
    await db.append_playlist_item(
        pool_id,
        {
            "media_type": "cm",
            "media_id": "clip1",
            "title": "Reusable Clip",
            "duration_sec": 600,
        },
    )
    assert (await db.get_item("clip1")) is None  # hidden while tagged

    # Untag the playlist: the clip becomes a normal, pay-eligible catalog item.
    await db.update_saved_playlist(
        pool_id,
        name="Events",
        description=None,
        is_immutable=False,
        promo_type=None,
    )
    assert (await db.get_item("clip1")) is not None
    assert "clip1" in {r["friendly_token"] for r in await db.browse()}
