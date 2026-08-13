"""Hiding recently-played catalog items from regular users (admins always see).

Items with a genuine play-completion (``play_completions``) within the
configured day window are hidden. Completions are produced by
``CompletionRecorder`` only when the now-playing pointer advances off an item
that played past the 50% threshold, so refunded / early-skipped items never hide.

Completing an item also rotates it to the bottom of every mutable playlist
containing it, so the next schedule fire naturally loads unplayed items first.
"""

import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.queue.completion import CompletionRecorder
from kryten_webqueue.queue.shadow import QueueShadow

MEDIACMS = "https://www.dropsugar.com"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "recently_played.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


def _manifest(token):
    return f"{MEDIACMS}/api/v1/media/cytube/{token}.json?format=json"


async def _add_catalog(db, token, title, duration_sec=600):
    await db.insert_catalog(
        {
            "friendly_token": token,
            "title": title,
            "description": "",
            "duration_sec": duration_sec,
            "manifest_url": _manifest(token),
            "thumbnail_url": "",
            "synced_at": "2026-01-01T00:00:00+00:00",
        }
    )


async def _browse_tokens(db, **kwargs):
    return {r["friendly_token"] for r in await db.browse(**kwargs)}


# --- Time-window rule ------------------------------------------------------


async def test_completed_item_hidden_for_users_visible_for_admins(db):
    await _add_catalog(db, "tok_recent", "Recent Movie", duration_sec=7200)
    await _add_catalog(db, "tok_old", "Old Movie", duration_sec=7200)
    await _add_catalog(db, "tok_never", "Never Played", duration_sec=7200)

    # Recent completion (now) via the real code path.
    await db.record_play_completion(friendly_token="tok_recent", duration_sec=7200)
    # Old completion (31 days ago) inserted directly with an explicit timestamp.
    await db._execute(
        "INSERT INTO play_completions (media_type, media_id, completed_at) "
        "VALUES ('cm', 'tok_old', datetime('now', '-31 days'))"
    )

    # Regular user (21-day window): only the recently-completed item hidden.
    assert await _browse_tokens(db, recently_played_days=21) == {"tok_old", "tok_never"}
    assert await db.browse_count(recently_played_days=21) == 2

    # Admin (window disabled): every title visible.
    assert await _browse_tokens(db, recently_played_days=0) == {
        "tok_recent",
        "tok_old",
        "tok_never",
    }
    assert await db.browse_count(recently_played_days=0) == 3


async def test_time_rule_applies_to_search(db):
    await _add_catalog(db, "tok_recent", "Dragon Recent", duration_sec=7200)
    await _add_catalog(db, "tok_never", "Dragon Fresh", duration_sec=7200)
    await db.record_play_completion(friendly_token="tok_recent", duration_sec=7200)

    user = {
        r["friendly_token"] for r in await db.search("Dragon", recently_played_days=21)
    }
    assert user == {"tok_never"}
    assert await db.search_count("Dragon", recently_played_days=21) == 1

    admin = {
        r["friendly_token"] for r in await db.search("Dragon", recently_played_days=0)
    }
    assert admin == {"tok_recent", "tok_never"}


# --- Helpers shared by rotation + fire tests ------------------------------


async def _make_mutable_playlist(db, tokens, *, duration_sec):
    pid = await db.create_saved_playlist(
        name="Show X",
        description=None,
        is_immutable=False,
        created_by="admin",
    )
    await db.replace_playlist_items(
        pid,
        [
            {
                "media_type": "cm",
                "media_id": t,
                "title": t,
                "duration_sec": duration_sec,
            }
            for t in tokens
        ],
    )
    return pid


class _FakeApiGate:
    def __init__(self):
        self.added = []
        self._uid = 0

    async def playlist_clear(self):
        pass

    async def playlist_add(self, *, media_type, media_id, position="end"):
        self._uid += 1
        self.added.append(
            {"media_type": media_type, "media_id": media_id, "uid": self._uid}
        )
        return {"success": True, "uid": self._uid}


