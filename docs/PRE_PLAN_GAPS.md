# kryten-webqueue — Pre-Plan Gaps Specification

**Version:** 1.3  
**Date:** 2026-05-30  
**Purpose:** Specify and track the upstream service changes required before kryten-webqueue development begins.

---

## Architecture Constraint (added v1.1)

**webqueue has NO NATS connection.** All communication with the Kryten ecosystem passes through kryten-api-gate (HTTP) or MediaCMS (HTTP). This overrides any NATS-based references in the product plan (§5 NATS KV buckets, §5 NATS event subscriptions, §12 kryten-py dependency, §17 `nats_url` config).

---

## Status Summary

| # | Gap | Service | Phase gate | Status |
|---|---|---|---|---|
| 1 | `GET /state/playlist` missing | kryten-api-gate | Phase 1 | ✅ Already exists |
| 2 | `GET /state/now-playing` missing | kryten-api-gate | Phase 1 | ✅ Already exists |
| 3 | `POST /playlist/add` does not return new item UID | kryten-api-gate + kryten-robot | Phase 1 | ✅ kryten-robot patched, api-gate v0.3.6, kryten-py v0.16.1 |
| 4 | `spending.queue_preview` missing | kryten-economy | Phase 2 | ✅ kryten-economy v0.8.11 |
| 5 | `spending.queue` missing | kryten-economy | Phase 2 | ✅ kryten-economy v0.8.11 |
| 6 | `spending.queue_refund` missing | kryten-economy | Phase 2 | ✅ kryten-economy v0.8.11 |
| 7 | `PUT /playlist/{uid}/move` rejects string position values | kryten-api-gate + kryten-py | Phase 1 | ✅ kryten-api-gate v0.3.6, kryten-py v0.16.1 |
| 8 | Economy proxy endpoints missing in api-gate | kryten-api-gate | Phase 1 (balance) / Phase 2 (spending) | ✅ kryten-api-gate v0.3.6 |
| 9 | OTP request rate limiting missing | kryten-webqueue | Phase 1 | ✅ Design-time (implemented in webqueue §12) |
| 10 | `GET /state/user/{username}` returns `null` for offline users | kryten-api-gate | Phase 1 | ✅ kryten-api-gate v0.3.6 |

---

## Gap 1 & 2 — Closed (already in kryten-api-gate)

The product plan listed `GET /state/playlist` and `GET /state/now-playing` as Phase 1 blockers. Both routes already exist in `src/kryten_api_gate/routes/state.py` and are registered under `/api/v1/state/`.

**`GET /api/v1/state/playlist`** reads from NATS KV bucket `kryten_{channel}_playlist`, key `items`.

