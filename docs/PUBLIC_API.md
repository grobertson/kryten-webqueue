# Public API — Developer's Guide

The kryten-webqueue **Public API** lets approved third-party applications (Smart
TV and tablet clients) read the live channel state — what's playing now, the
upcoming pay-to-play queue, and scheduled events — using a long-lived API key.

- **Base URL:** `https://queue.dropsugar.co`
- **API root:** `/api/public/v1`
- **Auth:** `Authorization: Bearer <api_key>` on every data request.
- **Format:** JSON, UTF-8. All timestamps are ISO-8601 (UTC, `+00:00`).

Every externally callable endpoint lives under `/api/public/v1`. The only
endpoint that does **not** require the `Authorization` header is the bootstrap
exchange (`POST /api/public/v1/link`), which trades a one-time code for a key.

---

## 1. Linking flow (how a device gets a key)

Keys are minted through a short, user-driven pairing flow so a device never
handles the user's credentials.

```
┌────────────┐   1. names device, gets code    ┌──────────────────┐
│  Browser   │ ───────────────────────────────▶│  queue.dropsugar │
│ (logged in)│   GET /link  →  "AB2CD"          │      .co         │
└────────────┘                                  └──────────────────┘
      │  2. user reads the 5-char code aloud / types it
      ▼
┌────────────┐   3. POST /api/public/v1/link    ┌──────────────────┐
│  TV / app  │ ───────────────────────────────▶│  queue.dropsugar │
│            │      { "code": "AB2CD" }         │      .co         │
│            │ ◀───────────────────────────────│                  │
└────────────┘   4. { "api_key": "kqd_…" }      └──────────────────┘
      │  5. store the key; send it as a Bearer token thereafter
      ▼
   GET /api/public/v1/current, /queue, /events …
```

**Rules the client must respect**

1. The user first names the device in the browser (`/link`) and receives a
   **5-character, uppercase alphanumeric** code.
2. The code is a **one-time pad**: valid for **10 minutes** and usable **once**.
   On a successful exchange it is destroyed and cannot be reused.
3. Codes use an unambiguous alphabet (no `O`, `0`, `I`, `1`). Uppercase the
   user's input before sending; the server also normalizes.
4. The API key is returned **exactly once**, in the exchange response. Store it
   securely on the device — it is never retrievable again. If lost, the user
   revokes the device and links again.
5. A user may link multiple devices; each has its own key and name.
6. A key is valid indefinitely until the user revokes it (or the user is
   banned, which revokes all of their keys).

---

## 2. Endpoints

### 2.1 `POST /api/public/v1/link` — exchange a code for a key

Bootstrap only. **No** `Authorization` header.

**Request body**

```json
{
  "code": "AB2CD"
}
```

| Field | Type   | Notes                                            |
|-------|--------|--------------------------------------------------|
| code  | string | The 5-char pad from `/link`. Case-insensitive.   |

**200 response**

```json
{
  "api_key": "kqd_9f3a1c7b2e5d4a6f8091b2c3d4e5f60718293a4b5c6d7e8f",
  "token_type": "Bearer",
  "device_id": 12,
  "device_name": "Living Room TV",
  "username": "alice"
}
```

| Field       | Type    | Notes                                          |
|-------------|---------|------------------------------------------------|
| api_key     | string  | Full secret. Send as `Authorization: Bearer …`. Shown once. |
| token_type  | string  | Always `"Bearer"`.                             |
| device_id   | integer | Server-side id for this linked device.         |
| device_name | string  | The name the user chose.                       |
| username    | string  | The account the key acts on behalf of.         |

**Errors**

| Status | Meaning                                             |
|--------|-----------------------------------------------------|
| 400    | Code is malformed (wrong length / illegal chars).   |
| 404    | Code is unknown, already used, or expired.          |
| 429    | Too many attempts from this client; back off.       |

---

### 2.2 `GET /api/public/v1/current` — now playing

Requires `Authorization: Bearer <key>`.

**200 response (something playing)**

```json
{
  "playing": true,
  "item": {
    "title": "The Blob",
    "friendly_token": "aB3xY",
    "synopsis": "A gelatinous alien terrorises a small town.",
    "duration_sec": 5400,
    "current_time_sec": 1234.5,
    "remaining_sec": 4165.5,
    "cover_art_url": "https://queue.dropsugar.co/images/aB3xY/500.webp",
    "categories": ["Horror", "Sci-Fi"],
    "tags": ["1958", "cult"]
  },
  "updated_at": "2026-08-24T21:40:00+00:00"
}
```

**200 response (nothing playing)**

```json
{ "playing": false, "item": null, "updated_at": "2026-08-24T21:40:00+00:00" }
```

| Field                  | Type            | Notes                                    |
|------------------------|-----------------|------------------------------------------|
| playing                | boolean         | `false` when the channel is idle.        |
| item.title             | string          | Display title.                           |
| item.friendly_token    | string \| null  | Catalog id (stable per item).            |
| item.synopsis          | string \| null  | Plot / description.                      |
| item.duration_sec      | integer \| null | Total runtime in seconds.                |
| item.current_time_sec  | number          | Elapsed playback position, seconds.      |
| item.remaining_sec     | number \| null  | Time left, seconds.                      |
| item.cover_art_url     | string \| null  | Absolute poster URL.                     |
| item.categories        | string[]        | Category names.                          |
| item.tags              | string[]        | Tag names.                               |
| updated_at             | string          | When the snapshot was produced.          |

