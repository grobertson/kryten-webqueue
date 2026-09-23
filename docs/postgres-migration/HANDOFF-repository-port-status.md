# HANDOFF: PostgreSQL Repository Port (Sortie 2/3) — In-Progress Status

**Status as of**: 2026-09-23, session end
**For**: The next agent/engineer picking up the Postgres cutover of `kryten-webqueue`
**Read this before touching anything.** Production (`https://queue.dropsugar.co/`) is
currently served by `kryten-webqueue.service` on `grindhouse.local`, running SQLite in
`layout=partitioned` mode at **v0.48.1**. It is healthy and untouched by this work.
**Nothing in this document has been deployed to production.**

---

## 1. TL;DR

The goal is to run `kryten-webqueue` as a Podman container on `chandra-1`, reading/writing
the already-provisioned PostgreSQL database `webqueue` (schemas `catalog`, `queue`, `jobs`,
`users`, `tmdb`) instead of SQLite.

**What actually exists and is verified working:**
- PostgreSQL `webqueue` database on `chandra-1`, schema applied, `pg_trgm` installed,
  `kryten` role granted full privileges on all 5 schemas.
- A one-shot ETL (`kryten_webqueue/migrate_sqlite_to_pg.py`) has already been run once and
  reported 100% row-count parity — **but this data is now stale** (see §5.6). It must be
  re-run in the final maintenance window, not relied upon as-is.
- A Podman image `localhost/kryten-webqueue:latest` exists on `chandra-1`, but it was built
  **2026-09-23 15:21 UTC from commit `533d6ed` (v0.48.0)** — it predates the v0.48.1 hotfix
  and **all** of the Postgres repository-port work described below. **It must be rebuilt
  before any deployment.**
- Quadlet unit files exist in the repo (`deploy/podman/webqueue/webqueue.network`,
  `webqueue-app.container`) but are **not installed** on `chandra-1`
  (`/etc/containers/systemd/` has nothing webqueue-related). No `webqueue-app.service`
  exists yet; nothing is running there.
- nginx on `grindhouse.local` still points at `127.0.0.1:2010`. It has not been touched.

**What does NOT exist yet (the actual remaining work):**
- The application's database layer (`kryten_webqueue/catalog/db/_catalog.py`, `_queue.py`,
  `_playlists.py`, `_jobs_db.py`, `_users_db.py`, `_devices.py`, `_feedback.py`,
  `_watchlist.py`, `_people.py`, `_enrichment.py`, `_fetch_queue.py`, `_blackouts.py`,
  `_motd.py` — roughly **4,700 lines**) is **100% SQLite-only**. There is no code path that
  reads or writes Postgres for any actual application query yet. `Database.__init__` in
  `kryten_webqueue/catalog/db/__init__.py` only branches on SQLite `layout`
  (monolith/partitioned); it does not look at `config.database.backend` at all.
- This session started that port (see §3) but only the shared low-level connection helper
  (`_pg_base_domain.py`) exists so far. **Zero of the ~140 domain methods have been ported.**

**Do not build/deploy the chandra-1 pod against Postgres until §4's checklist is complete
and tested.** Standing up the container today would either crash immediately (schema
mismatch, e.g. missing `id` column on `users.otps` — see §3.2) or, worse, silently write
malformed data if partially wired.

---

## 2. Why this exists / prior session context

This is the continuation of the `postgres-migration` sprint documented in
`docs/postgres-migration/PRD-postgres-migration.md` and its `SPEC-Sortie-*.md` files. Prior
sessions completed:
- Sortie 1 (config + engine scaffold) — done, `PostgresConfig`/`DatabaseConfig.backend` in
  `kryten_webqueue/config.py`, `kryten_webqueue/catalog/db/engine.py`.
- Sortie 4 (TMDB schema) and Sortie 5 (ETL) — schema + script exist and were run once.
- Sortie 6 (pruner, Quadlet files, cutover runbook) — files exist in the repo, not deployed.

