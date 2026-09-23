# Database Architecture & Evolution Plan: Kryten-WebQueue

**Status**: Approved Architecture Plan  
**Target System**: `kryten-webqueue`  
**Author**: Senior Microservice Architect (Kryten)  
**Last Updated**: September 2026  

---

## Executive Summary

`kryten-webqueue` has evolved from a lightweight catalog viewer into a multi-faceted web application hosting the CyTube channel catalog, real-time queue shadows, automated playlist schedulers, background media ingestion, TMDB enrichment pipelines, user watchlists, device API authentication, and per-line job logging.

Under heavy concurrent operation, the application suffers from SQLite database write-lock contention (`sqlite3.OperationalError: database is locked`) because all domains share a single SQLite database file and a single async worker connection.

This document outlines the **two-part architectural roadmap**:

1. **Part 1 (Immediate / Stopgap)**: **SQLite Domain Partitioning** — Split the monolithic SQLite database into 4 logical database files (`catalog.db`, `queue.db`, `jobs.db`, `users.db`) to isolate high-throughput writers (job logs, enrichment syncs) from real-time pollers and public browse traffic. This removes cross-domain contention; it does not remove writer serialization within a single domain file.
2. **Part 2 (Strategic / Chandra-1 Migration)**: **PostgreSQL on Chandra-1 via Podman** — Migrate all partitioned domains into one PostgreSQL database (`webqueue`) on `chandra-1` utilizing schemas (`catalog`, `queue`, `jobs`, `users`, `tmdb`), powered by **SQLAlchemy 2.0 (`asyncpg`)**, PostgreSQL Full-Text Search (`tsvector` + GIN) + `pg_trgm` fuzzy matching, and automated 30-day job log pruning (strictly exempting all financial, chat, and purchase records).

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                 TWO-PART ROADMAP                                       │
├────────────────────────────────────────┬───────────────────────────────────────────────┤
│ PART 1: SQLite Domain Partitioning     │ PART 2: Chandra-1 PostgreSQL & Podman Pod     │
├────────────────────────────────────────┼───────────────────────────────────────────────┤
│ • Split single DB into 4 files:        │ • Single Postgres database `webqueue` on      │
│   - catalog.db (MediaCMS mirror, art)  │   chandra-1 with logical schemas.             │
│   - queue.db (shadow, schedules, play) │ • SQLAlchemy 2.0 (asyncpg) connection pool.   │
│   - jobs.db (job runs, logs, fetch)    │ • English `tsvector` + GIN + `pg_trgm` search.│
│   - users.db (OTPs, devices, list)     │ • 30-day job run log pruning policy (strictly │
│ • Decouple cross-table JOINs in Python │   exempting economy, spend, and chat data).   │
│ • Zero lock contention between batch   │ • Podman container deployment on chandra-1    │
│   jobs and real-time playback pollers. │   with shared media asset volumes.            │
└────────────────────────────────────────┴───────────────────────────────────────────────┘
```

---

## Part 1: SQLite Domain Partitioning & Lock Elimination

### 1.1 Root Cause of Current Contention

1. **Single Writer Serialization**: In SQLite WAL mode, any number of readers can proceed concurrently, but only one write transaction can execute at a time per database file.
2. **Single Connection Dispatcher**: `_connection.py` instantiates a single `aiosqlite.Connection`. In `aiosqlite`, all calls queue onto a single background OS thread, serializing even concurrent read operations.
3. **Collision of Asynchronous Workloads**:
   - **Heavy Writers**: `catalog_enrich` (updating thousands of item records), `catalog_sync`, `fetch_queue_drain` (yt-dlp ingestion), and `log_capture.py` (bulk inserting hundreds of log rows per job run).
   - **Real-Time Pollers & API**: `StatePoller` (every 2–5s), `RacePoller`, `PlaylistScheduler`, `CompletionRecorder`, `PresenceRefundMonitor`, and device API key authentication (updating `last_used_at` per HTTP request).
   - When a batch job holds the WAL write lock or issues rapid write transactions, real-time pollers and web requests exceed the 5000ms `busy_timeout` and crash with `database is locked`.

### 1.2 Domain Partitioning Strategy

We partition tables into 4 dedicated SQLite databases:

```
┌──────────────────────────┐   ┌──────────────────────────┐
│        catalog.db        │   │         queue.db         │
├──────────────────────────┤   ├──────────────────────────┤
│ • catalog                │   │ • queue_shadow           │
│ • catalog_fts (FTS5)     │   │ • spend_requests         │
│ • categories             │   │ • queue_history          │
│ • catalog_categories     │   │ • saved_playlists        │
│ • tags                   │   │ • saved_playlist_items   │
│ • catalog_tags           │   │ • playlist_schedules     │
│ • people                 │   │ • active_schedule        │
│ • catalog_people         │   │ • play_completions       │
│ • studios                │   │ • playlist_item_played   │
│ • catalog_studios        │   │ • catalog_blackouts      │
│ • item_enrichment_state  │   └──────────────────────────┘
│ • item_edit_log          │
│ • sync_log               │   ┌──────────────────────────┐
│ • motd_overrides         │   │         users.db         │
└──────────────────────────┘   ├──────────────────────────┤
                               │ • user_watchlist         │
