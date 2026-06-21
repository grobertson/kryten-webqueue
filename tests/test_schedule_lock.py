"""Regression tests for the scheduled-event pre-fire pay-to-play lock.

These cover a bug where the pre-fire lock lingered far past ``fire_at`` (until
the calendar day rolled over at midnight). ``fire_at`` is stored as a raw ISO
string with a ``T`` separator (e.g. ``2026-06-21T15:00:00+00:00`` from the
scheduler, or ``...Z`` from the admin UI's ``toISOString``). The lock predicate
compared it against ``datetime('now')`` (space-separated) without normalizing,
so SQLite did a *string* comparison in which ``'T'`` (84) sorts after ``' '``
(32). That kept ``fire_at > datetime('now')`` true from fire time until the date
prefix changed, so a 15-minute lock effectively lasted until midnight.

The fix wraps ``fire_at`` in ``datetime(...)`` on both sides so the comparison
is over normalized timestamps.
"""

from datetime import datetime, timedelta, UTC

import pytest

from kryten_webqueue.catalog.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


def _iso_offset(dt: datetime) -> str:
    """Scheduler storage format, e.g. '2026-06-21T15:00:00+00:00'."""
    return dt.astimezone(UTC).isoformat()


def _iso_zulu(dt: datetime) -> str:
    """Admin-UI storage format (JS Date.toISOString), e.g. '...T15:00:00.000Z'."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def _make_schedule(db: Database, *, fire_at: str, lock_minutes: int = 15) -> int:
    return await db.create_schedule(
        playlist_id=None,
        label="Test Event",
        fire_at=fire_at,
        pre_fire_lock_minutes=lock_minutes,
        is_active=True,
        created_by="tester",
    )


@pytest.mark.parametrize("fmt", [_iso_offset, _iso_zulu])
async def test_pre_fire_lock_releases_at_fire_time(db, fmt):
    """A schedule whose fire_at has passed must NOT keep the queue locked.

    Before the fix this returned True for the rest of the calendar day.
    """
    past = datetime.now(UTC) - timedelta(hours=2)
    await _make_schedule(db, fire_at=fmt(past))
    assert await db.is_pre_fire_lock_active() is False
    assert await db.get_active_pre_fire_lock() is None


async def test_pre_fire_lock_active_inside_window(db):
    """Inside the pre-fire window (future fire_at), the lock is active."""
    soon = datetime.now(UTC) + timedelta(minutes=5)
    await _make_schedule(db, fire_at=_iso_offset(soon), lock_minutes=15)
    assert await db.is_pre_fire_lock_active() is True
    lock = await db.get_active_pre_fire_lock()
    assert lock is not None and lock["label"] == "Test Event"


async def test_pre_fire_lock_inactive_before_window(db):
    """Before the pre-fire window opens, the lock is not active."""
    later = datetime.now(UTC) + timedelta(hours=2)
    await _make_schedule(db, fire_at=_iso_offset(later), lock_minutes=15)
    assert await db.is_pre_fire_lock_active() is False


async def test_get_next_schedule_ignores_past_fire(db):
    """The 'next schedule' must skip events whose fire_at has already passed.

    Before the fix a same-day past schedule was returned (and could even sort
    ahead of a genuine upcoming one).
    """
    past = datetime.now(UTC) - timedelta(hours=2)
    future = datetime.now(UTC) + timedelta(hours=3)
    await _make_schedule(db, fire_at=_iso_offset(past))
    future_id = await _make_schedule(db, fire_at=_iso_offset(future))

    nxt = await db.get_next_schedule()
    assert nxt is not None
    assert nxt["id"] == future_id


async def test_disable_active_pre_fire_locks_ends_lockout(db):
    """One call lifts EVERY active pre-fire lock, across both stored formats."""
    soon = datetime.now(UTC) + timedelta(minutes=5)
    later = datetime.now(UTC) + timedelta(minutes=10)
    await _make_schedule(db, fire_at=_iso_offset(soon), lock_minutes=15)
    await _make_schedule(db, fire_at=_iso_zulu(later), lock_minutes=15)

    assert await db.is_pre_fire_lock_active() is True
    count = await db.disable_active_pre_fire_locks()
    assert count == 2
    assert await db.is_pre_fire_lock_active() is False


async def test_disable_active_pre_fire_locks_noop_when_unlocked(db):
    """No active window → nothing lifted, returns 0 (idempotent end-lockout)."""
    later = datetime.now(UTC) + timedelta(hours=2)  # before its pre-fire window
    await _make_schedule(db, fire_at=_iso_offset(later), lock_minutes=15)

    assert await db.is_pre_fire_lock_active() is False
    assert await db.disable_active_pre_fire_locks() == 0


async def test_disable_active_pre_fire_locks_leaves_future_windows(db):
    """Lifting the current lockout must not pre-emptively unlock a later event
    whose pre-fire window has not opened yet."""
    soon = datetime.now(UTC) + timedelta(minutes=5)    # in-window now
    later = datetime.now(UTC) + timedelta(hours=4)     # window opens much later
    await _make_schedule(db, fire_at=_iso_offset(soon), lock_minutes=15)
    later_id = await _make_schedule(db, fire_at=_iso_offset(later), lock_minutes=15)

    assert await db.disable_active_pre_fire_locks() == 1
    later_sched = await db.get_schedule(later_id)
    assert later_sched["lock_disabled"] == 0

