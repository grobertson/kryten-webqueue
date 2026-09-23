"""Queue domain schema and migrations for queue.sqlite3."""

QUEUE_MIGRATIONS: list[str] = [
    # v1: Migration tracking table
    """
    CREATE TABLE IF NOT EXISTS _migrations (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # v2: Queue baseline schema
    """
    CREATE TABLE IF NOT EXISTS queue_shadow (
        uid                INTEGER PRIMARY KEY,
        position           INTEGER NOT NULL,
        title              TEXT,
        friendly_token     TEXT,
        media_type         TEXT NOT NULL,
        media_id           TEXT NOT NULL,
        duration_sec       INTEGER,
        is_pay             BOOLEAN NOT NULL DEFAULT 0,
        paid_by            TEXT,
        tier               TEXT,
        z_cost             INTEGER,
        schedule_id        INTEGER,
        estimated_start_at TIMESTAMP,
        added_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_promo           BOOLEAN NOT NULL DEFAULT 0,
        promo_type         TEXT,
        lead_in_for_uid    INTEGER
    );

    CREATE TABLE IF NOT EXISTS spend_requests (
        request_id     TEXT PRIMARY KEY,
        username       TEXT NOT NULL,
        uid            INTEGER,
        friendly_token TEXT,
        tier           TEXT,
        z_cost         INTEGER,
        created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        refunded       BOOLEAN DEFAULT 0,
        refunded_at    TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS queue_history (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        username       TEXT NOT NULL,
        friendly_token TEXT,
        title          TEXT,
        tier           TEXT,
        z_cost         INTEGER,
        queued_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status         TEXT DEFAULT 'queued'
    );
    CREATE INDEX IF NOT EXISTS idx_queue_history_user ON queue_history(username);

    CREATE TABLE IF NOT EXISTS saved_playlists (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,
        description  TEXT,
        is_immutable BOOLEAN NOT NULL DEFAULT 0,
        created_by   TEXT NOT NULL,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        promo_type   TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_saved_playlists_promo ON saved_playlists(promo_type);

    CREATE TABLE IF NOT EXISTS saved_playlist_items (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        playlist_id  INTEGER NOT NULL REFERENCES saved_playlists(id) ON DELETE CASCADE,
        position     INTEGER NOT NULL,
        media_type   TEXT NOT NULL,
        media_id     TEXT NOT NULL,
        title        TEXT,
        duration_sec INTEGER,
        UNIQUE(playlist_id, position)
    );

    CREATE TABLE IF NOT EXISTS playlist_schedules (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        playlist_id             INTEGER REFERENCES saved_playlists(id) ON DELETE SET NULL,
        label                   TEXT NOT NULL,
        fire_at                 TIMESTAMP NOT NULL,
        is_recurring            BOOLEAN DEFAULT 0,
        rrule                   TEXT,
        immutability_expires_at TIMESTAMP,
        pre_fire_lock_minutes   INTEGER DEFAULT 15,
        fired_at                TIMESTAMP,
        is_active               BOOLEAN DEFAULT 1,
        created_by              TEXT NOT NULL,
        created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        lock_disabled           INTEGER NOT NULL DEFAULT 0,
        fallback_playlist_id    INTEGER REFERENCES saved_playlists(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS active_schedule (
        id                 INTEGER PRIMARY KEY DEFAULT 1,
        schedule_id        INTEGER REFERENCES playlist_schedules(id),
        playlist_id        INTEGER REFERENCES saved_playlists(id),
        is_immutable       BOOLEAN NOT NULL DEFAULT 0,
        started_at         TIMESTAMP,
        estimated_end_at   TIMESTAMP,
        last_item_uid      INTEGER,
        lock_disabled      INTEGER NOT NULL DEFAULT 0,
        last_item_media_id TEXT
    );

    CREATE TABLE IF NOT EXISTS play_completions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        media_type   TEXT NOT NULL,
        media_id     TEXT NOT NULL,
        completed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_play_completions_media ON play_completions(media_id);
    CREATE INDEX IF NOT EXISTS idx_play_completions_at ON play_completions(completed_at);

    CREATE TABLE IF NOT EXISTS playlist_item_played (
        playlist_id INTEGER NOT NULL REFERENCES saved_playlists(id) ON DELETE CASCADE,
        position    INTEGER NOT NULL,
        media_type  TEXT NOT NULL,
        media_id    TEXT NOT NULL,
        played_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (playlist_id, position)
    );
    CREATE INDEX IF NOT EXISTS idx_playlist_item_played_media ON playlist_item_played(media_id);

    CREATE TABLE IF NOT EXISTS catalog_blackouts (
        friendly_token TEXT PRIMARY KEY,
        reason         TEXT,
        expires_at     TIMESTAMP NOT NULL,
        created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_catalog_blackouts_expires ON catalog_blackouts(expires_at);
    """,
]
