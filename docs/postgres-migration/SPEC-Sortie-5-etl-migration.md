# SPEC — Sortie 5: One-Shot ETL Migration (Partitioned SQLite → PostgreSQL `webqueue`)

**Sprint**: `postgres-migration`
**PRD**: [PRD-postgres-migration.md](PRD-postgres-migration.md)
**Depends on**: Sortie 2 (schema), Sortie 3 (search vector)
**Estimated**: 4–6 h

---

## 1. Overview

Migrate all application data from the 4 partitioned SQLite databases (`catalog.sqlite3`, `queue.sqlite3`, `jobs.sqlite3`, `users.sqlite3`) into the target PostgreSQL `webqueue` database on `chandra-1` across their corresponding schemas (`catalog.*`, `queue.*`, `jobs.*`, `users.*`).

All enrichment state, cover-art paths, classifications, active schedules, and user credentials migrate verbatim so cutover never requires external TMDB/OMDB API calls.

## 2. Scope and Non-Goals

**In scope**
- `python -m kryten_webqueue.migrate_sqlite_to_pg` with `--data-dir`, `--verify`, and `--dry-run`.
- Copy every table in dependency-safe order into PostgreSQL schemas.
- Automatic sequence synchronization for all identity columns (`setval()`).
- Data type conversions: SQLite `0/1` $\to$ Postgres `boolean`, ISO timestamps $\to$ `TIMESTAMPTZ`, JSON strings $\to$ `JSONB`.
- `--verify` mode checking row-count parity, canonical hash parity, foreign keys, uniqueness, and sequence state.

**Non-goals**
- TMDB dump index ETL (handled in Sortie 4).
- External API calls.
- Copying derived SQLite `catalog_fts` data or legacy `_migrations` rows.

## 3. Migration Order & Schema Routing

```
catalog.sqlite3 ──────▶ catalog.* (catalog, categories, tags, people, studios, enrichment_state, motd)
queue.sqlite3   ──────▶ queue.*   (queue_shadow, spend_requests, queue_history, playlists, schedules, completions)
jobs.sqlite3    ──────▶ jobs.*    (job_runs, job_run_logs, job_schedules, fetch_queue)
users.sqlite3   ──────▶ users.*   (otps, device_link_codes, device_api_keys, user_watchlist, feedback, suggestions)
```

## 4. Implementation Plan

1. **Create** `kryten_webqueue/migrate_sqlite_to_pg.py` (CLI with `argparse`, async streaming).
2. **Implement** batched streaming with `chunk_size=1000` to maintain bounded memory footprint. Source files are a checkpoint-safe, read-only capture made while WebQueue and schedulers are stopped.
3. **Add** sequence reset queries:
   ```sql
   SELECT setval(
       pg_get_serial_sequence('jobs.job_runs', 'id'),
       coalesce(max(id), 1),
       max(id) IS NOT NULL
   )
   FROM jobs.job_runs;
   ```
   Imported `GENERATED ALWAYS` identities use `OVERRIDING SYSTEM VALUE` before this reset.
4. **Implement** verification mode comparing counts and canonical hashes in stable primary-key order, normalized UTC timestamps, and canonical JSON values. It also validates PostgreSQL foreign keys, uniqueness, and next identity values.

The command refuses a non-empty destination unless it is an explicitly named staging target. A
dry run performs source/configuration/type preflight without PostgreSQL writes. Invalid timestamps
or JSON are migration failures, not values to coerce silently.

## 5. Acceptance Criteria

- [ ] All 4 SQLite databases streamed into target schemas with zero loss.
- [ ] `--dry-run` performs validation without writing to PostgreSQL.
- [ ] `--verify` confirms 100% row-count and canonical-hash parity across all application tables.
- [ ] Identity sequences set properly to prevent collision on next insert.
- [ ] Source SQLite files opened in read-only mode and unmodified.
- [ ] Derived FTS data is rebuilt and tested rather than copied.
- [ ] All tests pass green.

- Referenced by the `docs/postgres-cutover.md` runbook (Sortie 6).
