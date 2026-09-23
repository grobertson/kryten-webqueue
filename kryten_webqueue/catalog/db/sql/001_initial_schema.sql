-- 001_initial_schema.sql: Initial PostgreSQL schema for webqueue database
-- Defines schemas: catalog, queue, jobs, users, tmdb

CREATE SCHEMA IF NOT EXISTS catalog;
CREATE SCHEMA IF NOT EXISTS queue;
CREATE SCHEMA IF NOT EXISTS jobs;
CREATE SCHEMA IF NOT EXISTS users;
CREATE SCHEMA IF NOT EXISTS tmdb;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Schema migration tracking
CREATE TABLE IF NOT EXISTS public.schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    description TEXT
);

-- ============================================================================
-- CATALOG SCHEMA
-- ============================================================================

CREATE TABLE IF NOT EXISTS catalog.catalog (
    friendly_token         TEXT PRIMARY KEY,
    title                  TEXT NOT NULL,
    description            TEXT,
    duration_sec           INTEGER,
    manifest_url           TEXT NOT NULL,
    thumbnail_url          TEXT,
    cover_art_path         TEXT,
    cover_art_source       TEXT,
    imdb_tt                TEXT UNIQUE,
    override_artwork_tt_id TEXT,
    added_at               TIMESTAMPTZ DEFAULT clock_timestamp(),
    updated_at             TIMESTAMPTZ DEFAULT clock_timestamp(),
    synced_at              TIMESTAMPTZ DEFAULT clock_timestamp(),
    search_vector          tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(description, '')), 'B')
    ) STORED
);

CREATE INDEX IF NOT EXISTS idx_catalog_search_vector ON catalog.catalog USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS idx_catalog_title_trgm ON catalog.catalog USING GIN (title gin_trgm_ops);
CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_imdb_tt ON catalog.catalog (imdb_tt) WHERE imdb_tt IS NOT NULL;