class _FakeWs:
    async def broadcast(self, *_a, **_k):
        pass


# --- Playlist rotation on completion -------------------------------------


async def test_rotation_moves_item_to_bottom(db):
    tokens = ["ep1", "ep2", "ep3", "ep4", "ep5"]
    for t in tokens:
        await _add_catalog(db, t, t, duration_sec=1500)
    pid = await _make_mutable_playlist(db, tokens, duration_sec=1500)

    rotated = await db.rotate_playlist_item_to_bottom("ep3")
    assert rotated == 1

    items = await db.get_saved_playlist_items(pid)
    assert [it["media_id"] for it in items] == ["ep1", "ep2", "ep4", "ep5", "ep3"]


async def test_rotation_on_last_item_is_noop(db):
    tokens = ["ep1", "ep2", "ep3"]
    for t in tokens:
        await _add_catalog(db, t, t, duration_sec=1500)
    pid = await _make_mutable_playlist(db, tokens, duration_sec=1500)

    rotated = await db.rotate_playlist_item_to_bottom("ep3")
    assert rotated == 0

    items = await db.get_saved_playlist_items(pid)
    assert [it["media_id"] for it in items] == ["ep1", "ep2", "ep3"]


async def test_rotation_not_applied_to_immutable_playlist(db):
    tokens = ["ep1", "ep2", "ep3"]
    for t in tokens:
        await _add_catalog(db, t, t, duration_sec=1500)
    pid = await db.create_saved_playlist(
        name="Event", description=None, is_immutable=True, created_by="admin"
    )
    await db.replace_playlist_items(
        pid,
        [
            {"media_type": "cm", "media_id": t, "title": t, "duration_sec": 1500}
            for t in tokens
        ],
    )

    rotated = await db.rotate_playlist_item_to_bottom("ep1")
    assert rotated == 0

    items = await db.get_saved_playlist_items(pid)
    assert [it["media_id"] for it in items] == ["ep1", "ep2", "ep3"]


async def test_rotation_not_applied_to_promo_pool(db):
    tokens = ["promo1", "promo2", "promo3"]
    for t in tokens:
        await _add_catalog(db, t, t, duration_sec=30)
    pid = await db.create_saved_playlist(
        name="Bumpers",
        description=None,
        is_immutable=False,
        created_by="admin",
        promo_type="general",
    )
    await db.replace_playlist_items(
        pid,
        [
            {"media_type": "cm", "media_id": t, "title": t, "duration_sec": 30}
            for t in tokens
        ],
    )

    rotated = await db.rotate_playlist_item_to_bottom("promo1")
    assert rotated == 0

    items = await db.get_saved_playlist_items(pid)
    assert [it["media_id"] for it in items] == ["promo1", "promo2", "promo3"]


async def test_rotation_multi_playlist_item(db):
    tokens_a = ["shared", "a2", "a3"]
    tokens_b = ["b1", "shared", "b3"]
    for t in {*tokens_a, *tokens_b}:
        await _add_catalog(db, t, t, duration_sec=1500)
    pid_a = await _make_mutable_playlist(db, tokens_a, duration_sec=1500)
    pid_b = await _make_mutable_playlist(db, tokens_b, duration_sec=1500)

    rotated = await db.rotate_playlist_item_to_bottom("shared")
    assert rotated == 2

    assert [it["media_id"] for it in await db.get_saved_playlist_items(pid_a)] == [
        "a2",
        "a3",
        "shared",
    ]
    assert [it["media_id"] for it in await db.get_saved_playlist_items(pid_b)] == [
        "b1",
        "b3",
        "shared",
    ]


async def test_completion_recorder_triggers_rotation(db):
    tokens = ["ep1", "ep2", "ep3"]
    for t in tokens:
        await _add_catalog(db, t, t, duration_sec=1500)
    pid = await _make_mutable_playlist(db, tokens, duration_sec=1500)
    rec = CompletionRecorder(db=db)

    await rec.on_poll(
        {"id": _manifest("ep1"), "type": "cm", "currentTime": 900, "seconds": 1500}
    )
    # Advance to ep2 — ep1 finalizes, rotation fires.
    await rec.on_poll(
        {"id": _manifest("ep2"), "type": "cm", "currentTime": 0, "seconds": 1500}
    )

    items = await db.get_saved_playlist_items(pid)
    assert [it["media_id"] for it in items] == ["ep2", "ep3", "ep1"]


