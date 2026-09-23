# PRD: PostgreSQL & Chandra-1 Podman Migration

**Sprint**: `postgres-migration`
**Status**: Planned - Post SQLite Domain Separation
**Builds on**: `sqlite-domain-separation`
**Target version**: `0.48.0`
**Workflow**: [../../AGENT-WORKFLOW-GUIDE.md](../../AGENT-WORKFLOW-GUIDE.md)
**Parent Plan**: [../DATABASE_ARCHITECTURE_PLAN.md](../DATABASE_ARCHITECTURE_PLAN.md)

---

## 1. Executive Summary

Following SQLite domain separation, `kryten-webqueue` will move its application data to one
PostgreSQL 16+ database, `webqueue`, on `chandra-1`. The database has five logical schemas:
`catalog`, `queue`, `jobs`, `users`, and `tmdb`. The application will use SQLAlchemy 2.0 async
sessions backed by `asyncpg`, with PostgreSQL full-text search and trigram matching replacing
SQLite FTS5.

The cutover is a planned maintenance-window migration. SQLite remains an immutable recovery
snapshot for one release, but production writes accepted after cutover are repaired forward in
PostgreSQL; the system does not silently fall back to a divergent SQLite copy.

## 2. Decisions and Boundaries

The following decisions are fixed for this sprint:

- One PostgreSQL database named `webqueue` owns all five schemas. `tmdb` is a schema, not a
  separate database, so catalog enrichment can use relational queries when needed.
- The application runs as a rootful Podman Quadlet service on `chandra-1`.
- PostgreSQL is host-managed on `chandra-1`. The rootful application container connects through
  an explicitly configured host-gateway address; it must not use `localhost`.
- The migration uses a maintenance window and a forward-fix rollback policy. There is no
  dual-write phase and no automatic return to SQLite after PostgreSQL accepts writes.
- Existing api-gate, MediaCMS, and CyTube integration contracts are unchanged. This sprint does
  not add direct service-to-service communication paths.

## 3. Goals and Success Metrics

- Preserve all application records and identity values during migration without external
  TMDB/OMDB calls.
- Remove SQLite writer starvation between independent workloads after the SQLite partitioning
  release, then move to PostgreSQL MVCC and pooled connections.
- Preserve public and administrative response shapes, queue semantics, OTP single-use behavior,
  device-key behavior, and audit history.
- Provide typo-tolerant search while removing FTS5 parser failures.
- Complete staging rehearsal and production cutover with source/destination parity, foreign-key
  validation, application smoke tests, and no unplanned data loss.

Success is measured by zero unhandled database errors during the concurrency test suite, no
migration verification mismatch, successful health/browse/search/queue smoke tests, and a
recorded cutover checkpoint that can be restored for investigation.

## 4. Target Architecture

### 4.1 Schema Ownership

| Schema | Tables and responsibility |
| --- | --- |
| `catalog` | Catalog mirror, FTS source data, categories, tags, people, studios, enrichment state, edit log, sync log, MOTD overrides. |
| `queue` | Queue shadow, spend requests, queue history, playlists, schedules, completions, and blackouts. |
| `jobs` | Job schedules, job runs, run logs, and fetch queue. |
| `users` | OTPs, device link codes, device API keys, watchlists, feedback, and title suggestions. |
| `tmdb` | Persistent TMDB dump index and index metadata. |

Schema ownership organizes privileges, migrations, and repository modules. It does not imply
separate transactions: a single SQLAlchemy session may atomically update related WebQueue
schemas. External economy and CyTube actions remain distributed operations and require
idempotency and reconciliation rather than a cross-system transaction.

### 4.2 Access Model

- All application repository I/O uses SQLAlchemy 2.0 async sessions and parameterized named
  binds. Raw `asyncpg` calls and mechanical `?` to `$n` translation are out of scope.
- Repository methods retain their public return shapes. A request or job defines the transaction
  boundary; helpers must not commit independently unless explicitly documented as standalone.
- Migrations use a dedicated migrator role and a PostgreSQL advisory lock. Application instances
  never race to apply DDL during startup.
- The runtime role receives only application DML privileges. The migration role owns schema DDL.
  The scheduled log-pruner uses a restricted maintenance role that can delete only from
  `jobs.job_run_logs`.

### 4.3 Configuration and Secrets

`database.backend` is `sqlite` or `postgres`. The transitional SQLite configuration explicitly
selects `monolith` or `partitioned`; startup rejects ambiguous layouts and rejects an unmigrated
legacy database instead of creating empty default files.

For PostgreSQL, connection resolution is `dsn_env`, then a password-free `dsn`, then assembled
host/port/user/database values. `KRYTEN_WEBQUEUE_PG_PASSWORD` is the only password source.
Configuration files must not contain a password or a DSN with an embedded password. The
container receives its database settings through a root-owned, mode-0600 environment file.

### 4.4 Container Connectivity

The rootful Quadlet supplies the host-gateway mapping and configures the database host as that
mapping (for example, `host.containers.internal` mapped to `host-gateway`). `localhost:5432` is
not valid for the application container unless PostgreSQL is deliberately moved into the same
pod, which is not part of this sprint. Deployment verification includes a connection test from
inside the running application container.

