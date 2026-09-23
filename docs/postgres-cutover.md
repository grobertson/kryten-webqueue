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

Verify local health inside `chandra-1`:
```bash
curl -I http://127.0.0.1:2010/auth/login
curl -s http://127.0.0.1:2010/queue/next-schedule
```

### Step 6: Update Grindhouse Nginx Reverse Proxy Across Network
The public website `https://queue.dropsugar.co/` terminates SSL via Let's Encrypt Nginx on `grindhouse.local`. Update its upstream to proxy across the LAN to `chandra-1.local:2010`.

On `grindhouse.local`:
1. Edit `/etc/nginx/sites-available/queue.conf`:
```nginx
    # WebSocket upgrade
    location /ws {
        proxy_pass http://chandra-1.local:2010;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }

    # API + pages
    location / {
        proxy_pass http://chandra-1.local:2010;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
```

2. Test and reload Nginx:
```bash
sudo nginx -t
sudo systemctl reload nginx
```

### Step 7: Public Smoke Testing & Verification
Test public endpoints through the reverse proxy:
```bash
curl -I https://queue.dropsugar.co/auth/login
curl -s https://queue.dropsugar.co/queue/next-schedule
```
Monitor logs on `chandra-1`:
```bash
sudo journalctl -u webqueue-app.service -f
```

---

## 3. Rollback & Forward Repair Policy

- **Pre-Cutover Failure**: If ETL validation fails, container startup fails on `chandra-1`, or initial smoke tests fail before Nginx is updated:
  1. Do not update or revert Nginx on `grindhouse.local` (`proxy_pass http://127.0.0.1:2010;`).
  2. Restart `kryten-webqueue.service` on `grindhouse.local`:
     ```bash
     sudo systemctl start kryten-webqueue.service
     ```
  3. The SQLite databases remain untouched and consistent.
- **Traffic Rollback (if Nginx was already switched but app has issues)**:
  1. Revert `/etc/nginx/sites-available/queue.conf` on `grindhouse.local` to point back to `http://127.0.0.1:2010`.
  2. Reload Nginx: `sudo nginx -t && sudo systemctl reload nginx`.
  3. Start `kryten-webqueue.service` on `grindhouse.local`.
- **Post-Cutover Forward Repair**: Once production traffic is accepted by PostgreSQL (user logins, new spend requests, play completions), recovery is a **forward repair in PostgreSQL**. The retained SQLite snapshot is preserved read-only for forensics.
