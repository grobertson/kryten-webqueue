"""Live-testing bug fixes (v0.9.3).

Covers:
  #1 ETA computed from the remainder of the current item and wrapping around
     the playlist when the now-playing item is not at index 0.
  #2 Paid FIFO anchor derived from the in-memory shadow (true play order).
  #4 Scheduled-event lock: blocks pay-to-play while an immutable event plays,
     auto-lifts when the last scheduled item begins, admin manual unlock, and
     the per-schedule pre-fire lock override.
"""

from datetime import datetime, timedelta, UTC

import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.queue.shadow import QueueShadow
from kryten_webqueue.queue.ordering import _last_pay_uid


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


def _polled(uid: int, *, seconds: int = 100):
    """A CyTube-style polled playlist entry."""
    return {
        "uid": uid,
        "queueby": None,
        "media": {
            "id": f"m{uid}",
            "type": "cm",
            "title": f"Item {uid}",
            "seconds": seconds,
        },
    }


# --- #1 ETA wrap-around ---


async def test_eta_wraps_from_current_item(db):
    shadow = QueueShadow(db)
    items = [_polled(1), _polled(2), _polled(3), _polled(4)]  # 100s each
    # The current item is uid=2 (index 1), 40s elapsed -> 60s remaining.
    now_playing = {"uid": 2, "seconds": 100, "currentTime": 40}
    await shadow.apply_poll_result(items, now_playing)

    eta = {it["uid"]: it["estimated_start_in_sec"] for it in shadow.items}
    # Current item plays now.
    assert eta[2] == 0
    # Next is uid=3 after the 60s remainder, then uid=4, then wrap to uid=1.
    assert eta[3] == 60
    assert eta[4] == 160
    assert eta[1] == 260


async def test_eta_without_now_playing_starts_at_head(db):
    shadow = QueueShadow(db)
    items = [_polled(1), _polled(2)]
    await shadow.apply_poll_result(items, None)
    eta = {it["uid"]: it["estimated_start_in_sec"] for it in shadow.items}
    assert eta[1] == 0
    assert eta[2] == 100


# --- #2 paid FIFO anchor from in-memory shadow ---


async def test_last_pay_uid_from_shadow_order(db):
    shadow = QueueShadow(db)
    # Build a shadow directly: now-playing free item, then two paid items.
    shadow._items = [
        {"uid": 10, "is_pay": False},
        {"uid": 11, "is_pay": True},
        {"uid": 12, "is_pay": True},
        {"uid": 13, "is_pay": False},
    ]
    assert _last_pay_uid(shadow) == 12

    # No paid items -> None (caller then anchors after now-playing).
    shadow._items = [{"uid": 1, "is_pay": False}]
    assert _last_pay_uid(shadow) is None


async def test_poll_persists_positions(db):
    """Reconciliation must persist positions so DB-backed queries don't drift."""
    shadow = QueueShadow(db)
    await shadow.apply_poll_result([_polled(1), _polled(2), _polled(3)], None)
    # Reorder externally: uid 3 moves to the front.
    await shadow.apply_poll_result([_polled(3), _polled(1), _polled(2)], None)
    rows = await db.get_shadow_items()
    by_uid = {r["uid"]: r["position"] for r in rows}
    assert by_uid == {3: 0, 1: 1, 2: 2}


# --- #4 scheduled-event lock ---


async def _make_event(db, *, is_immutable=True, last_item_uid=99):
    pid = await db.create_saved_playlist(
        name="Event", description=None, is_immutable=is_immutable, created_by="admin"
    )
    sid = await db.create_schedule(
        playlist_id=pid,
        label="Event",
        fire_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        is_active=1,
        created_by="admin",
    )
    now = datetime.now(UTC)
    await db.set_active_schedule(
        schedule_id=sid,
        playlist_id=pid,
        is_immutable=is_immutable,
        started_at=now.isoformat(),
        estimated_end_at=(now + timedelta(hours=1)).isoformat(),
        last_item_uid=last_item_uid,
    )
    return sid, pid


async def test_event_lock_active_and_manual_unlock(db):
    await _make_event(db, is_immutable=True)
    assert await db.is_event_lock_active() is True

    await db.disable_active_lock()
    assert await db.is_event_lock_active() is False
    # The active row is kept (event still "running"), just unlocked.
    assert (await db.get_active_schedule()) is not None


async def test_mutable_event_does_not_lock(db):
    await _make_event(db, is_immutable=False)
    assert await db.is_event_lock_active() is False


async def test_event_lock_auto_lifts_when_last_item_plays(db):
    _, _ = await _make_event(db, last_item_uid=3)
    assert await db.is_event_lock_active() is True

    shadow = QueueShadow(db)
    items = [_polled(1), _polled(2), _polled(3)]
    # The last scheduled item (uid=3) begins playing.
    await shadow.apply_poll_result(items, {"uid": 3, "seconds": 100, "currentTime": 0})
    assert await db.is_event_lock_active() is False


