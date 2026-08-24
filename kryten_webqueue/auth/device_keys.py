"""Device-linking primitives and the public-API auth dependency.

The smart-TV / tablet apps authenticate with a long-lived API key rather than a
session cookie. Keys are minted by exchanging a short-lived one-time pad (link
code) at ``POST /api/public/v1/link`` and are stored only as an irreversible
SHA-256 hash. This module owns the code/key formats, hashing, and the FastAPI
dependency that resolves ``Authorization: Bearer <key>`` to a user.
"""

import hashlib
import secrets

from fastapi import Request, HTTPException

# Unambiguous uppercase alphanumeric alphabet (no 0/O/1/I) — friendly for
# on-screen TV entry while remaining strictly alphanumeric.
LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LINK_CODE_LENGTH = 5
LINK_CODE_TTL_MINUTES = 10

API_KEY_PREFIX = "kqd_"  # kryten queue device
# Display prefix length: scheme prefix + 8 leading hex chars (non-secret).
_KEY_DISPLAY_LEN = len(API_KEY_PREFIX) + 8


def generate_link_code() -> str:
    """Return a fresh 5-char uppercase one-time pad."""
    return "".join(secrets.choice(LINK_CODE_ALPHABET) for _ in range(LINK_CODE_LENGTH))


def normalize_link_code(raw: str) -> str:
    """Normalize user/device-supplied input: strip and uppercase."""
    return (raw or "").strip().upper()


def is_valid_link_code_format(code: str) -> bool:
    """True when ``code`` is the right length and uses only the code alphabet."""
    return len(code) == LINK_CODE_LENGTH and all(
        ch in LINK_CODE_ALPHABET for ch in code
    )


def generate_api_key() -> str:
    """Return a fresh opaque API key: ``kqd_`` + 48 hex chars."""
    return f"{API_KEY_PREFIX}{secrets.token_hex(24)}"


def api_key_display_prefix(full_key: str) -> str:
    """Non-secret prefix stored for display (e.g. ``kqd_a1b2c3d4``)."""
    return full_key[:_KEY_DISPLAY_LEN]


def hash_api_key(full_key: str) -> str:
    """Irreversible SHA-256 hex digest of a full API key (what we persist)."""
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


def _extract_bearer_key(request: Request) -> str | None:
    """Pull the key from ``Authorization: Bearer <key>`` (or a bare key)."""
    header = request.headers.get("authorization")
    if not header:
        return None
    header = header.strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return header or None


async def get_api_key_user(request: Request) -> dict:
    """FastAPI dependency: authenticate a public-API request by API key.

    Resolves ``Authorization: Bearer <key>`` to the owning user, updates the
    key's last-used timestamp, and returns ``{username, device_id, device_name}``.
    Raises 401 when the header is missing or the key is unknown.
    """
    key = _extract_bearer_key(request)
    if not key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    db = request.app.state.db
    row = await db.get_device_key_by_hash(hash_api_key(key))
    if not row:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        await db.touch_device_key(row["id"])
    except Exception:  # noqa: BLE001 — last-used tracking must never block a request
        pass
    return {
        "username": row["username"],
        "device_id": row["id"],
        "device_name": row["device_name"],
    }
