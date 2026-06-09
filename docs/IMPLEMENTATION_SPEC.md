                        # kryten-webqueue — Implementation Specification

**Version:** 1.0  
**Date:** 2026-05-29  
**Scope:** Phase 1 + Phase 2 (full implementation guide)  
**Prerequisite:** `PRE_PLAN_GAPS.md` gaps resolved as specified

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Configuration](#2-configuration)
3. [App Factory & Startup](#3-app-factory--startup)
4. [Auth Module](#4-auth-module)
5. [API Gate Client](#5-api-gate-client)
6. [Catalog Module](#6-catalog-module)
7. [Queue Module](#7-queue-module)
8. [WebSocket Server](#8-websocket-server)
9. [Playlists & Scheduler](#9-playlists--scheduler)
10. [Economy Client](#10-economy-client)
11. [Routes](#11-routes)
12. [Rate Limiting](#12-rate-limiting)
13. [Database & Migrations](#13-database--migrations)
14. [Background Workers](#14-background-workers)
15. [Error Handling](#15-error-handling)
16. [Deployment & Operations](#16-deployment--operations)

---

## 1. Architecture Overview

### Constraint: HTTP-only

webqueue has **no NATS connection**. All ecosystem communication is via HTTP:

```
┌──────────────────────────────────────────────────────┐
│                       Browser                         │
│  (Alpine.js + WebSocket)                             │
└───────────────┬──────────────────────┬───────────────┘
                │ HTTP/WS              │
                ▼                      │
┌───────────────────────────┐          │
│       nginx :443          │          │
│  /images/ → filesystem    │          │
│  /        → :2010         │          │
└───────────┬───────────────┘          │
            │                          │
            ▼                          │
┌───────────────────────────────────────────────────────┐
│                kryten-webqueue :2010                    │
│                                                        │
│  FastAPI HTTP + WebSocket                              │
│  SQLite (catalog, queue_shadow, playlists, sessions)  │
│  APScheduler (sync, fire, expiry, poll)               │
│  httpx async client                                    │
└───────────┬─────────────────────────┬─────────────────┘
            │ HTTP                    │ HTTP
            ▼                         ▼
┌───────────────────┐    ┌──────────────────────────────┐
│ kryten-api-gate   │    │ MediaCMS                      │
│ :24444            │    │ https://www.dropsugar.com     │
│                   │    │ (catalog sync only)           │
│ - playlist CRUD   │    └──────────────────────────────┘
│ - state queries   │
│ - chat PM         │
│ - economy proxy   │
└───────────────────┘
```

### Key design decisions

| Decision | Rationale |
|---|---|
| No NATS | webqueue is a web app, not a microservice. Simpler deployment, fewer failure modes. |
| Poll for state | `GET /state/playlist` and `GET /state/now-playing` every 3 seconds. Acceptable latency for a queue display. |
| Own mutations bypass poll | When webqueue adds/moves/removes items, it updates its shadow immediately without waiting for next poll. |
| SQLite for everything | Single-file DB for catalog, sessions, OTPs, queue shadow, playlists. No external DB dependency. |
| Queue lock (asyncio.Lock) | Serializes add→move sequences to prevent FIFO ordering races. |

### Move semantics (critical)

`PUT /playlist/{uid}/move` accepts `position` as:
- **integer UID** — place item after the item with this UID
- **`"prepend"`** — place item at position 0 (before all others)
- **`"append"`** — place item at the end

The FIFO algorithm uses:
```python
if last_pay_uid is not None:
    position = last_pay_uid       # place after last pay item
else:
    position = "prepend"          # no pay items yet → front of queue
```

---

## 2. Configuration

**File:** `kryten_webqueue/config.py`

```python
from pathlib import Path
from pydantic import BaseModel, Field
import json


class Config(BaseModel):
    """Application configuration loaded from JSON file."""

    # Server
    channel: str = "Q_A"
    host: str = "0.0.0.0"
    port: int = 2010
    secret_key: str
    session_ttl_hours: int = 24

    # API Gate
    api_gate_url: str = "http://127.0.0.1:24444"
    api_gate_token: str

    # MediaCMS
    mediacms_url: str = "https://www.dropsugar.com"
    mediacms_token: str

    # Cover art APIs
    tmdb_api_key: str = ""
    omdb_api_key: str = ""

    # Database
    db_path: str = "/var/lib/kryten-webqueue/webqueue.db"

    # Images
    image_dir: str = "/var/lib/kryten-webqueue/images"
    placeholder_dir: str = "/var/lib/kryten-webqueue/images/placeholders"

    # Scheduling
    catalog_sync_interval_hours: int = 4
    pre_fire_lock_minutes_default: int = 15
    state_poll_interval_sec: float = 3.0

    # Monitoring
    prometheus_port: int = 28292

    @classmethod
    def from_file(cls, path: str | Path) -> "Config":
        with open(path) as f:
            return cls(**json.load(f))
```

**Removed from product plan config:** `nats_url` — not needed.

**Added:** `state_poll_interval_sec` — controls how often webqueue polls api-gate for playlist/now-playing state.

---

## 3. App Factory & Startup

**File:** `kryten_webqueue/app.py`

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

from .config import Config
from .api_gate.client import ApiGateClient
from .catalog.db import Database
from .queue.shadow import QueueShadow
from .queue.poller import StatePoller
from .ws.manager import WebSocketManager
from .playlists.scheduler import init_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: Config = app.state.config
    db = Database(config.db_path)
    await db.connect()
    await db.run_migrations()

    api_gate = ApiGateClient(config.api_gate_url, config.api_gate_token)
    ws_manager = WebSocketManager()
    shadow = QueueShadow(db, ws_manager)
    poller = StatePoller(api_gate, shadow, interval=config.state_poll_interval_sec)

    app.state.db = db
    app.state.api_gate = api_gate
    app.state.ws_manager = ws_manager
    app.state.shadow = shadow
    app.state.poller = poller

    # Start background tasks
    poller.start()
    init_scheduler(app)

    yield

    # Shutdown
    poller.stop()
    await api_gate.close()
    await db.close()


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="kryten-webqueue", lifespan=lifespan)
    app.state.config = config

    from .routes import auth, catalog, queue, user, admin_playlists, admin_schedules, admin_queue
    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(catalog.router, prefix="/catalog", tags=["catalog"])
    app.include_router(queue.router, prefix="/queue", tags=["queue"])
    app.include_router(user.router, prefix="/user", tags=["user"])
    app.include_router(admin_playlists.router, prefix="/admin/playlists", tags=["admin"])
    app.include_router(admin_schedules.router, prefix="/admin/schedules", tags=["admin"])
    app.include_router(admin_queue.router, prefix="/admin/queue", tags=["admin"])

    from .ws.queue import ws_router
    app.include_router(ws_router)

    return app
```

**Entry point** (`__main__.py`):
```python
import os, uvicorn
from .config import Config
from .app import create_app

config_path = os.environ.get("WQ_CONFIG", "/etc/kryten-webqueue/config.json")
config = Config.from_file(config_path)
app = create_app(config)
uvicorn.run(app, host=config.host, port=config.port)
```

---

## 4. Auth Module

### OTP Flow

**Files:** `kryten_webqueue/auth/otp.py`, `kryten_webqueue/auth/session.py`

OTPs are generated and stored locally in SQLite (not NATS KV). The full lifecycle is owned by webqueue.

#### OTP table schema

```sql
CREATE TABLE otps (
    username    TEXT NOT NULL,
    code        TEXT NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at  TIMESTAMP NOT NULL,
    used        BOOLEAN NOT NULL DEFAULT 0
);
CREATE INDEX idx_otps_username ON otps(username);
```

#### Flow

```
1. POST /auth/request-otp  { "username": "bob" }
2. webqueue generates 6-digit OTP
3. Stores in SQLite: (username="bob", code="123456", expires_at=now+5min)
4. Calls api-gate: POST /chat/pm { "username": "bob", "message": "Your login code: 123456" }
5. Returns 200 { "message": "OTP sent" }

6. POST /auth/verify-otp { "username": "bob", "code": "123456" }
7. webqueue looks up OTP in SQLite: WHERE username=? AND code=? AND used=0 AND expires_at > now
8. If valid: mark used=1, issue JWT session cookie
9. If invalid: return 401
```

#### OTP generation

```python
import secrets

def generate_otp() -> str:
    """Generate a 6-digit numeric OTP."""
    return f"{secrets.randbelow(1000000):06d}"
```

### Session (JWT cookie)

**File:** `kryten_webqueue/auth/session.py`

```python
import jwt
from datetime import datetime, timedelta, UTC

def issue_session(username: str, secret_key: str, ttl_hours: int) -> str:
    """Issue a signed JWT for the session cookie."""
    payload = {
        "sub": username,
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(hours=ttl_hours),
    }
    return jwt.encode(payload, secret_key, algorithm="HS256")


def verify_session(token: str, secret_key: str) -> str | None:
    """Verify JWT and return username, or None if invalid/expired."""
    try:
        payload = jwt.decode(token, secret_key, algorithms=["HS256"])
        return payload["sub"]
    except jwt.InvalidTokenError:
        return None
```

**Cookie settings:**
```python
response.set_cookie(
    key="session",
    value=token,
    httponly=True,
    secure=True,
    samesite="strict",
    max_age=ttl_hours * 3600,
)
```

### Rank lookup

On every privileged action, webqueue verifies the user's CyTube rank:

```python
async def get_user_rank(api_gate: ApiGateClient, username: str) -> int:
    """Fetch user rank from api-gate. Returns 0 if user not online."""
    result = await api_gate.get(f"/state/user/{username}")
    if not result:
        return 0
    return result.get("rank", 0)
```

Rank thresholds:
- Rank ≥ 1: Can use pay-to-play
- Rank ≥ 3: Admin panel access

---

## 5. API Gate Client

**File:** `kryten_webqueue/api_gate/client.py`

A thin httpx async wrapper for all kryten-api-gate calls.

```python
import httpx
from typing import Any


class ApiGateClient:
    """HTTP client for kryten-api-gate."""

    def __init__(self, base_url: str, token: str):
        self._base_url = base_url.rstrip("/") + "/api/v1"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def close(self):
        await self._client.aclose()

    async def get(self, path: str, **params) -> dict:
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def post(self, path: str, json: dict | None = None) -> dict:
        resp = await self._client.post(path, json=json)
        resp.raise_for_status()
        return resp.json()

    async def put(self, path: str, json: dict | None = None) -> dict:
        resp = await self._client.put(path, json=json)
        resp.raise_for_status()
        return resp.json()

    async def delete(self, path: str) -> dict:
        resp = await self._client.delete(path)
        resp.raise_for_status()
        return resp.json()

    # --- Typed convenience methods ---

    async def get_playlist(self) -> list[dict]:
        result = await self.get("/state/playlist")
        return result.get("items", [])

    async def get_now_playing(self) -> dict:
        return await self.get("/state/now-playing")

    async def get_user(self, username: str) -> dict:
        return await self.get(f"/state/user/{username}")

    async def playlist_add(self, media_type: str, media_id: str, *, position: str = "end", temp: bool = True) -> dict:
        """Add item to playlist. Returns {"success": bool, "uid": int|None}."""
        return await self.post("/playlist/add", json={
            "type": media_type,
            "id": media_id,
            "position": position,
            "temp": temp,
        })

    async def playlist_move(self, uid: int, position: int | str) -> dict:
        """Move item. position is a UID (int) or "prepend"/"append"."""
        return await self.put(f"/playlist/{uid}/move", json={"position": position})

    async def playlist_delete(self, uid: int) -> dict:
        return await self.delete(f"/playlist/{uid}")

    async def playlist_clear(self) -> dict:
        return await self.delete("/playlist/")

    async def send_pm(self, username: str, message: str) -> dict:
        return await self.post("/chat/pm", json={"username": username, "message": message})

    async def get_motd(self) -> str:
        result = await self.get("/admin/motd")
        return result.get("motd", "")

    # --- Economy proxy ---

    async def get_balance(self, username: str) -> dict:
        return await self.get(f"/economy/balance/{username}")

    async def get_transactions(self, username: str, limit: int = 20, offset: int = 0) -> dict:
        return await self.get(f"/economy/transactions/{username}", limit=limit, offset=offset)

    async def queue_preview(self, username: str, duration_sec: int, tier: str = "queue") -> dict:
        return await self.post("/economy/queue-preview", json={
            "username": username,
            "duration_sec": duration_sec,
            "tier": tier,
        })

    async def queue_spend(self, username: str, duration_sec: int, tier: str, request_id: str) -> dict:
        return await self.post("/economy/queue-spend", json={
            "username": username,
            "duration_sec": duration_sec,
            "tier": tier,
            "request_id": request_id,
        })

    async def queue_refund(self, username: str, request_id: str, reason: str) -> dict:
        return await self.post("/economy/queue-refund", json={
            "username": username,
            "request_id": request_id,
            "reason": reason,
        })
```

---

## 6. Catalog Module

### Database layer

**File:** `kryten_webqueue/catalog/db.py`

Provides async CRUD over the `catalog`, `catalog_fts`, `categories`, `tags` tables.

Key queries:

```python
async def browse(self, *, category: str | None = None, page: int = 1, per_page: int = 24) -> list[dict]:
    """Paginated catalog browse with immutability exclusion."""
    query = """
        SELECT c.friendly_token, c.title, c.duration_sec, c.cover_art_path, c.manifest_url
        FROM catalog c
        WHERE c.friendly_token NOT IN (
            SELECT spi.media_id FROM saved_playlist_items spi
            JOIN saved_playlists sp ON spi.playlist_id = sp.id
            WHERE sp.is_immutable = 1 AND spi.media_type = 'cm'
        )
    """
    params = []
    if category:
        query += """
            AND c.friendly_token IN (
                SELECT cc.friendly_token FROM catalog_categories cc
                JOIN categories cat ON cc.category_id = cat.id
                WHERE cat.slug = ?
            )
        """
        params.append(category)
    query += " ORDER BY c.title ASC LIMIT ? OFFSET ?"
    params.extend([per_page, (page - 1) * per_page])
    return await self._fetch_all(query, params)


async def search(self, query_text: str, *, page: int = 1, per_page: int = 24) -> list[dict]:
    """FTS5 full-text search with immutability exclusion."""
    sql = """
        SELECT c.friendly_token, c.title, c.duration_sec, c.cover_art_path, c.manifest_url,
               rank AS relevance
        FROM catalog_fts fts
        JOIN catalog c ON c.rowid = fts.rowid
        WHERE catalog_fts MATCH ?
          AND c.friendly_token NOT IN (
              SELECT spi.media_id FROM saved_playlist_items spi
              JOIN saved_playlists sp ON spi.playlist_id = sp.id
              WHERE sp.is_immutable = 1 AND spi.media_type = 'cm'
          )
        ORDER BY rank
        LIMIT ? OFFSET ?
    """
    return await self._fetch_all(sql, [query_text, per_page, (page - 1) * per_page])


async def get_item(self, friendly_token: str) -> dict | None:
    """Single item detail. Returns None if not found or restricted."""
    sql = """
        SELECT * FROM catalog
        WHERE friendly_token = ?
          AND friendly_token NOT IN (
              SELECT spi.media_id FROM saved_playlist_items spi
              JOIN saved_playlists sp ON spi.playlist_id = sp.id
              WHERE sp.is_immutable = 1 AND spi.media_type = 'cm'
          )
    """
    return await self._fetch_one(sql, [friendly_token])


async def get_item_admin(self, friendly_token: str) -> dict | None:
    """Single item detail for admin (no immutability filter)."""
    return await self._fetch_one("SELECT * FROM catalog WHERE friendly_token = ?", [friendly_token])


async def is_restricted(self, friendly_token: str) -> bool:
    """Return True if the item appears in any immutable saved playlist."""
    sql = """
        SELECT 1 FROM saved_playlist_items spi
        JOIN saved_playlists sp ON spi.playlist_id = sp.id
        WHERE sp.is_immutable = 1
          AND spi.media_type = 'cm'
          AND spi.media_id = ?
        LIMIT 1
    """
    row = await self._fetch_one(sql, [friendly_token])
    return row is not None


# --- Queue/shadow helpers (also on the Database class) ---

async def get_last_pay_uid(self) -> int | None:
    """Return the UID of the last pay-to-play item in the shadow, or None."""
    sql = """
        SELECT uid FROM queue_shadow
        WHERE is_pay = 1
        ORDER BY position DESC
        LIMIT 1
    """
    row = await self._fetch_one(sql, [])
    return row["uid"] if row else None


async def get_shadow_position_after(self, after_uid: int) -> int:
    """Return the position index that follows the given UID in the shadow."""
    sql = "SELECT position FROM queue_shadow WHERE uid = ?"
    row = await self._fetch_one(sql, [after_uid])
    return (row["position"] + 1) if row else 0


async def save_spend_request(self, request_id: str, *, username: str, uid: int | None) -> None:
    """Persist a spend request record so refund flows can look up uid by request_id."""
    sql = """
        INSERT OR IGNORE INTO spend_requests (request_id, username, uid, created_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    """
    await self._execute(sql, [request_id, username, uid])


async def get_request_id_for_uid(self, uid: int) -> str | None:
    """Return the request_id associated with a given queue shadow uid."""
    sql = "SELECT request_id FROM spend_requests WHERE uid = ? AND refunded = 0 LIMIT 1"
    row = await self._fetch_one(sql, [uid])
    return row["request_id"] if row else None
```

### Sync worker

**File:** `kryten_webqueue/catalog/sync.py`

```python
import httpx
from datetime import datetime, UTC


class CatalogSyncWorker:
    """Background worker that syncs catalog from MediaCMS API."""

    def __init__(self, config, db, image_pipeline):
        self.mediacms_url = config.mediacms_url.rstrip("/")
        self.mediacms_token = config.mediacms_token
        self.db = db
        self.image_pipeline = image_pipeline
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Token {config.mediacms_token}"},
            timeout=30.0,
        )

    async def run_full_sync(self):
        """Full paginated sync from MediaCMS."""
        log_id = await self.db.start_sync_log()
        stats = {"seen": 0, "new": 0, "updated": 0, "errors": 0}

        try:
            page = 1
            while True:
                resp = await self._client.get(
                    f"{self.mediacms_url}/api/v1/media",
                    params={"page": page, "page_size": 50},
                )
                resp.raise_for_status()
                items = resp.json().get("results", [])
                if not items:
                    break

                for item in items:
                    stats["seen"] += 1
                    try:
                        await self._upsert_item(item, stats)
                    except Exception:
                        stats["errors"] += 1

                page += 1

            await self.db.finish_sync_log(log_id, stats, status="ok")
        except Exception:
            await self.db.finish_sync_log(log_id, stats, status="failed")
            raise

    async def _upsert_item(self, item: dict, stats: dict):
        """Insert or update a single catalog item."""
        token = item["friendly_token"]
        existing = await self.db.get_item_admin(token)

        manifest_url = f"{self.mediacms_url}/api/v1/media/{token}/manifest.json"
        row = {
            "friendly_token": token,
            "title": item["title"],
            "description": item.get("description", ""),
            "duration_sec": item.get("duration", 0),
            "manifest_url": manifest_url,
            "thumbnail_url": item.get("thumbnail_url", ""),
            "synced_at": datetime.now(UTC).isoformat(),
        }

        if existing:
            await self.db.update_catalog(token, row)
            stats["updated"] += 1
        else:
            await self.db.insert_catalog(row)
            stats["new"] += 1

        # Fetch cover art if not present
        if not existing or not existing.get("cover_art_path"):
            await self.image_pipeline.fetch_cover_art(token, row["title"])
```

### Image pipeline

**File:** `kryten_webqueue/catalog/images.py`

Priority: TMDB → OMDB → branded placeholder → MediaCMS thumbnail.

```python
import io
import re
from pathlib import Path
from PIL import Image
import httpx
import hashlib

TARGET_SIZE = (400, 600)  # 2:3 portrait
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}


class ImagePipeline:
    """Fetches, validates, and resizes cover art."""

    def __init__(self, config):
        self.image_dir = Path(config.image_dir) / "catalog"
        self.placeholder_dir = Path(config.placeholder_dir)
        self.tmdb_key = config.tmdb_api_key
        self.omdb_key = config.omdb_api_key
        self.image_dir.mkdir(parents=True, exist_ok=True)

    async def fetch_cover_art(self, friendly_token: str, title: str) -> str | None:
        """Attempt to fetch cover art from all sources. Returns relative path or None."""
        output_path = self.image_dir / f"{friendly_token}.jpg"
        if output_path.exists():
            return f"catalog/{friendly_token}.jpg"

        # Try sources in priority order
        image_bytes = None
        source = None

        if self.tmdb_key:
            image_bytes = await self._try_tmdb(title)
            if image_bytes:
                source = "tmdb"

        if not image_bytes and self.omdb_key:
            image_bytes = await self._try_omdb(title)
            if image_bytes:
                source = "omdb"

        if not image_bytes:
            # Branded placeholder (deterministic selection)
            placeholder = self._select_placeholder(friendly_token)
            if placeholder:
                image_bytes = placeholder.read_bytes()
                source = "placeholder"

        if not image_bytes:
            return None

        # Validate and resize
        self._process_and_save(image_bytes, output_path)
        return f"catalog/{friendly_token}.jpg"

    def _process_and_save(self, image_bytes: bytes, output_path: Path):
        """Validate MIME, resize to 400x600, save as JPEG."""
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")
        img = img.resize(TARGET_SIZE, Image.LANCZOS)
        img.save(output_path, "JPEG", quality=85)

    def _select_placeholder(self, friendly_token: str) -> Path | None:
        """Deterministic placeholder selection seeded by token."""
        placeholders = sorted(self.placeholder_dir.glob("*.jpg"))
        if not placeholders:
            return None
        idx = int(hashlib.md5(friendly_token.encode()).hexdigest(), 16) % len(placeholders)
        return placeholders[idx]

    async def _try_tmdb(self, title: str) -> bytes | None:
        """Search TMDB for poster by title."""
        # Extract year if present: "Movie Title (2019)" → title="Movie Title", year=2019
        year_match = re.search(r"\((\d{4})\)$", title.strip())
        search_title = re.sub(r"\s*\(\d{4}\)$", "", title).strip()
        params = {"api_key": self.tmdb_key, "query": search_title}
        if year_match:
            params["year"] = year_match.group(1)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://api.themoviedb.org/3/search/movie", params=params)
            if resp.status_code != 200:
                return None
            results = resp.json().get("results", [])
            if not results or not results[0].get("poster_path"):
                return None
            poster_url = f"https://image.tmdb.org/t/p/w500{results[0]['poster_path']}"
            img_resp = await client.get(poster_url)
            if img_resp.status_code == 200 and img_resp.headers.get("content-type", "") in ALLOWED_MIME:
                return img_resp.content
        return None

    async def _try_omdb(self, title: str) -> bytes | None:
        """Search OMDB for poster by title."""
        search_title = re.sub(r"\s*\(\d{4}\)$", "", title).strip()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("http://www.omdbapi.com/", params={"apikey": self.omdb_key, "t": search_title})
            if resp.status_code != 200:
                return None
            data = resp.json()
            poster_url = data.get("Poster")
            if not poster_url or poster_url == "N/A":
                return None
            img_resp = await client.get(poster_url)
            if img_resp.status_code == 200 and img_resp.headers.get("content-type", "") in ALLOWED_MIME:
                return img_resp.content
        return None
```

---

## 7. Queue Module

### Shadow state

**File:** `kryten_webqueue/queue/shadow.py`

The queue shadow is the local representation of the live CyTube playlist. It is maintained by:
1. **Polling** — every N seconds, fetch `GET /state/playlist` and `GET /state/now-playing`
2. **Own mutations** — when webqueue adds/moves/removes items, update shadow immediately

```python
from datetime import datetime, timedelta, UTC
from typing import Any


class QueueShadow:
    """Maintains local shadow of the CyTube playlist state."""

    def __init__(self, db, ws_manager):
        self.db = db
        self.ws_manager = ws_manager
        self._now_playing: dict | None = None

    async def apply_poll_result(self, items: list[dict], now_playing: dict | None):
        """Called by StatePoller with fresh api-gate data. Diffs and updates."""
        old_items = await self.db.get_shadow_items()
        old_uids = {item["uid"] for item in old_items}
        new_uids = {item["uid"] for item in items}

        changed = False

        # Detect removals
        removed_uids = old_uids - new_uids
        if removed_uids:
            await self.db.remove_shadow_items(removed_uids)
            changed = True

        # Detect additions (items added outside webqueue, e.g. by admin via CyTube)
        added_uids = new_uids - old_uids
        if added_uids:
            for i, item in enumerate(items):
                if item["uid"] in added_uids:
                    await self.db.upsert_shadow_item({
                        "uid": item["uid"],
                        "position": i,
                        "title": item.get("title", ""),
                        "media_type": item.get("type", ""),
                        "media_id": item.get("id", ""),
                        "duration_sec": item.get("seconds", 0),
                        "is_pay": 0,
                        "added_at": datetime.now(UTC).isoformat(),
                    })
            changed = True

        # Detect reordering
        for i, item in enumerate(items):
            if item["uid"] in old_uids:
                await self.db.update_shadow_position(item["uid"], i)
                # Check if position actually changed
                old_item = next((x for x in old_items if x["uid"] == item["uid"]), None)
                if old_item and old_item["position"] != i:
                    changed = True

        # Update now-playing
        if now_playing != self._now_playing:
            self._now_playing = now_playing
            changed = True

        # Recalculate estimated start times
        if changed:
            await self._recalculate_start_times()
            await self.ws_manager.broadcast_queue_update(await self.get_full_state())

    async def _recalculate_start_times(self):
        """Recalculate estimated_start_at for all shadow items."""
        if not self._now_playing:
            return

        current_time = self._now_playing.get("currentTime", 0)
        total_duration = self._now_playing.get("seconds", 0)
        remaining_sec = max(0, total_duration - current_time)

        cursor = remaining_sec
        items = await self.db.get_shadow_items()  # ORDER BY position ASC
        now = datetime.now(UTC)
        for item in items:
            estimated = now + timedelta(seconds=cursor)
            await self.db.update_shadow_estimated_start(item["uid"], estimated.isoformat())
            cursor += (item.get("duration_sec") or 0)

    async def record_own_add(self, uid: int, item_data: dict):
        """Record an item we just added (bypass next poll cycle for immediate WS push)."""
        await self.db.upsert_shadow_item(item_data)
        await self._recalculate_start_times()
        await self.ws_manager.broadcast_queue_update(await self.get_full_state())

    async def record_own_remove(self, uid: int):
        """Record an item we just removed."""
        await self.db.remove_shadow_items({uid})
        await self._recalculate_start_times()
        await self.ws_manager.broadcast_queue_update(await self.get_full_state())

    async def get_full_state(self) -> dict:
        """Get full queue state for WebSocket broadcast."""
        items = await self.db.get_shadow_items()
        return {
            "now_playing": self._now_playing,
            "items": items,
        }
```

### State poller

**File:** `kryten_webqueue/queue/poller.py`

```python
import asyncio
import logging

logger = logging.getLogger(__name__)


class StatePoller:
    """Periodically polls api-gate for playlist and now-playing state."""

    def __init__(self, api_gate, shadow, *, interval: float = 3.0):
        self.api_gate = api_gate
        self.shadow = shadow
        self.interval = interval
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._poll_loop())

    def stop(self):
        if self._task:
            self._task.cancel()

    async def _poll_loop(self):
        while True:
            try:
                items = await self.api_gate.get_playlist()
                now_playing = await self.api_gate.get_now_playing()
                await self.shadow.apply_poll_result(items, now_playing or None)
            except Exception as e:
                logger.warning(f"State poll failed: {e}")
            await asyncio.sleep(self.interval)
```

### FIFO ordering

**File:** `kryten_webqueue/queue/ordering.py`

```python
import asyncio

# Module-level lock to serialize add→move sequences
_queue_lock = asyncio.Lock()


async def insert_pay_queue(api_gate, shadow, db, *, media_type: str, media_id: str, duration_sec: int, username: str, z_cost: int, request_id: str) -> dict:
    """Add a pay-to-play item and move it to the correct FIFO position.

    Returns: {"success": bool, "uid": int|None, "error": str|None}
    """
    async with _queue_lock:
        # 1. Add to end of CyTube playlist
        result = await api_gate.playlist_add(media_type, media_id, position="end", temp=True)
        if not result.get("success"):
            return {"success": False, "uid": None, "error": "playlist_add_failed"}

        uid = result.get("uid")
        if uid is None:
            # Fallback: poll playlist to find the new item (should be last)
            items = await api_gate.get_playlist()
            if items:
                uid = items[-1]["uid"]
            else:
                return {"success": False, "uid": None, "error": "uid_resolution_failed"}

        # 2. Determine correct position
        last_pay_uid = await db.get_last_pay_uid()

        if last_pay_uid is not None:
            position = last_pay_uid  # place after last pay item
        else:
            position = "prepend"     # no pay items → front of queue

        # 3. Move to position
        await api_gate.playlist_move(uid, position)

        # 4. Persist request_id → uid mapping (needed by refund_displaced_items)
        await db.save_spend_request(request_id, username=username, uid=uid)

        # 5. Record in shadow
        new_position = await db.get_shadow_position_after(last_pay_uid) if last_pay_uid else 0
        await shadow.record_own_add(uid, {
            "uid": uid,
            "position": new_position,
            "title": "",  # will be populated by next poll
            "media_type": media_type,
            "media_id": media_id,
            "duration_sec": duration_sec,
            "is_pay": 1,
            "paid_by": username,
            "tier": "queue",
            "z_cost": z_cost,
            "added_at": "now",
        })

        return {"success": True, "uid": uid, "error": None}


async def insert_pay_playnext(api_gate, shadow, db, *, media_type: str, media_id: str, duration_sec: int, username: str, z_cost: int, request_id: str) -> dict:
    """Add a playnext item (immediately after currently playing)."""
    async with _queue_lock:
        result = await api_gate.playlist_add(media_type, media_id, position="next", temp=True)
        if not result.get("success"):
            return {"success": False, "uid": None, "error": "playlist_add_failed"}

        uid = result.get("uid")

        # Persist request_id → uid mapping (needed by refund_displaced_items)
        await db.save_spend_request(request_id, username=username, uid=uid)

        await shadow.record_own_add(uid, {

        return {"success": True, "uid": uid, "error": None}
```

### Submission flow (Phase 2)

**File:** `kryten_webqueue/queue/submit.py`

```python
import uuid

from .ordering import insert_pay_queue, insert_pay_playnext


async def submit_queue(api_gate, shadow, db, catalog_db, *, username: str, friendly_token: str, tier: str = "queue") -> dict:
    """Full pay-to-play submission flow (§10 steps 1-13).

    Returns: {"success": bool, "error": str|None, "uid": int|None}
    """
    # 1. Resolve catalog item
    item = await catalog_db.get_item(friendly_token)
    if not item:
        return {"success": False, "error": "item_not_found"}

    media_type = "cm"
    media_id = item["manifest_url"]
    duration_sec = item["duration_sec"] or 0

    # 2. Check immutability restriction (§9 rule 1)
    if await catalog_db.is_restricted(friendly_token):
        return {"success": False, "error": "item_restricted"}

    # 3. Check pre-fire lock (§9 rule 2)
    if await db.is_pre_fire_lock_active():
        return {"success": False, "error": "pre_fire_lock_active"}

    # 4. Check estimated start vs immutable schedule (§9 rule 3)
    # (implementation detail: compare estimated_start_at against active_schedule window)

    # 5. Cost preview
    preview = await api_gate.queue_preview(username, duration_sec, tier)
    if not preview.get("available"):
        return {"success": False, "error": preview.get("error_code", "not_available")}

    # 6. Generate request_id for idempotency
    request_id = str(uuid.uuid4())

    # 7. Debit
    spend_result = await api_gate.queue_spend(username, duration_sec, tier, request_id)
    if not spend_result.get("success"):
        return {"success": False, "error": spend_result.get("error_code", "spend_failed")}

    z_cost = spend_result["cost_z"]

    # 8. Add to playlist + FIFO position
    if tier == "playnext":
        add_result = await insert_pay_playnext(
            api_gate, shadow, db,
            media_type=media_type, media_id=media_id,
            duration_sec=duration_sec, username=username, z_cost=z_cost,
            request_id=request_id,
        )
    else:
        add_result = await insert_pay_queue(
            api_gate, shadow, db,
            media_type=media_type, media_id=media_id,
            duration_sec=duration_sec, username=username, z_cost=z_cost, request_id=request_id,
        )

    # 9. Handle add failure → refund
    if not add_result.get("success"):
        await api_gate.queue_refund(username, request_id, "playlist_add_failed")
        await api_gate.send_pm(username, f"Your queue for {item['title']} failed. {z_cost} Z refunded.")
        return {"success": False, "error": "playlist_add_failed"}

    return {"success": True, "error": None, "uid": add_result["uid"]}
```

### Refund flows

**File:** `kryten_webqueue/queue/refund.py`

```python
async def refund_displaced_items(api_gate, db, reason: str):
    """Refund all pay-to-play items currently in queue_shadow. Used during schedule fire."""
    pay_items = await db.get_pay_items()
    for item in pay_items:
        if item.get("paid_by") and item.get("z_cost"):
            # request_id from original submission (stored in a separate table)
            request_id = await db.get_request_id_for_uid(item["uid"])
            if request_id:
                await api_gate.queue_refund(item["paid_by"], request_id, reason)
                await api_gate.send_pm(
                    item["paid_by"],
                    f"Your queued item was displaced. {item['z_cost']} Z refunded. Reason: {reason}",
                )
```

---

## 8. WebSocket Server

### Protocol

**File:** `kryten_webqueue/ws/queue.py`

**Upgrade:** Client connects to `wss://queue.dropsugar.co/ws/queue` with the session cookie. Server validates JWT on upgrade; rejects unauthenticated connections with 4001.

**Message format:** All messages are JSON.

#### Server → Client messages

| Type | Payload | When |
|---|---|---|
| `queue_state` | Full queue state (items + now_playing) | On initial connect |
| `queue_update` | Full queue state (items + now_playing) | On any change (poll diff or own mutation) |
| `now_playing` | `{uid, title, currentTime, seconds, paused}` | Now-playing changed |
| `item_added` | `{uid, title, position, is_pay, tier, paid_by}` | Item added to queue |
| `item_removed` | `{uid}` | Item removed from queue |
| `schedule_announcement` | `{schedule_id, label, fire_at, pre_fire_lock_at}` | Upcoming schedule info |
| `error` | `{code, message}` | Server-side error |

#### Client → Server messages

| Type | Payload | Purpose |
|---|---|---|
| `ping` | `{}` | Keepalive (client sends every 30s) |

Server sends `pong` in response.

#### Implementation

```python
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from ..auth.session import verify_session

ws_router = APIRouter()


@ws_router.websocket("/ws/queue")
async def websocket_queue(ws: WebSocket):
    # Auth on upgrade
    token = ws.cookies.get("session")
    if not token:
        await ws.close(code=4001, reason="missing session")
        return

    config = ws.app.state.config
    username = verify_session(token, config.secret_key)
    if not username:
        await ws.close(code=4001, reason="invalid session")
        return

    await ws.accept()
    manager: WebSocketManager = ws.app.state.ws_manager
    await manager.connect(ws, username)

    try:
        # Send initial state
        shadow: QueueShadow = ws.app.state.shadow
        state = await shadow.get_full_state()
        await ws.send_json({"type": "queue_state", "data": state})

        # Listen for client messages (ping/pong keepalive)
        while True:
            data = await ws.receive_json()
            if data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)
```

### WebSocket manager

**File:** `kryten_webqueue/ws/manager.py`

```python
from fastapi import WebSocket
import logging

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages active WebSocket connections and broadcasts."""

    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket, username: str):
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket):
        self._connections = [c for c in self._connections if c != ws]

    async def broadcast_queue_update(self, state: dict):
        """Broadcast queue state to all connected clients."""
        message = {"type": "queue_update", "data": state}
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)
```

---

## 9. Playlists & Scheduler

### Saved playlists CRUD

**File:** `kryten_webqueue/playlists/db.py`

Standard CRUD operations for `saved_playlists`, `saved_playlist_items` tables. All queries are parameterized.

### Playlist importer

**File:** `kryten_webqueue/playlists/importer.py`

```python
async def import_playlist_text(db, text: str) -> dict:
    """Parse plain-text playlist import format.

    Returns: {"imported": int, "errors": [{"line": int, "token": str, "reason": str}]}
    """
    items = []
    errors = []

    for line_num, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if ":" in line and not line.startswith("cm:"):
            # Explicit type:id
            media_type, media_id = line.split(":", 1)
            items.append({"media_type": media_type, "media_id": media_id, "title": None, "duration_sec": None})
        elif line.startswith("cm:"):
            media_id = line[3:]
            items.append({"media_type": "cm", "media_id": media_id, "title": None, "duration_sec": None})
        else:
            # Bare token — resolve from catalog
            catalog_item = await db.get_item_admin(line)
            if catalog_item:
                items.append({
                    "media_type": "cm",
                    "media_id": catalog_item["manifest_url"],
                    "title": catalog_item["title"],
                    "duration_sec": catalog_item["duration_sec"],
                })
            else:
                errors.append({"line": line_num, "token": line, "reason": "not_in_catalog"})

    return {"items": items, "errors": errors}
```

### Schedule fire

**File:** `kryten_webqueue/playlists/fire.py`

```python
import asyncio
from datetime import datetime, timedelta, UTC

# Shared with queue ordering
from ..queue.ordering import _queue_lock


async def fire_schedule(schedule_id: int, *, api_gate, db, shadow):
    """Execute the schedule fire sequence (PRODUCT_PLAN §8)."""

    async with _queue_lock:
        schedule = await db.get_schedule(schedule_id)
        if not schedule or not schedule["is_active"]:
            return

        playlist_id = schedule["playlist_id"]
        playlist = await db.get_saved_playlist(playlist_id)
        items = await db.get_saved_playlist_items(playlist_id)

        # 1. Refund displaced pay items
        from ..queue.refund import refund_displaced_items
        await refund_displaced_items(api_gate, db, reason="schedule_displacement")

        # 2. Clear entire CyTube queue
        await api_gate.playlist_clear()

        # 3. Add all items from saved playlist
        for item in items:
            await api_gate.playlist_add(
                item["media_type"], item["media_id"],
                position="end", temp=True,
            )

        # 4. Update active_schedule singleton
        total_duration = sum(i.get("duration_sec") or 0 for i in items)
        now = datetime.now(UTC)
        await db.set_active_schedule(
            schedule_id=schedule_id,
            playlist_id=playlist_id,
            is_immutable=playlist.get("is_immutable", False),
            started_at=now.isoformat(),
            estimated_end_at=(now + timedelta(seconds=total_duration)).isoformat(),
        )

        # 5. Rebuild shadow from live state
        live_items = await api_gate.get_playlist()
        await shadow.apply_poll_result(live_items, await api_gate.get_now_playing())

        # 6. Mark schedule as fired
        await db.mark_schedule_fired(schedule_id, now.isoformat())
```

### Scheduler setup

**File:** `kryten_webqueue/playlists/scheduler.py`

Uses APScheduler to run:
- Catalog sync every N hours
- Immutability expiry check every 5 minutes
- Scheduled playlist fires at configured `fire_at` times

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger


def init_scheduler(app) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    config = app.state.config

    # Catalog sync
    scheduler.add_job(
        run_catalog_sync, trigger=IntervalTrigger(hours=config.catalog_sync_interval_hours),
        kwargs={"app": app}, id="catalog_sync",
    )

    # Immutability expiry check
    scheduler.add_job(
        check_immutability_expiry, trigger=IntervalTrigger(minutes=5),
        kwargs={"app": app}, id="immutability_expiry",
    )

    # Load scheduled playlist fires from DB
    # (dynamic — schedules are added/removed via admin API)

    scheduler.start()
    return scheduler
```

---

## 10. Economy Client

**No direct NATS connection.** All economy operations go through api-gate HTTP endpoints (Gap 8).

The `ApiGateClient` (§5) already has typed methods:
- `get_balance(username)` → `GET /economy/balance/{username}`
- `get_transactions(username)` → `GET /economy/transactions/{username}`
- `queue_preview(username, duration_sec, tier)` → `POST /economy/queue-preview`
- `queue_spend(username, duration_sec, tier, request_id)` → `POST /economy/queue-spend`
- `queue_refund(username, request_id, reason)` → `POST /economy/queue-refund`

No separate economy module needed in webqueue. The api_gate client is the economy client.

---

## 11. Routes

### Auth routes

**File:** `kryten_webqueue/routes/auth.py`

```python
@router.post("/request-otp")
async def request_otp(body: OtpRequest, request: Request):
    """Generate OTP, store in DB, send via PM."""
    # Rate limit check (§12)
    # Generate OTP
    # Store in SQLite
    # Send PM via api-gate
    return {"message": "OTP sent"}

@router.post("/verify-otp")
async def verify_otp(body: VerifyRequest, response: Response):
    """Verify OTP, issue session cookie."""
    # Rate limit check (§12)
    # Look up OTP in DB
    # If valid: issue JWT cookie
    # If invalid: 401
    return {"username": username}

@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("session")
    return {"message": "logged out"}
```

### Catalog routes

**File:** `kryten_webqueue/routes/catalog.py`

```python
@router.get("")
async def browse_catalog(category: str | None = None, page: int = 1):
    """Paginated catalog browse."""
    items = await db.browse(category=category, page=page)
    # Return rendered template or JSON based on Accept header
    ...

@router.get("/search")
async def search_catalog(q: str, page: int = 1):
    """FTS5 full-text search."""
    results = await db.search(q, page=page)
    ...

@router.get("/{friendly_token}")
async def catalog_detail(friendly_token: str):
    """Single item detail page."""
    item = await db.get_item(friendly_token)
    if not item:
        raise HTTPException(404)
    ...

@router.get("/categories")
async def list_categories():
    categories = await db.get_categories()
    ...
```

### Queue routes

**File:** `kryten_webqueue/routes/queue.py`

```python
@router.get("")
async def get_queue():
    """Current queue state (queue_shadow)."""
    state = await shadow.get_full_state()
    ...

@router.post("/preview")
async def queue_preview(body: PreviewRequest, username: str = Depends(get_current_user)):
    """Cost preview before submission (Phase 2)."""
    item = await catalog_db.get_item(body.friendly_token)
    if not item:
        raise HTTPException(404)
    result = await api_gate.queue_preview(username, item["duration_sec"], body.tier)
    return result

@router.post("/submit")
async def queue_submit(body: SubmitRequest, username: str = Depends(get_current_user)):
    """Full pay-to-play submission (Phase 2)."""
    result = await submit_queue(
        api_gate, shadow, db, catalog_db,
        username=username, friendly_token=body.friendly_token, tier=body.tier,
    )
    if not result["success"]:
        raise HTTPException(400, detail=result["error"])
    return result
```

### User routes

**File:** `kryten_webqueue/routes/user.py`

```python
@router.get("/balance")
async def get_balance(username: str = Depends(get_current_user)):
    return await api_gate.get_balance(username)

@router.get("/history")
async def get_history(username: str = Depends(get_current_user)):
    """Personal queue history from local DB."""
    history = await db.get_user_queue_history(username)
    return {"history": history}
```

### Admin routes

**Files:** `admin_playlists.py`, `admin_schedules.py`, `admin_queue.py`

All admin routes validate `rank >= 3` via:
```python
async def require_admin(request: Request):
    username = get_current_user(request)
    user = await api_gate.get_user(username)
    if user.get("rank", 0) < 3:
        raise HTTPException(403, "Insufficient rank")
    return username
```

---

## 12. Rate Limiting

**File:** `kryten_webqueue/auth/rate_limit.py`

In-memory sliding window counters. Resets on restart (acceptable).

```python
from collections import deque
from datetime import datetime, UTC
from fastapi import HTTPException


class RateLimiter:
    """Sliding window rate limiter."""

    def __init__(self):
        self._windows: dict[str, deque] = {}

    def check(self, key: str, max_requests: int, window_seconds: int):
        """Raise 429 if rate limit exceeded."""
        now = datetime.now(UTC).timestamp()
        if key not in self._windows:
            self._windows[key] = deque()

        window = self._windows[key]
        cutoff = now - window_seconds

        # Prune expired entries
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= max_requests:
            oldest = window[0]
            retry_after = int(oldest + window_seconds - now) + 1
            raise HTTPException(429, detail={"error": "rate_limited", "retry_after_sec": retry_after})

        window.append(now)


# Singleton
_limiter = RateLimiter()


def check_otp_request_rate(username: str, client_ip: str):
    """Rate limit OTP requests: 3/username/10min + 10/IP/10min."""
    _limiter.check(f"otp_user:{username}", max_requests=3, window_seconds=600)
    _limiter.check(f"otp_ip:{client_ip}", max_requests=10, window_seconds=600)


def check_otp_verify_rate(client_ip: str):
    """Rate limit OTP verification: 5/IP/5min."""
    _limiter.check(f"verify_ip:{client_ip}", max_requests=5, window_seconds=300)
```

---

## 13. Database & Migrations

### Migration strategy

**File:** `kryten_webqueue/catalog/db.py` (method on Database class)

Simple sequential migration system. No external tool (Alembic is overkill for SQLite).

```python
MIGRATIONS = [
    # v1: Initial schema
    """
    CREATE TABLE IF NOT EXISTS _migrations (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # v2: Core tables
    """
    CREATE TABLE IF NOT EXISTS catalog (...);
    CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts USING fts5(...);
    CREATE TABLE IF NOT EXISTS categories (...);
    CREATE TABLE IF NOT EXISTS catalog_categories (...);
    CREATE TABLE IF NOT EXISTS tags (...);
    CREATE TABLE IF NOT EXISTS catalog_tags (...);
    CREATE TABLE IF NOT EXISTS sync_log (...);
    CREATE TABLE IF NOT EXISTS otps (...);
    CREATE TABLE IF NOT EXISTS queue_shadow (...);
    CREATE TABLE IF NOT EXISTS saved_playlists (...);
    CREATE TABLE IF NOT EXISTS saved_playlist_items (...);
    CREATE TABLE IF NOT EXISTS playlist_schedules (...);
    CREATE TABLE IF NOT EXISTS active_schedule (...);
    CREATE TABLE IF NOT EXISTS queue_history (...);
    CREATE TABLE IF NOT EXISTS spend_requests (...);
    """,
]


async def run_migrations(self):
    """Apply pending migrations sequentially."""
    await self._execute(MIGRATIONS[0])  # ensure _migrations table exists
    current = await self._fetch_one("SELECT MAX(version) as v FROM _migrations")
    current_version = (current["v"] or 0) if current else 0

    for version, sql in enumerate(MIGRATIONS[1:], start=1):
        if version > current_version:
            await self._executescript(sql)
            await self._execute("INSERT INTO _migrations (version) VALUES (?)", [version])
```

### Additional tables (not in product plan)

```sql
-- Tracks request_ids for refund correlation
CREATE TABLE spend_requests (
    request_id   TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    uid          INTEGER,           -- CyTube playlist UID (set after successful add)
    friendly_token TEXT,
    tier         TEXT,
    z_cost       INTEGER,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    refunded     BOOLEAN DEFAULT 0,
    refunded_at  TIMESTAMP
);

-- Personal queue history for /user/history
CREATE TABLE queue_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL,
    friendly_token TEXT,
    title        TEXT,
    tier         TEXT,
    z_cost       INTEGER,
    queued_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status       TEXT DEFAULT 'queued'  -- 'queued' | 'played' | 'refunded' | 'displaced'
);
CREATE INDEX idx_queue_history_user ON queue_history(username);
```

---

## 14. Background Workers

| Worker | Trigger | Function |
|---|---|---|
| StatePoller | Every 3 seconds | Polls `GET /state/playlist` + `GET /state/now-playing`; updates shadow; pushes WS |
| CatalogSync | APScheduler interval (4h) | Full MediaCMS sync + cover art fetch |
| ImmutabilityExpiry | APScheduler interval (5min) | Checks `immutability_expires_at` timestamps; clears `is_immutable` when all schedules expired |
| ScheduledFire | APScheduler date triggers | Fires saved playlists at configured `fire_at` times |
| OTPCleanup | APScheduler interval (1h) | Deletes expired OTPs from SQLite |

All workers use `asyncio` tasks or APScheduler async jobs. No threading.

---

## 15. Error Handling

### API Gate communication failures

```python
# In ApiGateClient — all methods handle httpx errors
try:
    result = await api_gate.playlist_add(...)
except httpx.HTTPStatusError as e:
    # 4xx: client error (bad request to api-gate)
    # 5xx: api-gate or upstream (robot) error
    logger.error(f"api-gate error: {e.response.status_code}")
    # Return failure to caller for appropriate handling
except httpx.ConnectError:
    # api-gate is down
    logger.error("api-gate unreachable")
```

### Schedule fire partial failure

If api-gate goes down mid-fire:
1. Queue clear succeeds but some adds fail → partial playlist loaded
2. The `active_schedule` is still set
3. Admin can manually trigger re-fire or clear active schedule
4. Refunds were already issued before the clear (step 1 of fire sequence)

### WebSocket disconnection

Dead connections are detected on broadcast failure and cleaned up. No explicit heartbeat timeout — the client sends `ping` every 30s; if a `send_json` fails, the connection is removed.

---

## 16. Deployment & Operations

### Dependencies (pyproject.toml)

```toml
[project]
name = "kryten-webqueue"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "httpx>=0.27",
    "aiosqlite>=0.20",
    "pyjwt>=2.8",
    "pillow>=10.0",
    "apscheduler>=3.10",
    "jinja2>=3.1",
    "websockets>=12.0",
]
```

**Not included:** kryten-py (no NATS connection).

### Health check

```python
@app.get("/health")
async def health():
    return {"status": "ok"}
```

### Prometheus metrics (port 28292)

Exported metrics:
- `webqueue_ws_connections_active` — gauge
- `webqueue_state_poll_duration_seconds` — histogram
- `webqueue_queue_submissions_total` — counter (labels: tier, status)
- `webqueue_catalog_sync_duration_seconds` — histogram
- `webqueue_schedule_fires_total` — counter

### File layout (final)

```
kryten-webqueue/
├── kryten_webqueue/
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py
│   ├── app.py
│   │
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── otp.py
│   │   ├── session.py
│   │   └── rate_limit.py
│   │
│   ├── api_gate/
│   │   ├── __init__.py
│   │   └── client.py
│   │
│   ├── catalog/
│   │   ├── __init__.py
│   │   ├── db.py
│   │   ├── sync.py
│   │   ├── images.py
│   │   └── search.py
│   │
│   ├── queue/
│   │   ├── __init__.py
│   │   ├── shadow.py
│   │   ├── poller.py
│   │   ├── ordering.py
│   │   ├── submit.py
│   │   └── refund.py
│   │
│   ├── playlists/
│   │   ├── __init__.py
│   │   ├── db.py
│   │   ├── importer.py
│   │   ├── scheduler.py
│   │   └── fire.py
│   │
│   ├── ws/
│   │   ├── __init__.py
│   │   ├── queue.py
│   │   └── manager.py
│   │
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── catalog.py
│   │   ├── queue.py
│   │   ├── user.py
│   │   ├── admin_playlists.py
│   │   ├── admin_schedules.py
│   │   └── admin_queue.py       # includes /admin/catalog/sync + /admin/catalog/sync-log
│   │
│   └── templates/
│       ├── base.html
│       ├── catalog/
│       ├── queue/
│       ├── auth/
│       ├── user/
│       └── admin/
│
├── static/
│   ├── css/main.css
│   └── js/main.js
│
├── docs/
│   ├── PRODUCT_PLAN.md
│   ├── PRE_PLAN_GAPS.md
│   └── IMPLEMENTATION_SPEC.md
│
├── tests/
├── pyproject.toml
├── README.md
└── config.example.json
```

---

## Appendix A: Product Plan Corrections

The following items in PRODUCT_PLAN.md are superseded by this implementation spec:

| Section | Correction |
|---|---|
| §2 Architecture diagram | Remove NATS arrow from webqueue. webqueue talks HTTP only. |
| §5 "NATS KV buckets" | Removed. OTP stored in local SQLite. Rank looked up via `GET /state/user/{username}`. |
| §5 "NATS event subscriptions" | Removed. Replaced by HTTP polling of `GET /state/playlist` + `GET /state/now-playing`. |
| §7 FIFO algorithm | `position` parameter is a UID (after-uid) or `"prepend"`, not a 0-based array index. |
| §11 User account panel | `balance.get` and `transactions.list` go through api-gate HTTP proxy, not direct NATS. Note: `transactions.recent` is channel-level; per-user history uses `transactions.list`. |
| §12 Technology stack | Remove "kryten-py ≥ 0.16.0". Add "httpx" as the primary client. |
| §17 config.json | Remove `nats_url`. Add `state_poll_interval_sec`. |
| §18 `economy/client.py` | Was "NATS request-reply wrappers". Now: not needed (ApiGateClient serves this role). |
| §18 `queue/shadow.py` | Was "NATS event handlers". Now: polling-based state maintenance. |

---

## Appendix B: Upstream Dependencies Summary

| Dependency | Gap # | Phase | Blocks |
|---|---|---|---|
| api-gate: POST /playlist/add returns UID | 3 | 1 | FIFO ordering |
| api-gate: PUT /playlist/{uid}/move accepts "prepend" | 7 | 1 | FIFO ordering |
| api-gate: GET /economy/balance/{username} | 8 | 1 | User balance display |
| api-gate: GET /economy/transactions/{username} | 8 | 1 | User transaction history |
| api-gate: POST /economy/queue-preview | 8 | 2 | Cost preview |
| api-gate: POST /economy/queue-spend | 8 | 2 | Pay-to-play debit |
| api-gate: POST /economy/queue-refund | 8 | 2 | Refund flows |
| economy: spending.queue_preview NATS command | 4 | 2 | (proxied through api-gate) |
| economy: spending.queue NATS command | 5 | 2 | (proxied through api-gate) |
| economy: spending.queue_refund NATS command | 6 | 2 | (proxied through api-gate) |
