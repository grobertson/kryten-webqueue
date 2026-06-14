import httpx


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

    # --- State ---

    async def get_playlist(self) -> list[dict]:
        result = await self.get("/state/playlist")
        return result.get("items", [])

    async def get_now_playing(self) -> dict:
        return await self.get("/state/now-playing")

    async def get_user(self, username: str) -> dict:
        return await self.get(f"/state/user/{username}")

    # --- Playlist CRUD ---

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

    async def playlist_jump(self, uid: int) -> dict:
        return await self.post(f"/playlist/{uid}/jump")

    # --- Chat ---

    async def send_chat(self, message: str) -> dict:
        return await self.post("/chat/send", json={"message": message})

    async def send_pm(self, username: str, message: str) -> dict:
        return await self.post("/chat/pm", json={"username": username, "message": message})

    # --- Admin ---

    async def get_motd(self) -> str:
        result = await self.get("/admin/motd")
        return result.get("motd", "")

    # --- Economy proxy ---

    async def get_balance(self, username: str) -> dict:
        return await self.get(f"/economy/balance/{username}")

    async def get_transactions(self, username: str, limit: int = 20, offset: int = 0) -> dict:
        return await self.get(f"/economy/transactions/{username}", limit=limit, offset=offset)

    async def get_account_summary(self, username: str) -> dict:
        return await self.get(f"/economy/account/{username}")

    async def set_vanity_greeting(self, username: str, value: str) -> dict:
        return await self.post("/economy/vanity/greeting", json={
            "username": username,
            "value": value,
        })

    async def set_vanity_color(self, username: str, value: str) -> dict:
        return await self.post("/economy/vanity/color", json={
            "username": username,
            "value": value,
        })

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