┌──────────────────────────┐   │ • otps                   │
│         jobs.db          │   │ • device_link_codes      │
├──────────────────────────┤   │ • device_api_keys        │
│ • job_runs               │   │ • feedback               │
│ • job_run_logs           │   │ • title_suggestions      │
│ • job_schedules          │   └──────────────────────────┘
│ • fetch_queue            │
└──────────────────────────┘
```

### 1.3 Cross-Domain Query Decoupling

Separating SQLite database files removes the ability to perform single SQL `JOIN`s across tables in different files. We resolve the existing cross-table queries in the application layer:

1. **Recently-Played Catalog Suppression Filter**:
   - *Previous*: `catalog` query joined `play_completions` and `playlist_item_played`.
   - *Partitioned*: `queue.db` executes `get_active_hidden_media_ids() -> set[str]`. The browse query in `catalog.db` filters with `WHERE friendly_token NOT IN (...)`.
2. **User Watchlist ("My List")**:
   - *Previous*: `user_watchlist JOIN catalog`.
   - *Partitioned*: Query `users.db` for `friendly_tokens` for the user, then query `catalog.db` with `get_catalog_items_by_tokens(tokens)`.
3. **Title Suggestions Catalog Match**:
   - *Previous*: `title_suggestions` lookup checking if `catalog_token` exists.
   - *Partitioned*: Check `catalog.db` by token/title independently during triage or submission.
4. **Job Run Logs & Job History**:
   - `JobManager` writes exclusively to `jobs.db`. Background enrichment tasks write their metadata updates to `catalog.db` without locking the job log tables.

### 1.4 Partitioning Limits and Safety

Partitioning removes only cross-domain SQLite contention. `catalog` still contains browse reads
and enrichment writes, while `queue` still contains poller, scheduler, and queue-history writes.
Each domain therefore uses short, bounded transactions, retry/latency metrics, and domain-scoped
concurrency tests. A `busy_timeout` is a wait budget, not a correctness mechanism.

The layout is selected explicitly as `monolith` or `partitioned`. Startup never chooses a new
default data directory when a legacy database exists, and migration history is never copied from
the monolith: each partition has a fresh baseline schema version after ETL validation.

### 1.5 Part 1 Implementation Artifacts

- **Sprint Specs**: Located in `docs/sqlite-domain-separation/`
  - `PRD-sqlite-domain-separation.md`
  - `SPEC-Sortie-1-schema-partitioning-and-config.md`
  - `SPEC-Sortie-2-multi-db-connection-layer.md`
  - `SPEC-Sortie-3-cross-domain-query-decoupling.md`
  - `SPEC-Sortie-4-etl-split-script-and-validation.md`

---

## Part 2: Chandra-1 PostgreSQL & Podman Migration

### 2.1 Target Architecture

Upon completion of Part 1, the partitioned domain model directly maps to PostgreSQL. Instead of maintaining multiple SQLite files, the application connects to a **single PostgreSQL database (`webqueue`)** on `chandra-1`, structured with logical schemas:

- `catalog.*` — MediaCMS catalog mirror, cast/crew, tags, categories, enrichment state, MOTD overrides.
- `queue.*` — Queue shadow, spend requests, saved playlists, schedules, play completions, blackouts.
- `jobs.*` — Job schedules, job runs, per-line job logs, fetch queue.
- `users.*` — OTP authentication, device link codes & API keys, user watchlists, feedback, title suggestions.
- `tmdb.*` — Persistent, queryable local TMDB dump index.

```
┌─────────────────────────────────────────┐
│          grindhouse.local Host          │
│                                         │
│   ┌─────────────────────────────────┐   │
│   │           nginx proxy           │   │
│   │    queue.dropsugar.co (SSL)     │   │
│   └────────────────┬────────────────┘   │
└────────────────────┼────────────────────┘
                     │ LAN Reverse Proxy
                     │ http://chandra-1.local:2010
                     ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                 chandra-1 Host System                                  │
