"""Save-all-results-to-playlist feature (0.14.2).

Covers the pure season/episode ordering helper and the bulk, de-duplicating
playlist append used by the admin "Save results to playlist" button.
"""

import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.playlists.ordering import (
    parse_season_episode,
    order_for_playlist,
)

MEDIACMS = "https://www.dropsugar.com"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


# --- pure: season/episode parsing ---


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Show Name S01E02 - The Title", (1, 2)),
        ("Show Name s1e2", (1, 2)),
        ("Show Name S01 E02", (1, 2)),
        ("Show Name S01.E02", (1, 2)),
        ("Show Name 1x02", (1, 2)),
        ("Show Name 01x003", (1, 3)),
        ("Show Name Season 2 Episode 11", (2, 11)),
        ("A Plain Movie (2020)", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_season_episode(title, expected):
    parsed = parse_season_episode(title)
    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert (parsed[0], parsed[1]) == expected


# --- pure: ordering ---


def test_order_groups_series_by_season_episode():
    items = [
        {"title": "Cool Show S01E03"},
        {"title": "Cool Show S02E01"},
        {"title": "Cool Show S01E01"},
        {"title": "Cool Show S01E02"},
    ]
    ordered = [i["title"] for i in order_for_playlist(items)]
    assert ordered == [
        "Cool Show S01E01",
        "Cool Show S01E02",
        "Cool Show S01E03",
        "Cool Show S02E01",
    ]


def test_order_is_stable_for_non_episodic():
    # No S/E markers: alphabetical by title, ties keep input order.
    items = [
        {"title": "Banana"},
        {"title": "apple"},
        {"title": "Cherry"},
    ]
    ordered = [i["title"] for i in order_for_playlist(items)]
    assert ordered == ["apple", "Banana", "Cherry"]


def test_order_separates_distinct_series():
    items = [
        {"title": "Zeta Show S01E02"},
        {"title": "Alpha Show S01E02"},
        {"title": "Alpha Show S01E01"},
        {"title": "Zeta Show S01E01"},
    ]
    ordered = [i["title"] for i in order_for_playlist(items)]
    assert ordered == [
        "Alpha Show S01E01",
        "Alpha Show S01E02",
        "Zeta Show S01E01",
        "Zeta Show S01E02",
    ]


# --- db: bulk append with de-dupe ---


async def _make_playlist(db):
    return await db.create_saved_playlist(
        name="Results",
        description=None,
        is_immutable=False,
        created_by="admin",
    )


async def test_append_playlist_items_appends_in_order(db):
    pid = await _make_playlist(db)
    added = await db.append_playlist_items(
        pid,
        [
            {"media_type": "cm", "media_id": "a", "title": "A", "duration_sec": 60},
            {"media_type": "cm", "media_id": "b", "title": "B", "duration_sec": 60},
        ],
    )
    assert added == 2
    items = await db.get_saved_playlist_items(pid)
    assert [i["media_id"] for i in items] == ["a", "b"]
    assert [i["position"] for i in items] == [0, 1]


async def test_append_playlist_items_skips_existing(db):
    pid = await _make_playlist(db)
    await db.append_playlist_items(
        pid,
        [
            {"media_type": "cm", "media_id": "a", "title": "A"},
            {"media_type": "cm", "media_id": "b", "title": "B"},
        ],
    )
    # Second pass: only "c" is new; "a"/"b" already present.
    added = await db.append_playlist_items(
        pid,
        [
            {"media_type": "cm", "media_id": "a", "title": "A"},
            {"media_type": "cm", "media_id": "c", "title": "C"},
            {"media_type": "cm", "media_id": "b", "title": "B"},
        ],
    )
    assert added == 1
    items = await db.get_saved_playlist_items(pid)
    assert [i["media_id"] for i in items] == ["a", "b", "c"]
    assert [i["position"] for i in items] == [0, 1, 2]


async def test_append_playlist_items_dedupes_within_batch(db):
    pid = await _make_playlist(db)
    added = await db.append_playlist_items(
        pid,
        [
            {"media_type": "cm", "media_id": "x", "title": "X"},
            {"media_type": "cm", "media_id": "x", "title": "X dup"},
        ],
    )
    assert added == 1
    items = await db.get_saved_playlist_items(pid)
    assert [i["media_id"] for i in items] == ["x"]
