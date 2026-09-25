from datetime import UTC, datetime, timedelta, timezone

from kryten_webqueue.catalog.db._pg_queue_db import _sqlite_timestamp_text


def test_postgres_timestamp_uses_sqlite_compatible_text() -> None:
    played_at = datetime(2026, 9, 25, 6, 33, 30, tzinfo=UTC)

    assert _sqlite_timestamp_text(played_at) == "2026-09-25 06:33:30"


def test_postgres_timestamp_is_normalized_to_utc() -> None:
    played_at = datetime(2026, 9, 25, 1, 33, 30, tzinfo=timezone(-timedelta(hours=5)))

    assert _sqlite_timestamp_text(played_at) == "2026-09-25 06:33:30"
