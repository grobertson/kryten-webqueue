# kryten-webqueue — Product Plan

**Version:** 3.1  
**Date:** 2026-05-30  
**Status:** Design complete — all pre-plan gaps resolved — ready for implementation

---

## Table of Contents

1. [Overview](#1-overview)
2. [System Context](#2-system-context)
3. [Authentication & Sessions](#3-authentication--sessions)
4. [Permission Model](#4-permission-model)
5. [Integration Touchpoints](#5-integration-touchpoints)
6. [Local Catalog Database](#6-local-catalog-database)
7. [Playlist State Management](#7-playlist-state-management)
8. [Saved Playlists & Scheduler](#8-saved-playlists--scheduler)
9. [Immutability & Content Reservation](#9-immutability--content-reservation)
10. [Pay-to-Play Queue Flow](#10-pay-to-play-queue-flow)
11. [Core UI Features](#11-core-ui-features)
12. [Technology Stack](#12-technology-stack)
13. [Internal API Surface](#13-internal-api-surface)
14. [kryten-economy Extension Required](#14-kryten-economy-extension-required)
15. [Security](#15-security)
16. [Phased Rollout](#16-phased-rollout)
17. [Deployment](#17-deployment)
18. [Repository Structure](#18-repository-structure)
19. [Gap Analysis](#19-gap-analysis)
20. [Open Questions](#20-open-questions)

---

## 1. Overview

kryten-webqueue is a web application that replaces CyTube private-message queue commands with a Netflix/Tubi-style catalog browser and pay-to-play queue management interface, accessible at `queue.dropsugar.co`.

**What it replaces:** Users currently queue content by sending PM commands to a bot (`search`, `queue`, `playnext`). This is opaque, inaccessible to new users, and has no visual context for what is being queued.

**What it provides:**
- A full visual catalog of all content available on the channel, with cover art
- Pay-to-play queue submission with live cost preview before any Z-coins are charged
- A live queue view showing what is currently playing and what is coming up
- Admin tools to build, import, schedule, and manage playlists
- A unified blackout/content-reservation system backed by saved playlist immutability
- Complete ownership of CyTube playlist state — webqueue is the single authoritative playlist manager

**Port:** `2010` (HTTP, proxied via nginx from `queue.dropsugar.co`)

---

## 2. System Context

```
┌─────────────────────────────────────────────────────────┐
│                     browser / user                      │
└────────────────────────┬────────────────────────────────┘
                         │ HTTPS  queue.dropsugar.co
┌────────────────────────▼────────────────────────────────┐
│                   nginx reverse proxy                   │
│   /images/ → /var/lib/kryten-webqueue/images/ (direct)  │
│   /        → :2010                                      │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP :2010
┌────────────────────────▼────────────────────────────────┐
│                   kryten-webqueue                       │
│                                                         │
│  FastAPI HTTP app      SQLite DB (catalog + queue state)│
│  APScheduler           httpx async HTTP client          │
│  Alpine.js templates   Background sync worker           │
└──────────────────────────┬──────────────────────────────┘
                           │ HTTP :24444
                           ▼
              ┌────────────────────────────┐
              │  kryten-api-gate           │
              │                            │
              │  POST /playlist/add        │
              │  DELETE /playlist/{uid}    │
              │  PUT /playlist/{uid}/move  │
              │  POST /chat/pm             │
              │  GET /state/playlist       │
              │  GET /state/now-playing    │
              │  GET /state/user/{u}       │
              │  GET /economy/balance/{u}  │
              │  POST /economy/queue-*     │
              └────────────┬───────────────┘
                           │ NATS (internal)
                 ┌─────────┴──────────┐
                 ▼                    ▼
  ┌──────────────────────┐  ┌──────────────────────────┐
  │  kryten-economy      │  │  kryten-robot             │
  │  (Z-coin ledger)     │  │  (CyTube websocket proxy) │
  └──────────────────────┘  └────────────┬─────────────┘
                                         │
                                   CyTube websocket
                                   channel: Q_A
```

**Key data flows:**
- Browse/search: browser → webqueue → local SQLite catalog (no external calls)
- Queue submit: browser → webqueue → kryten-api-gate `/economy/queue-spend` → kryten-economy → kryten-api-gate `/playlist/add`
- Playlist state: webqueue polls `GET /state/playlist` + `GET /state/now-playing` every 3 s; no direct NATS connection
- Image serving: nginx serves `/images/` directly from filesystem, bypassing Python
- Catalog sync: webqueue background worker → MediaCMS API (not in browser request path)

**Architecture constraint:** webqueue has **no NATS connection**. All upstream communication is HTTP to kryten-api-gate. kryten-api-gate proxies economy commands to kryten-economy over NATS internally.

---

## 3. Authentication & Sessions

### Login flow

1. User enters their CyTube username and clicks **Send Code**
2. webqueue generates a 6-digit OTP and stores it in the local SQLite `otps` table with a 5-minute expiry
3. webqueue calls `POST /chat/pm` on kryten-api-gate to deliver the code to the user in CyTube chat
4. User enters the 6-digit code; webqueue validates it against SQLite (single-use: marked `used=1` on first successful verification)
5. On success, webqueue issues a signed JWT stored in an `HttpOnly; Secure; SameSite=Strict` cookie, valid for 24 hours
6. webqueue reads the user's current rank from `GET /state/user/{username}` on kryten-api-gate

### Session storage

| Store | Key | Purpose |
|---|---|---|
| SQLite `otps` table | `(username, code)` | Pending OTP, expires after 5 min |
| HTTP cookie | `session` | Signed JWT, TTL 24 h |

### JWT payload

```json
{
  "sub": "cytube_username",
  "rank": 2,
  "iat": 1234567890,
  "exp": 1234654290
}
```

Rank is re-validated via `GET /state/user/{username}` on kryten-api-gate on every privileged action (queue submission, admin operations). The JWT rank is used only for UI rendering decisions; the live API value is authoritative for server-side enforcement.

---

## 4. Permission Model

CyTube rank values map directly to webqueue capabilities:

| Rank | CyTube role | Pay-to-queue | Playnext | Admin panel | Force-play |
|---|---|---|---|---|---|
| 0 | Guest / unregistered | — | — | — | — |
| 1 | Registered user | ✓ | — | — | — |
| 1.5 | Contributor | ✓ | — | — | — |
| 2 | Moderator | ✓ | ✓ | — | — |
| 3 | Admin | ✓ | ✓ | ✓ | — |
| 4+ | Owner | ✓ | ✓ | ✓ | ✓ |

Rank is always read from kryten-api-gate `GET /state/user/{username}` at action time, never trusted from the session cookie alone.

---

## 5. Integration Touchpoints

### kryten-api-gate (HTTP, :24444)

Used by webqueue for all CyTube actions. All calls use a Bearer token from `config.json`.

| Endpoint | Usage |
|---|---|
| `POST /chat/pm` | Deliver OTP during login; deliver refund notifications |
| `POST /playlist/add` | Add item to CyTube playlist (returns `uid`) |
| `DELETE /playlist/{uid}` | Remove item (refund flows, admin) |
| `PUT /playlist/{uid}/move` | Reposition item (FIFO pay ordering; accepts `int` UID or `"prepend"`/`"append"`) |
| `POST /playlist/{uid}/jump` | Force-play (rank 4+) |
| `DELETE /playlist/` | Clear entire playlist (scheduled playlist fire) |
| `GET /state/playlist` | Read current CyTube playlist state |
| `GET /state/now-playing` | Read current item + elapsed time |
| `GET /state/user/{username}` | Rank lookup; returns `{"rank": 0, "online": false}` when offline |
| `GET /admin/motd` | Display MOTD in UI header |
| `GET /economy/balance/{username}` | User Z-coin balance display |
| `GET /economy/transactions/{username}` | Per-user transaction history |
| `POST /economy/queue-preview` | Read-only cost estimate before user confirms |
| `POST /economy/queue-spend` | Atomic validate + debit; idempotent via `request_id` |
| `POST /economy/queue-refund` | Compensating credit on failure or schedule displacement |

### kryten-economy (via kryten-api-gate HTTP proxy)

webqueue never connects to kryten-economy directly. All economy commands are sent as HTTP calls to kryten-api-gate's `/api/v1/economy/*` routes, which proxy them to kryten-economy over NATS internally. See economy endpoints in the kryten-api-gate table above.

### MediaCMS API (`https://www.dropsugar.com/api/v1`)

Used exclusively by the background catalog sync worker. Never accessed during a browser request.

| Endpoint | Usage |
|---|---|
| `GET /media` (paginated) | Full catalog sync |
| `GET /media/{friendly_token}` | Single-item refresh |
| `GET /categories` | Sync category taxonomy |
| `GET /tags` | Sync tag taxonomy |

Auth: `Authorization: Token <token>`

### Queue shadow state maintenance

webqueue has no NATS connection. Queue shadow state is maintained by polling:

| Poll target | Interval | Action |
|---|---|---|
| `GET /state/playlist` | every 3 s | Diff against `queue_shadow`; detect add/remove/reorder |
| `GET /state/now-playing` | every 3 s | Update now-playing; recalculate estimated start times |

webqueue also updates the shadow immediately after its own mutations (add/move/delete) without waiting for the next poll cycle.

---

## 6. Local Catalog Database

All catalog browse and search runs against a local SQLite database. MediaCMS is never in the browser request path.

### Schema

```sql
CREATE TABLE catalog (
    friendly_token   TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    description      TEXT,
    duration_sec     INTEGER,
    manifest_url     TEXT NOT NULL,   -- full cm: URL for kryten-api-gate
    thumbnail_url    TEXT,            -- original MediaCMS thumbnail (not served to browser)
    cover_art_path   TEXT,            -- relative path under /images/catalog/
    cover_art_source TEXT,            -- 'tmdb' | 'omdb' | 'placeholder' | 'mediacms'
    added_at         TIMESTAMP,
    updated_at       TIMESTAMP,
    synced_at        TIMESTAMP
);

CREATE VIRTUAL TABLE catalog_fts USING fts5(
    friendly_token UNINDEXED,
    title,
    description,
    content='catalog',
    content_rowid='rowid'
);

CREATE TABLE categories (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE,
    slug  TEXT NOT NULL UNIQUE
);

CREATE TABLE catalog_categories (
    friendly_token TEXT REFERENCES catalog(friendly_token) ON DELETE CASCADE,
    category_id    INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (friendly_token, category_id)
);

CREATE TABLE tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE catalog_tags (
    friendly_token TEXT REFERENCES catalog(friendly_token) ON DELETE CASCADE,
    tag_id         INTEGER REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (friendly_token, tag_id)
);

CREATE TABLE sync_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TIMESTAMP NOT NULL,
    ended_at      TIMESTAMP,
    items_seen    INTEGER,
    items_new     INTEGER,
    items_updated INTEGER,
    errors        INTEGER,
    status        TEXT      -- 'running' | 'ok' | 'failed'
);
```

### Cover art pipeline

For every catalog item, the background sync worker fetches cover art in priority order:

1. **TMDB** — primary; searched by title + year; subscription key `TMDB_API_KEY`
2. **OMDB** — secondary; searched by title; subscription key `OMDB_API_KEY`
3. **Branded placeholder** — random selection from `/var/lib/kryten-webqueue/images/placeholders/` (operator-managed directory of branded poster images); selection seeded from `friendly_token` for visual stability across refreshes
4. **MediaCMS thumbnail** — last resort only; noted as low quality and inconsistent aspect ratio

All images are downloaded server-side, MIME-validated, and resized to **400 × 600 JPEG** (2:3 portrait ratio) using Pillow. Stored at `/var/lib/kryten-webqueue/images/catalog/{friendly_token}.jpg`. nginx serves this directory directly with `Cache-Control: max-age=2592000` (30 days).

### Catalog sync schedule

APScheduler runs a full sync every 4 hours. A single-item refresh can be triggered manually from the admin panel. The sync worker:
1. Paginates `GET /media` from MediaCMS
2. For each new or updated item: upsert catalog row, fetch cover art if not present, update FTS index
3. Writes a `sync_log` row on completion

---

## 7. Playlist State Management

webqueue is the **single authoritative manager** of the CyTube playlist. All insertions, reorderings, and removals are made exclusively through webqueue. Direct manipulation of the CyTube playlist by other means (including the CyTube UI) will cause queue_shadow to diverge; a reconciliation endpoint is available for admins.

### Shadow playlist table

```sql
CREATE TABLE queue_shadow (
    uid              INTEGER PRIMARY KEY,  -- CyTube playlist UID
    position         INTEGER NOT NULL,     -- live 0-based position in CyTube
    title            TEXT,
    friendly_token   TEXT,                 -- NULL if not from local catalog
    media_type       TEXT NOT NULL,        -- 'cm', 'yt', etc.
    media_id         TEXT NOT NULL,
    duration_sec     INTEGER,
    is_pay           BOOLEAN NOT NULL DEFAULT 0,
    paid_by          TEXT,                 -- CyTube username, NULL if not pay-to-play
    tier             TEXT,                 -- 'queue' | 'playnext' | NULL
    z_cost           INTEGER,
    schedule_id      INTEGER,              -- set if loaded by a scheduled playlist fire
    estimated_start_at TIMESTAMP,         -- recalculated on every change event
    added_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Estimated start time maintenance

After every NATS playlist event, webqueue recalculates the full estimated schedule:

```python
async def recalculate_schedule():
    now_playing = await get_now_playing()   # GET /state/now-playing
    remaining_sec = now_playing.duration - now_playing.elapsed
    cursor = remaining_sec
    for item in ordered_shadow_items():     # ORDER BY position ASC
        item.estimated_start_at = now() + timedelta(seconds=cursor)
        cursor += (item.duration_sec or 0)
```

This gives every queued item a live estimated start time used for blackout conflict checks.

### FIFO ordering guarantee

Pay-to-play items always play before non-pay items, and maintain strict FIFO order among themselves.

**`queue` tier insertion:**

```python
async def insert_pay_queue(media_type, media_id, duration_sec, username):
    # 1. Find position of last pay-to-play item in CyTube
    last_pay_pos = SELECT MAX(position) FROM queue_shadow WHERE is_pay = 1

    # 2. Add to end of CyTube playlist
    await api_gate.playlist_add(type=media_type, id=media_id, position="end")

    # 3. Retrieve updated playlist to obtain the new item's UID
    playlist = await api_gate.get_state_playlist()
    new_uid = playlist[-1].uid

    # 4. Move to correct position: immediately after last pay-to-play item
    target = (last_pay_pos + 1) if last_pay_pos is not None else 0
    await api_gate.playlist_move(uid=new_uid, position=target)

    # 5. Record in queue_shadow; recalculate estimated start times
```

**`playnext` tier insertion:**

```python
async def insert_pay_playnext(media_type, media_id, username):
    # position="next" places item immediately after currently playing
    await api_gate.playlist_add(type=media_type, id=media_id, position="next")
    # Record in queue_shadow at position 0; recalculate
```

**Example result with both tiers active:**

```
[now playing] Movie A
[playnext]    Movie E  ← playnext tier, absolute front
[queue]       Movie D  ← queue tier, earliest-paid
[queue]       Movie F  ← queue tier, later-paid
[scheduled]   Movie B  ← non-pay (loaded by scheduler)
[scheduled]   Movie C  ← non-pay (loaded by scheduler)
```

---

## 8. Saved Playlists & Scheduler

### Schema

```sql
-- Named, reusable playlist definitions
CREATE TABLE saved_playlists (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    description  TEXT,
    is_immutable BOOLEAN NOT NULL DEFAULT 0,  -- see §9
    created_by   TEXT NOT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Ordered items within a saved playlist
CREATE TABLE saved_playlist_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id  INTEGER NOT NULL REFERENCES saved_playlists(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    media_type   TEXT NOT NULL,    -- 'cm', 'yt', 'vi', etc.
    media_id     TEXT NOT NULL,    -- manifest URL for cm, token/ID for others
    title        TEXT,             -- cached for display
    duration_sec INTEGER,          -- cached for schedule duration estimation
    UNIQUE(playlist_id, position)
);

-- Scheduled firings of a saved playlist
CREATE TABLE playlist_schedules (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id             INTEGER REFERENCES saved_playlists(id) ON DELETE SET NULL,
    label                   TEXT NOT NULL,
    fire_at                 TIMESTAMP NOT NULL,
    is_recurring            BOOLEAN DEFAULT 0,
    rrule                   TEXT,              -- RFC 5545 RRULE for recurring events
    immutability_expires_at TIMESTAMP,         -- NULL = restriction never auto-lifts
    pre_fire_lock_minutes   INTEGER DEFAULT 15,-- minutes before fire to close pay-to-play
    fired_at                TIMESTAMP,         -- last actual fire time (NULL = never fired)
    is_active               BOOLEAN DEFAULT 1,
    created_by              TEXT NOT NULL,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Singleton row: which schedule (if any) is currently running
CREATE TABLE active_schedule (
    id               INTEGER PRIMARY KEY DEFAULT 1,   -- always row 1
    schedule_id      INTEGER REFERENCES playlist_schedules(id),
    playlist_id      INTEGER REFERENCES saved_playlists(id),
    is_immutable     BOOLEAN NOT NULL DEFAULT 0,
    started_at       TIMESTAMP,
    estimated_end_at TIMESTAMP   -- started_at + sum(all item durations)
);
```

### Playlist import format

Admins can import items from a plain-text file (upload or paste into the admin UI):

```
# Lines beginning with # are comments and are ignored
# Blank lines are ignored

# Local catalog items — bare friendly token, resolved from local DB
abc123def456
ghi789jkl012

# Explicit type:id for non-catalog sources
yt:dQw4w9WgXcQ
vi:123456789

# Full manifest URL (custom media type)
cm:https://www.dropsugar.com/api/v1/media/abc123/manifest.json
```

Resolution rules per non-comment line:
- `type:value` prefix → used directly as `media_type` / `media_id`
- bare token → looked up in local `catalog` table; resolved to `cm:{manifest_url}`
- not found in catalog → collected as import error; remainder of import continues
- import result returned to admin: count imported, count errors, list of unresolved tokens

### Schedule fire sequence

When APScheduler fires a scheduled playlist at `fire_at`:

```
1.  Acquire queue lock (short mutex; blocks concurrent pay-to-play submissions)

2.  Query queue_shadow for all is_pay=1 items still in the CyTube playlist
    → Issue spending.queue_refund via kryten-economy for each
    → Send PM via kryten-api-gate notifying each affected user of the refund

3.  DELETE /playlist/  (clear entire CyTube queue)

4.  For each item in saved_playlist_items (ORDER BY position ASC):
        POST /playlist/add  position="end"

5.  Update active_schedule singleton:
        schedule_id      = this schedule
        playlist_id      = this playlist
        is_immutable     = saved_playlists.is_immutable
        started_at       = now()
        estimated_end_at = now() + sum(all item durations)

6.  Rebuild queue_shadow from GET /state/playlist
    Recalculate all estimated_start_at values

7.  Update playlist_schedules.fired_at = now()

8.  Release queue lock
```

### Pre-fire lock window

Each schedule entry has a `pre_fire_lock_minutes` value (default: 15). In that window before `fire_at`, new pay-to-play submissions are refused. Users see: *"Pay-to-play closes in N minutes before [Friday Night Marathon]. Try again after the event."*

The next upcoming schedule is displayed on the catalog and queue UI at all times so users are not caught off-guard.

---

## 9. Immutability & Content Reservation

Immutability serves a dual purpose: it is both a **scheduling blackout** (pay-to-play is blocked during the event) and a **content reservation** (items are exclusively reserved and hidden from regular users at all times while the restriction is active).

### Core restriction rule

An item is **restricted** (completely hidden from catalog and unavailable for pay-to-play) if and only if:

> It belongs to any `saved_playlists` row where `is_immutable = 1`.

This rule is always active, regardless of whether a schedule has been created or fired. Adding an item to an immutable playlist restricts it immediately.

### Conflict resolution

If the same `friendly_token` appears on both an immutable and a mutable playlist, **immutable wins**. The item remains hidden until it is no longer on any immutable playlist.

### Catalog query enforcement

Every catalog browse and search query applies this exclusion filter:

```sql
WHERE friendly_token NOT IN (
    SELECT spi.media_id
    FROM saved_playlist_items spi
    JOIN saved_playlists sp ON spi.playlist_id = sp.id
    WHERE sp.is_immutable = 1
      AND spi.media_type = 'cm'
)
```

Items are completely absent from all browse, search, and category results. No partial display or "not available" state is shown to regular users. Admin views show all items regardless of immutability.

### Expiry mechanism

`playlist_schedules.immutability_expires_at` is a per-schedule-entry optional timestamp:
- `NULL` — the restriction from this schedule never auto-lifts
- set to a datetime — the restriction window ends at this time

A background job (runs every 5 minutes) checks whether restrictions should be lifted:

```python
async def check_immutability_expiry():
    for playlist in immutable_playlists():
        schedules = get_schedules_for(playlist.id)

        # Restriction is permanent if there are no schedules,
        # or if any schedule has immutability_expires_at IS NULL
        has_permanent = (
            len(schedules) == 0 or
            any(s.immutability_expires_at is None for s in schedules)
        )
        if has_permanent:
            continue  # nothing to expire

        # All schedules have explicit expiry times
        if all(s.immutability_expires_at < now() for s in schedules):
            playlist.is_immutable = 0
            # items reappear in catalog on next request
```

### Recurring playlist behaviour

For recurring immutable playlists: each firing generates a new schedule entry. As long as any non-expired entry exists, `is_immutable` remains `1`. Items in a recurring immutable playlist are **permanently reserved** — they are exclusively available through the scheduled event and never appear in ad-hoc browse or search between firings. This is the correct behaviour for perpetual exclusive content libraries (e.g. a weekly curated marathon).

### Pay-to-play enforcement for immutable items

Before `spending.queue_preview` or `spending.queue` is invoked, webqueue enforces:

1. Is the item on any immutable playlist (`is_immutable = 1`)? → Refuse immediately, no Z touched. *"This item is reserved and not available for pay-to-play."*
2. Is there an upcoming scheduled fire within its `pre_fire_lock_minutes` window? → Refuse. *"Pay-to-play closes N minutes before [Event Name]."*
3. Would the item's estimated start time fall within a currently-running immutable schedule's window? → Refuse. *"Pay-to-play is not available during [Event Name]."*

---

## 10. Pay-to-Play Queue Flow

### Full submission flow

```
1.  User clicks "Queue" on a catalog item

2.  webqueue checks item is not restricted (§9, rule 1)

3.  webqueue checks pre-fire lock is not active (§9, rule 2)

4.  webqueue calls spending.queue_preview (kryten-economy)
    → returns: { cost_z, tier_label, discount_pct, available, error_code }
    → if not available (daily limit, cooldown, zero balance): show error, stop

5.  UI shows cost confirmation modal:
    "Queue [Title] ([duration]) for [N] Z-coins?"

6.  User confirms

7.  webqueue generates request_id (UUID4)

8.  webqueue calls spending.queue (kryten-economy)
    → request: { username, friendly_token, duration_sec, tier, request_id }
    → response: { success, new_balance, error_code }
    → on failure: show error to user, stop

9.  webqueue calls POST /playlist/add (kryten-api-gate)
    → on failure:
        → call spending.queue_refund (same request_id)
        → send PM to user: "Your queue for [Title] failed. [N] Z refunded."
        → return error to browser

10. webqueue calls PUT /playlist/{uid}/move to correct FIFO position (§7)

11. webqueue records item in queue_shadow (is_pay=1, tier, z_cost, paid_by)

12. Recalculate all estimated_start_at values

13. UI updates live queue view via WebSocket
```

### System-initiated refund triggers

| Trigger | Refund action |
|---|---|
| `POST /playlist/add` fails after debit | `spending.queue_refund` + PM notification |
| Scheduled playlist fires, displacing queued pay items | `spending.queue_refund` for each + PM notification |
| Admin creates/modifies immutable playlist covering already-queued pay items | `spending.queue_refund` for each + PM notification |
| Admin manually removes a pay-to-play item from the queue | `spending.queue_refund` + PM notification |

All refunds are idempotent via `request_id`. PM notifications are sent via `POST /chat/pm` on kryten-api-gate.

---

## 11. Core UI Features

### Catalog browser

- **Hero banner**: featured or recently-added item, full-width with overlay text
- **Category rows**: horizontal scroll rows per category (Netflix/Tubi aesthetic)
- **Grid view**: full catalog, paginated, sortable by title/added date
- **Search**: FTS5-backed full-text search, real-time results as user types
- All images served from local `/images/` path via nginx; no external image requests from the browser

### Live queue view

- Sidebar or dedicated page showing currently playing item with progress bar
- Ordered list of upcoming items with pay/scheduled distinction visible
- Upcoming scheduled playlist announcement with countdown timer
- Updates via WebSocket — no polling

### Pay-to-play submit

- Queue button on every item detail page
- Cost preview modal (calls `spending.queue_preview`) before any Z is charged
- Explicit user confirmation step before debit
- Clear error messages for: item restricted, daily limit, cooldown, pre-fire lock, zero balance, active immutable schedule

### User account panel

- Current Z-coin balance (from kryten-economy `balance.get`)
- Recent transaction history (`transactions.recent`)
- Personal queue history (from local DB)

### Admin panel (rank ≥ 3)

| Section | Capabilities |
|---|---|
| Catalog | Trigger manual sync; view sync log; browse all items including restricted ones |
| Playlist Library | Create, edit, clone, delete saved playlists; toggle `is_immutable` flag |
| Playlist Editor | Drag-to-reorder items; search catalog to add items; bulk remove; import from text file |
| Schedule Manager | Calendar + list view of upcoming fires; create/edit/delete schedules; set `immutability_expires_at` and `pre_fire_lock_minutes`; "Fire Now" manual trigger; toggle active/disabled |
| Queue Management | Full queue_shadow view with pay/schedule metadata; remove items (with auto-refund); trigger reconciliation against CyTube |
| Active Schedule | Current schedule name, immutable/mutable state, estimated end time; manual clear |
| Channel Controls | MOTD, CSS, JS passthrough (via kryten-api-gate) |

---

## 12. Technology Stack

| Layer | Choice | Reason |
|---|---|---|
| HTTP framework | FastAPI | Already used in kryten-api-gate; async |
| HTTP client | httpx (async) | kryten-api-gate calls, cover art fetch |
| JWT | PyJWT | Session cookies |
| Database | aiosqlite + SQLite | Single-file, no external service, FTS5 built in |
| Image processing | Pillow | Cover art resize/convert |
| Scheduler | APScheduler | Catalog sync, playlist fire, expiry checks |
| Frontend | Alpine.js + vanilla JS | No build toolchain required |
| WebSocket | FastAPI native (`websockets`) | Live queue updates |
| Templating | Jinja2 (via FastAPI) | Server-rendered HTML |

No frontend build toolchain. CSS is hand-written with a Netflix/Tubi dark aesthetic.

---

## 13. Internal API Surface

All endpoints require session cookie except `/auth/*` and `/ws`.

### Auth

| Method | Path | Description |
|---|---|---|
| `POST` | `/auth/request-otp` | Send OTP to CyTube username via PM |
| `POST` | `/auth/verify-otp` | Validate OTP, issue session cookie |
| `POST` | `/auth/logout` | Clear session cookie |

### Catalog

| Method | Path | Description |
|---|---|---|
| `GET` | `/catalog` | List/browse (paginated, category filter) |
| `GET` | `/catalog/search` | FTS full-text search |
| `GET` | `/catalog/{friendly_token}` | Single item detail |
| `GET` | `/catalog/categories` | Category list |

### Queue

| Method | Path | Description |
|---|---|---|
| `GET` | `/queue` | Live queue state (queue_shadow) |
| `POST` | `/queue/preview` | Cost preview for an item (calls economy) |
| `POST` | `/queue/submit` | Submit pay-to-play queue request |
| `WS` | `/ws/queue` | WebSocket for live queue updates |

### User

| Method | Path | Description |
|---|---|---|
| `GET` | `/user/balance` | Current Z balance |
| `GET` | `/user/history` | Personal queue history |

### Admin — Playlists

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/playlists` | List saved playlists |
| `POST` | `/admin/playlists` | Create playlist |
| `GET` | `/admin/playlists/{id}` | Get playlist with full item list |
| `PUT` | `/admin/playlists/{id}` | Update metadata (name, description, is_immutable) |
| `DELETE` | `/admin/playlists/{id}` | Delete playlist |
| `PUT` | `/admin/playlists/{id}/items` | Replace full item list (reorder/save) |
| `POST` | `/admin/playlists/{id}/import` | Import from text file; returns import result |

### Admin — Schedules

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/schedules` | List schedules (with next_fire_at) |
| `POST` | `/admin/schedules` | Create schedule |
| `PUT` | `/admin/schedules/{id}` | Update schedule |
| `DELETE` | `/admin/schedules/{id}` | Delete schedule |
| `POST` | `/admin/schedules/{id}/fire` | Fire immediately (manual trigger) |
| `GET` | `/admin/active-schedule` | Get active_schedule singleton |
| `DELETE` | `/admin/active-schedule` | Clear active schedule (revert to open queue) |

### Admin — Queue & Catalog

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/queue` | Full queue_shadow with pay/schedule metadata |
| `DELETE` | `/admin/queue/{uid}` | Remove item from CyTube queue (with refund if pay) |
| `POST` | `/admin/queue/reconcile` | Re-sync queue_shadow from CyTube state |
| `POST` | `/admin/catalog/sync` | Trigger manual catalog sync |
| `GET` | `/admin/catalog/sync-log` | View recent sync log entries |

---

## 14. kryten-economy Extension Required

**Three new NATS commands must be added to kryten-economy before Phase 2 can ship.** These are a hard blocker. webqueue never performs Z-coin balance manipulation itself.

### `spending.queue_preview`

Read-only cost calculation. No state is modified.

**Request:**
```json
{
  "username": "someuser",
  "friendly_token": "abc123def",
  "duration_sec": 5520,
  "tier": "queue"
}
```

**Response:**
```json
{
  "available": true,
  "cost_z": 250,
  "tier_label": "Movie",
  "discount_pct": 10,
  "error_code": null
}
```

`error_code` values: `"daily_limit_reached"`, `"cooldown_active"`, `"insufficient_balance"`, `"blackout_active"`

### `spending.queue`

Atomic validate + debit. Idempotent: the same `request_id` twice returns the original result without double-charging.

**Request:**
```json
{
  "username": "someuser",
  "friendly_token": "abc123def",
  "duration_sec": 5520,
  "tier": "queue",
  "request_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Response:**
```json
{
  "success": true,
  "cost_z": 250,
  "new_balance": 1750,
  "error_code": null
}
```

### `spending.queue_refund`

Compensating credit. Safe to call multiple times with the same `request_id` (idempotent).

**Request:**
```json
{
  "username": "someuser",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "reason": "schedule_displacement"
}
```

**Response:**
```json
{
  "success": true,
  "refunded_z": 250,
  "new_balance": 2000
}
```

`reason` values: `"playlist_add_failed"`, `"schedule_displacement"`, `"immutability_restriction"`, `"admin_removal"`

All three commands delegate to existing `SpendingEngine` methods in kryten-economy. This is purely new NATS API surface — no new business logic is required in the economy service.

---

## 15. Security

| Concern | Mitigation |
|---|---|
| OTP brute-force | Rate-limit `/auth/verify-otp` to 5 attempts per 5 minutes per IP |
| OTP replay | OTP deleted from NATS KV on first successful use |
| Session hijacking | `HttpOnly; Secure; SameSite=Strict` cookie |
| Rank escalation | Rank re-validated from KV on every privileged server-side action |
| Cover art SSRF | MIME validation + allowlist of trusted origins (TMDB, OMDB, own MediaCMS host) |
| Cover art content | Pillow re-encodes all images; original binary is discarded |
| Double-charge | `request_id` idempotency in `spending.queue` |
| Double-refund | `request_id` idempotency in `spending.queue_refund` |
| Playlist import | Lines resolved against catalog DB only; no shell execution; no URL fetch at import time |
| SQL injection | Parameterised queries throughout; no string interpolation |
| Admin endpoints | All `/admin/*` routes re-validate rank ≥ 3 from KV at request time |

---

## 16. Phased Rollout

### Phase 1 — Read-only catalog (no Z spend)

**Goal:** Validate the catalog UI, cover art pipeline, login, and live queue view with real users.

- Catalog browse, search, category rows, item detail pages
- OTP login via CyTube PM
- Live queue view (WebSocket)
- User account panel (balance display only — no spending)
- Admin panel: catalog sync, playlist library, playlist editor, schedule manager (view + edit; "Fire Now" disabled)
- Immutability restriction is enforced (hidden items stay hidden)
- No queue submission; no `spending.queue*` calls except `balance.get` for display

**kryten-economy blocker: none**

### Phase 2 — Pay-to-play queue

**Gate:** `spending.queue_preview`, `spending.queue`, `spending.queue_refund` implemented and deployed in kryten-economy.

- Queue submission flow (preview → confirm → debit → add → FIFO position)
- Playnext tier
- All system-initiated refund flows
- Scheduled playlist fire (clear + rebuild + refund displaced pay items)
- Pre-fire lock window
- "Fire Now" in admin schedule manager

### Phase 3 — PM command retirement

- Pay-to-play functionality verified stable through Phase 2
- PM queue commands (`queue`, `playnext`, `search`) removed from kryten-robot / kryten-economy
- Channel announcement
- Bot PM responses to old commands redirect to `queue.dropsugar.co`

---

## 17. Deployment

### Port assignments

| Service | HTTP | Prometheus |
|---|---|---|
| kryten-webqueue | 2010 | 28292 |
| kryten-api-gate | 24444 | — |
| kryten-economy | — | 28286 |

### nginx configuration

```nginx
server {
    listen 443 ssl;
    server_name queue.dropsugar.co;

    # Static image assets served directly — bypasses Python entirely
    location /images/ {
        alias /var/lib/kryten-webqueue/images/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    # Everything else proxied to the FastAPI app
    location / {
        proxy_pass         http://127.0.0.1:2010;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # WebSocket support
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
    }
}
```

### config.json

```json
{
  "channel": "Q_A",
  "host": "0.0.0.0",
  "port": 2010,
  "secret_key": "CHANGE_ME_long_random_string",
  "session_ttl_hours": 24,

  "api_gate_url": "http://127.0.0.1:24444",
  "api_gate_token": "CHANGE_ME",

  "mediacms_url": "https://www.dropsugar.com",
  "mediacms_token": "CHANGE_ME",

  "tmdb_api_key": "CHANGE_ME",
  "omdb_api_key": "CHANGE_ME",

  "db_path": "/var/lib/kryten-webqueue/webqueue.db",

  "image_dir": "/var/lib/kryten-webqueue/images",
  "placeholder_dir": "/var/lib/kryten-webqueue/images/placeholders",

  "catalog_sync_interval_hours": 4,
  "pre_fire_lock_minutes_default": 15,

  "prometheus_port": 28292
}
```

### systemd service

```ini
[Unit]
Description=kryten-webqueue
After=network.target nats.service

[Service]
Type=simple
User=kryten
WorkingDirectory=/opt/kryten-webqueue
ExecStart=/opt/kryten-webqueue/.venv/bin/python -m kryten_webqueue
Restart=on-failure
RestartSec=5
Environment=WQ_CONFIG=/etc/kryten-webqueue/config.json

[Install]
WantedBy=multi-user.target
```

---

## 18. Repository Structure

```
kryten-webqueue/
├── kryten_webqueue/
│   ├── __init__.py
│   ├── __main__.py              # entry point: uvicorn startup
│   ├── config.py                # config.json loader
│   ├── app.py                   # FastAPI app factory
│   │
│   ├── auth/
│   │   ├── otp.py               # OTP generation, SQLite write/read/expire
│   │   └── session.py           # JWT issue/validate
│   │
│   ├── catalog/
│   │   ├── db.py                # aiosqlite helpers, catalog queries
│   │   ├── sync.py              # MediaCMS sync worker
│   │   ├── images.py            # cover art pipeline (TMDB → OMDB → placeholder → MediaCMS)
│   │   └── search.py            # FTS5 query wrapper
│   │
│   ├── queue/
│   │   ├── shadow.py            # queue_shadow maintenance, poll-based state updates
│   │   ├── ordering.py          # FIFO insertion algorithm (queue + playnext tiers)
│   │   ├── submit.py            # full pay-to-play submission flow
│   │   └── refund.py            # system-initiated refund flows
│   │
│   ├── playlists/
│   │   ├── db.py                # saved_playlists / saved_playlist_items CRUD
│   │   ├── importer.py          # text file import parser and token resolver
│   │   ├── scheduler.py         # APScheduler jobs: fire, expiry check, pre-fire lock
│   │   └── fire.py              # schedule fire sequence (lock → refund → clear → load → rebuild)
│   │
│   ├── api_gate/
│   │   └── client.py            # httpx async client for kryten-api-gate (CyTube + economy proxy)
│   │
│   ├── routes/
│   │   ├── auth.py
│   │   ├── catalog.py
│   │   ├── queue.py
│   │   ├── user.py
│   │   ├── admin_playlists.py
│   │   ├── admin_schedules.py
│   │   └── admin_queue.py
│   │
│   ├── ws/
│   │   └── queue.py             # WebSocket handler for live queue updates
│   │
│   └── templates/
│       ├── base.html
│       ├── catalog/
│       │   ├── index.html
│       │   ├── search.html
│       │   └── detail.html
│       ├── queue/
│       │   └── index.html
│       ├── auth/
│       │   └── login.html
│       ├── user/
│       │   └── account.html
│       └── admin/
│           ├── index.html
│           ├── playlists.html
│           ├── playlist_editor.html
│           ├── schedules.html
│           └── queue.html
│
├── static/
│   ├── css/
│   │   └── main.css             # Netflix/Tubi dark aesthetic
│   └── js/
│       └── main.js              # Alpine.js components
│
├── docs/
│   └── PRODUCT_PLAN.md          # this document
│
├── pyproject.toml
├── README.md
└── config.example.json
```

---

## 19. Gap Analysis

| # | Gap | Severity | Resolution |
|---|---|---|---|
| 1 | ~~`spending.queue_preview/queue/refund` not in kryten-economy~~ | ~~Blocker (Phase 2)~~ | ✅ **RESOLVED** — All three commands implemented in kryten-economy v0.8.11. Economy proxy routes added to kryten-api-gate v0.3.6. |
| 2 | ~~No `GET /state/playlist` or `GET /state/now-playing` in kryten-api-gate~~ | ~~Blocker (Phase 1)~~ | ✅ **RESOLVED** — Both endpoints already exist in `routes/state.py` (kryten-api-gate 0.3.5). No action needed. |
| 3 | ~~`POST /playlist/add` does not return the new item's UID~~ | ~~Blocker (Phase 1 ordering)~~ | ✅ **RESOLVED** — kryten-robot `_handle_add_video` awaits CyTube `queue` event and returns UID; kryten-api-gate v0.3.6 passes it through. |
| 4 | queue_shadow can diverge if CyTube playlist is manipulated outside webqueue | Medium | Admin reconcile endpoint re-syncs from CyTube; document external manipulation as unsupported |
| 5 | TMDB/OMDB rate limits during bulk catalog sync | Medium | Honour rate-limit response headers; add per-request jitter; skip re-fetch if `cover_art_source` already set |
| 6 | Recurring immutable playlists permanently hide items | Low (by design) | Documented behaviour; operator must remove items from playlist to restore availability; noted clearly in admin UI |
| 7 | WebSocket connection requires authentication | Low | Validate JWT cookie on WS upgrade handshake; reject unauthenticated upgrades |
| 8 | Duration unknown for non-catalog items (yt, vi, etc.) | Low | Fall back to 0 in schedule estimation; log warning; admin can set duration manually on saved_playlist_items |
| 9 | kryten-economy blackout windows vs webqueue immutability are separate concepts | Informational | kryten-economy blackout blocks all Z spend; webqueue immutability controls scheduling and catalog visibility; both may be active simultaneously and independently |
| 10 | MediaCMS manifest URL format may change | Low | `manifest_url` stored in catalog; re-sync refreshes it automatically |

---

## 20. Open Questions

1. ~~**Should `POST /playlist/add` in kryten-api-gate return the new UID?**~~ **Resolved** — see PRE_PLAN_GAPS.md §Gap 3. kryten-robot will await CyTube queue confirmation and return the UID in the NATS response; api-gate will surface it in HTTP response.

2. **`playnext` refund amount on schedule fire:** Current plan is full refund always. If a playnext item is displaced 90 seconds after paying, should any partial credit apply? Deferring to post-Phase 2 review.

3. **Import from URL:** Should the playlist importer accept a remote URL (`.m3u`, plain list)? Deferred to post-Phase 2.

4. **Admin notification on immutability expiry:** Should the creating admin receive a notification when items reappear in catalog after expiry? Not in scope for Phase 1.

5. ~~**`spending.queue` tier mapping:**~~ **Resolved** — Confirmed in kryten-economy v0.8.11. The `tier` field (`"queue"` / `"playnext"`) is used exclusively for priority ordering in webqueue. Pricing is derived solely from `duration_sec` via `SpendingEngine.get_price_tier(duration_sec)` in kryten-economy; `tier` is not referenced in any pricing calculation.