## 5. PostgreSQL Design

### 5.1 Migrations and Types

PostgreSQL DDL lives in ordered, immutable migration files with a tracked schema-version table.
The initial migration is a clean PostgreSQL baseline; it does not replay the SQLite migration
history or its historic data mutations. Each foreign key is schema-qualified, and migration tests
validate constraints, unique indexes, cascade behavior, and application-required indexes.

SQLite timestamps are interpreted as UTC during ETL. The ETL preflight rejects unparseable
values rather than guessing. Columns designated as structured JSON are validated before writing
`jsonb`; free-form log and detail fields remain `text` unless a separate compatibility decision
changes their contract. Identity values are imported with `OVERRIDING SYSTEM VALUE`, then every
identity sequence is reset with correct empty-table semantics.

### 5.2 Search

`catalog.catalog` stores a generated English `tsvector` with title-weighted ranking and a GIN
index. `pg_trgm` supplies a title trigram index and a fuzzy fallback with an initial similarity
threshold of `0.3`. Search uses `websearch_to_tsquery` with bound parameters.

`catalog_fts` is a derived SQLite-only index. It is neither copied nor checksum-compared during
ETL. PostgreSQL search is rebuilt from `catalog.catalog` and verified against exact, multi-word,
fuzzy, and adversarial-input fixtures.

### 5.3 Retention

Only `jobs.job_run_logs` is eligible for retention deletion. The daily 04:00 UTC task deletes
bounded batches using an index on `logged_at`, records a metric and audit log with the deleted
row count, and retries on transient database failures. It never deletes `queue.spend_requests`,
`queue.queue_history`, `users.feedback`, `users.title_suggestions`, `catalog.item_edit_log`, or
records owned by other services.

## 6. ETL and Verification

The ETL reads partitioned SQLite files read-only after the source service and schedulers have
stopped. It creates a fresh PostgreSQL target, streams tables in foreign-key-safe order, and
never calls an external API. It writes only to a staging target that has passed migrations.

Verification is deterministic for every source table:

1. Compare row counts.
2. Compare canonical hashes over stable primary-key ordering, normalized UTC timestamps, and
   canonical JSON representations.
3. Validate foreign keys, uniqueness, and sequence next values in PostgreSQL.
4. Rebuild and test derived search indexes.
5. Run application-level fixtures for browse, recently-played suppression, watchlist ordering,
   queue history, OTP redemption, device-key authentication, jobs, and search.

The source is captured with a SQLite backup/checkpoint-safe procedure while the service is
stopped. Target files are created in a staging location for the SQLite split release; the source
is never renamed or deleted automatically. Archival requires an explicit operator action after
verification.

## 7. Cutover and Recovery

1. Rehearse the complete migration against a representative production snapshot and record
   timings, hashes, and failures.
2. Build and verify the rootful Quadlet image and host-gateway database connectivity on
   `chandra-1`.
3. Enter maintenance mode; stop WebQueue, its scheduler, and any writer that can touch the
   SQLite files.
4. Capture a verified SQLite backup, run PostgreSQL migrations once with the migrator role, and
   run the ETL into a fresh target.
5. Require all verification gates to pass before granting the runtime/pruner roles and starting
   the Quadlet service.
6. Run health, browse, search, queue, admin, OTP, device-key, and job-log smoke tests. Monitor
   connection-pool saturation, query latency, database errors, and scheduler health.
7. Preserve the SQLite snapshot read-only for one release. After PostgreSQL accepts a write,
   recovery is a forward repair in PostgreSQL using recorded request identifiers and audit data;
   reverting to SQLite is not an automatic rollback path.

A failed pre-cutover verification returns to the unchanged SQLite service. A post-cutover defect
is triaged as a forward fix; any manual recovery must explicitly account for writes accepted by
PostgreSQL.

## 8. Dependencies and Security

Runtime dependencies are `sqlalchemy>=2.0.30`, `asyncpg>=0.29.0`, and `greenlet>=3.0`.
PostgreSQL 16+ must provide `pg_trgm`; extension installation is performed by an administrator,
not the application runtime role.

Device-key hashes, OTPs, and session/auth behavior are preserved. TLS is required for a remote
PostgreSQL endpoint; the approved host-local connection uses the configured host-gateway and
host firewall restrictions. Backup ownership, retention, and restore testing are operational
requirements before production cutover.

## 9. Explicit Non-Goals

- Redis caching, vector search, recommender features, and cross-service live database joins.
- A dual-write migration or automatic rollback to a stale SQLite database.
- Raw single-row speed as the primary success metric.

## 10. Release Acceptance Criteria

- One canonical `webqueue` database contains the five approved schemas.
- SQLite configuration prevents accidental empty-database startup during transition.
- All migrations, ETL verification, and application fixture checks pass in rehearsal.
- The runtime uses SQLAlchemy async sessions; migrations and pruning use least-privilege roles.
- The Quadlet app connects to host-managed PostgreSQL without relying on container-localhost.
- Search, queue/payment reconciliation, retention, observability, and forward-recovery procedures
  are documented and tested before the production cutover.