async def test_event_lock_stays_until_last_item(db):
    await _make_event(db, last_item_uid=3)
    shadow = QueueShadow(db)
    items = [_polled(1), _polled(2), _polled(3)]
    # An earlier item is playing -> still locked.
    await shadow.apply_poll_result(items, {"uid": 1, "seconds": 100, "currentTime": 0})
    assert await db.is_event_lock_active() is True


# --- active-schedule auto-expiry (v0.18.0) ---


async def test_active_schedule_cleared_when_last_item_plays_out(db):
    await _make_event(db, last_item_uid=3)
    shadow = QueueShadow(db)
    # The last scheduled item (uid=3) is still in the queue -> row kept.
    await shadow.apply_poll_result(
        [_polled(1), _polled(2), _polled(3)],
        {"uid": 2, "seconds": 100, "currentTime": 0},
    )
    assert (await db.get_active_schedule()) is not None
    # uid=3 has now left the queue (played out, temp item removed) -> row cleared.
    await shadow.apply_poll_result(
        [_polled(4), _polled(5)], {"uid": 4, "seconds": 100, "currentTime": 0}
    )
    assert (await db.get_active_schedule()) is None


async def test_active_schedule_not_cleared_when_nothing_playing(db):
    await _make_event(db, last_item_uid=3)
    shadow = QueueShadow(db)
    # Last item absent but nothing is playing (transient empty poll) -> keep row.
    await shadow.apply_poll_result([], None)
    assert (await db.get_active_schedule()) is not None


async def test_active_schedule_cleared_when_estimated_end_passed(db):
    # last_item_uid is None so the event-driven path can't fire; rely on the
    # time safety net with an estimated end well in the past.
    pid = await db.create_saved_playlist(
        name="Stale", description=None, is_immutable=True, created_by="admin"
    )
    sid = await db.create_schedule(
        playlist_id=pid,
        label="Stale",
        fire_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        is_active=1,
        created_by="admin",
    )
    past = datetime.now(UTC) - timedelta(hours=1)
    await db.set_active_schedule(
        schedule_id=sid,
        playlist_id=pid,
        is_immutable=True,
        started_at=(past - timedelta(hours=1)).isoformat(),
        estimated_end_at=past.isoformat(),
        last_item_uid=None,
    )
    shadow = QueueShadow(db)
    await shadow.apply_poll_result(
        [_polled(1)], {"uid": 1, "seconds": 100, "currentTime": 0}
    )
    assert (await db.get_active_schedule()) is None


# --- #4 pre-fire lock override ---


