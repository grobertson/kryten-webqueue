"""SQLite DDL for MOTD templates, schedules, publications, media, and audit.

Shared by the monolith migration list and the partitioned catalog domain list.
The PostgreSQL equivalent lives in ``sql/002_motd_templates_media.sql``.
Timestamps are fixed-width UTC ISO strings; JSON columns are TEXT.
"""

MOTD_TEMPLATES_SQLITE = """
CREATE TABLE IF NOT EXISTS motd_templates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    kind                TEXT NOT NULL CHECK (kind IN ('master', 'fragment')),
    display_name        TEXT NOT NULL,
    description         TEXT,
    current_revision_id INTEGER,
    is_default          INTEGER NOT NULL DEFAULT 0,
    archived_at         TEXT,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS motd_templates_one_default
    ON motd_templates (is_default) WHERE is_default = 1;

CREATE TABLE IF NOT EXISTS motd_template_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES motd_templates(id),
    revision_no INTEGER NOT NULL,
    body        TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    zones       TEXT NOT NULL DEFAULT '[]',
    note        TEXT,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (template_id, revision_no)
);

CREATE TABLE IF NOT EXISTS motd_schedules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id      INTEGER NOT NULL REFERENCES motd_templates(id),
    zone             TEXT,
    label            TEXT NOT NULL,
    starts_at        TEXT NOT NULL,
    ends_at          TEXT,
    rrule            TEXT,
    duration_minutes INTEGER,
    tz               TEXT NOT NULL DEFAULT 'America/New_York',
    priority         INTEGER NOT NULL DEFAULT 0,
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS motd_publications (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    published_at       TEXT NOT NULL,
    trigger            TEXT NOT NULL,
    composition_key    TEXT NOT NULL,
    master_revision_id INTEGER NOT NULL,
    fragment_revisions TEXT NOT NULL DEFAULT '{}',
    week_key           TEXT NOT NULL,
    html_sha256        TEXT NOT NULL,
    html_chars         INTEGER NOT NULL,
    backup_path        TEXT,
    job_run_id         INTEGER,
    published_by       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS motd_publications_at ON motd_publications (published_at);

CREATE TABLE IF NOT EXISTS media_assets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT NOT NULL UNIQUE,
    filename      TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL CHECK (kind IN ('image')),
    mime          TEXT NOT NULL,
    bytes         INTEGER NOT NULL,
    width         INTEGER,
    height        INTEGER,
    animated      INTEGER NOT NULL DEFAULT 0,
    frame_count   INTEGER,
    sha256        TEXT NOT NULL,
    original_name TEXT,
    description   TEXT,
    uploaded_by   TEXT NOT NULL,
    uploaded_at   TEXT NOT NULL,
    deleted_at    TEXT,
    deleted_by    TEXT
);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    summary     TEXT,
    before      TEXT,
    after       TEXT
);
CREATE INDEX IF NOT EXISTS admin_audit_log_entity
    ON admin_audit_log (entity_type, entity_id, at);
CREATE INDEX IF NOT EXISTS admin_audit_log_at ON admin_audit_log (at);
"""
