"""Phase 2 (SPEC_JOBS_AND_BROWSE) jobs-framework tests.

Covers schema validation (A1.3) and the parameterized JobManager run path:
params are validated, defaults applied, passed to the job, and persisted to
``job_runs``; progress updates land on the row; unknown jobs and invalid
params raise the expected errors.
"""

import asyncio
import json

import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.jobs.manager import JobManager, validate_params


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "jobs.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


async def _wait_terminal(db: Database, name: str, *, timeout: float = 2.0) -> dict:
    """Poll until the latest run of ``name`` leaves the 'running' state."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        runs = await db.get_job_runs(job_name=name, limit=1)
        if runs and runs[0]["status"] != "running":
            return runs[0]
        await asyncio.sleep(0.01)
    raise AssertionError(f"Job {name} did not finish within {timeout}s")


SCHEMA = [
    {"name": "url", "type": "string", "required": True, "label": "URL"},
    {"name": "quality", "type": "enum", "default": "medium",
     "options": ["best", "good", "medium"], "label": "Quality"},
    {"name": "max_videos", "type": "int", "default": 50, "label": "Max"},
    {"name": "dry_run", "type": "bool", "default": False, "label": "Dry run"},
]


# --- validate_params ---

def test_validate_applies_defaults_and_coerces():
    out = validate_params(SCHEMA, {"url": "http://x", "max_videos": "10", "dry_run": "true"})
    assert out == {"url": "http://x", "quality": "medium", "max_videos": 10, "dry_run": True}


def test_validate_required_missing_raises():
    with pytest.raises(ValueError, match="URL is required"):
        validate_params(SCHEMA, {"quality": "best"})


def test_validate_enum_rejects_unknown_option():
    with pytest.raises(ValueError, match="Quality"):
        validate_params(SCHEMA, {"url": "http://x", "quality": "ultra"})


def test_validate_int_rejects_non_numeric():
    with pytest.raises(ValueError, match="Max"):
        validate_params(SCHEMA, {"url": "http://x", "max_videos": "abc"})


def test_validate_no_schema_returns_empty():
    assert validate_params([], {"anything": 1}) == {}


# --- JobManager parameterized run ---

async def test_parameterized_job_receives_params_and_persists(db):
    seen = {}

    async def job(params, ctx):
        seen.update(params)
        await ctx.progress({"processed": 1})
        return {"ok": True, "count": params["max_videos"]}

    jm = JobManager(db)
    jm.register("fetch", job, label="Fetch", schema=SCHEMA)

    result = await jm.run("fetch", triggered_by="admin", params={"url": "http://x"})
    assert result["started"] is True

    run = await _wait_terminal(db, "fetch")
    assert run["status"] == "completed"
    assert json.loads(run["params"]) == {
        "url": "http://x", "quality": "medium", "max_videos": 50, "dry_run": False,
    }
    assert json.loads(run["detail"]) == {"ok": True, "count": 50}
    assert seen["url"] == "http://x"


async def test_progress_updates_detail_on_running_row(db):
    progressed = asyncio.Event()
    release = asyncio.Event()

    async def job(params, ctx):
        await ctx.progress({"processed": 7, "total": 10})
        progressed.set()
        await release.wait()
        return {"done": True}

    jm = JobManager(db)
    jm.register("p", job)
    await jm.run("p")
    await progressed.wait()

    run = (await db.get_job_runs(job_name="p", limit=1))[0]
    assert run["status"] == "running"
    assert json.loads(run["detail"]) == {"processed": 7, "total": 10}
    release.set()
    await _wait_terminal(db, "p")


async def test_invalid_params_raise_before_run(db):
    async def job(params, ctx):
        return None

    jm = JobManager(db)
    jm.register("fetch", job, schema=SCHEMA)
    with pytest.raises(ValueError):
        await jm.run("fetch", params={})  # missing required url
    # No run row should have been created.
    assert await db.get_job_runs(job_name="fetch") == []


async def test_unknown_job_raises_keyerror(db):
    jm = JobManager(db)
    with pytest.raises(KeyError):
        await jm.run("nope")


async def test_job_error_records_clean_message(db):
    from kryten_webqueue.jobs.manager import JobError

    async def job(params, ctx):
        raise JobError("This weekend's worksheet '6.12-6.13' was not found.")

    jm = JobManager(db)
    jm.register("fetchurls", job)
    await jm.run("fetchurls")
    run = await _wait_terminal(db, "fetchurls")
    assert run["status"] == "failed"
    # Clean message, no "RuntimeError:"/"JobError:" type prefix or traceback.
    assert json.loads(run["detail"]) == {
        "error": "This weekend's worksheet '6.12-6.13' was not found."
    }


async def test_unexpected_error_keeps_type_prefix(db):
    async def job(params, ctx):
        raise ValueError("boom")

    jm = JobManager(db)
    jm.register("crashy", job)
    await jm.run("crashy")
    run = await _wait_terminal(db, "crashy")
    assert run["status"] == "failed"
    # Unexpected (bug) failures retain the exception type for debugging.
    assert json.loads(run["detail"]) == {"error": "ValueError: boom"}


async def test_already_running_guard(db):
    started = asyncio.Event()
    release = asyncio.Event()

    async def job(params, ctx):
        started.set()
        await release.wait()

    jm = JobManager(db)
    jm.register("slow", job)
    first = await jm.run("slow")
    assert first["started"] is True
    await started.wait()
    second = await jm.run("slow")
    assert second == {"started": False, "reason": "already_running"}
    release.set()
    await _wait_terminal(db, "slow")


def test_list_jobs_includes_schema():
    # Pure in-memory check that schema is surfaced for the Run modal.
    class _NullDB:
        pass

    jm = JobManager(_NullDB())
    jm.register("a", lambda p, c: None, label="A", schema=SCHEMA)
    jm.register("b", lambda p, c: None, label="B")
    listed = {j["name"]: j for j in jm.list_jobs()}
    assert listed["a"]["schema"] == SCHEMA
    assert listed["b"]["schema"] == []
    assert jm.get_schema("a") == SCHEMA
