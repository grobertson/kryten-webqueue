# Kryten-Robot — Implementation Spec (Gap 3)

**Version:** 1.0  
**Date:** 2026-05-30  
**Service:** Kryten-Robot (current → next patch)  
**Gaps covered:** 3 (`POST /playlist/add` must return new item UID)

---

## Overview

`_handle_add_video` currently fires `queue` at CyTube and returns `{"success": True}` immediately.
It must instead register a one-shot listener for CyTube's confirmation `queue` event, await the UID
contained in that event, then return `{"success": True, "uid": <int>}`.

No changes are needed to `CytubeEventSender`, `state_updater.py`, or any other file.

---

## How the CyTube confirmation event arrives

When the bot emits `queue` to CyTube, CyTube processes it and emits a `queue` event back to all
clients (including the bot) confirming the item was added. The payload shape is:

```json
{
  "item": {
    "uid": 42,
    "media": { "title": "...", "duration": 123, ... },
    "temp": true
  },
  "after": null
}
```

`CytubeConnector._consume_socket_events()` (cytube_connector.py line 709) calls
`self._fire_callbacks(event_name, payload)` synchronously as each raw socket event arrives.
Callbacks registered via `connector.on_event("queue", cb)` therefore run in the event loop — it is
safe to call `future.set_result()` from inside them.

---

## 1. Modify `_handle_add_video`

**File:** `kryten/robot_command_handler.py`

**Existing method (lines 408–428):**

```python
async def _handle_add_video(self, args: dict[str, Any]) -> dict[str, Any]:
    """Handle add video command."""
    if not self.sender:
        raise RuntimeError("CytubeEventSender not available")

    # Map args to add_video parameters
    # args might contain: type, id, pos, temp OR url
    # client.py sends: type, id, pos, temp

    success = await self.sender.add_video(
        url=args.get("url"),
        media_type=args.get("type"),
        media_id=args.get("id"),
        position=args.get("pos", "end"),
        temp=args.get("temp", True),
    )

    if success:
        return {"success": True}
    return {"success": False, "error": "Failed to add video"}
```

**Replace with:**

```python
async def _handle_add_video(self, args: dict[str, Any]) -> dict[str, Any]:
    """Handle add video command. Returns UID of newly added item."""
    if not self.sender:
        raise RuntimeError("CytubeEventSender not available")
    if not self.connector:
        raise RuntimeError("CytubeConnector not available")

    loop = asyncio.get_event_loop()
    uid_future: asyncio.Future[int | None] = loop.create_future()

    def _on_queue_event(event_name: str, payload: dict) -> None:
        # Called synchronously from _fire_callbacks while the event loop is running.
        # Resolve only the FIRST matching event; ignore subsequent ones.
        if uid_future.done():
            return
        item = payload.get("item", {})
        uid = item.get("uid")
        uid_future.set_result(int(uid) if uid is not None else None)

    self.connector.on_event("queue", _on_queue_event)
    try:
        success = await self.sender.add_video(
            url=args.get("url"),
            media_type=args.get("type"),
            media_id=args.get("id"),
            position=args.get("pos", "end"),
            temp=args.get("temp", True),
        )

        if not success:
            return {"success": False, "error": "Failed to add video"}

        try:
            uid = await asyncio.wait_for(uid_future, timeout=8.0)
        except asyncio.TimeoutError:
            self.logger.warning("Timed out waiting for CyTube queue confirmation")
            uid = None

    finally:
        self.connector.off_event("queue", _on_queue_event)

    return {"success": True, "uid": uid}
```

### Notes

- **Timeout:** 8 seconds matches the kryten-py `add_media` default timeout. If the confirmation
  does not arrive in time, `uid` is returned as `null` — webqueue handles this by falling back to
  a `GET /state/playlist` diff (as documented in PRE_PLAN_GAPS.md Gap 3).

- **Race condition:** If two simultaneous `_handle_add_video` calls are in flight, each callback
  will fire for every incoming `queue` event and whichever future is not yet resolved will take the
  first event. This is acceptable because kryten-api-gate serialises playlist mutations through
  the NATS request-reply pattern (one outstanding request at a time per channel). Concurrent adds
  on the same channel from different sources (e.g. bot auto-queue) would be a rare edge case and
  the fallback null → diff covers it.

- **`asyncio.get_event_loop()`:** The method runs inside the NATS subscription callback which is
  already on the event loop. Use `asyncio.get_running_loop()` if targeting Python 3.10+.

---

## 2. Add `logger` attribute check

Confirm that `RobotCommandHandler` exposes `self.logger` (it is already set as `self.logger` in the
constructor). No change needed — the attribute name is consistent throughout the class.

---

## 3. Version bump

**File:** `pyproject.toml`

Kryten-Robot's current version is `1.6.0`. This change introduces new observable behaviour
(add-media response shape), so a minor bump is appropriate.

```toml
version = "1.7.0"
```

`kryten/__init__.py` reads `__version__` via `importlib.metadata.version("kryten-robot")` —
`pyproject.toml` is the single source of truth. No other files need updating.

CI (`release.yml`) detects the `pyproject.toml` change on `main`, creates tag `v1.7.0`, and
calls `python-publish.yml` to publish to PyPI.

---

## 4. Tests

**File:** `tests/test_handle_add_video.py` (new file)

| Test | What to verify |
|---|---|
| `test_add_video_returns_uid` | Simulate CyTube `queue` event arriving after emit; assert `{"success": True, "uid": 42}` |
| `test_add_video_uid_null_on_timeout` | Do not fire any `queue` event; assert `{"success": True, "uid": None}` after 8s timeout (mock the timeout) |
| `test_add_video_sender_failure` | `sender.add_video()` returns `False`; assert `{"success": False, ...}` |
| `test_add_video_callback_cleaned_up` | After success, callback is unregistered via `off_event` |

**Testing pattern for the confirmation event:**

Because `_fire_callbacks` is called synchronously in the event loop, you can simulate the
confirmation by scheduling `connector._fire_callbacks("queue", {"item": {"uid": 42}})` in the
event loop after the `add_video` emit:

```python
async def test_add_video_returns_uid():
    handler = build_handler()  # construct with mock connector and sender

    # Schedule the CyTube confirmation to fire after a short delay
    async def fire_confirmation():
        await asyncio.sleep(0.01)
        handler.connector._fire_callbacks("queue", {"item": {"uid": 42}})

    asyncio.create_task(fire_confirmation())
    result = await handler._handle_add_video({"type": "yt", "id": "abc123"})

    assert result == {"success": True, "uid": 42}
```
