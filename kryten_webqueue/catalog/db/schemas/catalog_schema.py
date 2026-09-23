"""Catalog domain schema and migrations for catalog.sqlite3."""

CATALOG_MIGRATIONS: list[str] = [
    # v1: Migration tracking table
    """
    CREATE TABLE IF NOT EXISTS _migrations (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # v2: Catalog baseline schema
    """
    CREATE TABLE IF NOT EXISTS catalog (
        friendly_token         TEXT PRIMARY KEY,
        title                  TEXT NOT NULL,
        description            TEXT,
        duration_sec           INTEGER,
        manifest_url           TEXT NOT NULL,
        thumbnail_url          TEXT,
        cover_art_path         TEXT,
        cover_art_source       TEXT,
        added_at               TIMESTAMP,
        updated_at             TIMESTAMP,
        synced_at              TIMESTAMP,
        imdb_tt                TEXT,
        override_artwork_tt_id TEXT
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_imdb_tt
        ON catalog(imdb_tt) WHERE imdb_tt IS NOT NULL;

    CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts USING fts5(
        friendly_token UNINDEXED,
        title,
        description,
        content='catalog',
        content_rowid='rowid'
    );

    CREATE TABLE IF NOT EXISTS categories (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        slug TEXT NOT NULL UNIQUE
    );

    CREATE TABLE IF NOT EXISTS catalog_categories (
        friendly_token TEXT REFERENCES catalog(friendly_token) ON DELETE CASCADE,
        category_id    INTEGER REFERENCES categories(id) ON DELETE CASCADE,
        PRIMARY KEY (friendly_token, category_id)
    );

    CREATE TABLE IF NOT EXISTS tags (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    );

    CREATE TABLE IF NOT EXISTS catalog_tags (
        friendly_token TEXT REFERENCES catalog(friendly_token) ON DELETE CASCADE,
        tag_id         INTEGER REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (friendly_token, tag_id)
    );

    CREATE TABLE IF NOT EXISTS sync_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at    TIMESTAMP NOT NULL,
        ended_at      TIMESTAMP,
        items_seen    INTEGER,
        items_new     INTEGER,
        items_updated INTEGER,
        errors        INTEGER,
        status        TEXT
    );

    CREATE TABLE IF NOT EXISTS people (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    );
    CREATE INDEX IF NOT EXISTS idx_people_name ON people(name);

    CREATE TABLE IF NOT EXISTS catalog_people (
        friendly_token TEXT NOT NULL REFERENCES catalog(friendly_token) ON DELETE CASCADE,
        person_id      INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        role           TEXT NOT NULL,
        position       INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (friendly_token, person_id, role)
    );
    CREATE INDEX IF NOT EXISTS idx_catalog_people_token  ON catalog_people(friendly_token);
    CREATE INDEX IF NOT EXISTS idx_catalog_people_person ON catalog_people(person_id);

    CREATE TABLE IF NOT EXISTS studios (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    );

    CREATE TABLE IF NOT EXISTS catalog_studios (
        friendly_token TEXT NOT NULL REFERENCES catalog(friendly_token) ON DELETE CASCADE,
        studio_id      INTEGER NOT NULL REFERENCES studios(id) ON DELETE CASCADE,
        PRIMARY KEY (friendly_token, studio_id)
    );
    CREATE INDEX IF NOT EXISTS idx_catalog_studios_token ON catalog_studios(friendly_token);

    CREATE TABLE IF NOT EXISTS item_enrichment_state (
        friendly_token     TEXT PRIMARY KEY REFERENCES catalog(friendly_token) ON DELETE CASCADE,
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

    CREATE TABLE IF NOT EXISTS item_edit_log (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        friendly_token TEXT NOT NULL,
        username       TEXT NOT NULL,
        field_name     TEXT NOT NULL,
        old_value      TEXT,
        new_value      TEXT,
        edited_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_item_edit_log_token ON item_edit_log(friendly_token, edited_at DESC);

    CREATE TABLE IF NOT EXISTS motd_overrides (
        week_key   TEXT NOT NULL,
        slot_key   TEXT NOT NULL,
        title      TEXT,
        poster_url TEXT,
        href       TEXT,
        created_by TEXT,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (week_key, slot_key)
    );
    """,
]
