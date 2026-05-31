# kryten-py — Implementation Spec (Gaps 3, 7, 8)

**Version:** 1.0  
**Date:** 2026-05-30  
**Library:** kryten-py v0.16.0 → v0.16.1  
**Gaps covered:**
- Gap 3 — `add_media()` return type change (mock update + docstring)
- Gap 7 — `move_media()` must accept `int | str` for position
- Gap 8 — New `economy_request()` method

---

## Overview

Three targeted changes to `src/kryten/client.py` and `src/kryten/mock.py`. No config, model, or
transport layer changes.

---

## 1. Gap 7 — `move_media()` accepts `int | str`

**File:** `src/kryten/client.py`

`CytubeEventSender.move_video()` already handles string `after` values (`"prepend"`, `"append"`).
The type restriction lives only in `client.py`.

**Existing signature (line 781):**

```python
async def move_media(
    self,
    channel: str,
    uid: int,
    position: int,
    *,
    domain: str | None = None,
) -> str:
    """Move media to new position in playlist."""
    return await self.__send_command(
        service="robot",
        channel=channel,
        type="mvvideo",
        body={"from": uid, "after": position},
        domain=domain,
    )
```

**Replace signature line only:**

```python
async def move_media(
    self,
    channel: str,
    uid: int,
    position: int | str,
    *,
    domain: str | None = None,
) -> str:
    """Move media to new position in playlist.

    Args:
        channel: Channel name.
        uid: UID of item to move.
        position: UID of item to place after (int), or "prepend" / "append" (str).
        domain: Optional domain.

    Returns:
        Correlation ID.
    """
    return await self.__send_command(
        service="robot",
        channel=channel,
        type="mvvideo",
        body={"from": uid, "after": position},
        domain=domain,
    )
```

The body dict passes `position` unchanged — kryten-robot's `_handle_move_video` reads `args.get("after")` without type enforcement, and `CytubeEventSender.move_video()` already handles both int and string values.

---

## 2. Gap 7 — `MockKrytenClient.move_media()` signature

**File:** `src/kryten/mock.py`

**Existing signature (line 179):**

```python
async def move_media(
    self,
    channel: str,
    uid: int,
    position: int,
    *,
    domain: str | None = None,
) -> str:
    """Mock move media command."""
    return self._record_command(channel, "move", {"from": uid, "after": position}, domain)
```

**Replace with:**

```python
async def move_media(
    self,
    channel: str,
    uid: int,
    position: int | str,
    *,
    domain: str | None = None,
) -> str:
    """Mock move media command."""
    return self._record_command(channel, "move", {"from": uid, "after": position}, domain)
```

---

## 3. Gap 3 — `add_media()` return type and mock

After the kryten-robot change (IMPL_ROBOT.md), `client.add_media()` will receive
`{"success": True, "uid": 42}` from the NATS reply instead of `{"success": True}`.
`client.add_media()` is already a passthrough (`return await self.nats_request(...)`), so no
functional change is needed in `client.py` — the new `uid` field arrives automatically.

Two changes are required:

### 3a. Update docstring

**File:** `src/kryten/client.py`, `add_media()` docstring (around line 723):

Change the `Returns:` section from:

```
Returns:
    Response dict with "success" bool and optional "error" string
```

to:

```
Returns:
    Response dict with keys:
        - "success" (bool)
        - "uid" (int | None): UID of the newly added item, or None if CyTube
          confirmation timed out
        - "error" (str): present when success is False
```

### 3b. Update mock

**File:** `src/kryten/mock.py`

`MockKrytenClient.add_media()` currently returns a correlation ID string (the `_record_command`
return value), which is inconsistent with the real client's dict return. After the robot change
the real client returns a dict. The mock must also return a dict.

**Existing method (lines 152–167):**

```python
async def add_media(
    self,
    channel: str,
    media_type: str,
    media_id: str,
    *,
    position: str = "end",
    domain: str | None = None,
) -> str:
    """Mock add media command."""
    return self._record_command(
        channel,
        "queue",
        {"type": media_type, "id": media_id, "pos": position},
        domain,
    )
```

**Replace with:**

```python
async def add_media(
    self,
    channel: str,
    media_type: str,
    media_id: str,
    *,
    position: str = "end",
    temp: bool = True,
    domain: str | None = None,
) -> dict:
    """Mock add media command. Returns synthetic UID for testing."""
    import random
    uid = random.randint(1000, 9999)
    self._record_command(
        channel,
        "queue",
        {"type": media_type, "id": media_id, "pos": position, "temp": temp},
        domain,
    )
    return {"success": True, "uid": uid}
```

