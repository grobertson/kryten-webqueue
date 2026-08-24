# kryten-webqueue

Netflix/Tubi-style catalog browser and pay-to-play queue management for CyTube.

## Architecture

```
Browser → nginx (queue.dropsugar.co) → kryten-webqueue (:2010) → kryten-api-gate (:24444) → NATS services
```

- **HTTP-only** — webqueue never touches NATS directly
- **SQLite** for all local state (catalog, OTPs, queue shadow, playlists, schedules)
- **WebSocket** for real-time queue updates
- **Polling** api-gate every 3 seconds for playlist state

## Quick Start

```bash
# Install
python -m venv .venv
.venv/bin/pip install -e .

# Configure
cp config.example.json /etc/kryten-webqueue/config.json
# Edit config.json with your secrets

# Run
WQ_CONFIG=/etc/kryten-webqueue/config.json python -m kryten_webqueue
```

## Configuration

See `config.example.json` for all options. Required secrets:

| Key | Description |
|-----|-------------|
| `secret_key` | Random string for JWT signing (≥32 chars) |
| `api_gate_token` | Bearer token for kryten-api-gate |
| `mediacms_token` | API token for MediaCMS catalog sync |

## Deployment

```bash
# Install service
sudo cp deploy/kryten-webqueue.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kryten-webqueue

# nginx
sudo cp deploy/nginx-queue.conf /etc/nginx/sites-available/queue.dropsugar.co
sudo ln -s /etc/nginx/sites-available/queue.dropsugar.co /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## API Endpoints

### Auth
- `POST /auth/otp/request` — Request OTP (delivered via PM)
- `POST /auth/otp/verify` — Verify OTP, get session cookie
- `POST /auth/logout` — Clear session
- `GET /auth/me` — Current user info

### Catalog
- `GET /catalog/browse` — Paginated browse (with category filter)
- `GET /catalog/search?q=...` — Full-text search
- `GET /catalog/item/{token}` — Item detail
- `GET /catalog/categories` — List categories

### Queue
- `GET /queue/state` — Current queue state
- `POST /queue/add` — Add item (FIFO pay-queue)
- `POST /queue/playnext` — Add as play-next (premium)
- `GET /queue/preview?friendly_token=...&tier=...` — Cost preview
- `GET /queue/history` — User's queue history

### User
- `GET /user/balance` — Economy balance
- `GET /user/transactions` — Transaction history
- `GET /user/profile` — Profile info

### Admin (rank ≥ 3)
- `GET/POST/PUT/DELETE /admin/playlists/...` — Saved playlists CRUD
- `GET/POST/PUT/DELETE /admin/schedules/...` — Schedule CRUD
- `POST /admin/schedules/{id}/fire` — Manual fire
- `POST /admin/queue/clear` — Clear queue (refunds pay items)
- `DELETE /admin/queue/{uid}` — Remove item
- `POST /admin/queue/sync-now` — Trigger catalog sync

### WebSocket
- `ws://host/ws` — Real-time queue updates (auth via session cookie)

### Public API (third-party clients)
- `POST /api/public/v1/link` — Exchange a one-time device code for an API key
- `GET /api/public/v1/current` — Now playing
- `GET /api/public/v1/queue` — Live queue with predicted start times
- `GET /api/public/v1/events` — Upcoming scheduled events

Key-authenticated (`Authorization: Bearer <key>`) endpoints for Smart TV /
tablet apps. Users link a device at `/link`. Full contract, JSON schemas, and a
developer's guide: [docs/PUBLIC_API.md](docs/PUBLIC_API.md).

## Dependencies

- Python ≥ 3.12
- kryten-api-gate ≥ 0.3.6
- kryten-py ≥ 0.16.1 (upstream, not a direct dependency)
- kryten-economy ≥ 0.8.11 (upstream, not a direct dependency)

## License

Proprietary — DropSugar / Q&A