async def test_fire_after_rotation_loads_in_rotated_order(db):
    from datetime import datetime, UTC
    from kryten_webqueue.playlists.fire import fire_schedule

    tokens = ["ep1", "ep2", "ep3"]
    for t in tokens:
        await _add_catalog(db, t, t, duration_sec=1500)
    pid = await _make_mutable_playlist(db, tokens, duration_sec=1500)

    await db.rotate_playlist_item_to_bottom("ep1")

    sid = await db.create_schedule(
        playlist_id=pid,
        label="Show",
        fire_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        is_active=1,
        created_by="admin",
    )
    api = _FakeApiGate()
    await fire_schedule(
        schedule_id=sid,
        api_gate=api,
        db=db,
        shadow=QueueShadow(db),
        ws_manager=_FakeWs(),
    )

    assert [a["media_id"] for a in api.added] == ["ep2", "ep3", "ep1"]


# --- CompletionRecorder threshold ----------------------------------------


async def test_recorder_records_only_past_threshold(db):
    await _add_catalog(db, "played", "Played", duration_sec=100)
    await _add_catalog(db, "skipped", "Skipped", duration_sec=100)
    rec = CompletionRecorder(db=db)

    # 'played' reaches 60% then advances away -> recorded.
    await rec.on_poll(
        {"id": _manifest("played"), "type": "cm", "currentTime": 5, "seconds": 100}
    )
    await rec.on_poll(
        {"id": _manifest("played"), "type": "cm", "currentTime": 60, "seconds": 100}
    )
    # Advance to 'skipped', only reaches 30% then stops -> not recorded.
    await rec.on_poll(
        {"id": _manifest("skipped"), "type": "cm", "currentTime": 30, "seconds": 100}
    )
    await rec.on_poll({})  # playback stops -> finalize 'skipped'

    hidden = await db._fetch_all("SELECT media_id FROM play_completions")
    assert {r["media_id"] for r in hidden} == {"played"}


async def test_recorder_ignores_item_that_never_played(db):
    # An item that is queued and refunded never becomes now-playing, so the
    # recorder never sees it and it is never hidden.
    await _add_catalog(db, "refunded", "Refunded", duration_sec=100)
    await _add_catalog(db, "actually_played", "Played", duration_sec=100)
    rec = CompletionRecorder(db=db)

    await rec.on_poll(
        {
            "id": _manifest("actually_played"),
            "type": "cm",
            "currentTime": 90,
            "seconds": 100,
        }
    )
    await rec.on_poll({})  # advance -> record 'actually_played'

    hidden = {
        r["media_id"]
        for r in await db._fetch_all("SELECT media_id FROM play_completions")
    }
    assert hidden == {"actually_played"}
    assert "refunded" not in hidden


# --- Admin test helpers (mark-played / clear / debug) ---------------------


async def test_clear_play_state_unhides_time_window_item(db):
    await _add_catalog(db, "movie", "Movie", duration_sec=7200)
    await db.record_play_completion(friendly_token="movie", duration_sec=7200)
    assert await _browse_tokens(db, recently_played_days=21) == set()

    removed = await db.clear_play_state("movie")
    assert removed == {"completions": 1}
    assert await _browse_tokens(db, recently_played_days=21) == {"movie"}


async def test_recently_played_debug_reports_completion_signal(db):
    await _add_catalog(db, "movie", "Movie", duration_sec=7200)
    await _add_catalog(db, "ep1", "Episode 1", duration_sec=1500)
    await _make_mutable_playlist(db, ["ep1", "ep2"], duration_sec=1500)

    await db.record_play_completion(friendly_token="movie", duration_sec=7200)
    await db.record_play_completion(friendly_token="ep1", duration_sec=1500)

    debug = await db.get_recently_played_debug(21)
    assert debug["window_days"] == 21
    assert {r["media_id"] for r in debug["by_completion"]} == {"movie", "ep1"}