**Sortie 2 (SQLAlchemy 2.0 repository port) and Sortie 3 (FTS5 → tsvector/pg_trgm) were
never done.** That is the actual bulk of "finishing the migration," and it's what this
session began. See `docs/postgres-migration/SPEC-Sortie-2-connection-layer-port.md` and
`SPEC-Sortie-3-fts5-to-tsvector.md` for the original design intent (SQLAlchemy Core). **This
session took a different, lighter-weight implementation approach — see §3.1 for why, and
make a deliberate decision about whether to continue with it or switch to the SQLAlchemy
plan before writing more code.**

---

## 3. What this session actually did

### 3.1 Architecture decision made (needs confirmation/ratification)

Instead of the SPEC's SQLAlchemy Core/ORM approach, this session started a **direct
`asyncpg`, per-domain connection pool** design:

- Each of the 4 domains (`catalog`, `queue`, `jobs`, `users`) gets its own `asyncpg.Pool`,
  created with an `init` callback that runs `SET search_path TO <schema>, public` on every
  connection.
- Because of that `search_path` scoping, **the existing SQL text's unqualified table names
  (`catalog`, `tags`, `queue_shadow`, `job_runs`, `otps`, etc.) resolve correctly to
  `<schema>.<table>` without rewriting every query to be schema-qualified.** This was the
  key insight that makes porting ~140 methods tractable instead of requiring a full ORM
  rewrite.
- `?` positional placeholders are mechanically translated to asyncpg's `$1, $2, …` by a
  shared `to_pg_sql()` helper. This is the *only* automatic translation — everything else
  (SQLite-specific functions, `rowid`/`lastrowid`, upsert syntax, FTS5) must be hand-fixed
  per call site (see §4).

This is **not** what `kryten_webqueue/catalog/db/engine.py` was scaffolded for — that file
builds a SQLAlchemy `AsyncEngine`/`async_sessionmaker` and **is not used by anything**
(dead code as of now). Before continuing, an explicit decision is needed:

- **(A) Keep the asyncpg/search_path approach** (what this session started) — faster to
  finish, less abstraction, `engine.py` should probably be deleted or repurposed once this
  is proven, and `SPEC-Sortie-2-connection-layer-port.md` should be updated to reflect
  reality.
- **(B) Discard `_pg_base_domain.py` and do the original SQLAlchemy Core port** — more
  idiomatic, matches the existing SPEC and `engine.py`, but is materially more work (schema
  metadata, Core `text()` binds, session-per-request wiring in FastAPI dependencies) with no
  session-1 progress toward it yet.

**Recommendation: (A).** The reasoning above (search_path reuse) is sound and most of the
remaining work is mechanical per-file translation, not architecture. This document assumes
(A) going forward.

### 3.2 Files created/modified this session

| File | Status |
|---|---|
| `kryten_webqueue/catalog/db/_pg_base_domain.py` | **New.** `_PgDomainDB` base class: connection pool, `_execute`, `_execute_returning_id`, `_executemany`, `_fetch_one`, `_fetch_all`, `_fetch_val`, plus `to_pg_sql()` and a `_PgResult` shim (`.rowcount`, `.lastrowid`) so call sites can look similar to the aiosqlite ones. **Untested — never connected to a live database, never imported anywhere.** |
| `kryten_webqueue/catalog/db/sql/001_initial_schema.sql` | **Modified.** Added `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` to `users.otps` (SQLite's `verify_otp`/etc. use `rowid`, which doesn't exist in Postgres — needed a real PK). **This change is repo-only. It has NOT been applied to the live `chandra-1` database.** Live `users.otps` on chandra-1 currently has no `id` column (verified via `\d users.otps` this session). |

**Nothing else changed.** No domain classes, no facade wiring, no tests, no deployment.

### 3.3 Immediate next action if picking (A)

Apply the schema fix live before writing/testing the users-domain Postgres class:

