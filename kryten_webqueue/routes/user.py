from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, field_validator

from ..auth.session import get_current_user

router = APIRouter(prefix="/user", tags=["user"])


@router.get("/balance")
async def get_balance(request: Request, user: dict = Depends(get_current_user)):
    """Get user's economy balance."""
    api_gate = request.app.state.api_gate
    return await api_gate.get_balance(user["username"])


@router.get("/account")
async def get_account(request: Request, user: dict = Depends(get_current_user)):
    """Get the user's full economy account summary (rank, progress, perks, vanity)."""
    api_gate = request.app.state.api_gate
    return await api_gate.get_account_summary(user["username"])


class GreetingUpdate(BaseModel):
    value: str


class ColorUpdate(BaseModel):
    value: str


class ShoutoutRequest(BaseModel):
    # Mirrors the economy's shoutout limits so a bypassed UI still gets a
    # consistent, early rejection instead of forwarding arbitrary-length text.
    value: str

    @field_validator("value")
    @classmethod
    def _validate_value(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Shoutout message is required.")
        if len(v) > 200:
            raise ValueError("Message too long (max 200 characters).")
        return v


@router.post("/vanity/greeting")
async def set_vanity_greeting(
    body: GreetingUpdate, request: Request, user: dict = Depends(get_current_user)
):
    """Purchase/update the current user's custom greeting."""
    api_gate = request.app.state.api_gate
    try:
        return await api_gate.set_vanity_greeting(user["username"], body.value)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=_economy_error(exc)) from exc


@router.post("/vanity/color")
async def set_vanity_color(
    body: ColorUpdate, request: Request, user: dict = Depends(get_current_user)
):
    """Purchase/update the current user's custom chat color (6-digit hex)."""
    api_gate = request.app.state.api_gate
    try:
        return await api_gate.set_vanity_color(user["username"], body.value)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=_economy_error(exc)) from exc


@router.post("/vanity/shoutout")
async def set_vanity_shoutout(
    body: ShoutoutRequest, request: Request, user: dict = Depends(get_current_user)
):
    """Purchase a shoutout — the bot posts the message to public chat."""
    api_gate = request.app.state.api_gate
    try:
        return await api_gate.set_vanity_shoutout(user["username"], body.value)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=_economy_error(exc)) from exc


def _economy_error(exc: Exception) -> str:
    """Extract a human-readable message from an api-gate HTTP error."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        try:
            detail = exc.response.json().get("detail")
            if detail:
                return str(detail)
        except Exception:  # noqa: BLE001
            pass
    return "Purchase failed. Please try again."


@router.get("/transactions")
async def get_transactions(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    user: dict = Depends(get_current_user),
):
    """Get user's transaction history."""
    api_gate = request.app.state.api_gate
    return await api_gate.get_transactions(user["username"], limit=limit, offset=offset)


@router.get("/profile")
async def get_profile(request: Request, user: dict = Depends(get_current_user)):
    """Get current user profile info from api-gate."""
    api_gate = request.app.state.api_gate
    try:
        user_data = await api_gate.get_user(user["username"])
    except Exception:
        user_data = {}
    return {
        "username": user["username"],
        "rank": user["rank"],
        "online": user_data.get("online", False) if user_data else False,
    }


# --- Watchlist ---


@router.get("/watchlist")
async def get_watchlist(request: Request, user: dict = Depends(get_current_user)):
    """Return all friendly_tokens in the user's watchlist (for client-side state)."""
    db = request.app.state.db
    tokens = await db.watchlist_tokens(user["username"])
    return {"tokens": tokens}


@router.post("/watchlist/{token}")
async def add_to_watchlist(
    token: str, request: Request, user: dict = Depends(get_current_user)
):
    """Add a catalog item to the user's watchlist."""
    db = request.app.state.db
    item = await db.get_item(token)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    added = await db.watchlist_add(user["username"], token)
    if not added:
        raise HTTPException(status_code=409, detail="Already in watchlist")
    return {"added": True}


@router.delete("/watchlist/{token}")
async def remove_from_watchlist(
    token: str, request: Request, user: dict = Depends(get_current_user)
):
    """Remove a catalog item from the user's watchlist."""
    db = request.app.state.db
    removed = await db.watchlist_remove(user["username"], token)
    if not removed:
        raise HTTPException(status_code=404, detail="Not in watchlist")
    return {"removed": True}
