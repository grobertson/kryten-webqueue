"""Tests for Sortie 6: 30-Day Job Log Retention Pruner with Strict Exemption Guarantees."""

import pytest
from datetime import datetime, timedelta, timezone

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.config import DatabaseConfig
from kryten_webqueue.jobs.tasks import job_log_prune_job


class MockJobContext:
    def __init__(self, db: Database):
        self.db = db


@pytest.fixture
async def partitioned_prune_db(tmp_path):
    data_dir = tmp_path / "prune_test_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = DatabaseConfig(layout="partitioned", data_dir=str(data_dir))
    db = Database(cfg)
    await db.connect()
    await db.run_migrations()
    yield db
    await db.close()


async def test_job_log_prune_deletes_old_logs_only(partitioned_prune_db):
    db = partitioned_prune_db

    # Create a job run
    run_id = await db.jobs.start_job_run("test_job")

    # Add old log records (> 30 days old)
    old_time = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    await db.jobs._execute(
        "INSERT INTO job_run_logs (run_id, seq, logged_at, level, logger, message) VALUES (?, 1, ?, 'INFO', 'test', 'Old log line 1')",
        [run_id, old_time],
    )
    await db.jobs._execute(
        "INSERT INTO job_run_logs (run_id, seq, logged_at, level, logger, message) VALUES (?, 2, ?, 'INFO', 'test', 'Old log line 2')",
        [run_id, old_time],
    )

    # Add recent log records (< 30 days old)
    recent_time = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    await db.jobs._execute(
        "INSERT INTO job_run_logs (run_id, seq, logged_at, level, logger, message) VALUES (?, 3, ?, 'INFO', 'test', 'Recent log line 1')",
        [run_id, recent_time],
    )

    # Verify initial count
    logs_before = await db.jobs.get_job_run_logs(run_id)
    assert len(logs_before) == 3

    # Execute pruning task with default 30 days
    ctx = MockJobContext(db)
    result = await job_log_prune_job({"retention_days": 30}, ctx)
    assert result["deleted_logs"] == 2

    # Verify only recent log remains
    logs_after = await db.jobs.get_job_run_logs(run_id)
    assert len(logs_after) == 1
    assert logs_after[0]["message"] == "Recent log line 1"


async def test_job_log_prune_strictly_exempts_protected_tables(partitioned_prune_db):
    """COMPLIANCE TEST:

    Verify that job_log_prune NEVER deletes rows from:
    - queue.spend_requests
    - queue.queue_history
    - users.feedback
    - users.title_suggestions
    - catalog.item_edit_log
    """
    db = partitioned_prune_db

    # Insert old records into protected tables (> 90 days ago)
    old_time = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()

    # spend_requests
    await db.queue._execute(
        "INSERT INTO spend_requests (request_id, username, uid, z_cost, created_at) VALUES ('req_1', 'user1', 101, 50, ?)",
        [old_time],
    )

    # queue_history
    await db.queue._execute(
        "INSERT INTO queue_history (username, friendly_token, title, z_cost, queued_at) VALUES ('user1', 'tok_1', 'Old Movie', 50, ?)",
        [old_time],
    )

    # feedback
    await db.users._execute(
        "INSERT INTO feedback (username, body, created_at) VALUES ('user1', 'Old feedback from last year', ?)",
        [old_time],
    )

    # title_suggestions
    await db.users._execute(
        "INSERT INTO title_suggestions (username, query, created_at) VALUES ('user1', 'Old suggestion', ?)",
        [old_time],
    )

    # item_edit_log
    await db.catalog._execute(
        "INSERT INTO item_edit_log (friendly_token, username, field_name, old_value, new_value, edited_at) VALUES ('tok_1', 'admin', 'title', 'Old', 'New', ?)",
        [old_time],
    )

    # Run the prune task
    ctx = MockJobContext(db)
    await job_log_prune_job({"retention_days": 30}, ctx)

    # Verify ALL protected records remain 100% intact
    spends = await db.queue._fetch_all("SELECT * FROM spend_requests")
    assert len(spends) == 1
    assert spends[0]["request_id"] == "req_1"

    history = await db.queue.get_user_queue_history("user1")
    assert len(history) == 1

    feedback = await db.users.list_feedback()
    assert len(feedback) == 1

    suggestions = await db.users.list_title_suggestions()
    assert len(suggestions) == 1

    edits = await db.catalog.get_item_edit_history("tok_1")
    assert len(edits) == 1
