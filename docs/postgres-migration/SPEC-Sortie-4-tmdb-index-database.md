# SPEC — Sortie 4: TMDB Index → `tmdb.*` Schema in `webqueue` Database

**Sprint**: `postgres-migration`
**PRD**: [PRD-postgres-migration.md](PRD-postgres-migration.md)
**Depends on**: Sortie 1 & Sortie 2
**Estimated**: 4–6 h

---

## 1. Overview

Move the TMDB dump index from the disposable local `tmdb_index.db` (SQLite, `mode=ro`) into the dedicated **`tmdb` schema** within the single PostgreSQL `webqueue` database on `chandra-1`. This is the only approved TMDB target; no separate `kryten_tmdb` database is created.

## 2. Scope and Non-Goals

**In scope**
- `tmdb.*` schema DDL (Postgres) mirroring TMDB index tables (`movies`, `tv`, `people`, `keywords`, `companies`, `networks`, `index_meta`).
- Rewrite the index **build job** to populate `tmdb.*` from TMDB JSONL dumps (streaming batched inserts) using SQLAlchemy 2.0 async sessions.
- Rewrite the index **read path** (`tmdb_index/index.py`) to query `tmdb.*` tables via SQLAlchemy async sessions.
- ETL the existing `tmdb_index.db` into `tmdb.*` so cutover needs no rebuild.

**Non-goals**
- No vector embeddings / recommender models (deferred to future feature sprints).

## 3. Requirements

- Read parity: enrichment lookups (`_norm`/title-similarity matching) return identical candidates.
- Build job streams dumps with bounded memory and uses transactional truncate-and-load or staging-swap so a failed rebuild never corrupts the live index.
- ETL utility provides verification comparing row counts per table between SQLite and PostgreSQL.
- The TMDB loader runs under a role limited to the `tmdb` schema and publishes the new index only after all staging validation succeeds.

## 4. Design

```sql
CREATE SCHEMA IF NOT EXISTS tmdb;

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

CREATE TABLE IF NOT EXISTS tmdb.index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

## 5. Implementation Plan

1. **Create** `sql/004_tmdb_schema.sql`.
2. **Update** `kryten_webqueue/catalog/tmdb_index/builder.py` to populate `tmdb.*` tables via async streaming.
3. **Update** `kryten_webqueue/catalog/tmdb_index/index.py` to query `tmdb.*` schema using SQLAlchemy 2.0.
4. **Create** `migrate_tmdb_index.py` CLI (`--verify`).

The CLI reads the SQLite source read-only, uses deterministic primary-key ordering for validation,
and compares canonical row hashes as well as counts. The live `tmdb` tables remain unchanged if a
load or verification step fails.

## 6. Testing Strategy

- Build index from test fixture JSONL; verify lookups return expected candidates.
- ETL test fixture `tmdb_index.db` to Postgres and verify row-count parity.
- Transactional rollback test: simulated error during dump load leaves existing data intact.

## 7. Acceptance Criteria

- [ ] `tmdb` schema created in `webqueue` database.
- [ ] TMDB index build job writes to `tmdb.*` with bounded memory.
- [ ] Enrichment lookups return parity results.
- [ ] A failed staging load cannot replace the live index.
- [ ] Full tests pass green.
  rollback one release. A later scheduled job refresh keeps it current.

## 9. Documentation

- Update `docs/SPEC_TMDB_LOCAL_INDEX.md` / `docs/PRD_TMDB_LOCAL_INDEX.md` to reflect the PG
  backing. CHANGELOG `feat:`. Note the future recommender/mining opportunities this unlocks.