Response shape (array of CyTube queue items, as stored by kryten-robot's state manager):
```json
{
  "items": [
    {
      "uid": 42,
      "title": "Some Movie (2019)",
      "type": "cm",
      "id": "https://www.dropsugar.com/api/v1/media/abc123/manifest.json",
      "seconds": 5520,
      "temp": true
    }
  ]
}
```

**`GET /api/v1/state/now-playing`** reads from the same KV bucket under key `current`, then overlays fresh `currentTime` / `paused` values from the in-process `PlaybackCache` (which subscribes to `mediaUpdate` NATS events).

Response shape:
```json
{
  "uid": 42,
  "title": "Some Movie (2019)",
  "type": "cm",
  "id": "https://...",
  "seconds": 5520,
  "currentTime": 312.5,
  "paused": false
}
```

Returns `{}` if nothing is currently playing.

**Action:** Update product plan §19 Gap Analysis rows 2 and 3 to mark as resolved.

---

## Gap 3 — `POST /playlist/add` must return the new item UID

### Why it matters

After adding an item to the CyTube playlist, webqueue immediately calls `PUT /playlist/{uid}/move` to insert it at the correct FIFO position. Without the UID, webqueue cannot do this atomically — it must resort to polling `GET /state/playlist` and diffing against its local shadow, which introduces a race window.

### Current behaviour

`POST /api/v1/playlist/add` → kryten-api-gate → `client.add_media()` → NATS request `kryten.robot.command` → kryten-robot `_handle_add_video()` → `sender.add_video()` → socket emit `queue` to CyTube.

`_handle_add_video` returns `{"success": True}` immediately after the socket emit. It does not wait for CyTube's confirmation `queue` event and therefore has no UID to return.

### Required change

#### Step 1 — kryten-robot: wait for CyTube queue confirmation

In `kryten/robot_command_handler.py`, modify `_handle_add_video` to wait for the CyTube `queue` event that confirms the add, then return the assigned UID.

CyTube emits a `queue` event back to the bot after processing each add. The event payload contains the full item including `uid`. kryten-robot's state manager already handles this event to update the KV playlist — we need to also route the first matching `queue` event back to the waiting command handler.

**Implementation approach:** Use a `asyncio.Future` keyed on `(type, id)`, registered before the emit, resolved by the `queue` event handler.

```python
# In robot_command_handler.py

async def _handle_add_video(self, args: dict[str, Any]) -> dict[str, Any]:
    """Handle add video command. Returns UID of newly added item."""
    if not self.sender:
        raise RuntimeError("CytubeEventSender not available")

    media_type = args.get("type")
    media_id   = args.get("id")
    position   = args.get("pos", "end")
    temp       = args.get("temp", True)

    # Register a one-shot future to capture the resulting CyTube queue event
    future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
    key = (media_type, media_id)
    self._pending_add_futures[key] = future

    success = await self.sender.add_video(
        media_type=media_type,
        media_id=media_id,
        position=position,
        temp=temp,
    )
    if not success:
        self._pending_add_futures.pop(key, None)
        return {"success": False, "error": "Failed to add video"}

    try:
        item = await asyncio.wait_for(future, timeout=8.0)
        return {"success": True, "uid": item["uid"]}
    except asyncio.TimeoutError:
        self._pending_add_futures.pop(key, None)
        # Timeout: add was sent but confirmation didn't arrive in time.
        # Return success=True with uid=None — api-gate falls back to polling.
        return {"success": True, "uid": None}
```

The `queue` event handler (wherever CyTube's `queue` events are processed) must resolve any matching pending future:

```python
# In the CyTube queue event handler (state_manager.py or event router)

def _on_cytube_queue_event(self, item: dict) -> None:
    """Called when CyTube emits a queue event (item added)."""
    # ... existing KV update logic ...

    # Resolve any waiting add_video future
    key = (item.get("type"), item.get("id"))
    future = self.command_handler._pending_add_futures.pop(key, None)
    if future and not future.done():
        future.set_result(item)
```

`_pending_add_futures` is a `dict[tuple, asyncio.Future]` on `RobotCommandHandler`, initialised in `__init__`.

#### Step 2 — kryten-api-gate: pass UID through

In `src/kryten_api_gate/routes/playlist.py`, update `add_media` to include `uid` in the response:

```python
@router.post("/add")
async def add_media(
    body: AddMediaRequest,
    client: KrytenClient = Depends(get_client),
    config: Config = Depends(get_config),
) -> dict:
    result = await client.add_media(
        config.channel,
        body.type,
        body.id,
        position=body.position,
        temp=body.temp,
        domain=config.domain,
    )
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to add video"))
    # uid may be None if kryten-robot timed out on the CyTube confirmation
    return {"success": True, "uid": result.get("uid")}
```

#### Updated response contract

```json
{ "success": true, "uid": 42 }
```

`uid` is `null` only if the CyTube confirmation event did not arrive within 8 seconds (should not happen in normal operation). webqueue handles `null` uid by falling back to a `GET /state/playlist` diff.

#### Step 3 — kryten-py: surface the uid field

Update `add_media` docstring and mock to reflect the new return shape. The actual return value is already a passthrough dict — no functional change needed in kryten-py.

Update `mock.py` `add_media` return value to include a synthetic `uid` for testing:

```python
async def add_media(self, channel, media_type, media_id, *, position="end", domain=None) -> dict:
    self._record_command(channel, "queue", {"type": media_type, "id": media_id, "pos": position}, domain)
    # Return synthetic UID for test assertions
    uid = hash((channel, media_type, media_id)) % 10000
    return {"success": True, "uid": uid}
```

### Version bumps

| Service | Current | Bump to |
|---|---|---|
| Kryten-Robot | current | patch |
| kryten-api-gate | 0.3.5 | 0.3.6 |
| kryten-py | 0.16.0 | 0.16.1 (mock + docstring only) |

---

## Gap 4, 5, 6 — kryten-economy: three new NATS spending commands

These are required before Phase 2 (pay-to-play queue submission) can be built. They have no effect on Phase 1 functionality.

All three commands are handled on NATS subject `kryten.economy.command`. The existing command dispatch router in kryten-economy must be extended with three new command names: `spending.queue_preview`, `spending.queue`, and `spending.queue_refund`.

**What exists:** `SpendingEngine.get_price_tier(duration_sec)`, `apply_discount(base_cost, rank_tier_index)`, `validate_spend(username, channel, amount, spend_type)`, `db.get_last_queue_time(username, channel)`, `daily_activity.queues_used`, `config.spending.max_queues_per_day`, `config.spending.queue_cooldown_minutes`, `config.spending.blackout_windows`, `db.debit()`, `db.credit()`.

**What needs adding:** (1) Three command handlers in `command_handler.py`. (2) A new `queue_spend_requests` table for idempotency. (3) Helper to read/increment `daily_activity.queues_used`.

**NATS envelope note:** All kryten-economy command handlers read fields flat from the top-level request dict (not nested under a `payload` key). Requests below are shown in the correct flat format.

---

### Gap 4 — `spending.queue_preview`

Read-only cost estimate. No state is modified. Callable at any time, including before the user confirms.

**NATS request envelope:**
```json
{
  "command": "spending.queue_preview",
  "username": "someuser",
  "duration_sec": 5520,
  "tier": "queue",
  "channel": "Q_A"
}
```

`tier` is `"queue"` or `"playnext"`. Pricing is determined solely by `duration_sec` against the existing tier brackets in kryten-economy config. The `tier` field is for queue ordering only and does not affect cost calculation.

**NATS response:**
```json
{
  "available": true,
  "cost_z": 250,
  "tier_label": "Movie",
  "discount_pct": 10,
  "daily_remaining": 2,
  "error_code": null
}
```

`available: false` cases:

| `error_code` | Condition |
|---|---|
| `"daily_limit_reached"` | User has hit their daily queue limit |
| `"cooldown_active"` | User must wait before queuing again; include `cooldown_remaining_sec` |
| `"insufficient_balance"` | Balance < cost |
| `"blackout_active"` | kryten-economy blackout window is active |

When `available` is false, `cost_z` is still populated (the cost they *would* pay) so the UI can display it.

**Implementation notes:**
- Call `SpendingEngine.get_price_tier(duration_sec)` → `(tier_label, base_cost)` for the raw cost
- Call `db.get_account(username, channel)` then `SpendingEngine.get_rank_tier_index(account)` → tier index
- Call `SpendingEngine.apply_discount(base_cost, tier_index)` → `(final_cost, discount_fraction)` for the discounted price
- For `error_code` checks (in order):
  1. `blackout_active` — check `config.spending.blackout_windows` against current UTC time
  2. `daily_limit_reached` — call `get_or_create_daily_activity()` and compare `queues_used` against `config.spending.max_queues_per_day`
  3. `cooldown_active` — call `db.get_last_queue_time(username, channel)`; if within `config.spending.queue_cooldown_minutes`, set `cooldown_remaining_sec`
  4. `insufficient_balance` — `SpendingEngine.validate_spend(username, channel, final_cost, "queue")` returns `SpendResult.INSUFFICIENT_BALANCE`
- Do not modify any ledger or state

---

### Gap 5 — `spending.queue`

Atomic validate + debit. Idempotent via `request_id`.

**NATS request envelope:**
```json
{
  "command": "spending.queue",
  "username": "someuser",
  "duration_sec": 5520,
  "tier": "queue",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "channel": "Q_A"
}
```

**NATS response (success):**
```json
{
  "success": true,
  "cost_z": 250,
  "new_balance": 1750,
  "error_code": null
}
```

**NATS response (failure):**
```json
{
  "success": false,
  "cost_z": 250,
  "new_balance": 2000,
  "error_code": "insufficient_balance"
}
```

**Idempotency contract:**
- Insert `(request_id, username, channel, cost_z, tier, created_at)` into new table `queue_spend_requests` **before** debiting
- `queue_spend_requests` schema: `request_id TEXT PK, username, channel, cost_z, tier, created_at, refunded INTEGER DEFAULT 0, refunded_at`
- On duplicate `request_id`: return the stored result without re-debiting
- Idempotency window: 24 hours (covers the session TTL)

**Implementation notes:**
- Run the same eligibility checks as `spending.queue_preview` (blackout → daily limit → cooldown → balance); return failure without debiting if any fail
- Insert `request_id` record into `queue_spend_requests` (INSERT OR IGNORE to guard against race)
- Debit atomically: `db.debit(username, channel, cost_z, tx_type="debit", reason=f"Queue: {tier}", trigger_id="spend.queue")`
- If `db.debit()` returns `None` (insufficient funds), delete the idempotency record and return `error_code: "insufficient_balance"`
- Increment `daily_activity.queues_used` for the debit day

---

### Gap 6 — `spending.queue_refund`

Compensating credit. Safe to call multiple times with the same `request_id` (idempotent).

**NATS request envelope:**
```json
{
  "command": "spending.queue_refund",
  "username": "someuser",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "reason": "playlist_add_failed",
  "channel": "Q_A"
}
```

`reason` values (informational — for ledger annotation only):

| Value | When issued |
|---|---|
| `"playlist_add_failed"` | kryten-api-gate call failed after debit |
| `"schedule_displacement"` | Paid item was in queue when a scheduled playlist fired |
| `"immutability_restriction"` | Item turned out to be on an immutable playlist |
| `"admin_removal"` | Admin manually removed the queued item |

**NATS response:**
```json
{
  "success": true,
  "refunded_z": 250,
  "new_balance": 2000
}
```

**Idempotency contract:**
- Look up `request_id` in the ledger/idempotency store
- If the original debit exists and has not already been refunded: credit `cost_z` back, mark as refunded
- If already refunded: return `success: true` with the stored `refunded_z` and current balance (no double-credit)
- If `request_id` not found (debit never occurred or TTL expired): return `success: false, error: "unknown_request_id"`

**Implementation notes:**
- Look up `request_id` in `queue_spend_requests`
- If the record exists and `refunded = 0`: call `db.credit(username, channel, cost_z, tx_type="credit", reason=f"Refund: {reason}", trigger_id="spend.queue_refund")`; set `refunded = 1`, `refunded_at = now()` on the record
- If `refunded = 1`: return `success: true` with `refunded_z = cost_z` and current balance (no double-credit)
- If `request_id` not found: return `success: false, error: "unknown_request_id"`

### Version bump

| Service | Bump to | Notes |
|---|---|---|
| kryten-economy | next minor | New NATS command surface only |

---

## Gap 7 — `PUT /playlist/{uid}/move` must accept string position values

### Why it matters

The FIFO ordering algorithm needs to place an item at the **front** of the playlist (before all other items) when no pay-to-play items exist. CyTube's `moveMedia` socket event supports `"prepend"` as the `after` value, meaning "place before everything." However, the api-gate `MoveMediaRequest` model and kryten-py's `move_media()` both restrict `position` to `int`.

### Current behaviour

```python
# kryten-api-gate: routes/playlist.py
class MoveMediaRequest(BaseModel):
    position: int  # ← rejects "prepend"

# kryten-py: client.py
async def move_media(self, channel: str, uid: int, position: int, ...) -> str:
    return await self.__send_command(..., body={"from": uid, "after": position}, ...)
```

CyTube event sender already supports string values:
```python
# Kryten-Robot: cytube_event_sender.py
async def move_video(self, uid: str, after: str) -> bool:
    # after can be UID, "prepend", or "append"
    payload = {"from": from_uid, "after": after_val}
    await self._connector._socket.emit("moveMedia", payload)
```

### Required changes

#### kryten-api-gate

```python
class MoveMediaRequest(BaseModel):
    position: int | str  # UID (int) or "prepend" / "append"

    @field_validator("position")
    @classmethod
    def validate_position(cls, v):
        if isinstance(v, str) and v not in ("prepend", "append"):
            raise ValueError('string position must be "prepend" or "append"')
        return v
```

#### kryten-py

```python
async def move_media(
    self,
    channel: str,
    uid: int,
    position: int | str,  # ← was int
    *,
    domain: str | None = None,
) -> str:
    return await self.__send_command(
        service="robot",
        channel=channel,
        type="mvvideo",
        body={"from": uid, "after": position},
        domain=domain,
    )
```

### Version bumps

| Service | Bump to |
|---|---|
| kryten-api-gate | 0.3.6 (combine with Gap 3) |
| kryten-py | 0.16.1 (combine with Gap 3) |

---

## Gap 8 — Economy proxy endpoints in kryten-api-gate

### Why it matters

webqueue communicates exclusively via HTTP. The product plan's references to direct NATS calls to `kryten.economy.command` are invalid. kryten-api-gate must proxy all economy commands.

### Required new endpoints

All routes on a new `economy` router, prefix `/api/v1/economy/`.

#### Phase 1 endpoints (balance display)

**`GET /economy/balance/{username}`**

Proxies to existing NATS command `balance.get`.

```
Request:  GET /api/v1/economy/balance/someuser
Response: { "username": "someuser", "balance": 2000, "lifetime_earned": 5000, "rank_name": "Member", "found": true }
```

Returns `{ "found": false }` if the user has no economy account.

**`GET /economy/transactions/{username}`**

Proxies to existing NATS command `transactions.list` (per-user history, newest first).

> **Note:** `transactions.recent` is a channel-level command (all users). For per-user history, use `transactions.list`.

```
Request:  GET /api/v1/economy/transactions/someuser?limit=20
Response: {
  "username": "someuser",
  "limit": 20,
  "offset": 0,
  "transactions": [
    { "id": 42, "type": "debit", "amount": 250, "reason": "Queue: Movie Title", "created_at": "..." },
    ...
  ]
}
```

#### Phase 2 endpoints (spending)

**`POST /economy/queue-preview`**

Proxies to `spending.queue_preview` (Gap 4).

```
Request body:  { "username": "someuser", "duration_sec": 5520, "tier": "queue" }
Response:      { "available": true, "cost_z": 250, "tier_label": "Movie", "discount_pct": 10, "error_code": null }
```

**`POST /economy/queue-spend`**

Proxies to `spending.queue` (Gap 5).

```
Request body:  { "username": "someuser", "duration_sec": 5520, "tier": "queue", "request_id": "uuid" }
Response:      { "success": true, "cost_z": 250, "new_balance": 1750, "error_code": null }
```

**`POST /economy/queue-refund`**

Proxies to `spending.queue_refund` (Gap 6).

```
Request body:  { "username": "someuser", "request_id": "uuid", "reason": "playlist_add_failed" }
Response:      { "success": true, "refunded_z": 250, "new_balance": 2000 }
```

### Implementation in api-gate

New file: `src/kryten_api_gate/routes/economy.py`

**Important:** kryten-economy's `_handle_command` reads all fields flat from the top-level request dict (not nested under a `payload` key). The NATS response envelope is `{"service", "command", "success": bool, "data": {...}}` — the proxy must unwrap `data` and raise on `success: false`.

```python
"""Economy proxy routes — forwards to kryten-economy via NATS."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from kryten import KrytenClient

from ..auth import verify_api_key
from ..deps import get_client, get_config
from ..config import Config

router = APIRouter(dependencies=[Depends(verify_api_key)])


def _unwrap(result: dict) -> dict:
    """Unwrap NATS economy response envelope, raising on failure."""
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("error", "economy error"))
    return result.get("data", {})


@router.get("/balance/{username}")
async def get_balance(
    username: str,
    client: KrytenClient = Depends(get_client),
    config: Config = Depends(get_config),
) -> dict:
    result = await client.economy_request(
        config.channel, "balance.get", {"username": username}
    )
    return _unwrap(result)


@router.get("/transactions/{username}")
async def get_transactions(
    username: str,
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    client: KrytenClient = Depends(get_client),
    config: Config = Depends(get_config),
) -> dict:
    result = await client.economy_request(
        config.channel, "transactions.list", {"username": username, "limit": limit, "offset": offset}
    )
    return _unwrap(result)


class QueuePreviewRequest(BaseModel):
    username: str
    duration_sec: int
    tier: str = "queue"


@router.post("/queue-preview")
async def queue_preview(
    body: QueuePreviewRequest,
    client: KrytenClient = Depends(get_client),
    config: Config = Depends(get_config),
) -> dict:
    result = await client.economy_request(
        config.channel, "spending.queue_preview", body.model_dump()
    )
    return _unwrap(result)


class QueueSpendRequest(BaseModel):
    username: str
    duration_sec: int
    tier: str = "queue"
    request_id: str


@router.post("/queue-spend")
async def queue_spend(
    body: QueueSpendRequest,
    client: KrytenClient = Depends(get_client),
    config: Config = Depends(get_config),
) -> dict:
    result = await client.economy_request(
        config.channel, "spending.queue", body.model_dump()
    )
    return _unwrap(result)


class QueueRefundRequest(BaseModel):
    username: str
    request_id: str
    reason: str


@router.post("/queue-refund")
async def queue_refund(
    body: QueueRefundRequest,
    client: KrytenClient = Depends(get_client),
    config: Config = Depends(get_config),
) -> dict:
    result = await client.economy_request(
        config.channel, "spending.queue_refund", body.model_dump()
    )
    return _unwrap(result)
```

Register in `app.py`:
```python
from .routes import economy
app.include_router(economy.router, prefix="/api/v1/economy", tags=["economy"])
```

### kryten-py dependency

Requires a new `economy_request()` method in KrytenClient that sends to `kryten.economy.command` subject. If this method doesn't exist, it needs implementing as a generic NATS request-reply:

```python
async def economy_request(self, channel: str, command: str, payload: dict) -> dict:
    """Send a command to kryten-economy via NATS request-reply."""
    # kryten-economy reads all fields flat from the top-level request dict.
    # Merge payload into the envelope rather than nesting under 'payload'.
    envelope = {"command": command, "channel": channel, **payload}
    return await self.nats_request("kryten.economy.command", envelope)
```

**Note:** kryten-py already has a public `nats_request(subject, payload)` method. `economy_request` is a thin typed wrapper around it. If the api-gate economy routes call `client.nats_request("kryten.economy.command", envelope)` directly, adding the named wrapper is optional but recommended for clarity.

### Version bumps

| Service | Bump to | Notes |
|---|---|---|
| kryten-api-gate | 0.3.6 (combine with Gaps 3, 7 & 10) | New economy router |
| kryten-py | 0.16.1 (if `economy_request` doesn't exist) | New method |

### Prerequisite confirmation — RESOLVED

**`balance.get`** ✅ Confirmed in kryten-economy v0.8.10 (`kryten_economy/command_handler.py` line 686).
**`transactions.list`** ✅ Confirmed (line 690). Note: `transactions.recent` (line 691) is channel-level, not per-user; use `transactions.list` for the `/transactions/{username}` proxy.

---

## Gap 9 — OTP request rate limiting

### Why it matters

`POST /auth/request-otp` sends a PM to the target CyTube user via api-gate. Without rate limiting, a malicious actor could flood any user with OTP PMs.

### Design (implemented in webqueue, no upstream changes)

| Limit | Scope | Window |
|---|---|---|
| 3 requests | per target username | 10 minutes |
| 10 requests | per source IP | 10 minutes |

Implementation: in-memory sliding window counter (e.g. `collections.deque` with timestamps). No persistence needed — resets on restart are acceptable.

Response when rate-limited: `429 Too Many Requests` with body `{"error": "rate_limited", "retry_after_sec": N}`.

---

## Implementation order (updated)

```
Phase 1 prerequisites (do before starting webqueue Phase 1):
  1. kryten-robot: _handle_add_video await queue confirmation, return uid
  2. kryten-api-gate 0.3.6:
     a. Pass uid through POST /playlist/add response (Gap 3)
     b. Accept int|str position in PUT /playlist/{uid}/move (Gap 7)
     c. Add economy proxy routes: balance, transactions (Gap 8)
     d. Null-guard GET /state/user/{username} for offline users (Gap 10)
  3. kryten-py 0.16.1:
     a. Update mock add_media return shape (Gap 3)
     b. Update move_media type annotation (Gap 7)
     c. Add economy_request method if missing (Gap 8)

Phase 2 prerequisites (can be developed in parallel with webqueue Phase 1):
  4. kryten-economy: implement spending.queue_preview
  5. kryten-economy: implement spending.queue
  6. kryten-economy: implement spending.queue_refund
  7. kryten-api-gate: add economy proxy routes: queue-preview, queue-spend, queue-refund
  8. kryten-economy: release next minor version
```
```

---

---

## Gap 10 — `GET /state/user/{username}` must not return null for offline users

### Why it matters

webqueue calls `GET /state/user/{username}` in two contexts:
1. **Rank validation** — `require_admin` and `get_user_rank` call this on every privileged action.
2. **OTP request** — to confirm the user exists on CyTube before sending a PM (Phase 1).

kryten-py's `get_user()` returns `None` when the user is not found in the channel. The current api-gate route passes this through:

```python
@router.get("/user/{username}")
async def get_user(...) -> dict:
    result = await client.get_user(config.channel, username, domain=config.domain)
    return result  # ← None when offline → FastAPI 500 or JSON null
```

webqueue's rank lookup does:
```python
result = await api_gate.get(f"/state/user/{username}")
return result.get("rank", 0)  # ← AttributeError if result is None
```

This is a Phase 1 blocker because OTP-requesting users are not guaranteed to be online at the time of request.

### Required change — kryten-api-gate

In `src/kryten_api_gate/routes/state.py`, add a null guard and return a safe default:

```python
@router.get("/user/{username}")
async def get_user(
    username: str,
    client: KrytenClient = Depends(get_client),
    config: Config = Depends(get_config),
) -> dict:
    result = await client.get_user(config.channel, username, domain=config.domain)
    if result is None:
        return {"username": username, "rank": 0, "online": False}
    return {**result, "online": True}
```

This guarantees the response is always a valid dict with a `rank` field, regardless of online status.

### webqueue defensive guard (Belt-and-suspenders)

Even after the api-gate fix, webqueue's `get_user_rank` should guard against unexpected null:

```python
async def get_user_rank(api_gate: ApiGateClient, username: str) -> int:
    """Fetch user rank from api-gate. Returns 0 if user not online."""
    result = await api_gate.get(f"/state/user/{username}")
    if not result:
        return 0
    return result.get("rank", 0)
```

### Version bump

| Service | Bump to | Notes |
|---|---|---|
| kryten-api-gate | 0.3.6 (combine with Gaps 3, 7, 8) | Null guard in GET /state/user/{username} |

---

## API_REFERENCE.md update for kryten-api-gate

Once Gap 3 is implemented, the `POST /playlist/add` response section of `API_REFERENCE.md` should be updated from:

```json
{ "success": true }
```

to:

```json
{ "success": true, "uid": 42 }
```

with a note that `uid` is `null` in the rare case that the CyTube confirmation event times out.
