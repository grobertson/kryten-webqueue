"""Jobs domain schema and migrations for jobs.sqlite3."""

JOBS_MIGRATIONS: list[str] = [
    # v1: Migration tracking table
    """
    CREATE TABLE IF NOT EXISTS _migrations (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # v2: Jobs baseline schema
    """
    CREATE TABLE IF NOT EXISTS job_runs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        job_name     TEXT NOT NULL,
        started_at   TIMESTAMP NOT NULL,
        ended_at     TIMESTAMP,
        status       TEXT NOT NULL DEFAULT 'running',
        detail       TEXT,
        triggered_by TEXT,
        params       TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_job_runs_name ON job_runs(job_name, started_at);

    CREATE TABLE IF NOT EXISTS job_run_logs (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id    INTEGER NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
        seq       INTEGER NOT NULL,
        logged_at TIMESTAMP NOT NULL,
        level     TEXT,
        logger    TEXT,
        message   TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_job_run_logs_run ON job_run_logs(run_id, seq);
    CREATE INDEX IF NOT EXISTS idx_job_run_logs_logged ON job_run_logs(logged_at);

    CREATE TABLE IF NOT EXISTS job_schedules (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        job_name         TEXT NOT NULL UNIQUE,
        label            TEXT,
        cron_expression  TEXT NOT NULL,
        params_json      TEXT,
        is_active        INTEGER NOT NULL DEFAULT 1,
        run_next_job     TEXT,
        created_by       TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS fetch_queue (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        url             TEXT NOT NULL,
        quality         TEXT NOT NULL DEFAULT 'medium',
        max_videos      INTEGER NOT NULL DEFAULT 50,
        add_to_playlist INTEGER,
        status          TEXT NOT NULL DEFAULT 'pending',
        added_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        started_at      TIMESTAMP,
        finished_at     TIMESTAMP,
        added_by        TEXT,
        result_json     TEXT,
        error           TEXT,
        attempts        INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_fetch_queue_status ON fetch_queue(status, added_at);
    """,
]
