import json
import logging
import uuid
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import jwt

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket endpoint. Authenticates via session cookie on upgrade."""
    # Authenticate from cookie
    token = ws.cookies.get("session")
    if not token:
        await ws.close(code=4001, reason="Not authenticated")
        return

    config = ws.app.state.config
    try:
        payload = jwt.decode(token, config.secret_key, algorithms=["HS256"])
        username = payload["sub"]
    except jwt.InvalidTokenError:
        await ws.close(code=4001, reason="Invalid session")
        return

    await ws.accept()
    manager = ws.app.state.ws_manager
    shadow = ws.app.state.shadow

    await manager.connect(username, ws)

    # Send current queue state on connect
    try:
        state = shadow.get_queue_state()
        await ws.send_text(json.dumps({"type": "queue_state", "data": state}))
    except Exception:
        await manager.disconnect(username)
        return

    # Message loop
    try:
        while True:
            text = await ws.receive_text()
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")
            if msg_type == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"WS error for {username}: {e}")
    finally:
        await manager.disconnect(username)


@router.websocket("/ws/race")
async def race_websocket_endpoint(ws: WebSocket):
    """Public race-view WebSocket. No authentication — streams only race frames.

    Anyone watching the channel can open the race view without logging into the
    queue dashboard. Connections are anonymous (keyed by a random id) and only
    ever receive race frames (no user/queue data), so this is safe to expose.
    """
    await ws.accept()
    manager = ws.app.state.race_ws_manager
    poller = getattr(ws.app.state, "race_poller", None)
    conn_id = uuid.uuid4().hex

    await manager.connect(conn_id, ws)

    # Show a race that's already in progress to a late-joining spectator.
    try:
        if poller is not None and poller.last_frame is not None:
            await ws.send_text(
                json.dumps({"type": "race_frame", "data": poller.last_frame})
            )
    except Exception:
        await manager.disconnect(conn_id)
        return

    # Keep the socket open; ignore inbound except ping/pong.
    try:
        while True:
            text = await ws.receive_text()
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"Race WS error: {e}")
    finally:
        await manager.disconnect(conn_id)
