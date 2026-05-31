import asyncio
import json
import logging
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages active WebSocket connections and broadcasts."""

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, username: str, ws: WebSocket):
        async with self._lock:
            # Disconnect existing connection for same user
            if username in self._connections:
                try:
                    await self._connections[username].close()
                except Exception:
                    pass
            self._connections[username] = ws
        logger.info(f"WS connected: {username} (total: {len(self._connections)})")

    async def disconnect(self, username: str):
        async with self._lock:
            self._connections.pop(username, None)
        logger.info(f"WS disconnected: {username} (total: {len(self._connections)})")

    async def broadcast(self, message: dict):
        """Broadcast a message to all connected clients."""
        data = json.dumps(message)
        async with self._lock:
            stale = []
            for username, ws in self._connections.items():
                try:
                    await ws.send_text(data)
                except Exception:
                    stale.append(username)
            for username in stale:
                self._connections.pop(username, None)

    async def send_to(self, username: str, message: dict):
        """Send a message to a specific user."""
        async with self._lock:
            ws = self._connections.get(username)
        if ws:
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                await self.disconnect(username)

    @property
    def connection_count(self) -> int:
        return len(self._connections)
