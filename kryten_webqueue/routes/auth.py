from fastapi import APIRouter, Request, HTTPException, Response, Depends

from ..auth.otp import generate_otp, get_otp_expiry
from ..auth.session import create_session_token, get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/otp/request")
async def request_otp(request: Request):
    """Request a one-time password delivered via PM."""
    body = await request.json()
    username = body.get("username", "").strip()
    if not username:
        raise HTTPException(400, "username required")

    rate_limiter = request.app.state.rate_limiter
    if not rate_limiter.is_allowed(f"otp:{username}"):
        raise HTTPException(429, "Too many OTP requests. Try again later.")

    # Generate OTP
    code = generate_otp()
    expires_at = get_otp_expiry()

    # Store in DB
    db = request.app.state.db
    await db.store_otp(username, code, expires_at)

    # Deliver via PM through api-gate
    api_gate = request.app.state.api_gate
    await api_gate.send_pm(username, f"Your login code: {code} (expires in 5 minutes)")

    return {"success": True}


@router.post("/otp/verify")
async def verify_otp(request: Request, response: Response):
    """Verify OTP and issue session cookie."""
    body = await request.json()
    username = body.get("username", "").strip()
    code = body.get("code", "").strip()
    if not username or not code:
        raise HTTPException(400, "username and code required")

    db = request.app.state.db
    valid = await db.verify_otp(username, code)
    if not valid:
        raise HTTPException(401, "Invalid or expired code")

    # Look up user rank from api-gate
    api_gate = request.app.state.api_gate
    try:
        user_data = await api_gate.get_user(username)
        rank = user_data.get("rank", 1) if user_data else 1
    except Exception:
        rank = 1

    # Issue JWT session cookie
    config = request.app.state.config
    token = create_session_token(username, rank, config.secret_key, config.session_ttl_hours)

    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=config.session_ttl_hours * 3600,
    )
    return {"success": True, "username": username, "rank": rank}


@router.post("/logout")
async def logout(response: Response, user: dict = Depends(get_current_user)):
    """Clear session cookie."""
    response.delete_cookie("session")
    return {"success": True}


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    """Return current session info."""
    return user
