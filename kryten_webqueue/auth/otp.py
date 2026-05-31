import secrets
from datetime import datetime, timedelta, UTC


def generate_otp() -> str:
    return str(secrets.randbelow(1000000)).zfill(6)


def get_otp_expiry(minutes: int = 5) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()
