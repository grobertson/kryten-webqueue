# SPEC — Sortie 4: ETL Split Script & Concurrency Validation

**Sprint**: `sqlite-domain-separation`  
**PRD**: [PRD-sqlite-domain-separation.md](PRD-sqlite-domain-separation.md)  
**Depends on**: Sortie 3  
**Estimated Effort**: 3 hours  

---

## 1. Overview

Build an automated database migration script (`scripts/split_databases.py`) that reads a legacy monolithic `webqueue.db` SQLite database and cleanly splits the data into the 4 new partitioned databases (`catalog.sqlite3`, `queue.sqlite3`, `jobs.sqlite3`, `users.sqlite3`). Build end-to-end concurrency tests verifying that heavy background jobs do not block real-time pollers.

---

## 2. Migration Script Architecture (`split_databases.py`)

### 2.1 Process Flow
1. **Maintenance and Source Capture**: Stop WebQueue and every scheduler/writer, then capture a verified SQLite backup using a checkpoint-safe procedure. Open the captured source read-only; do not infer quiescence from WAL file presence alone.
2. **Target Initialization**: Create destination databases in a staging directory and run the new domain baseline migrations and indexes. Never copy legacy `_migrations` rows.
3. **Table Data Copy**:
   - Stream rows from source tables into target domain tables.
   - For `catalog.db`: copy `catalog`, `categories`, `catalog_categories`, `tags`, `catalog_tags`, `people`, `catalog_people`, `studios`, `catalog_studios`, `item_enrichment_state`, `item_edit_log`, `sync_log`, `motd_overrides`.
   - For `queue.db`: copy `queue_shadow`, `spend_requests`, `queue_history`, `saved_playlists`, `saved_playlist_items`, `playlist_schedules`, `active_schedule`, `play_completions`, `playlist_item_played`, `catalog_blackouts`.
   - For `jobs.db`: copy `job_runs`, `job_run_logs`, `job_schedules`, `fetch_queue`.
   - For `users.db`: copy `otps`, `device_link_codes`, `device_api_keys`, `user_watchlist`, `feedback`, `title_suggestions`.
4. **FTS Rebuild**: Populate `catalog_fts` in `catalog.db` from migrated `catalog` rows.
5. **Validation**: Compare row counts and canonical hashes for every application table, then validate foreign keys and required indexes. `catalog_fts` is derived and rebuilt from migrated catalog rows; it is not copied or parity-checked as source data.
6. **Activation**: Activate the staged destination only after validation succeeds and update configuration to `partitioned`. Never rename or delete `webqueue.db`, `webqueue.db-wal`, or `webqueue.db-shm` automatically; source archival is an explicit operator action.

---

## 3. Concurrency Stress Testing

Build test suite `tests/test_concurrency_split.py`:
- **Test 1: Batch Enrichment vs StatePoller**:
  - Run continuous simulated `catalog_enrich` batch inserts in `catalog.db` + continuous log streaming in `jobs.db`.
  - Concurrently execute `StatePoller.poll()` updating `queue_shadow` in `queue.db` at 100ms intervals.
  - Assert zero cross-domain lock failures and record domain-scoped latency and retry counters. The test does not claim that two writers to one SQLite domain can run concurrently.
- **Test 2: Device Key Auth vs Catalog Browse**:
  - Concurrently execute 50 parallel requests updating `device_api_keys.last_used_at` in `users.db` while browsing `catalog.db`.
  - Assert both complete without cross-domain locks against a CI-appropriate latency budget; the test reports timings instead of enforcing a hardware-dependent 10ms threshold.

---

## 4. Acceptance Criteria

- [ ] `split_databases.py` migrates 100% of application rows with zero data loss and verifies row-count and canonical-hash parity.
- [ ] `catalog_fts` correctly indexed and searchable in `catalog.db`.
- [ ] Concurrency stress tests pass reliably without lock contention.
- [ ] Full test suite (`pytest`) runs green across all domain operations.
