"""Full-text run-log capture + download-queue visibility filtering.

Covers the v29 feature set: JobManager captures the log records a job emits
(scoped to that run) into ``job_run_logs``, and the fetch-queue visibility
filter hides successful downloads older than 24h while retaining failures and
pending/running items (rows are never deleted — audit trail preserved).
"""

import logging

import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.jobs.manager import JobManager


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "logs.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


async def _wait_terminal(db: Database, name: str, *, timeout: float = 2.0) -> dict:
    """Poll until the latest run of ``name`` leaves the 'running' state."""
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        runs = await db.get_job_runs(job_name=name, limit=1)
        if runs and runs[0]["status"] != "running":
            return runs[0]
        await asyncio.sleep(0.01)
    raise AssertionError(f"Job {name} did not finish within {timeout}s")


# --- Full-text log capture ---


async def test_job_logs_are_captured_and_scoped(db):
    job_logger = logging.getLogger("kryten_webqueue.tests.capture")
    job_logger.setLevel(logging.DEBUG)

    async def job(params, ctx):
        job_logger.info("hello from the job")
        job_logger.warning("a warning line")
        return {"ok": True}

    jm = JobManager(db)
    jm.register("logged", job)

    # A log emitted outside any run must not attach to the upcoming run.
    job_logger.info("this line is outside a run")

    await jm.run("logged")
    run = await _wait_terminal(db, "logged")

    lines = await db.get_job_run_logs(run["id"])
    messages = [ln["message"] for ln in lines]
    assert "hello from the job" in messages
    assert "a warning line" in messages
    assert "this line is outside a run" not in messages
    # seq preserves emission order.
    assert [ln["seq"] for ln in lines] == sorted(ln["seq"] for ln in lines)


async def test_failed_job_still_persists_captured_log(db):
    job_logger = logging.getLogger("kryten_webqueue.tests.capture_fail")
    job_logger.setLevel(logging.DEBUG)

    async def job(params, ctx):
        job_logger.error("about to blow up")
        raise RuntimeError("boom")

    jm = JobManager(db)
    jm.register("boomer", job)
    await jm.run("boomer")
    run = await _wait_terminal(db, "boomer")

    assert run["status"] == "failed"
    messages = [ln["message"] for ln in await db.get_job_run_logs(run["id"])]
    assert "about to blow up" in messages


# --- Download-queue visibility filter ---


async def _age_finished(db: Database, item_id: int, expr: str) -> None:
    await db._db.execute(
        f"UPDATE fetch_queue SET finished_at = datetime('now', '{expr}') WHERE id = ?",
        [item_id],
    )
    await db._db.commit()


async def test_expired_done_hidden_but_failures_and_pending_kept(db):
    old_done = await db.enqueue_fetch(url="http://old-done")
    await db.finish_fetch_item(old_done, status="done")
    await _age_finished(db, old_done, "-2 days")

    fresh_done = await db.enqueue_fetch(url="http://fresh-done")
    await db.finish_fetch_item(fresh_done, status="done")

    old_failed = await db.enqueue_fetch(url="http://old-failed")
    await db.finish_fetch_item(old_failed, status="failed", error="nope")
    await _age_finished(db, old_failed, "-2 days")

    pending = await db.enqueue_fetch(url="http://pending")

    visible = {r["id"] for r in await db.get_fetch_queue(hide_expired_done=True)}
    assert old_done not in visible  # expired success hidden
    assert fresh_done in visible  # recent success shown
    assert old_failed in visible  # failures never hidden
    assert pending in visible  # pending never hidden

    assert await db.count_fetch_queue(hide_expired_done=True) == len(visible)
    # The row still exists — visibility filter only, nothing deleted.
    assert await db.count_fetch_queue() == 4


async def test_fetch_queue_pagination(db):
    for i in range(5):
        await db.enqueue_fetch(url=f"http://item-{i}")

    page1 = await db.get_fetch_queue(limit=2, offset=0)
    page2 = await db.get_fetch_queue(limit=2, offset=2)
    page3 = await db.get_fetch_queue(limit=2, offset=4)
    assert len(page1) == 2 and len(page2) == 2 and len(page3) == 1
    ids = {r["id"] for r in page1 + page2 + page3}
    assert len(ids) == 5  # no overlap across pages