```bash
ssh groberts@chandra-1.local
PGPASSWORD=kryten_secret_password psql -h 127.0.0.1 -U kryten -d webqueue -c \
  "ALTER TABLE users.otps ADD COLUMN id BIGINT GENERATED ALWAYS AS IDENTITY;"
PGPASSWORD=kryten_secret_password psql -h 127.0.0.1 -U kryten -d webqueue -c \
  "ALTER TABLE users.otps ADD PRIMARY KEY (id);"
```

Verify existing rows got sequential ids (`SELECT * FROM users.otps ORDER BY id;`) and that
this didn't violate anything (it won't — `otps` currently has ~4-8 rows, no FK references
it).

---

## 4. Remaining work: exact checklist

Authoritative method lists below are taken directly from `_DOMAIN_METHOD_MAP` in
`kryten_webqueue/catalog/db/__init__.py` — that dict is the single source of truth for what
must exist on each domain object. Cross-check against it; it may have shifted since this
was written.

### 4.1 Wiring (do this last, after at least one domain is ported and tested)

In `kryten_webqueue/catalog/db/__init__.py`, `Database.__init__` currently only branches on
`self._layout` (`monolith` vs `partitioned`) — it never looks at
`self._db_config.backend`. Needs a new top-level branch:

```python
if self._db_config.backend == "postgres":
    dsn = self._db_config.postgres.get_async_url()  # already asyncpg-flavored (postgresql+asyncpg://...)
    # NOTE: asyncpg.connect()/create_pool() wants a plain "postgresql://" DSN, NOT
    # "postgresql+asyncpg://" (that prefix is a SQLAlchemy convention). Strip it, or add
    # a separate get_asyncpg_dsn() to PostgresConfig that returns the bare form.
    self.catalog = _PgCatalogDB(dsn, schema="catalog")
    self.queue = _PgQueueDB(dsn, schema="queue")
    self.jobs = _PgJobsDB(dsn, schema="jobs")
    self.users = _PgUsersDB(dsn, schema="users")
elif self._layout == "partitioned":
    ...  # existing sqlite branch, unchanged
else:
    ...  # existing sqlite monolith branch, unchanged
```

`connect()`, `close()` already just `asyncio.gather` across `self.catalog/queue/jobs/users`
— those should work unchanged as long as the Pg domain classes expose `connect()`/`close()`
with the same signature (they do, per `_pg_base_domain.py`).

`run_migrations()` must **skip** entirely for the Postgres branch — schema is applied via
`sql/001_initial_schema.sql` out-of-band, not via the SQLite-style versioned migration list.
`_PgDomainDB.run_migrations()` is already a no-op; just make sure `Database.run_migrations()`
doesn't call it in a way that breaks (currently fine, it's `await asyncio.gather(...)` on
whatever `.run_migrations()` no-ops to).

**PostgresConfig needs a fix**: `get_async_url()` returns `postgresql+asyncpg://...` which is
correct for SQLAlchemy but `asyncpg.connect()`/`create_pool()` do **not** accept the
`+asyncpg` dialect suffix — they want bare `postgresql://`. Add a
`get_asyncpg_dsn()` method (or strip the suffix inline) before wiring this up. This will
crash instantly if not fixed — write a quick unit test for it.

### 4.2 Domain: `users` (smallest, do first)

Methods (from `_DOMAIN_METHOD_MAP`, `users` domain), source mixins in parens:

```text
watchlist_add, watchlist_remove, watchlist_tokens, watchlist_count,
get_user_watchlist_tokens                              (_watchlist.py, _users_db.py)
store_otp, verify_otp, cleanup_expired_otps             (_users_db.py)
create_link_code, get_valid_link_code, delete_link_code,
link_code_exists, purge_expired_link_codes,
create_device_key, get_device_key_by_hash, touch_device_key,
list_device_keys, delete_device_key, revoke_user_device_keys,
device_key_usernames                                    (_devices.py)
add_feedback, list_feedback, count_feedback,
set_feedback_status, delete_feedback,
add_title_suggestion, list_title_suggestions,
count_title_suggestions, set_title_suggestion_status,
delete_title_suggestion                                 (_feedback.py)
```
(Note: `create_feedback`/`update_feedback_status`/`create_title_suggestion`/
`update_title_suggestion_status` are also in the map as aliases — check whether those are
actually called anywhere or are dead map entries before implementing twice.)

