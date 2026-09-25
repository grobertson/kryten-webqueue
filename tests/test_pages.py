from datetime import datetime, timedelta, timezone

from kryten_webqueue.routes.pages import _played_after_cutoff


def test_played_cutoff_accepts_postgres_datetime() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=21)
    assert _played_after_cutoff(datetime.now(timezone.utc), cutoff)
    assert not _played_after_cutoff(cutoff - timedelta(seconds=1), cutoff)


def test_played_cutoff_preserves_sqlite_text_behavior() -> None:
    cutoff = datetime(2026, 9, 2, tzinfo=timezone.utc)
    assert _played_after_cutoff("2026-09-02 00:00:00", cutoff)
    assert not _played_after_cutoff("2026-09-01 23:59:59", cutoff)
