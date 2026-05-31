from fastapi import APIRouter, Request, Depends

from ..auth.session import get_current_user

router = APIRouter(prefix="/user", tags=["user"])


@router.get("/balance")
async def get_balance(request: Request, user: dict = Depends(get_current_user)):
    """Get user's economy balance."""
    api_gate = request.app.state.api_gate
    return await api_gate.get_balance(user["username"])


@router.get("/transactions")
async def get_transactions(request: Request, limit: int = 20, offset: int = 0,
                           user: dict = Depends(get_current_user)):
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
