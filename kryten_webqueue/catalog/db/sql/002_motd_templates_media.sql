-- 002_motd_templates_media.sql: MOTD templates, schedules, publications,
-- media library, and admin audit log (kryten-webqueue 0.51.0).
-- Apply out-of-band like 001; every statement is idempotent.
-- JSON columns are TEXT to keep one code path with the SQLite layouts.

CREATE TABLE IF NOT EXISTS catalog.motd_templates (
    id                  BIGSERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    kind                TEXT NOT NULL CHECK (kind IN ('master', 'fragment')),
    display_name        TEXT NOT NULL,
    description         TEXT,
    current_revision_id BIGINT,
    is_default          BOOLEAN NOT NULL DEFAULT FALSE,
    archived_at         TIMESTAMPTZ,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS motd_templates_one_default
    ON catalog.motd_templates (is_default) WHERE is_default;

CREATE TABLE IF NOT EXISTS catalog.motd_template_revisions (
    id          BIGSERIAL PRIMARY KEY,
    template_id BIGINT NOT NULL REFERENCES catalog.motd_templates(id),
    revision_no INTEGER NOT NULL,
    body        TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    zones       TEXT NOT NULL DEFAULT '[]',
    note        TEXT,
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (template_id, revision_no)
);

CREATE TABLE IF NOT EXISTS catalog.motd_schedules (
    id               BIGSERIAL PRIMARY KEY,
    template_id      BIGINT NOT NULL REFERENCES catalog.motd_templates(id),
    zone             TEXT,
    label            TEXT NOT NULL,
    starts_at        TIMESTAMPTZ NOT NULL,
    ends_at          TIMESTAMPTZ,
    rrule            TEXT,
    duration_minutes INTEGER,
    tz               TEXT NOT NULL DEFAULT 'America/New_York',
    priority         INTEGER NOT NULL DEFAULT 0,
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_by       TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog.motd_publications (
    id                 BIGSERIAL PRIMARY KEY,
    published_at       TIMESTAMPTZ NOT NULL,
    trigger            TEXT NOT NULL,
    composition_key    TEXT NOT NULL,
    master_revision_id BIGINT NOT NULL,
    fragment_revisions TEXT NOT NULL DEFAULT '{}',
    week_key           TEXT NOT NULL,
    html_sha256        TEXT NOT NULL,
    html_chars         INTEGER NOT NULL,
    backup_path        TEXT,
    job_run_id         BIGINT,
    published_by       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS motd_publications_at
    ON catalog.motd_publications (published_at);

CREATE TABLE IF NOT EXISTS catalog.media_assets (
    id            BIGSERIAL PRIMARY KEY,
    slug          TEXT NOT NULL UNIQUE,
    filename      TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL CHECK (kind IN ('image')),
    mime          TEXT NOT NULL,
    bytes         BIGINT NOT NULL,
    width         INTEGER,
    height        INTEGER,
    animated      BOOLEAN NOT NULL DEFAULT FALSE,
    frame_count   INTEGER,
    sha256        TEXT NOT NULL,
    original_name TEXT,
    description   TEXT,
    uploaded_by   TEXT NOT NULL,
    uploaded_at   TIMESTAMPTZ NOT NULL,
    deleted_at    TIMESTAMPTZ,
    deleted_by    TEXT
);

CREATE TABLE IF NOT EXISTS catalog.admin_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    summary     TEXT,
    before      TEXT,
    after       TEXT
);
CREATE INDEX IF NOT EXISTS admin_audit_log_entity
    ON catalog.admin_audit_log (entity_type, entity_id, at);
CREATE INDEX IF NOT EXISTS admin_audit_log_at ON catalog.admin_audit_log (at);

INSERT INTO public.schema_version (version, description)
VALUES (2, 'MOTD templates, schedules, publications, media library, admin audit log')
ON CONFLICT (version) DO NOTHING;