> **Note on `temp` parameter:** The real `client.add_media()` accepts `temp: bool = True` but the
> mock was missing it. Added here for parity. Any test that passes `temp=False` to the real client
> should work with the mock now.

---

## 4. Gap 8 — `economy_request()` method

**File:** `src/kryten/client.py`

Add a new public method after `nats_request()` (currently ending around line 3498):

```python
async def economy_request(
    self,
    channel: str,
    command: str,
    payload: dict[str, Any],
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Send a command to kryten-economy via NATS request-reply.

    kryten-economy reads all fields flat from the top-level request dict.
    This method merges the payload fields into the envelope alongside
    'command' and 'channel' — do NOT nest payload under a 'payload' key.

    Args:
        channel: Economy channel (e.g. "Q_A").
        command: Economy command name (e.g. "balance.get").
        payload: Additional fields for the command (e.g. {"username": "alice"}).
        timeout: Timeout in seconds.

    Returns:
        Raw NATS response dict: {"service", "command", "success": bool, "data": {...}}
        or {"service", "command", "success": false, "error": "..."}.

    Example:
        >>> result = await client.economy_request(
        ...     "Q_A", "balance.get", {"username": "alice"}
        ... )
        >>> if result.get("success"):
        ...     balance = result["data"]["balance"]
    """
    envelope: dict[str, Any] = {"command": command, "channel": channel, **payload}
    return await self.nats_request("kryten.economy.command", envelope, timeout)
```

### 4a. Mock equivalent

**File:** `src/kryten/mock.py`

Add after `send_command()` or at the end of the playlist methods section:

```python
async def economy_request(
    self,
    channel: str,
    command: str,
    payload: dict[str, Any],
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Mock economy_request. Records the call; returns a configurable response.

    To set up test responses, assign to mock._economy_responses:
        mock._economy_responses["balance.get"] = {
            "success": True,
            "data": {"found": True, "balance": 2000, ...}
        }
    Falls back to {"success": True, "data": {}} for unknown commands.
    """
    self._record_command(channel, f"economy.{command}", payload, None)
    responses: dict = getattr(self, "_economy_responses", {})
    return responses.get(command, {"success": True, "data": {}})
```

Also add `_economy_responses: dict[str, Any] = {}` to `MockKrytenClient.__init__()`.

### 4b. `__init__.py` export

**File:** `src/kryten/__init__.py`

`economy_request` is a method on `KrytenClient` — no new top-level exports needed. Verify that
`KrytenClient` and `MockKrytenClient` are already exported (they are).

---

## 5. Version bump

**File:** `pyproject.toml`

```toml
version = "0.16.1"
```

---

## 6. Summary of file changes

| File | Change |
|---|---|
| `src/kryten/client.py` | `move_media` signature `int | str` (Gap 7); `add_media` docstring (Gap 3); new `economy_request()` method (Gap 8) |
| `src/kryten/mock.py` | `move_media` signature `int | str` (Gap 7); `add_media` returns dict with uid (Gap 3); new `economy_request()` mock method (Gap 8); `_economy_responses` in `__init__` |
| `pyproject.toml` | `version = "0.16.1"` |

---

## 7. Tests

**`tests/test_move_media.py`** (new or extend existing):

| Test | What to verify |
|---|---|
| `test_move_media_int_position` | `position=42` builds `{"from": uid, "after": 42}` in NATS payload |
| `test_move_media_string_prepend` | `position="prepend"` builds `{"from": uid, "after": "prepend"}` |
| `test_move_media_string_append` | `position="append"` builds `{"from": uid, "after": "append"}` |

**`tests/test_economy_request.py`** (new file):

| Test | What to verify |
|---|---|
| `test_economy_request_envelope` | NATS payload is `{"command": "balance.get", "channel": "Q_A", "username": "alice"}` — no nesting |
| `test_economy_request_timeout_forwarded` | Custom `timeout` is passed to `nats_request` |
| `test_economy_request_not_connected` | Raises `KrytenConnectionError` when not connected |

**`tests/test_mock_add_media.py`** (new or extend):

| Test | What to verify |
|---|---|
| `test_mock_add_media_returns_dict` | Return value is a dict with `"success"` and `"uid"` keys |
| `test_mock_add_media_uid_is_int` | `uid` is an integer |
| `test_mock_economy_request_default` | Returns `{"success": True, "data": {}}` when no preset response |
| `test_mock_economy_request_preset` | Returns preset response when `_economy_responses[command]` is set |
