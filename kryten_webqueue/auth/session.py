import jwt
from datetime import datetime, timedelta, UTC
from collections.abc import Iterable
from fastapi import Request, HTTPException


def create_session_token(
    username: str, rank: int, secret_key: str, ttl_hours: int = 24
) -> str:
    payload = {
        "sub": username,
        "rank": rank,
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(hours=ttl_hours),
    }
    return jwt.encode(payload, secret_key, algorithm="HS256")


def decode_session_token(
    token: str,
    secret_key: str,
    previous_secret_keys: Iterable[str] = (),
) -> dict:
    """Decode a session JWT, allowing bounded verification-only key rotation."""
    for key in (secret_key, *previous_secret_keys):
        try:
            return jwt.decode(token, key, algorithms=["HS256"])
        except jwt.InvalidSignatureError:
            continue
    raise jwt.InvalidSignatureError("Session token signature did not match any key")


async def get_current_user(request: Request) -> dict:
    """FastAPI dependency: extract user from session cookie."""
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    config = request.app.state.config
    try:
        payload = decode_session_token(
            token, config.secret_key, config.session_previous_secret_keys
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session")
    return {"username": payload["sub"], "rank": payload["rank"]}


async def require_admin(request: Request) -> dict:
    """FastAPI dependency: require rank >= 3."""
    user = await get_current_user(request)
    if user["rank"] < 3:
        raise HTTPException(status_code=403, detail="Admin required")
    return user
