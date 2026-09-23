# SPEC — Sortie 1: Schema Partitioning & Config Model

**Sprint**: `sqlite-domain-separation`  
**PRD**: [PRD-sqlite-domain-separation.md](PRD-sqlite-domain-separation.md)  
**Depends on**: None  
**Estimated Effort**: 3 hours  

---

## 1. Overview

Define the schema definitions and migration sequences for each of the 4 partitioned SQLite databases (`catalog.db`, `queue.db`, `jobs.db`, `users.db`), and update the application configuration model to support individual database paths with clean directory-level defaults.

---

## 2. Scope & Non-Goals

### In Scope

- Categorize existing `MIGRATIONS` from `_connection.py` into 4 distinct migration sets: `CATALOG_MIGRATIONS`, `QUEUE_MIGRATIONS`, `JOBS_MIGRATIONS`, `USERS_MIGRATIONS`.
- Define a clean baseline for each domain rather than replaying the monolithic migration history and its historic data mutations.
- Update `kryten_webqueue/config.py` with an explicit `monolith` or `partitioned` layout, `data_dir`, and domain-specific path resolvers (`catalog_db_path`, `queue_db_path`, `jobs_db_path`, `users_db_path`).
- Update `config.example.json` and system default paths.

### Non-Goals

- Modifying query logic or connection wrappers (Sortie 2 & 3).
- Data migration ETL script (Sortie 4).

### Migration Baselines

The new domain migration histories start at a partitioned-schema baseline. Do not copy legacy
`_migrations` rows and do not rerun old one-time SQLite data migrations such as cover-art clears,
hide-state cleanup, or historical backfills. Sortie 4 copies application data only after the
target schema baseline has been applied.

### Layout Validation

`db_path` remains the monolith source path and is never a silent fallback in partitioned mode.
Validation rejects incomplete domain paths, missing monolith paths, and a legacy database that
would be bypassed by the new default `data_dir`. The production default remains `monolith` until
a validated split explicitly switches the configuration.

---

## 3. Detailed Design

### 3.1 Migration Sets

#### `CATALOG_MIGRATIONS`
- `_migrations`
- `catalog` (with `imdb_tt`, `override_artwork_tt_id`)
- `catalog_fts` (FTS5 virtual table on `catalog`)
- `categories`, `catalog_categories`
- `tags`, `catalog_tags`
- `people`, `catalog_people`
- `studios`, `catalog_studios`
- `item_enrichment_state`
- `item_edit_log`
- `sync_log`
- `motd_overrides`

#### `QUEUE_MIGRATIONS`
- `_migrations`
- `queue_shadow`
- `spend_requests`
- `queue_history`
- `saved_playlists`
- `saved_playlist_items`
- `playlist_schedules`
- `active_schedule`
- `play_completions`
- `playlist_item_played`
- `catalog_blackouts`

#### `JOBS_MIGRATIONS`
- `_migrations`
- `job_runs`
- `job_run_logs`
- `job_schedules`
- `fetch_queue`

#### `USERS_MIGRATIONS`
- `_migrations`
- `otps`
- `device_link_codes`
- `device_api_keys`
- `user_watchlist`
- `feedback`
- `title_suggestions`

---

## 4. Implementation Plan

1. **Create** `kryten_webqueue/catalog/db/schemas/`:
   - `catalog_schema.py`
   - `queue_schema.py`
   - `jobs_schema.py`
   - `users_schema.py`
2. **Update** `kryten_webqueue/config.py`:
   - Add explicit layout validation and database path resolution methods.
3. **Update** `config.example.json`:
   - Document monolith-to-partitioned activation, `data_dir`, and optional explicit domain paths.

---

## 5. Acceptance Criteria

- [ ] All 35+ existing tables cleanly mapped into the 4 domain schema files.
- [ ] Config correctly resolves default paths (`./data/catalog.sqlite3`, etc.) when `data_dir` is specified.
- [ ] Unit tests reject ambiguous layouts and prevent an existing monolith from being silently bypassed.
- [ ] Unit tests pass for config validation and schema definition structure.