async def test_pre_fire_lock_can_be_disabled(db):
    pid = await db.create_saved_playlist(
        name="P", description=None, is_immutable=True, created_by="admin"
    )
    fire_at = (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    sid = await db.create_schedule(
        playlist_id=pid,
        label="Soon",
        fire_at=fire_at,
        pre_fire_lock_minutes=15,
        is_active=1,
        created_by="admin",
    )
    # Within the 15-min pre-fire window (fires in 5 min).
    assert await db.is_pre_fire_lock_active() is True

    await db.update_schedule(sid, lock_disabled=1)
    assert await db.is_pre_fire_lock_active() is False


# --- #2 (v0.9.4) scheduled-event fallback playlist ---


class _FakeApiGate:
    """Minimal api_gate stub recording playlist_add calls."""

    def __init__(self):
        self.added = []
        self._uid = 0
        self.cleared = False

    async def playlist_clear(self):
        self.cleared = True

    async def playlist_add(self, *, media_type, media_id, position="end"):
        self._uid += 1
        self.added.append(
            {"media_type": media_type, "media_id": media_id, "uid": self._uid}
        )
        return {"success": True, "uid": self._uid}


class _FakeWs:
    async def broadcast(self, *_a, **_k):
        pass


async def test_fire_appends_fallback_after_event(db):
    from kryten_webqueue.playlists.fire import fire_schedule

    event_pid = await db.create_saved_playlist(
        name="Event", description=None, is_immutable=True, created_by="admin"
    )
    await db.replace_playlist_items(
        event_pid,
        [
            {"media_type": "cm", "media_id": "e1", "title": "E1", "duration_sec": 100},
            {"media_type": "cm", "media_id": "e2", "title": "E2", "duration_sec": 100},
        ],
    )
    fallback_pid = await db.create_saved_playlist(
        name="Filler", description=None, is_immutable=False, created_by="admin"
    )
    await db.replace_playlist_items(
        fallback_pid,
        [
            {"media_type": "cm", "media_id": "f1", "title": "F1", "duration_sec": 100},
        ],
    )
    sid = await db.create_schedule(
        playlist_id=event_pid,
        label="Event",
        fire_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        is_active=1,
        created_by="admin",
        fallback_playlist_id=fallback_pid,
    )

    api = _FakeApiGate()
    shadow = QueueShadow(db)
    await fire_schedule(
        schedule_id=sid, api_gate=api, db=db, shadow=shadow, ws_manager=_FakeWs()
    )

    # Event items first, fallback appended after.
    assert [a["media_id"] for a in api.added] == ["e1", "e2", "f1"]

    # The event lock's last item is the last EVENT item (e2, uid=2), NOT the
    # fallback — so the lock lifts when e2 starts, not when the filler plays.
    active = await db.get_active_schedule()
    assert active["last_item_uid"] == 2


async def test_fire_without_fallback_only_adds_event(db):
    from kryten_webqueue.playlists.fire import fire_schedule

    event_pid = await db.create_saved_playlist(
        name="Event", description=None, is_immutable=True, created_by="admin"
    )
    await db.replace_playlist_items(
        event_pid,
        [
            {"media_type": "cm", "media_id": "e1", "title": "E1", "duration_sec": 100},
        ],
    )
    sid = await db.create_schedule(
        playlist_id=event_pid,
        label="Event",
        fire_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        is_active=1,
        created_by="admin",
    )

    api = _FakeApiGate()
    shadow = QueueShadow(db)
    await fire_schedule(
        schedule_id=sid, api_gate=api, db=db, shadow=shadow, ws_manager=_FakeWs()
    )
    assert [a["media_id"] for a in api.added] == ["e1"]


# --- Bug #1 regression: media_id fallback for event-lock lift ---


async def test_event_lock_lifts_via_media_id_when_uid_absent(db):
    """Lock lifts when now-playing matches last_item_media_id, even if
    last_item_uid is None (uid not captured during bulk add)."""
    pid = await db.create_saved_playlist(
        name="E", description=None, is_immutable=True, created_by="admin"
    )
    sid = await db.create_schedule(
        playlist_id=pid,
        label="E",
        fire_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        is_active=1,
        created_by="admin",
    )
    now = datetime.now(UTC)
    await db.set_active_schedule(
        schedule_id=sid,
        playlist_id=pid,
        is_immutable=True,
        started_at=now.isoformat(),
        estimated_end_at=(now + timedelta(hours=1)).isoformat(),
        last_item_uid=None,  # uid never captured
        last_item_media_id="https://cms.example/media/last.json",
    )
    assert await db.is_event_lock_active() is True

    shadow = QueueShadow(db)
    # now_playing has no uid — CyTube's changeMedia payload — only id/type.
    items = [_polled(1), _polled(2)]
    # Override the second item's media_id to match last_item_media_id.
    items[1] = {
        "uid": 2,
        "queueby": None,
        "media": {
            "id": "https://cms.example/media/last.json",
            "type": "cm",
            "title": "Last Item",
            "seconds": 100,
        },
    }
    np_no_uid = {
        "id": "https://cms.example/media/last.json",
        "type": "cm",
        "seconds": 100,
        "currentTime": 0,
    }
    await shadow.apply_poll_result(items, np_no_uid)
    assert await db.is_event_lock_active() is False


async def test_event_lock_stays_when_earlier_item_plays_by_media_id(db):
    """Lock must NOT lift when an earlier item (not the last) is playing."""
    pid = await db.create_saved_playlist(
        name="E", description=None, is_immutable=True, created_by="admin"
    )
    sid = await db.create_schedule(
        playlist_id=pid,
        label="E",
        fire_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        is_active=1,
        created_by="admin",
    )
    now = datetime.now(UTC)
    await db.set_active_schedule(
        schedule_id=sid,
        playlist_id=pid,
        is_immutable=True,
        started_at=now.isoformat(),
        estimated_end_at=(now + timedelta(hours=1)).isoformat(),
        last_item_uid=None,
        last_item_media_id="https://cms.example/media/last.json",
    )
    shadow = QueueShadow(db)
    items = [_polled(1), _polled(2)]
    items[1] = {
        "uid": 2,
        "queueby": None,
        "media": {
            "id": "https://cms.example/media/last.json",
            "type": "cm",
            "title": "Last",
            "seconds": 100,
        },
    }
    # Earlier item (uid=1 / id=m1) is playing — lock must stay on.
    np_earlier = {"id": "m1", "type": "cm", "seconds": 100, "currentTime": 0}
    await shadow.apply_poll_result(items, np_earlier)
    assert await db.is_event_lock_active() is True


async def test_fire_stores_last_item_media_id(db):
    """fire_schedule must store last_item_media_id on the active_schedule row."""
    from kryten_webqueue.playlists.fire import fire_schedule

    pid = await db.create_saved_playlist(
        name="Event", description=None, is_immutable=True, created_by="admin"
    )
    await db.replace_playlist_items(
        pid,
        [
            {
                "media_type": "cm",
                "media_id": "https://cms/a.json",
                "title": "A",
                "duration_sec": 60,
            },
            {
                "media_type": "cm",
                "media_id": "https://cms/b.json",
                "title": "B",
                "duration_sec": 60,
            },
        ],
    )
    sid = await db.create_schedule(
        playlist_id=pid,
        label="Test",
        fire_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        is_active=1,
        created_by="admin",
    )
    api = _FakeApiGate()
    shadow = QueueShadow(db)
    await fire_schedule(
        schedule_id=sid, api_gate=api, db=db, shadow=shadow, ws_manager=_FakeWs()
    )
    active = await db.get_active_schedule()
    assert active["last_item_media_id"] == "https://cms/b.json"