CREATE TABLE IF NOT EXISTS catalog.categories (
    id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS catalog.catalog_categories (
    friendly_token TEXT NOT NULL REFERENCES catalog.catalog(friendly_token) ON DELETE CASCADE,
    category_id    BIGINT NOT NULL REFERENCES catalog.categories(id) ON DELETE CASCADE,
    PRIMARY KEY (friendly_token, category_id)
);

CREATE TABLE IF NOT EXISTS catalog.tags (
    id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS catalog.catalog_tags (
    friendly_token TEXT NOT NULL REFERENCES catalog.catalog(friendly_token) ON DELETE CASCADE,
    tag_id         BIGINT NOT NULL REFERENCES catalog.tags(id) ON DELETE CASCADE,
    PRIMARY KEY (friendly_token, tag_id)
);

CREATE TABLE IF NOT EXISTS catalog.sync_log (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL,
    ended_at      TIMESTAMPTZ,
    items_seen    INTEGER,
    items_new     INTEGER,
    items_updated INTEGER,
    errors        INTEGER,
    status        TEXT
);

CREATE TABLE IF NOT EXISTS catalog.people (
    id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_people_name ON catalog.people(name);

CREATE TABLE IF NOT EXISTS catalog.catalog_people (
    friendly_token TEXT NOT NULL,
    person_id      BIGINT NOT NULL REFERENCES catalog.people(id) ON DELETE CASCADE,
    role           TEXT NOT NULL,
    position       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (friendly_token, person_id, role)
);
CREATE INDEX IF NOT EXISTS idx_catalog_people_token  ON catalog.catalog_people(friendly_token);
CREATE INDEX IF NOT EXISTS idx_catalog_people_person ON catalog.catalog_people(person_id);

CREATE TABLE IF NOT EXISTS catalog.studios (
    id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS catalog.catalog_studios (
    friendly_token TEXT NOT NULL,
    studio_id      BIGINT NOT NULL REFERENCES catalog.studios(id) ON DELETE CASCADE,
    PRIMARY KEY (friendly_token, studio_id)
);
CREATE INDEX IF NOT EXISTS idx_catalog_studios_token ON catalog.catalog_studios(friendly_token);

CREATE TABLE IF NOT EXISTS catalog.item_enrichment_state (
    friendly_token     TEXT PRIMARY KEY,
    content_type       TEXT,
    hosted_show        TEXT,
    lookup_title       TEXT,
    lookup_year        TEXT,
    tv_show            TEXT,
    tv_season          INTEGER,
    tv_episode_num     INTEGER,
    description_score  INTEGER,
    tmdb_id            TEXT,
    imdb_id            TEXT,
    meta_json          TEXT,
    last_classify_at   TEXT,
    last_title_at      TEXT,
    last_meta_at       TEXT,
    last_art_at        TEXT,
    last_tags_at       TEXT,
    last_categories_at TEXT,
    last_identify_at   TEXT,
    identify_source    TEXT,
    identify_reason    TEXT
);

CREATE TABLE IF NOT EXISTS catalog.item_edit_log (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    friendly_token TEXT NOT NULL,
    username       TEXT NOT NULL,
    field_name     TEXT NOT NULL,
    old_value      TEXT,
    new_value      TEXT,
    edited_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_item_edit_log_token ON catalog.item_edit_log(friendly_token, edited_at DESC);

CREATE TABLE IF NOT EXISTS catalog.motd_overrides (
    week_key   TEXT NOT NULL,
    slot_key   TEXT NOT NULL,
    title      TEXT,
    poster_url TEXT,
    href       TEXT,
    created_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (week_key, slot_key)
);

-- ============================================================================
-- QUEUE SCHEMA
-- ============================================================================

CREATE TABLE IF NOT EXISTS queue.queue_shadow (
    uid                BIGINT PRIMARY KEY,
    position           INTEGER NOT NULL,
    title              TEXT,
    friendly_token     TEXT,
    media_type         TEXT NOT NULL,
    media_id           TEXT NOT NULL,
    duration_sec       INTEGER,
    is_pay             BOOLEAN NOT NULL DEFAULT false,
    paid_by            TEXT,
    tier               TEXT,
    z_cost             INTEGER,
    schedule_id        BIGINT,
    estimated_start_at TIMESTAMPTZ,
    added_at           TIMESTAMPTZ DEFAULT clock_timestamp(),
    is_promo           BOOLEAN NOT NULL DEFAULT false,
    promo_type         TEXT,
    lead_in_for_uid    BIGINT
);

CREATE TABLE IF NOT EXISTS queue.spend_requests (
    request_id     TEXT PRIMARY KEY,
    username       TEXT NOT NULL,
    uid            BIGINT,
    friendly_token TEXT,
    tier           TEXT,
    z_cost         INTEGER,
    created_at     TIMESTAMPTZ DEFAULT clock_timestamp(),
    refunded       BOOLEAN DEFAULT false,
    refunded_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS queue.queue_history (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username       TEXT NOT NULL,
    friendly_token TEXT,
    title          TEXT,
    tier           TEXT,
    z_cost         INTEGER,
    queued_at      TIMESTAMPTZ DEFAULT clock_timestamp(),
    status         TEXT DEFAULT 'queued'
);
CREATE INDEX IF NOT EXISTS idx_queue_history_user ON queue.queue_history(username);

CREATE TABLE IF NOT EXISTS queue.saved_playlists (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT,
    is_immutable BOOLEAN NOT NULL DEFAULT false,
    created_by   TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT clock_timestamp(),
    updated_at   TIMESTAMPTZ DEFAULT clock_timestamp(),
    promo_type   TEXT
);
CREATE INDEX IF NOT EXISTS idx_saved_playlists_promo ON queue.saved_playlists(promo_type);

CREATE TABLE IF NOT EXISTS queue.saved_playlist_items (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    playlist_id  BIGINT NOT NULL REFERENCES queue.saved_playlists(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    media_type   TEXT NOT NULL,
    media_id     TEXT NOT NULL,
    title        TEXT,
    duration_sec INTEGER,
    UNIQUE(playlist_id, position)
);

CREATE TABLE IF NOT EXISTS queue.playlist_schedules (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    playlist_id             BIGINT REFERENCES queue.saved_playlists(id) ON DELETE SET NULL,
    label                   TEXT NOT NULL,
    fire_at                 TIMESTAMPTZ NOT NULL,
    is_recurring            BOOLEAN DEFAULT false,
    rrule                   TEXT,
    immutability_expires_at TIMESTAMPTZ,
    pre_fire_lock_minutes   INTEGER DEFAULT 15,
    fired_at                TIMESTAMPTZ,
    is_active               BOOLEAN DEFAULT true,
    created_by              TEXT NOT NULL,
    created_at              TIMESTAMPTZ DEFAULT clock_timestamp(),
    lock_disabled           INTEGER NOT NULL DEFAULT 0,
    fallback_playlist_id    BIGINT REFERENCES queue.saved_playlists(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS queue.active_schedule (
    id                 INTEGER PRIMARY KEY DEFAULT 1,
    schedule_id        BIGINT REFERENCES queue.playlist_schedules(id),
    playlist_id        BIGINT REFERENCES queue.saved_playlists(id),
    is_immutable       BOOLEAN NOT NULL DEFAULT false,
    started_at         TIMESTAMPTZ,
    estimated_end_at   TIMESTAMPTZ,
    last_item_uid      BIGINT,
    lock_disabled      INTEGER NOT NULL DEFAULT 0,
    last_item_media_id TEXT
);

CREATE TABLE IF NOT EXISTS queue.play_completions (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    media_type   TEXT NOT NULL,
    media_id     TEXT NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_play_completions_media ON queue.play_completions(media_id);
CREATE INDEX IF NOT EXISTS idx_play_completions_at ON queue.play_completions(completed_at);

CREATE TABLE IF NOT EXISTS queue.playlist_item_played (
    playlist_id BIGINT NOT NULL REFERENCES queue.saved_playlists(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    media_type  TEXT NOT NULL,
    media_id    TEXT NOT NULL,
    played_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (playlist_id, position)
);
CREATE INDEX IF NOT EXISTS idx_playlist_item_played_media ON queue.playlist_item_played(media_id);

CREATE TABLE IF NOT EXISTS queue.catalog_blackouts (
    friendly_token TEXT PRIMARY KEY,
    reason         TEXT,
    expires_at     TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_catalog_blackouts_expires ON queue.catalog_blackouts(expires_at);

-- ============================================================================
-- JOBS SCHEMA
-- ============================================================================

CREATE TABLE IF NOT EXISTS jobs.job_runs (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_name     TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ,
    status       TEXT NOT NULL DEFAULT 'running',
    detail       TEXT,
    triggered_by TEXT,
    params       TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_name ON jobs.job_runs(job_name, started_at);

CREATE TABLE IF NOT EXISTS jobs.job_run_logs (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id    BIGINT NOT NULL REFERENCES jobs.job_runs(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,
    logged_at TIMESTAMPTZ NOT NULL,
    level     TEXT,
    logger    TEXT,
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_run_logs_run ON jobs.job_run_logs(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_job_run_logs_logged ON jobs.job_run_logs(logged_at);

CREATE TABLE IF NOT EXISTS jobs.job_schedules (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_name         TEXT NOT NULL UNIQUE,
    label            TEXT,
    cron_expression  TEXT NOT NULL,
    params_json      TEXT,
    is_active        BOOLEAN NOT NULL DEFAULT true,
    run_next_job     TEXT,
    created_by       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS jobs.fetch_queue (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url             TEXT NOT NULL,
    quality         TEXT NOT NULL DEFAULT 'medium',
    max_videos      INTEGER NOT NULL DEFAULT 50,
    add_to_playlist BIGINT,
    status          TEXT NOT NULL DEFAULT 'pending',
    added_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    added_by        TEXT,
    result_json     TEXT,
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fetch_queue_status ON jobs.fetch_queue(status, added_at);

-- ============================================================================
-- USERS SCHEMA
-- ============================================================================

CREATE TABLE IF NOT EXISTS users.otps (
    username   TEXT NOT NULL,
    code       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    used       BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_otps_username ON users.otps(username);

CREATE TABLE IF NOT EXISTS users.device_link_codes (
    code        TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    device_name TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_device_link_codes_user ON users.device_link_codes(username);
CREATE INDEX IF NOT EXISTS idx_device_link_codes_expires ON users.device_link_codes(expires_at);

CREATE TABLE IF NOT EXISTS users.device_api_keys (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username     TEXT NOT NULL,
    device_name  TEXT NOT NULL,
    key_prefix   TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_used_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_device_api_keys_user ON users.device_api_keys(username);
CREATE INDEX IF NOT EXISTS idx_device_api_keys_hash ON users.device_api_keys(key_hash);

CREATE TABLE IF NOT EXISTS users.user_watchlist (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username       TEXT NOT NULL,
    friendly_token TEXT NOT NULL,
    added_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (username, friendly_token)
);
CREATE INDEX IF NOT EXISTS idx_user_watchlist_username ON users.user_watchlist(username);

CREATE TABLE IF NOT EXISTS users.feedback (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username   TEXT NOT NULL,
    body       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'new',
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_feedback_status ON users.feedback(status, created_at);

CREATE TABLE IF NOT EXISTS users.title_suggestions (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
    created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_title_suggestions_status ON users.title_suggestions(status, created_at);

-- ============================================================================
-- TMDB SCHEMA
-- ============================================================================

CREATE TABLE IF NOT EXISTS tmdb.movies (
    id             INTEGER PRIMARY KEY,
    title          TEXT NOT NULL,
    original_title TEXT NOT NULL,
    norm_title     TEXT NOT NULL,
    norm_original  TEXT NOT NULL,
    popularity     DOUBLE PRECISION NOT NULL,
    adult          BOOLEAN NOT NULL,
    video          BOOLEAN NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tmdb_movies_norm_title ON tmdb.movies(norm_title);
CREATE INDEX IF NOT EXISTS idx_tmdb_movies_norm_orig  ON tmdb.movies(norm_original);

CREATE TABLE IF NOT EXISTS tmdb.tv (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    original_name  TEXT NOT NULL,
    norm_name      TEXT NOT NULL,
    norm_original  TEXT NOT NULL,
    popularity     DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tmdb_tv_norm_name ON tmdb.tv(norm_name);
CREATE INDEX IF NOT EXISTS idx_tmdb_tv_norm_orig ON tmdb.tv(norm_original);

CREATE TABLE IF NOT EXISTS tmdb.people (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    popularity DOUBLE PRECISION NOT NULL,
    adult      BOOLEAN NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tmdb_people_name ON tmdb.people(name);

CREATE TABLE IF NOT EXISTS tmdb.keywords (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tmdb.companies (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tmdb.networks (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tmdb.index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
