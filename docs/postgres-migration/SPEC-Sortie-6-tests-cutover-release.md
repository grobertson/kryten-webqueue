# SPEC — Sortie 6: 30-Day Log Pruner, Chandra-1 Podman Unit & Cutover

**Sprint**: `postgres-migration`
**PRD**: [PRD-postgres-migration.md](PRD-postgres-migration.md)
**Depends on**: Sorties 1–5
**Estimated**: 4–5 h

---

## 1. Overview

Finalize the PostgreSQL migration: implement the automated **30-day job log pruner** with strict exemptions, package the **Podman container configuration** for deployment on `chandra-1`, configure test suites for PostgreSQL validation, and write the operational cutover runbook.

## 2. Scope and Non-Goals

**In scope**
- Automated 30-day log pruning job for `jobs.job_run_logs` (registered with `JobScheduler`).
- Explicit unit and integration test assertions verifying that financial (`spend_requests`, `queue_history`), feedback, and audit tables are **never** pruned.
- Podman container units: `deploy/podman/webqueue/webqueue.network` and `webqueue-app.container`.
- Cutover runbook (`docs/postgres-cutover.md`), version bump, and CHANGELOG updates.

**Non-goals**
- Redis read-through cache (future performance sprint).
- Vector recommender model integration.

## 3. Requirements

### 3.1 30-Day Job Run Log Retention Policy
- Implement `job_log_prune_job` registered in `jobs/tasks.py`:
  ```sql
  DELETE FROM jobs.job_run_logs
  WHERE logged_at < NOW() - INTERVAL '30 days';
  ```
- Add an index on `jobs.job_run_logs(logged_at)` and execute the delete in bounded batches so the task does not create long-running locks or vacuum pressure.
- Seed the default schedule in a scheduler explicitly configured for UTC: daily at `04:00 UTC` (`0 4 * * *`).
- Run the pruner under a restricted maintenance role with `DELETE` permission only on `jobs.job_run_logs`; emit a metric and audit record with the number of rows deleted.
- **CRITICAL COMPLIANCE CONSTRAINT**: The pruner must execute strictly against `jobs.job_run_logs`. Under no circumstances may it touch:
  - Chat logs (in `kryten-userstats` / `kryten-robot`)
  - Economy or spend requests (`queue.spend_requests`, `queue.queue_history`)
  - User feedback or title suggestions (`users.feedback`, `users.title_suggestions`)
  - Item edit audit logs (`catalog.item_edit_log`)

### 3.2 Chandra-1 Podman Topology
- Containerfile builds with Python 3.12, Hatchling, and non-root user `kryten`.
- Rootful Quadlet configuration, managed with `sudo systemctl` rather than `systemctl --user`:
  - `webqueue.network` (internal Podman bridge network).
  - `webqueue-app.container` mounting `/var/lib/kryten/media` for cover art and TMDB dumps, with an explicit `host-gateway` mapping such as `host.containers.internal`.
  - A root-owned mode-0600 environment file supplies non-secret endpoint settings and `KRYTEN_WEBQUEUE_PG_PASSWORD`; configuration must not use `localhost` for host-managed PostgreSQL.
  - The runtime container health check proves it can connect to PostgreSQL through the configured host gateway.

### 3.3 Cutover Runbook
Step-by-step procedure:
```bash
# 1. Rehearse this process against a production-like snapshot, then enter maintenance mode.
#    Stop WebQueue, its scheduler, and every SQLite writer before capturing the source.

# 2. Bootstrap PostgreSQL on chandra-1 with the migrator role.
sudo -u postgres psql -d webqueue -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"

# 3. Run DDL migrations once under the migrator lock.
python -m kryten_webqueue.catalog.db.migrate --backend postgres

# 4. Stream a checkpoint-safe, read-only SQLite capture into an empty staging target.
python -m kryten_webqueue.migrate_sqlite_to_pg --data-dir /path/to/sqlite/data --verify

# 5. Grant runtime/pruner roles only after verification, then start the rootful Quadlet.
sudo systemctl start webqueue-app.service

# 6. Verify health and smoke test browse/search/queue/OTP/device-key/job-log paths.
curl -f http://127.0.0.1:2010/api/public/v1/state
```

If a gate fails before PostgreSQL receives production writes, restart the unchanged SQLite
service. Once PostgreSQL accepts a write, recovery is a forward repair using request identifiers
and audit records; the retained SQLite snapshot is not an automatic rollback target.

## 4. Implementation Plan

1. **Create** `job_log_prune` task in `kryten_webqueue/jobs/tasks.py`.
2. **Add** `deploy/podman/webqueue/` Podman container definitions and systemd unit files.
3. **Write** comprehensive tests verifying retention logic and financial/chat data immutability.
4. **Create** `docs/postgres-cutover.md`.
5. **Update** `CHANGELOG.md` and bump version to `0.48.0`.

## 5. Acceptance Criteria

- [ ] `job_log_prune` automatically cleans `jobs.job_run_logs` older than 30 days.
- [ ] Tests prove 0 financial or user records are ever affected by log pruning.
- [ ] Pruning is batched, indexed, metered, and uses a role unable to delete protected tables.
- [ ] Rootful Podman container starts up and connects to host-managed `webqueue` PostgreSQL through the configured host gateway.
- [ ] Cutover runbook verified with `--verify` validation exiting 0.
- [ ] A full rehearsal records deterministic parity, smoke-test results, and forward-recovery ownership.
- [ ] All linters, type checks, and tests pass green (`black`, `ruff`, `mypy`, `pytest`).
