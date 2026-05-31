# kryten-api-gate — Implementation Spec (Gaps 3, 7, 8, 10)

**Version:** 1.0  
**Date:** 2026-05-30  
**Service:** kryten-api-gate v0.3.5 → v0.3.6  
**Gaps covered:**
- Gap 3 — `POST /playlist/add` must return new item UID
- Gap 7 — `PUT /playlist/{uid}/move` rejects string position values
- Gap 8 — Economy proxy endpoints missing
- Gap 10 — `GET /state/user/{username}` returns `null` for offline users

---

## 1. Gap 10 — Fix `GET /state/user/{username}`

**File:** `src/kryten_api_gate/routes/state.py`

`client.get_user()` returns `None` when the user is not in the channel user-list (offline, or never seen). FastAPI raises a serialisation error when `return None` is reached for a route typed `-> dict`.

**Existing handler (lines 13–20):**

```python
@router.get("/user/{username}")
async def get_user(
    username: str,
    client: KrytenClient = Depends(get_client),
    config: Config = Depends(get_config),
) -> dict:
    result = await client.get_user(config.channel, username, domain=config.domain)
    return result
```

**Replace with:**

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
    return result
```

No other imports needed.

---

## 2. Gap 3 — `POST /playlist/add` returns UID

**File:** `src/kryten_api_gate/routes/playlist.py`

`client.add_media()` already passes the full response dict from kryten-robot. After the robot change (IMPL_ROBOT.md), that dict will include `"uid"`. The api-gate handler just needs to stop discarding it.

**Existing handler (lines 27–43):**

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
    return result
```

**Replace with:**

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
    return {"success": True, "uid": result.get("uid")}
```

The only change is the final `return` line — it now explicitly projects `uid` instead of returning the raw `result` dict (which prevents accidental exposure of internal robot fields).

`uid` is `null` when the robot timed out waiting for the CyTube confirmation event. webqueue handles this by falling back to a `GET /state/playlist` diff.

---

## 3. Gap 7 — `PUT /playlist/{uid}/move` accepts string position

**File:** `src/kryten_api_gate/routes/playlist.py`

The `after` parameter sent to CyTube can be an integer UID (place after that item) or the string `"prepend"` / `"append"`. `MoveMediaRequest.position` is currently typed `int`, so string values are rejected by Pydantic at request validation time.

**Existing model (lines 21–22):**

```python
class MoveMediaRequest(BaseModel):
    position: int
```

**Replace with:**

```python
class MoveMediaRequest(BaseModel):
    position: int | str
```

No other changes needed in `playlist.py`. `client.move_media()` already passes `position` as-is to kryten-py, and after the kryten-py fix (IMPL_KRYTEN_PY.md §1) it will accept `int | str`. `CytubeEventSender.move_video()` already handles string values.

---

## 4. Gap 8 — Economy proxy routes

### 4a. New file

**File:** `src/kryten_api_gate/routes/economy.py`

```python
"""Economy proxy routes — forwards to kryten-economy via NATS."""

from fastapi import APIRouter, Depends, HTTPException, Query
from kryten import KrytenClient
from pydantic import BaseModel

from ..auth import verify_api_key
from ..config import Config
from ..deps import get_client, get_config

router = APIRouter(dependencies=[Depends(verify_api_key)])


def _unwrap(result: dict) -> dict:
    """Unwrap NATS economy response envelope, raising HTTP 502 on failure."""
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("error", "economy error"))
    return result.get("data", {})


# ── Phase 1: Balance display ───────────────────────────────────

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
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    client: KrytenClient = Depends(get_client),
    config: Config = Depends(get_config),
) -> dict:
    result = await client.economy_request(
        config.channel,
        "transactions.list",
        {"username": username, "limit": limit, "offset": offset},
    )
    return _unwrap(result)


# ── Phase 2: Queue spending ────────────────────────────────────

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

### 4b. Register the router

**File:** `src/kryten_api_gate/app.py`

Add import alongside the other route imports:

```python
from .routes import (
    admin,
    chat,
    economy,       # ← add
    emotes,
    filters,
    kv,
    library,
    moderation,
    playback,
    playlist,
    polls,
    state,
    system,
)
```

Add registration inside `create_app()` after the `state` router:

```python
    app.include_router(state.router, prefix="/api/v1/state", tags=["state"])
    app.include_router(economy.router, prefix="/api/v1/economy", tags=["economy"])  # ← add
```

### 4c. `_unwrap` and `spending.*` routes

The Phase 2 routes (`queue-preview`, `queue-spend`, `queue-refund`) proxy to kryten-economy
commands that do **not yet exist** (they are added by IMPL_ECONOMY.md). Including them in the
route file now is correct — they will return HTTP 502 with `"Unknown command: spending.queue_preview"`
until the economy service is updated, which is an acceptable failure mode.

---

## 5. Version bump

**File:** `pyproject.toml`

```toml
version = "0.3.6"
```

---

## 6. Summary of file changes

| File | Change |
|---|---|
| `src/kryten_api_gate/routes/state.py` | Null-guard on `get_user` result (Gap 10) |
| `src/kryten_api_gate/routes/playlist.py` | `add_media` return explicit `uid` (Gap 3); `MoveMediaRequest.position: int \| str` (Gap 7) |
| `src/kryten_api_gate/routes/economy.py` | **New file** — economy proxy router (Gap 8) |
| `src/kryten_api_gate/app.py` | Register economy router (Gap 8) |
| `pyproject.toml` | `version = "0.3.6"` |

---

## 7. Tests

### Existing tests to update

If `test_endpoints.py` has tests for `POST /playlist/add`, update expected response shape to
`{"success": True, "uid": <int|null>}`.

### New tests

**`tests/test_state.py`** (or extend existing):

| Test | What to verify |
|---|---|
| `test_get_user_online` | Returns full user dict when `client.get_user()` returns data |
| `test_get_user_offline` | Returns `{"username": "x", "rank": 0, "online": False}` when `client.get_user()` returns `None` |

**`tests/test_economy.py`** (new file):

| Test | What to verify |
|---|---|
| `test_get_balance_success` | `_unwrap` extracts `data`; 200 response with balance fields |
| `test_get_balance_economy_error` | Economy returns `success: false`; HTTP 502 raised |
| `test_get_transactions_pagination` | `limit` and `offset` query params forwarded to economy request |
| `test_queue_preview_proxied` | Request body forwarded flat (not nested under `payload`) |
| `test_queue_spend_proxied` | `request_id` included in forwarded payload |
| `test_queue_refund_proxied` | `reason` included in forwarded payload |

**`tests/test_playlist.py`** (extend or create):

| Test | What to verify |
|---|---|
| `test_move_with_int_position` | `{"position": 42}` accepted |
| `test_move_with_string_prepend` | `{"position": "prepend"}` accepted, not rejected by Pydantic |
| `test_move_with_string_append` | `{"position": "append"}` accepted |