**Known SQLite-isms to fix in this domain** (grep for these patterns yourself too, this list
may not be exhaustive):
- `_users_db.py::verify_otp` — `SELECT rowid FROM otps ...` / `UPDATE otps SET used=1 WHERE
  rowid=?` → use the new `id` column (§3.3) instead of `rowid`.
- `datetime('now')` → `now()`. Appears in: `_devices.py` (`get_valid_link_code`,
  `purge_expired_link_codes`, `touch_device_key`), `_users_db.py` (`verify_otp`,
  `cleanup_expired_otps`).
- `datetime(expires_at) > datetime('now')` → `expires_at > now()` (the `datetime(...)` wrap
  on a timestamptz column is a SQLite-ism for string-normalizing; unnecessary and invalid in
  Postgres — just drop it).
- `cursor.lastrowid` → `_execute_returning_id(sql + " RETURNING id", params)`. Appears in
  `_devices.py::create_device_key`, `_feedback.py::add_feedback` and
  `add_title_suggestion`.
- `INSERT OR IGNORE INTO user_watchlist (...)` (`_watchlist.py::watchlist_add`) → `INSERT
  INTO user_watchlist (...) VALUES (...) ON CONFLICT DO NOTHING`. Return value should be
  based on whether a row was actually inserted — asyncpg's `execute()` status string for a
  no-op conflict is `"INSERT 0 0"` vs `"INSERT 0 1"` on success; `_PgResult.rowcount` already
  parses the trailing number, so `rowcount > 0` still works as the existing code expects.

### 4.3 Domain: `jobs`

Methods:

```text
start_job_run, finish_job_run, update_job_run_detail, add_job_run_logs,
get_job_run_logs, get_job_run, get_job_runs, reconcile_orphaned_job_runs,
get_job_schedules, get_job_schedule, upsert_job_schedule, delete_job_schedule,
prune_job_run_logs                                       (_jobs_db.py)
enqueue_fetch, claim_next_fetch_item, finish_fetch_item, requeue_fetch_item,
requeue_fetch_item_for_retry, reset_running_fetch_items, get_fetch_queue,
count_fetch_queue, count_fetch_queue_pending, delete_fetch_queue_item
                                                          (_fetch_queue.py)
```

**Known SQLite-isms:**
- `cursor.lastrowid` in `start_job_run`, `_fetch_queue.py::enqueue_fetch` → `RETURNING id`.
- `_db.executemany(...)` in `add_job_run_logs` → `_executemany()` (already implemented in
  `_pg_base_domain.py` via `conn.executemany`).
- `datetime('now')` / `datetime('now', ?)` throughout (`finish_job_run`,
  `upsert_job_schedule`'s `updated_at`, `prune_job_run_logs`'s `WHERE logged_at <
  datetime('now', ?)`, `_fetch_queue.py`'s `started_at`/`finished_at`/the
  `added_at = (SELECT datetime(MAX(added_at), '+1 second') FROM fetch_queue)` self-referencing
  insert, and `finished_at < datetime('now', '-24 hours')`).
  - `datetime('now', ?)` with a param like `'-30 days'` → `(now() + ($n)::interval)` — the
    interval literal syntax (`'-30 days'`) is valid in both dialects, so the **parameter
    value itself doesn't need to change**, only the SQL text wrapping it.
  - `datetime(MAX(added_at), '+1 second')` → `(MAX(added_at) + interval '1 second')`.
  - `datetime('now', '-24 hours')` (literal, no param) → `(now() - interval '24 hours')`.
- `ON CONFLICT(job_name) DO UPDATE SET ... updated_at = datetime('now')` in
  `upsert_job_schedule` — the `ON CONFLICT` structure is already Postgres-compatible, just
  fix the `datetime('now')` inside it.
