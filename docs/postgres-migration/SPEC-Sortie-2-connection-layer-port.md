# SPEC — Sortie 2: SQLAlchemy 2.0 Repository Layer & PostgreSQL Schemas

**Sprint**: `postgres-migration`
**PRD**: [PRD-postgres-migration.md](PRD-postgres-migration.md)
**Depends on**: Sortie 1
**Estimated**: 6 h

---

## 1. Overview

Port `Database` and all domain repository mixins (`_catalog`, `_queue`, `_playlists`, `_enrichment`, `_people`, `_watchlist`, `_devices`, `_blackouts`, `_feedback`, `_fetch_queue`) to **SQLAlchemy 2.0 (`asyncpg`)**, organizing tables across logical PostgreSQL schemas (`catalog`, `queue`, `jobs`, `users`, `tmdb`) inside the single `webqueue` database.

## 2. Scope and Non-Goals

**In scope**
- Implement SQLAlchemy 2.0 async database session manager.
- Map domain tables to schemas (`catalog.*`, `queue.*`, `jobs.*`, `users.*`).
- Translate queries to SQLAlchemy Core `text()` / ORM expressions with `:param` binding.
- Schema DDL files in `sql/` tracked via a `schema_version` table.
- Route-level exception handling: replace `sqlite3.IntegrityError`/`DatabaseError` with `sqlalchemy.exc.IntegrityError` / `sqlalchemy.exc.DBAPIError`.

**Non-goals**
- `tsvector` and `pg_trgm` full search engine integration (Sortie 3).
- TMDB dump ETL into `tmdb.*` (Sortie 4).
- Data migration ETL script (Sortie 5).

## 3. Requirements

- All database methods preserve their public signatures and return dictionaries or dataclasses matching existing contracts.
- 100% async/await; fully non-blocking I/O via SQLAlchemy 2.0 and `asyncpg`.
- Repository SQL uses SQLAlchemy Core or `text()` with named binds. Raw `asyncpg` calls and mechanical `?` to `$n` conversion are not permitted.
- Request and job services define transaction boundaries with `AsyncSession.begin()`; repository helpers do not commit independently.
- PostgreSQL dialect translations: `datetime('now')` $\to$ `now()`, `INSERT OR REPLACE` $\to$ `ON CONFLICT (...) DO UPDATE`, `AUTOINCREMENT` $\to$ `BIGINT GENERATED ALWAYS AS IDENTITY`.

## 4. Design & Schema Organization

```sql
-- sql/001_initial_schema.sql
CREATE SCHEMA IF NOT EXISTS catalog;
CREATE SCHEMA IF NOT EXISTS queue;
CREATE SCHEMA IF NOT EXISTS jobs;
CREATE SCHEMA IF NOT EXISTS users;
CREATE SCHEMA IF NOT EXISTS tmdb;

-- Catalog Schema
CREATE TABLE IF NOT EXISTS catalog.catalog (
    friendly_token   TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    description      TEXT,
    duration_sec     INTEGER,
    manifest_url     TEXT NOT NULL,
    thumbnail_url    TEXT,
    cover_art_path   TEXT,
    cover_art_source TEXT,
    imdb_tt          TEXT UNIQUE,
    override_artwork_tt_id TEXT,
    added_at         TIMESTAMPTZ DEFAULT clock_timestamp(),
    updated_at       TIMESTAMPTZ DEFAULT clock_timestamp(),
    synced_at        TIMESTAMPTZ DEFAULT clock_timestamp()
);

-- Queue Schema
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
    is_promo           BOOLEAN NOT NULL DEFAULT false,
    promo_type         TEXT,
    lead_in_for_uid    BIGINT,
    estimated_start_at TIMESTAMPTZ,
    added_at           TIMESTAMPTZ DEFAULT clock_timestamp()
);

-- Jobs Schema
CREATE TABLE IF NOT EXISTS jobs.job_runs (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_name     TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ,
    status       TEXT NOT NULL DEFAULT 'running',
    params       TEXT,
    detail       TEXT,
    triggered_by TEXT
);

CREATE TABLE IF NOT EXISTS jobs.job_run_logs (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id    BIGINT NOT NULL REFERENCES jobs.job_runs(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,
    logged_at TIMESTAMPTZ NOT NULL,
    level     TEXT,
    logger    TEXT,
    message   TEXT NOT NULL
);

-- Users Schema
CREATE TABLE IF NOT EXISTS users.otps (
    username   TEXT NOT NULL,
    code       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    used       BOOLEAN NOT NULL DEFAULT false
);
```

## 5. Implementation Plan

1. **Create** `kryten_webqueue/catalog/db/session.py` with SQLAlchemy session generator and context manager.
2. **Translate** SQL queries in domain mixins to SQLAlchemy parameterized statements.
3. **Update** exception handling in `routes/admin_catalog.py` and `routes/pages.py`.
4. **Implement** `sql/` DDL migration runner.

The DDL runner uses a dedicated migrator role and a PostgreSQL advisory lock. The initial DDL is a
clean PostgreSQL baseline covering every current table, foreign key, uniqueness constraint, index,
and cascade behavior; it never replays SQLite's historic data migrations or `_reconcile_schema`.
The application runtime role cannot apply DDL.

## 6. Acceptance Criteria

- [ ] Repository layer runs on SQLAlchemy 2.0 with `asyncpg`.
- [ ] Tables properly isolated across schemas (`catalog`, `queue`, `jobs`, `users`).
- [ ] Exception handlers catch SQLAlchemy exceptions correctly.
- [ ] Transaction tests prove a failed multi-repository local operation rolls back as one unit.
- [ ] Migration tests prove only one migrator can apply DDL and that all schema-qualified constraints and indexes exist.
- [ ] Unit and integration tests pass with PostgreSQL test backend.

- CHANGELOG `refactor:`/`feat:`; note the retired reconcile hack and the migration model change.