│                                                                                        │
│  ┌──────────────────────────────────────────────────────────────────────────────────┐  │
│  │                     Podman Container: webqueue-app                               │  │
│  │                                                                                  │  │
│  │  ┌─────────────────────────────┐                                                 │  │
│  │  │    kryten-webqueue-app      │                                                 │  │
│  │  │   (FastAPI + uvicorn:2010)  │                                                 │  │
│  │  │   SQLAlchemy 2.0 (asyncpg)  │                                                 │  │
│  │  └──────────────┬──────────────┘                                                 │  │
│  └─────────────────┼───────────────────────────────────────────┬────────────────────┘  │
│                    │                                           │                       │
│                    ▼                                           ▼                       │
│  ┌─────────────────────────────────────┐     ┌──────────────────────────────────────┐  │
│  │        PostgreSQL on chandra-1      │     │         Shared Media Volume          │  │
│  │        Database: webqueue           │     │        /var/lib/kryten/media         │  │
│  │        Schemas: catalog, queue,     │     │        - cover art / posters         │  │
│  │                 jobs, users, tmdb   │     │        - TMDB dump files             │  │
│  └─────────────────────────────────────┘     └──────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

The public hostname `https://queue.dropsugar.co/` terminates TLS via Let's Encrypt Nginx on
`grindhouse.local`. During the Chandra-1 cutover, the Nginx reverse proxy configuration on
`grindhouse.local` (`/etc/nginx/sites-available/queue.conf`) is updated to forward HTTP and
WebSocket traffic across the local network to `http://chandra-1.local:2010`.

### 2.2 Core Technical Specifications

1. **ORM & Driver**: **SQLAlchemy 2.0 with `asyncpg`** (`create_async_engine`, `async_sessionmaker`, `AsyncSession`).
   - Retains clean async repository layer with type-safe query construction and parameterized execution.
   - Automatic connection pooling (`pool_size=10, max_overflow=20, pool_pre_ping=True`).
2. **Search Engine**:
   - **Full-Text Search**: English `tsvector` generated column + `GIN` index on `catalog.title` and `catalog.description`.
   - **Typo-Tolerant Matching**: `pg_trgm` extension with GIN/GiST index on `catalog.title` for fuzzy `similarity()` matching (e.g., matching "terminatr" to "The Terminator").
   - Eliminates all SQLite FTS5 query-sanitization crashes.
3. **Automated Log Pruning Policy**:
   - A scheduled background job cleans up rows in `jobs.job_run_logs` older than **30 days**.
   - **CRITICAL COMPLIANCE CONSTRAINT**: This pruning policy applies **EXCLUSIVELY** to `jobs.job_run_logs`. It must **NEVER** touch or prune:
     - Chat logs or user communication records
     - Economy or z-coin purchase history (`spend_requests`, `queue_history`)
     - User feedback or moderation audit logs (`item_edit_log`, `feedback`)
4. **ETL & Data Migration**:
   - Streaming migration script (`migrate_sqlite_to_pg.py`) reading from the 4 SQLite databases and writing into PostgreSQL with foreign key verification, sequence resets, and JSONB conversion.

### 2.3 Cutover Contract

WebQueue runs as a rootful Quadlet application container on `chandra-1`; PostgreSQL is
host-managed on the same host and reached through an explicit container host gateway, never
container-localhost. The PostgreSQL cutover uses a maintenance window and forward-fix recovery:
the SQLite snapshot remains read-only for one release, but it is not an automatic rollback target
after PostgreSQL accepts writes.

### 2.4 Part 2 Implementation Artifacts

- **Sprint Specs**: Located in `docs/postgres-migration/`
  - `PRD-postgres-migration.md`
  - `SPEC-Sortie-1-db-config-and-pool.md`
  - `SPEC-Sortie-2-connection-layer-port.md`
  - `SPEC-Sortie-3-fts5-to-tsvector.md`
  - `SPEC-Sortie-4-tmdb-index-database.md`
  - `SPEC-Sortie-5-etl-migration.md`
  - `SPEC-Sortie-6-tests-cutover-release.md`

---

## Sequencing & Next Steps

1. **Phase 1 Execution (Immediate)**:
   - Implement Part 1 (SQLite Domain Partitioning) to resolve production write-lock crashes immediately without requiring infrastructure migration on chandra-1.
   - Run `split_databases.py` in staging/production to migrate single SQLite DB to `catalog.sqlite3`, `queue.sqlite3`, `jobs.sqlite3`, `users.sqlite3`.
2. **Phase 2 Execution (Subsequent)**:
   - Bootstrap PostgreSQL `webqueue` database on `chandra-1`.
   - Deploy Podman container on `chandra-1` with SQLAlchemy 2.0 + asyncpg.
   - Execute ETL migration and switch production traffic.
