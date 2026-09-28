"""Scheduled-playlist immutability expiry regression coverage."""

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


async def _schedule(db: Database, playlist_id: int, expires_at: datetime) -> None:
    await db.create_schedule(
        playlist_id=playlist_id,
        label="Scheduled event",
        fire_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        immutability_expires_at=expires_at.isoformat(),
        is_active=True,
        created_by="tester",
    )


async def test_expire_immutable_scheduled_playlists_releases_final_expired_window(db):
    playlist_id = await db.create_saved_playlist(
        name="Event", description="", is_immutable=False, created_by="tester"
    )
    await db._execute(
        "UPDATE saved_playlists SET is_immutable = 1 WHERE id = ?", [playlist_id]
    )
    await _schedule(db, playlist_id, datetime.now(UTC) - timedelta(minutes=1))

    assert await db.expire_immutable_scheduled_playlists() == 1
    playlist = await db.get_saved_playlist(playlist_id)
    assert playlist["is_immutable"] == 0


async def test_expiry_keeps_playlist_immutable_while_another_window_is_active(db):
    playlist_id = await db.create_saved_playlist(
        name="Event", description="", is_immutable=False, created_by="tester"
    )
    await db._execute(
        "UPDATE saved_playlists SET is_immutable = 1 WHERE id = ?", [playlist_id]
    )
    await _schedule(db, playlist_id, datetime.now(UTC) - timedelta(minutes=1))
    await _schedule(db, playlist_id, datetime.now(UTC) + timedelta(hours=1))

    assert await db.expire_immutable_scheduled_playlists() == 0
    playlist = await db.get_saved_playlist(playlist_id)
    assert playlist["is_immutable"] == 1
