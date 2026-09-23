# PostgreSQL Cutover Runbook: Kryten-WebQueue

**Target Version**: `0.48.0`  
**Host Target**: `chandra-1` (PostgreSQL 16 + Podman Quadlet)  
**Source Host**: `grindhouse.local` (pipx systemd / SQLite partitioned data)  
**Parent Plan**: [DATABASE_ARCHITECTURE_PLAN.md](DATABASE_ARCHITECTURE_PLAN.md)  
**PRD**: [postgres-migration/PRD-postgres-migration.md](postgres-migration/PRD-postgres-migration.md)  

---

## 1. Pre-Flight Checklist

- [ ] `chandra-1` has PostgreSQL 16+ active and `pg_trgm` extension installed.
- [ ] Database `webqueue` exists with owner `kryten`.
- [ ] Schema `001_initial_schema.sql` applied cleanly across `catalog`, `queue`, `jobs`, `users`, `tmdb`.
- [ ] Privileges granted to `kryten`:
  ```sql
  GRANT USAGE, CREATE ON SCHEMA catalog, queue, jobs, users, tmdb TO kryten;
  GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA catalog, queue, jobs, users, tmdb, public TO kryten;
  GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA catalog, queue, jobs, users, tmdb, public TO kryten;
  ALTER DEFAULT PRIVILEGES IN SCHEMA catalog, queue, jobs, users, tmdb GRANT ALL ON TABLES TO kryten;
  ALTER DEFAULT PRIVILEGES IN SCHEMA catalog, queue, jobs, users, tmdb GRANT ALL ON SEQUENCES TO kryten;
  ```
- [ ] Podman image built on `chandra-1`:
  ```bash
  sudo podman build -f Containerfile -t localhost/kryten-webqueue:latest .
  ```
- [ ] Environment file `/etc/kryten/webqueue/webqueue.env` created on `chandra-1`:
  ```env
  KRYTEN_WEBQUEUE_PG_PASSWORD=kryten_secret_password
  ```

---

## 2. Maintenance Window Cutover (Step-by-Step)

### Step 1: Stop Source Service & Freeze Writes
On `grindhouse.local`:
```bash
sudo systemctl stop kryten-webqueue.service
sudo systemctl status kryten-webqueue.service --no-pager
```

### Step 2: Checkpoint SQLite WAL & Create Final Snapshot
On `grindhouse.local`:
```bash
SNAPSHOT_DIR="/var/lib/kryten-webqueue/backups/cutover_$(date +%Y%m%d_%H%M%S)"
sudo mkdir -p "$SNAPSHOT_DIR"

for db in catalog queue jobs users; do
  sudo sqlite3 "/var/lib/kryten-webqueue/data/$db.sqlite3" "PRAGMA wal_checkpoint(TRUNCATE);"
done

sudo cp -av /var/lib/kryten-webqueue/data "$SNAPSHOT_DIR/"
sudo cp -av /etc/kryten-webqueue/config.json "$SNAPSHOT_DIR/"
```

### Step 3: Stream SQLite Data to PostgreSQL on Chandra-1
Run the ETL migration script with `--verify`:
```bash
sudo -u kryten /home/kryten/.local/pipx/venvs/kryten-webqueue/bin/python -m kryten_webqueue.migrate_sqlite_to_pg \
  --data-dir /var/lib/kryten-webqueue/data \
  --pg-dsn "postgresql://kryten:kryten_secret_password@chandra-1.local:5432/webqueue"
```
Verify `postgres_migration_report.json` indicates `"status": "success"` and 0 errors.

### Step 4: Transfer Media & Image Assets to Chandra-1
Sync cover art images to `/var/lib/kryten/media`:
```bash
rsync -avz /var/lib/kryten-webqueue/images/ groberts@chandra-1.local:/var/lib/kryten/media/images/
```

### Step 5: Start Podman Quadlet on Chandra-1
On `chandra-1`:
```bash
sudo cp deploy/podman/webqueue/webqueue.network /etc/containers/systemd/
sudo cp deploy/podman/webqueue/webqueue-app.container /etc/containers/systemd/
sudo systemctl daemon-reload
sudo systemctl start webqueue-app.service
sudo systemctl status webqueue-app.service --no-pager
```

### Step 6: Smoke Testing & Health Verification
```bash
curl -I http://127.0.0.1:2010/auth/login
curl -s http://127.0.0.1:2010/queue/next-schedule
```

---

## 3. Rollback & Forward Repair Policy

- **Pre-Cutover Failure**: If ETL validation fails or container startup fails before DNS/traffic cutover, immediately restart `kryten-webqueue.service` on `grindhouse.local`. SQLite databases remain untouched and consistent.
- **Post-Cutover Failure**: Once traffic is switched and PostgreSQL accepts writes (new spend requests, logins, completions), recovery is a **forward repair in PostgreSQL**. The retained SQLite snapshot is preserved read-only for forensics.
