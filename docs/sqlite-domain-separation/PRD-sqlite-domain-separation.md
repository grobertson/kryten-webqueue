# PRD: SQLite Domain Partitioning & Lock Elimination

**Sprint**: `sqlite-domain-separation`  
**Status**: Ready for Implementation  
**Target Version**: `0.47.0`  
**Workflow**: [../../AGENT-WORKFLOW-GUIDE.md](../../AGENT-WORKFLOW-GUIDE.md)  
**Parent Plan**: [../DATABASE_ARCHITECTURE_PLAN.md](../DATABASE_ARCHITECTURE_PLAN.md)  

---

## 1. Executive Summary

`kryten-webqueue` currently runs all database operations against a single SQLite database file (`config.db_path`) using a single `aiosqlite.Connection`. As concurrency has grown, long-running enrichment batch jobs, yt-dlp media fetch operations, and per-line job logging hold or rapidly cycle the SQLite write lock. This causes real-time operations (`StatePoller`, `PlaylistScheduler`, user queue submissions, and device API requests) to time out with `sqlite3.OperationalError: database is locked`.

This sprint partitions the single SQLite database into **4 independent SQLite databases**:
1. `catalog.db` — Catalog metadata, categories, tags, people, studios, enrichment state, MOTD overrides.
2. `queue.db` — Live queue shadow, playback history, saved playlists, schedules, completions, blackouts.
3. `jobs.db` — Background job execution tracking, per-line job run logs, fetch queue.
4. `users.db` — One-time passwords, linked device API keys, user watchlists, feedback, title suggestions.

By isolating heavy write tasks (especially `jobs.db` and `catalog.db` batch updates) into separate database files, each database gains its own write lock and WAL stream. This removes cross-domain writer contention; bounded transactions and retries remain necessary for writers within the same domain.

---

## 2. Problem Statement

### 2.1 The Issue
- In SQLite WAL mode, readers do not block writers and writers do not block readers, but **there can only ever be ONE write transaction at a time per database file**.
- Furthermore, `aiosqlite` routes all calls on a single connection to a single underlying Python worker thread.
- When `job_run_logs` inserts hundreds of log lines during an enrichment or sync job, or when `catalog_enrich` executes batch updates, the single write lock is monopolized.
- Real-time components (`StatePoller` running every 2 seconds, `PlaylistScheduler` triggering events, `PresenceRefundMonitor`) and public HTTP requests fail when their write/read requests exceed SQLite's `busy_timeout` (5 seconds).

### 2.2 Desired Outcome
- Background jobs running in `jobs.db` can write thousands of log lines without touching `queue.db` or `catalog.db`.
- Catalog synchronization writing to `catalog.db` does not block `StatePoller` updating `queue_shadow` in `queue.db`.
- Device API keys updating `last_used_at` in `users.db` do not contend with catalog browse queries in `catalog.db`.

---

## 3. Goals and Success Metrics

### Goals

- **Cross-Domain Lock Isolation**: Real-time queue polling and user-facing browse requests do not contend with background job execution or job logging in another domain database.
- **Independent Database Files**: Clean separation into 4 SQLite database files with dedicated connection pools and WAL settings.
- **Application-Layer Query Decoupling**: Refactor cross-database queries (`recently_played` filter, `user_watchlist`) to query domain databases sequentially in memory.
- **Lossless Migration Script**: Provide an automated `split_databases.py` CLI script to migrate existing single-database SQLite files into the 4 new databases.

### Success Metrics

- Concurrently running `catalog_enrich` (with full logging) + `StatePoller` (2s interval) + continuous `/browse` requests produces zero cross-domain `database is locked` errors and records domain-scoped latency/retry metrics.
- 100% test suite pass rate with full coverage across all partitioned domains.
- Data integrity verified post-split with exact row count parity.

---

## 4. Technical Design & Domain Boundaries

### 4.1 Partition Table Mapping