- **This is the domain with the automated 30-day log pruner** (`prune_job_run_logs`,
  see `docs/postgres-migration/SPEC-Sortie-6-tests-cutover-release.md` §3.1). The compliance
  constraint from that spec still applies: this must only ever delete from
  `jobs.job_run_logs`. There's an existing SQLite test for this
  (`tests/test_postgres_pruner.py`) — port/duplicate it for the Postgres path once this
  domain works, don't skip it.

### 4.4 Domain: `queue`

Methods (largest non-catalog domain):

```text
get_shadow_items, upsert_shadow_item, remove_shadow_items, update_shadow_position,
update_shadow_estimated_start, get_last_pay_uid, get_shadow_position_after,
get_pay_items, get_request_id_for_uid                    (_queue.py)
save_spend_request, get_spend_request, mark_spend_refunded, add_queue_history,
get_user_queue_history                                   (_queue.py)
create_saved_playlist, get_saved_playlist, get_saved_playlists, list_saved_playlists,
update_saved_playlist, delete_saved_playlist, save_playlist_items,
get_saved_playlist_items, replace_playlist_items, append_playlist_item,
append_playlist_items, rotate_playlist_item_to_bottom, get_most_recent_playlist,
get_playlist_by_name, get_playlist_by_name_any, get_promo_pools,
get_promo_pool_items                                     (_playlists.py)
create_playlist_schedule, get_playlist_schedule, list_playlist_schedules,
update_playlist_schedule, delete_playlist_schedule, get_schedules, get_schedule,
create_schedule, update_schedule, delete_schedule, mark_schedule_fired,
get_active_schedule, set_active_schedule, clear_active_schedule,
disable_active_lock, is_event_lock_active                (_playlists.py)
record_play_completion, unrecord_play_completion, clear_play_state,
get_active_hidden_media_ids, get_reserved_media_ids, get_active_blackout_tokens,
is_media_restricted, get_played_at_for_tokens, get_promo_pool_media_ids,
purge_promo_completions, get_recently_played_completions (_queue_db.py)
upsert_blackout, prune_expired_blackouts, is_blackout,
count_active_blackouts, list_active_blackouts            (_blackouts.py)
```

**Known SQLite-isms — this is the highest-risk domain (real money via `spend_requests`,
live playback scheduling via `playlist_schedules`):**
- `INSERT OR REPLACE INTO queue_shadow (...)` (`_queue.py::upsert_shadow_item`) → needs
  `ON CONFLICT (uid) DO UPDATE SET <every column> = EXCLUDED.<column>`. Write out every
  column explicitly; don't try to be clever/generic here, get it right by hand and test it.
