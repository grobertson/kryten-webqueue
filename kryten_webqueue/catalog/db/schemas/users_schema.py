"""Users domain schema and migrations for users.sqlite3."""

USERS_MIGRATIONS: list[str] = [
    # v1: Migration tracking table
    """
    CREATE TABLE IF NOT EXISTS _migrations (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # v2: Users baseline schema
    """
    CREATE TABLE IF NOT EXISTS otps (
        username   TEXT NOT NULL,
        code       TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP NOT NULL,
        used       BOOLEAN NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_otps_username ON otps(username);

    CREATE TABLE IF NOT EXISTS device_link_codes (
        code        TEXT PRIMARY KEY,
        username    TEXT NOT NULL,
        device_name TEXT NOT NULL,
        created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at  TIMESTAMP NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_device_link_codes_user ON device_link_codes(username);
    CREATE INDEX IF NOT EXISTS idx_device_link_codes_expires ON device_link_codes(expires_at);

    CREATE TABLE IF NOT EXISTS device_api_keys (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        username     TEXT NOT NULL,
        device_name  TEXT NOT NULL,
        key_prefix   TEXT NOT NULL,
        key_hash     TEXT NOT NULL UNIQUE,
        created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_used_at TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_device_api_keys_user ON device_api_keys(username);
    CREATE INDEX IF NOT EXISTS idx_device_api_keys_hash ON device_api_keys(key_hash);

    CREATE TABLE IF NOT EXISTS user_watchlist (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        username       TEXT NOT NULL,
        friendly_token TEXT NOT NULL,
        added_at       TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (username, friendly_token)
    );
    CREATE INDEX IF NOT EXISTS idx_user_watchlist_username ON user_watchlist(username);

    CREATE TABLE IF NOT EXISTS feedback (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        username   TEXT NOT NULL,
        body       TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'new',
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status, created_at);

    CREATE TABLE IF NOT EXISTS title_suggestions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        username        TEXT NOT NULL,
        query           TEXT NOT NULL,
        resolved_title  TEXT,
        resolved_year   TEXT,
        resolved_source TEXT,
        resolved_id     TEXT,
        poster_url      TEXT,
        resolution      TEXT NOT NULL DEFAULT 'unresolved',
        catalog_token   TEXT,
        status          TEXT NOT NULL DEFAULT 'new',
        created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_title_suggestions_status ON title_suggestions(status, created_at);
    """,
]