---

### 2.3 `GET /api/public/v1/queue` — the upcoming queue

Requires `Authorization: Bearer <key>`.

**200 response**

```json
{
  "items": [
    {
      "position": 0,
      "uid": 88213,
      "title": "The Blob",
      "friendly_token": "aB3xY",
      "duration_sec": 5400,
      "estimated_start_at": "2026-08-24T21:40:00+00:00",
      "estimated_start_in_sec": 0,
      "cover_art_url": "https://queue.dropsugar.co/images/aB3xY/500.webp",
      "queued_by": "alice",
      "tier": "queue",
      "is_now_playing": true
    },
    {
      "position": 1,
      "uid": 88214,
      "title": "Plan 9 from Outer Space",
      "friendly_token": "cD4wZ",
      "duration_sec": 4740,
      "estimated_start_at": "2026-08-24T23:10:00+00:00",
      "estimated_start_in_sec": 4165,
      "cover_art_url": "https://queue.dropsugar.co/images/cD4wZ/500.webp",
      "queued_by": "bob",
      "tier": "playnext",
      "is_now_playing": false
    }
  ],
  "count": 2,
  "updated_at": "2026-08-24T21:40:00+00:00"
}
```

| Field                        | Type            | Notes                                        |
|------------------------------|-----------------|----------------------------------------------|
| items[].position             | integer         | 0-based order in the queue.                  |
| items[].uid                  | integer \| null | Playlist uid (for correlating updates).      |
| items[].title                | string          | Display title.                               |
| items[].friendly_token       | string \| null  | Catalog id.                                  |
| items[].duration_sec         | integer \| null | Runtime in seconds.                          |
| items[].estimated_start_at   | string \| null  | Predicted start (ISO-8601).                  |
| items[].estimated_start_in_sec | integer \| null | Predicted seconds until start.             |
| items[].cover_art_url        | string \| null  | Absolute poster URL.                         |
| items[].queued_by            | string \| null  | Username who queued it (may be absent).      |
| items[].tier                 | string \| null  | `queue` or `playnext`.                       |
| items[].is_now_playing       | boolean         | `true` for the item currently on screen.     |
| count                        | integer         | `items.length`.                              |
| updated_at                   | string          | Snapshot time.                               |

> The now-playing item is included here **and** in `/current`. Filter on
> `is_now_playing` if you want strictly upcoming items.

---

### 2.4 `GET /api/public/v1/events` — scheduled events

Requires `Authorization: Bearer <key>`. Returns upcoming **enabled** scheduled
playlists (what operators call "events"), soonest first.

**200 response**

```json
{
  "events": [
    { "label": "Friday Night Grindhouse", "fire_at": "2026-08-28T02:00:00+00:00", "is_recurring": true },
    { "label": "Halloween Marathon",      "fire_at": "2026-10-31T20:00:00+00:00", "is_recurring": false }
  ],
  "count": 2,
  "updated_at": "2026-08-24T21:40:00+00:00"
}
```

| Field                 | Type    | Notes                                         |
|-----------------------|---------|-----------------------------------------------|
| events[].label        | string  | Event name.                                   |
| events[].fire_at      | string  | Scheduled start (ISO-8601, future only).      |
| events[].is_recurring | boolean | Whether the event repeats.                    |
| count                 | integer | `events.length`.                              |
| updated_at            | string  | Snapshot time.                                |

---

## 3. Authentication details

Send the key on every data request:

```
Authorization: Bearer kqd_9f3a1c7b2e5d4a6f8091b2c3d4e5f60718293a4b5c6d7e8f
```

- Missing header → **401** `{"detail": "Missing API key"}`.
- Unknown/revoked key → **401** `{"detail": "Invalid API key"}`.
- Both 401s include `WWW-Authenticate: Bearer`.

Keys are stored server-side only as an irreversible SHA-256 hash; the plaintext
lives solely on the device.

---

## 4. Errors (all endpoints)

Errors use FastAPI's standard shape:

```json
{ "detail": "Invalid or expired code." }
```

| Status | When                                                        |
|--------|-------------------------------------------------------------|
| 400    | Malformed request (bad code format, bad JSON).              |
| 401    | Missing or invalid API key.                                 |
| 404    | Unknown/expired link code.                                  |
| 429    | Rate limited (link generation or code exchange).            |

---

## 5. Client implementation notes

- **Polling:** these endpoints return point-in-time snapshots. Poll `/current`
  and `/queue` every ~10–15s; `/events` changes rarely (every few minutes is
  plenty).
- **Countdowns:** compute live countdowns on-device from `remaining_sec` /
  `estimated_start_in_sec` rather than re-polling every second.
- **Cover art:** `cover_art_url` is absolute and ready to load. It may be
  `null`; show a placeholder.
- **Key loss / revocation:** if requests start returning 401, the key was
  revoked (by the user, or automatically when the account is banned — bans are
  reconciled against the moderator service on a schedule). Prompt the user to
  re-link.
- **Time zones:** all times are UTC with an explicit offset; convert for
  display.