- `INSERT OR REPLACE INTO active_schedule (...)` (`_playlists.py::set_active_schedule`) →
  same pattern, conflict target is `(id)` (it's always id=1, a singleton row) —
  `ON CONFLICT (id) DO UPDATE SET ...`.
- `INSERT OR REPLACE INTO playlist_item_played (...)` (`_queue_db.py::record_play_completion`)
  → conflict target `(playlist_id, position)`.
- `INSERT OR IGNORE INTO spend_requests (...)` (`_queue.py`) → `ON CONFLICT DO NOTHING`.
- `cursor.lastrowid` in `_playlists.py` (playlist creation, schedule creation) →
  `RETURNING id`.
- Pervasive `datetime('now')` / `datetime(col, ...)` arithmetic in `_playlists.py`'s
  schedule-firing queries — **read the existing code comment above
  `get_due_schedules`-equivalent logic carefully, it explicitly warns**:
  > `# NOTE: fire_at must be wrapped in datetime() on BOTH sides. fire_at is ... A bare
  > 'fire_at > datetime('now')' is a [bug]`
  The Postgres equivalent needs the same care but is actually **simpler** since `fire_at` is
  a native `timestamptz` column — comparisons like `fire_at > now()` just work without any
  wrapping. But the *dynamic interval arithmetic* needs an explicit cast:
  ```sql
  -- SQLite:
  datetime(fire_at, '-' || pre_fire_lock_minutes || ' minutes') <= datetime('now')
  -- Postgres:
  (fire_at - (pre_fire_lock_minutes::text || ' minutes')::interval) <= now()
  ```
  There are at least 3 near-identical copies of this predicate in `_playlists.py`
  (`get_due_schedules`, `claim_due_schedule`-ish methods — check current method names).
  **Write one test per schedule-firing method with a schedule due in 1 minute and one due in
  1 hour, confirm only the due one is returned, before trusting this.**
- `record_play_completion` in `_queue_db.py` also does `datetime('now', ?)` for the
  recently-played window — same interval-cast fix as jobs domain.
- `is_media_restricted`, `get_reserved_media_ids`, `resolve_friendly_tokens` — these were
  added in the *SQLite partitioned* cross-domain-decoupling work (Sortie 3 of the SQLite
  sprint, not the Postgres one) to work around SQLite's inability to `JOIN` across separate
  database files. **In Postgres, catalog/queue/jobs/users are schemas in the *same*
  database** — a real cross-schema `JOIN` is possible again (grant already covers it). You
  have a choice here:
  - (a) Keep the same Python-side decoupling pattern (`Database._get_reserved_tokens()` etc.
    in `__init__.py`) for consistency with the SQLite code path and lower risk, or
  - (b) Simplify to a single cross-schema SQL query now that it's actually possible.
  **Recommendation: keep (a) for this port.** Don't do (b) as part of this migration; it's
  a separate, lower-priority cleanup that touches both backends' facade methods and isn't
  required for correctness. Flag it as a follow-up instead.
  - **Important correctness note**: the SQLite path's `get_reserved_media_ids()` returns raw
    `media_id` values that are often manifest URLs, not `friendly_token`s, and there is a
    `catalog.resolve_friendly_tokens()` helper that maps them back (this was itself a bug fix
    made earlier in this same session for the SQLite partitioned path — see CHANGELOG
    v0.48.1 entry). **The Postgres queue domain's `get_reserved_media_ids()` must have the
    same behavior** (return raw `media_id`, let the catalog domain resolve it) — don't
    "fix" this differently for Postgres or you'll reintroduce the exact bug that was just
    patched for SQLite.

### 4.5 Domain: `catalog` (largest, hardest, do last)

Methods:

```text
get_item, get_item_admin, resolve_media, delete_catalog_item, get_catalog_brief,
get_item_facets, get_categories, get_tags, upsert_category, upsert_tag,
set_catalog_categories, set_catalog_tags, add_catalog_tag, remove_catalog_tag,
insert_catalog, update_catalog, update_cover_art, set_imdb_tt, get_item_by_imdb_tt,
delete_stale_catalog_items, find_catalog_by_title, start_sync_log, finish_sync_log,
get_sync_logs, log_item_edit, get_item_edit_history, get_items_by_tokens,
get_hidden_category_and_tag_tokens, resolve_friendly_tokens        (_catalog.py)
get_people, get_studios, upsert_person, upsert_studio,
set_catalog_people, set_catalog_studios                            (_people.py)
get_enrichment_state, update_enrichment_state, get_enrichment_candidates,
get_identify_coverage                                              (_enrichment.py)
get_motd_override, get_motd_overrides_for_week, upsert_motd_override,
delete_motd_override                                               (_motd.py)
```
Plus **`browse`, `browse_count`, `search`, `search_count`** — these are NOT in the domain
map because they're already overridden directly on the `Database` facade in `__init__.py`
(cross-domain orchestration between `catalog` and `queue`). The facade methods call
`self.catalog.browse(..., is_partitioned=True, exclude_tokens=...)` etc. — the underlying
`_catalog.py::browse`/`browse_count` already accept `is_partitioned`/`exclude_tokens` params
(added for the SQLite partitioned path). **Whatever Postgres catalog implementation you
write needs to accept the same two params with the same semantics**, so the existing facade
orchestration code in `__init__.py` keeps working unchanged for both backends.

**Known SQLite-isms — this is where FTS5 must be replaced (Sortie 3's actual scope):**
- `search()`/`search_count()` currently do `FROM catalog_fts fts JOIN catalog c ON c.rowid =
  fts.rowid WHERE catalog_fts MATCH ?` — **`catalog_fts` does not exist in Postgres and
  `rowid` doesn't either.** This must become the hybrid `tsvector` + `pg_trgm` query
  described in `docs/postgres-migration/SPEC-Sortie-3-fts5-to-tsvector.md` §4.3:
  ```sql
  WITH fts_matches AS (
      SELECT c.friendly_token, c.title, ts_rank(c.search_vector, q) AS rank_score, 1 AS match_type
      FROM catalog c, websearch_to_tsquery('english', $1) q
      WHERE c.search_vector @@ q
  ),
  trgm_matches AS (
      SELECT c.friendly_token, c.title, similarity(c.title, $1) AS rank_score, 2 AS match_type
      FROM catalog c
      WHERE similarity(c.title, $1) > 0.3
        AND c.friendly_token NOT IN (SELECT friendly_token FROM fts_matches)
  )
  SELECT * FROM fts_matches UNION ALL SELECT * FROM trgm_matches ORDER BY match_type ASC, rank_score DESC;
  ```
  This is a genuinely different query shape (ranking, pagination, and the existing
  `_facet_filter`/`_duration_range_filter`/exclusion-fragment helpers in `_catalog.py`
  compose SQL via string concatenation onto a `WHERE` clause) — you cannot just swap the
  `FROM`/`WHERE` and keep everything else; the whole `search()` method needs rewriting
  around this two-CTE shape. Budget real time for this specifically; it's the single
  hardest piece in the whole port. There is a fixture-corpus test plan in the SPEC (§6) —
  build it before considering this domain "done."
- `insert_catalog`/`update_catalog` currently maintain the FTS5 index manually
  (`INSERT INTO catalog_fts(rowid, friendly_token, ...) SELECT rowid, ... FROM catalog WHERE
  friendly_token = ?`). **Delete this logic entirely for Postgres** — `search_vector` is a
  `GENERATED ALWAYS AS (...) STORED` column, Postgres maintains it automatically on
  INSERT/UPDATE. Do not try to port the manual FTS maintenance calls.
- `cursor.lastrowid` — none expected in this domain (catalog rows are keyed by
  `friendly_token`, not autoincrement), but double-check `upsert_category`/`upsert_tag`/
  `upsert_person`/`upsert_studio`/`upsert_month...` — those likely do need `RETURNING id`.
- `INSERT OR IGNORE INTO catalog_categories/catalog_tags/catalog_people/catalog_studios` →
  `ON CONFLICT DO NOTHING` (these are all pure junction tables with composite PKs already).
- `datetime('now')` in `update_catalog`'s `updated_at`, `log_item_edit`'s `edited_at`
  (check — may already default via `DEFAULT clock_timestamp()` in the Postgres schema, in
  which case just omit the column from the INSERT rather than porting the SQLite literal).

---

## 5. Testing plan (do not skip this)

There is currently **zero** test coverage for any Postgres domain CRUD path. Existing
`tests/test_postgres_live.py` and `tests/test_postgres_config_and_engine.py` only check
schema/config, not application behavior, and they run against **the real chandra-1 database
that now holds real migrated production data** (spend history, watchlists, etc. from the
one ETL run).

### 5.1 Do not write test data into the shared chandra-1 database carelessly

Any new domain-CRUD tests must either:
- Use uniquely-prefixed, easily-identifiable test rows (e.g. `username='__test_pguser__'`,
  `friendly_token` prefixed `test_pg_`) and clean them up in a fixture teardown, **or**
- Stand up a disposable test database/schema set on chandra-1 (e.g. `webqueue_test`) so real
  data is never touched. **This is the safer option and is recommended** — ask for/verify
  whether creating a second database is acceptable, or reuse the existing `sql/
  001_initial_schema.sql` against a throwaway DB name.

### 5.2 Suggested test structure

Mirror `tests/test_multi_db_connection.py` (the SQLite partitioned test file) — same test
names/scenarios, but against the new Pg domain classes. That file already has good coverage
of exactly the cross-domain behaviors that matter (decoupled watchlist, reserved-item
exclusion via manifest_url resolution, blackout/recently-played filtering, job log
independence). Port those tests 1:1 as a baseline, then add the FTS/trigram-specific search
tests from `SPEC-Sortie-3-fts5-to-tsvector.md` §6.

### 5.3 Run order

1. Apply the `users.otps.id` migration (§3.3).
2. Fix `PostgresConfig.get_asyncpg_dsn()` (§4.1) and add a unit test for it.
3. Port `users` domain, test it in isolation.
4. Port `jobs` domain (includes the pruner — port `tests/test_postgres_pruner.py` for it),
   test it.
5. Port `queue` domain (highest risk — economy + scheduling), test it thoroughly, including
   the `datetime(fire_at, ...)` interval-arithmetic edge cases explicitly.
6. Port `catalog` domain including the FTS/trigram search rewrite, test it against a fixture
   corpus per the SPEC.
7. Wire `Database.__init__` backend branch (§4.1).
8. Run the **entire** existing test suite (`uv run pytest -q`, currently 407 passing) to
   confirm the SQLite path is completely unaffected — it must still be 100% green.
9. Only then: rebuild the chandra-1 Podman image, install the Quadlet units, start the
   service, and smoke-test it **standing alone, before touching nginx**.

### 5.4 Smoke test after deployment (before nginx cutover)

From `chandra-1` itself (never through the public domain until this passes):
```bash
curl -I http://127.0.0.1:2010/auth/login
curl -s http://127.0.0.1:2010/queue/next-schedule
```
Then exercise an actual login + browse + search + watchlist round trip using a real browser
session pointed at `http://chandra-1.local:2010` directly (bypass nginx entirely for this
first pass).

### 5.5 Re-run the ETL immediately before real cutover

The one-shot ETL was run once, earlier, from a since-superseded snapshot of the grindhouse
SQLite data. `grindhouse.local` has continued serving live production traffic since then
(spend requests, watchlist adds, job runs, etc.). **Before the actual maintenance-window
cutover**, re-run `kryten_webqueue/migrate_sqlite_to_pg.py` against a *fresh* stop-the-world
snapshot, per `docs/postgres-cutover.md`. Do not assume the existing chandra-1 data is
current — it is now stale by however many days elapse between this session and the actual
cutover.

### 5.6 Rebuild and redeploy the image

```bash
# on chandra-1, after pulling latest main (including this port + v0.48.1 hotfix)
cd /opt/Devel/Kryten-Ecosystem/kryten-webqueue
git fetch --all --tags
git checkout <new-tag>   # whatever version this port ships as
sudo podman build -f Containerfile -t localhost/kryten-webqueue:latest .
```
Then install the Quadlet units and start the service per
`docs/postgres-cutover.md` §"Step 5: Start Podman Quadlet on Chandra-1".

---

## 6. Safety reminders

- **Never point nginx at chandra-1 until the smoke tests in §5.4 pass directly against the
  container.** Reverting nginx is the fast rollback if something goes wrong post-cutover —
  see `docs/postgres-cutover.md` §3 for the exact rollback commands.
- **The economy/spend-request and playlist-scheduling logic in the `queue` domain is the
  highest blast-radius code in this port.** Real money and live playback scheduling depend
  on it. Do not rush the `datetime(fire_at, ...)` interval-arithmetic translation — test it
  explicitly with schedules due in the near future vs. far future before trusting it.
- **Do not delete/rename the SQLite domain classes or the `_layout` branch in
  `Database.__init__`.** `grindhouse.local` continues running SQLite in production during
  and after this work; both code paths must coexist and both must stay green in the test
  suite.
- If anything here is unclear or a method/table has changed since this was written, treat
  `_DOMAIN_METHOD_MAP` in `kryten_webqueue/catalog/db/__init__.py` and the live chandra-1
  schema (`\dt catalog.*`, `\dt queue.*`, etc. via `psql`) as the sources of truth, not this
  document's method lists.