| Database File | Tables Included | Primary Read/Write Access Patterns |
|---|---|---|
| **`catalog.db`** | `catalog`, `catalog_fts`, `categories`, `catalog_categories`, `tags`, `catalog_tags`, `people`, `catalog_people`, `studios`, `catalog_studios`, `item_enrichment_state`, `item_edit_log`, `sync_log`, `motd_overrides`, `_migrations` | Read-heavy from web browse/search/item detail; write-heavy during scheduled sync & enrichment. |
| **`queue.db`** | `queue_shadow`, `spend_requests`, `queue_history`, `saved_playlists`, `saved_playlist_items`, `playlist_schedules`, `active_schedule`, `play_completions`, `playlist_item_played`, `catalog_blackouts`, `_migrations` | High-frequency read/write from `StatePoller` (2s), `PlaylistScheduler`, `RacePoller`, and user queue/pay actions. |
| **`jobs.db`** | `job_runs`, `job_run_logs`, `job_schedules`, `fetch_queue`, `_migrations` | Burst writes from `JobManager`, `log_capture.py` (per-line logging), and `fetch_queue_drain`. |
| **`users.db`** | `otps`, `device_link_codes`, `device_api_keys`, `user_watchlist`, `feedback`, `title_suggestions`, `_migrations` | Low-latency reads/writes for user login, device API key auth (`last_used_at`), watchlist management, feedback. |

---

### 4.2 Configuration Schema Update

In `kryten_webqueue/config.py`:
```python
class DatabaseConfig(BaseModel):
    # Base directory for database files
    data_dir: str = "./data"
    
    # Specific database file paths (default to files inside data_dir)
    catalog_db_path: str | None = None
    queue_db_path: str | None = None
    jobs_db_path: str | None = None
    users_db_path: str | None = None

    # Explicitly selects the source layout. The default remains monolith until
    # the split utility has completed and recorded its validated handoff.
    layout: Literal["monolith", "partitioned"] = "monolith"

    # Legacy source path. Used only while layout == "monolith" or by ETL.
    db_path: str | None = "./data/webqueue.db"

    def get_catalog_path(self) -> str:
        return self.catalog_db_path or str(Path(self.data_dir) / "catalog.sqlite3")

    def get_queue_path(self) -> str:
        return self.queue_db_path or str(Path(self.data_dir) / "queue.sqlite3")

    def get_jobs_path(self) -> str:
        return self.jobs_db_path or str(Path(self.data_dir) / "jobs.sqlite3")

    def get_users_path(self) -> str:
        return self.users_db_path or str(Path(self.data_dir) / "users.sqlite3")
```

Configuration validation must reject missing monolith paths, incomplete partitioned paths, and a
legacy database that would otherwise be silently bypassed by a new `data_dir`. `db_path` is not a
runtime fallback in partitioned mode.

---

### 4.3 Connection Architecture

Refactor `Database` in `kryten_webqueue/catalog/db/`:
- `Database` becomes an aggregator containing 4 discrete connection instances:
  - `self.catalog`: `_CatalogDB(catalog_path)`
  - `self.queue`: `_QueueDB(queue_path)`
  - `self.jobs`: `_JobsDB(jobs_path)`
  - `self.users`: `_UsersDB(users_path)`
- Each sub-database maintains one dedicated `aiosqlite.Connection`, independent WAL checkpointing, and `busy_timeout=10000`. This is four independent connection dispatchers, not a connection pool.
- The aggregate `Database` delegates existing method calls to the appropriate domain connection, preserving backward compatibility across routes and services.
- Writers use short, bounded transactions; retry and latency telemetry is emitted per domain. The design does not promise to eliminate same-domain write serialization.

---

## 5. Sortie Breakdown

1. **Sortie 1: Schema Partitioning & Config Model** (`SPEC-Sortie-1-schema-partitioning-and-config.md`)
   - Define partitioned migration scripts per domain (`catalog_migrations`, `queue_migrations`, etc.).
   - Update `Config` and `config.example.json`.
2. **Sortie 2: Multi-Database Connection Layer** (`SPEC-Sortie-2-multi-db-connection-layer.md`)
   - Build domain connection classes and aggregate `Database` interface.
   - Configure WAL, PRAGMAs, and lifespan initialization.
3. **Sortie 3: Cross-Domain Query Decoupling** (`SPEC-Sortie-3-cross-domain-query-decoupling.md`)
   - Refactor `recently_played` filter, `user_watchlist`, and admin job logs to avoid cross-file SQL joins.
4. **Sortie 4: ETL Split Script & Concurrency Validation** (`SPEC-Sortie-4-etl-split-script-and-validation.md`)
   - Create `split_databases.py` migration utility.
   - Add concurrency stress tests with synthetic lock contention simulation.

## 6. Migration Safety Contract

The source service and all schedulers are stopped before the split. The utility captures a
checkpoint-safe SQLite backup, creates all targets in a staging directory, validates rows and
foreign keys, and activates the partitioned layout only after success. It never copies the legacy
`_migrations` rows, renames the source database, or deletes SQLite WAL/SHM files automatically.
Archiving the verified source is an explicit operator action.