async def test_promo_clips_are_exempt_from_hiding(db):
    # A clip in a promo pool (promo_type set) must never be recorded as played
    # nor appear in the recently-played debug list.
    await _add_catalog(db, "promo1", "Bumper", duration_sec=30)
    pid = await db.create_saved_playlist(
        name="Bumpers",
        description=None,
        is_immutable=False,
        created_by="admin",
        promo_type="general",
    )
    await db.replace_playlist_items(
        pid,
        [
            {
                "media_type": "cm",
                "media_id": "promo1",
                "title": "Bumper",
                "duration_sec": 30,
            },
        ],
    )

    await db.record_play_completion(friendly_token="promo1", duration_sec=30)

    rows = await db._fetch_all("SELECT media_id FROM play_completions")
    assert rows == []
    debug = await db.get_recently_played_debug(21)
    assert debug["by_completion"] == []


async def test_purge_promo_hide_state_removes_stale_rows(db):
    # Rows recorded before the promo exemption (e.g. in production) are cleaned up
    # at the data layer by purge_promo_hide_state (also run by migration v14).
    await _add_catalog(db, "promo1", "Bumper", duration_sec=30)
    pid = await db.create_saved_playlist(
        name="Bumpers",
        description=None,
        is_immutable=False,
        created_by="admin",
        promo_type="general",
    )
    await db.replace_playlist_items(
        pid,
        [
            {
                "media_type": "cm",
                "media_id": "promo1",
                "title": "Bumper",
                "duration_sec": 30,
            },
        ],
    )
    # Simulate a stale row written before the exemption existed.
    await db._execute(
        "INSERT INTO play_completions (media_type, media_id) VALUES ('cm', 'promo1')"
    )
    # Before the purge it shows in the (unfiltered) debug view.
    assert {
        r["media_id"] for r in (await db.get_recently_played_debug(21))["by_completion"]
    } == {"promo1"}

    removed = await db.purge_promo_hide_state()
    assert removed["completions"] == 1

    assert await db._fetch_all("SELECT media_id FROM play_completions") == []
    assert (await db.get_recently_played_debug(21))["by_completion"] == []


async def test_hidden_tag_promo_is_exempt_from_hiding(db):
    # Station promos/bumpers are usually classified by hidden tag/category
    # (e.g. `channelz`), not promo-pool membership. They must still be exempt.
    await _add_catalog(db, "bumper", "CHANNEL Z - ITS 10PM", duration_sec=15)
    tid = await db.upsert_tag("channelz")
    await db.set_catalog_tags("bumper", [tid])

    await db.record_play_completion(friendly_token="bumper", duration_sec=15)
    assert await db._fetch_all("SELECT media_id FROM play_completions") == []


async def test_hidden_category_promo_is_exempt_from_hiding(db):
    await _add_catalog(db, "bumper", "Z Promo", duration_sec=15)
    cid = await db.upsert_category("Z Channel Promos")
    await db.set_catalog_categories("bumper", [cid])

    await db.record_play_completion(friendly_token="bumper", duration_sec=15)
    assert await db._fetch_all("SELECT media_id FROM play_completions") == []


async def test_purge_removes_hidden_tag_promo_rows(db):
    # A stale row written before the tag/category exemption is cleaned by purge.
    await _add_catalog(db, "bumper", "CHANNEL Z - ITS 10PM", duration_sec=15)
    tid = await db.upsert_tag("channelz")
    await db.set_catalog_tags("bumper", [tid])
    await db._execute(
        "INSERT INTO play_completions (media_type, media_id) VALUES ('cm', 'bumper')"
    )

    removed = await db.purge_promo_hide_state()
    assert removed["completions"] == 1
    assert await db._fetch_all("SELECT media_id FROM play_completions") == []
