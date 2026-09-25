import json
from datetime import UTC, datetime

from kryten_webqueue.ws.manager import WebSocketManager


class _FakeWebSocket:
    def __init__(self):
        self.messages: list[str] = []

    async def send_text(self, data: str):
        self.messages.append(data)


async def test_broadcast_encodes_postgres_datetime_values():
    manager = WebSocketManager()
    websocket = _FakeWebSocket()
    manager._connections["viewer"] = websocket
    timestamp = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)

    await manager.broadcast({"type": "queue_state", "data": {"added_at": timestamp}})

    assert json.loads(websocket.messages[0]) == {
        "type": "queue_state",
        "data": {"added_at": "2026-09-23T20:00:00+00:00"},
    }


async def test_send_to_encodes_postgres_datetime_values():
    manager = WebSocketManager()
    websocket = _FakeWebSocket()
    manager._connections["viewer"] = websocket
    timestamp = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)

    await manager.send_to("viewer", {"updated_at": timestamp})

    assert json.loads(websocket.messages[0]) == {
        "updated_at": "2026-09-23T20:00:00+00:00"
    }
