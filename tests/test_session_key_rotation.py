import jwt
import pytest

from kryten_webqueue.auth.session import create_session_token, decode_session_token


def test_decode_session_token_accepts_current_key():
    token = create_session_token("current", 1, "current-key-that-is-long-enough-1234")

    payload = decode_session_token(
        token,
        "current-key-that-is-long-enough-1234",
        ["previous-key-that-is-long-enough-12"],
    )

    assert payload["sub"] == "current"


def test_decode_session_token_accepts_previous_verification_key():
    token = create_session_token("previous", 1, "previous-key-that-is-long-enough-12")

    payload = decode_session_token(
        token,
        "current-key-that-is-long-enough-1234",
        ["previous-key-that-is-long-enough-12"],
    )

    assert payload["sub"] == "previous"


def test_decode_session_token_rejects_unknown_key():
    token = create_session_token("unknown", 1, "unknown-key-that-is-long-enough-123")

    with pytest.raises(jwt.InvalidSignatureError):
        decode_session_token(
            token,
            "current-key-that-is-long-enough-1234",
            ["previous-key-that-is-long-enough-12"],
        )
