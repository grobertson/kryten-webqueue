"""Fetch-queue drain pacing + interrupted-download recovery.

Covers the anti-bot cooldown between drain items (config-driven, jittered,
live-reloadable) and the crash/shutdown recovery paths that re-queue an
interrupted download so it retries instead of being lost.
"""

import json
from types import SimpleNamespace

import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.config import Config, FetchQueueConfig
from kryten_webqueue.jobs import tasks
from kryten_webqueue.jobs.manager import JobContext


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "fetch_queue.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


def _ctx(db, config):
    return JobContext(
        db=db, api_gate=None, config=config, run_id=1, triggered_by="admin"
    )


# --- DB recovery helpers -------------------------------------------------------


async def test_reset_running_fetch_items_requeues_orphans(db):
    a = await db.enqueue_fetch(url="https://x/1")
    b = await db.enqueue_fetch(url="https://x/2")
    # Simulate a crash mid-download: one item stuck at 'running'.
    await db.claim_next_fetch_item()  # claims oldest (a) → running

    reset = await db.reset_running_fetch_items()
    assert reset == 1
    assert await db.count_fetch_queue_pending() == 2  # both pending again

    # The reset item can be claimed again (fresh re-attempt).
    claimed = await db.claim_next_fetch_item()
    assert claimed["id"] == a
    _ = b  # second item remains pending


async def test_requeue_fetch_item_clears_error_and_timestamps(db):
    item_id = await db.enqueue_fetch(url="https://x/1")
    await db.claim_next_fetch_item()
    await db.finish_fetch_item(item_id, status="failed", error="boom")

    await db.requeue_fetch_item(item_id)
    rows = await db.get_fetch_queue(limit=10)
    row = next(r for r in rows if r["id"] == item_id)
    assert row["status"] == "pending"
    assert row["started_at"] is None
    assert row["finished_at"] is None
    assert row["error"] is None


# --- live config reload --------------------------------------------------------


def test_reload_fetch_queue_reads_current_file(tmp_path):
    path = tmp_path / "config.json"
    base = {
        "secret_key": "x" * 16,
        "api_gate_token": "t",
        "mediacms_token": "t",
        "fetch_queue": {"cooldown_mean_minutes": 10.0},
    }
    path.write_text(json.dumps(base), encoding="utf-8")
    config = Config.from_file(path)
    assert config.reload_fetch_queue().cooldown_mean_minutes == 10.0

    # Edit the file on disk: a running drain must pick up the new value.
    base["fetch_queue"]["cooldown_mean_minutes"] = 55.0
    path.write_text(json.dumps(base), encoding="utf-8")
    assert config.reload_fetch_queue().cooldown_mean_minutes == 55.0


def test_reload_fetch_queue_falls_back_on_bad_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {"secret_key": "x" * 16, "api_gate_token": "t", "mediacms_token": "t"}
        ),
        encoding="utf-8",
    )
    config = Config.from_file(path)
    # Corrupt the file; reload must fall back to the in-memory section, not raise.
    path.write_text("{ not valid json", encoding="utf-8")
    fq = config.reload_fetch_queue()
    assert fq.cooldown_mean_minutes == config.fetch_queue.cooldown_mean_minutes


# --- cooldown behaviour --------------------------------------------------------


async def test_cooldown_disabled_does_not_sleep(db, monkeypatch):
    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(tasks.asyncio, "sleep", _fake_sleep)
    config = SimpleNamespace(
        reload_fetch_queue=lambda: FetchQueueConfig(cooldown_enabled=False)
    )
    ctx = _ctx(db, config)
    await tasks._fetch_queue_cooldown(ctx, processed=1, failed=0)
    assert slept == []


async def test_cooldown_sleeps_within_jitter_band(db, monkeypatch):
    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(tasks.asyncio, "sleep", _fake_sleep)
    fq = FetchQueueConfig(cooldown_mean_minutes=42.0, cooldown_jitter_minutes=8.0)
    config = SimpleNamespace(reload_fetch_queue=lambda: fq)
    ctx = _ctx(db, config)
    await tasks._fetch_queue_cooldown(ctx, processed=2, failed=1)
    assert len(slept) == 1
    # Within [34, 50] minutes, expressed in seconds.
    assert 34 * 60 <= slept[0] <= 50 * 60


async def test_drain_cooldown_runs_between_items_only(db, monkeypatch):
    await db.enqueue_fetch(url="https://x/1")
    await db.enqueue_fetch(url="https://x/2")

    cooldowns: list[int] = []

    async def _fake_cooldown(ctx, *, processed, failed):
        cooldowns.append(processed)

    async def _fake_single_fetch(params, ctx):
        return {"ok": True}

    monkeypatch.setattr(tasks, "_fetch_queue_cooldown", _fake_cooldown)
    monkeypatch.setattr(tasks, "_run_single_fetch", _fake_single_fetch)

    fq = FetchQueueConfig()
    ctx = _ctx(db, SimpleNamespace(reload_fetch_queue=lambda: fq))
    result = await tasks.fetch_queue_drain_job({}, ctx)

    assert result == {"processed": 2, "failed": 0}
    # Two items → exactly one inter-item cooldown (after the first, before the
    # second); none after the last item.
    assert cooldowns == [1]
