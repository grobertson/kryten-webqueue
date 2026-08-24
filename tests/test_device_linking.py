"""Tests for device linking: DB layer + key/code primitives."""

from datetime import datetime, timedelta, UTC

import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.auth.device_keys import (
    LINK_CODE_ALPHABET,
    LINK_CODE_LENGTH,
    API_KEY_PREFIX,
    generate_link_code,
    normalize_link_code,
    is_valid_link_code_format,
    generate_api_key,
    api_key_display_prefix,
    hash_api_key,
)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "devices.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


def _future(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def _past(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


# ── primitives ────────────────────────────────────────────────────────────────


def test_link_code_format():
    code = generate_link_code()
    assert len(code) == LINK_CODE_LENGTH
    assert all(ch in LINK_CODE_ALPHABET for ch in code)
    assert is_valid_link_code_format(code)


def test_link_code_alphabet_is_unambiguous():
    for bad in ("0", "O", "1", "I"):
        assert bad not in LINK_CODE_ALPHABET


def test_normalize_link_code():
    assert normalize_link_code("  ab2cd ") == "AB2CD"
    assert is_valid_link_code_format(normalize_link_code(" ab2cd ")) is True


def test_invalid_code_formats_rejected():
    assert not is_valid_link_code_format("ABC")  # too short
    assert not is_valid_link_code_format("ABCDEF")  # too long
    assert not is_valid_link_code_format("ABCD!")  # symbol
    assert not is_valid_link_code_format("ABCD0")  # ambiguous char not in alphabet


def test_api_key_format_and_hash():
    key = generate_api_key()
    assert key.startswith(API_KEY_PREFIX)
    prefix = api_key_display_prefix(key)
    assert key.startswith(prefix)
    assert len(prefix) == len(API_KEY_PREFIX) + 8
    # Hash is deterministic and hides the key.
    assert hash_api_key(key) == hash_api_key(key)
    assert key not in hash_api_key(key)
    assert generate_api_key() != generate_api_key()


# ── link codes (one-time pads) ─────────────────────────────────────────────────


async def test_link_code_lifecycle(db):
    await db.create_link_code("AB2CD", "alice", "Living Room TV", _future(10))
    assert await db.link_code_exists("AB2CD")

    row = await db.get_valid_link_code("AB2CD")
    assert row is not None
    assert row["username"] == "alice"
    assert row["device_name"] == "Living Room TV"

    await db.delete_link_code("AB2CD")
    assert await db.get_valid_link_code("AB2CD") is None
    assert not await db.link_code_exists("AB2CD")


async def test_expired_link_code_not_returned(db):
    await db.create_link_code("EXPCD", "bob", "Tablet", _past(1))
    # Still present in the table...
    assert await db.link_code_exists("EXPCD")
    # ...but never resolves as valid.
    assert await db.get_valid_link_code("EXPCD") is None


async def test_purge_expired_link_codes(db):
    await db.create_link_code("VALID", "alice", "TV", _future(10))
    await db.create_link_code("STALE", "alice", "TV2", _past(5))
    removed = await db.purge_expired_link_codes()
    assert removed == 1
    assert await db.link_code_exists("VALID")
    assert not await db.link_code_exists("STALE")


# ── device API keys ─────────────────────────────────────────────────────────────


async def test_device_key_create_and_resolve(db):
    key = generate_api_key()
    key_id = await db.create_device_key(
        "alice", "Living Room TV", api_key_display_prefix(key), hash_api_key(key)
    )
    assert key_id > 0

    resolved = await db.get_device_key_by_hash(hash_api_key(key))
    assert resolved is not None
    assert resolved["username"] == "alice"
    assert resolved["device_name"] == "Living Room TV"

    # An unknown hash resolves to nothing.
    assert await db.get_device_key_by_hash(hash_api_key(generate_api_key())) is None


async def test_touch_updates_last_used(db):
    key = generate_api_key()
    key_id = await db.create_device_key(
        "alice", "TV", api_key_display_prefix(key), hash_api_key(key)
    )
    before = (await db.get_device_key_by_hash(hash_api_key(key)))["last_used_at"]
    assert before is None
    await db.touch_device_key(key_id)
    after = (await db.get_device_key_by_hash(hash_api_key(key)))["last_used_at"]
    assert after is not None


async def test_list_devices_hides_hashes(db):
    for name in ("TV", "Tablet"):
        k = generate_api_key()
        await db.create_device_key(
            "alice", name, api_key_display_prefix(k), hash_api_key(k)
        )
    devices = await db.list_device_keys("alice")
    assert len(devices) == 2
    for d in devices:
        assert "key_hash" not in d
        assert set(d.keys()) == {
            "id",
            "device_name",
            "key_prefix",
            "created_at",
            "last_used_at",
        }


async def test_delete_device_scoped_to_owner(db):
    k = generate_api_key()
    key_id = await db.create_device_key(
        "alice", "TV", api_key_display_prefix(k), hash_api_key(k)
    )
    # Wrong owner cannot delete.
    assert await db.delete_device_key(key_id, "mallory") is False
    assert await db.get_device_key_by_hash(hash_api_key(k)) is not None
    # Owner can.
    assert await db.delete_device_key(key_id, "alice") is True
    assert await db.get_device_key_by_hash(hash_api_key(k)) is None


async def test_revoke_all_user_keys(db):
    for name in ("TV", "Tablet", "Phone"):
        k = generate_api_key()
        await db.create_device_key(
            "alice", name, api_key_display_prefix(k), hash_api_key(k)
        )
    other = generate_api_key()
    await db.create_device_key(
        "bob", "TV", api_key_display_prefix(other), hash_api_key(other)
    )

    removed = await db.revoke_user_device_keys("alice")
    assert removed == 3
    assert await db.list_device_keys("alice") == []
    # Bob's key is untouched.
    assert await db.get_device_key_by_hash(hash_api_key(other)) is not None


async def test_device_key_usernames_distinct(db):
    for _ in range(2):
        k = generate_api_key()
        await db.create_device_key(
            "alice", "d", api_key_display_prefix(k), hash_api_key(k)
        )
    k = generate_api_key()
    await db.create_device_key("bob", "d", api_key_display_prefix(k), hash_api_key(k))
    assert set(await db.device_key_usernames()) == {"alice", "bob"}


# ── ban reconciliation job ──────────────────────────────────────────────────────


class _FakeApiGate:
    """Stub api-gate returning a fixed moderator ban list."""

    def __init__(self, banned: list[str]):
        self._banned = banned
        self.calls: list[tuple[str, str | None]] = []

    async def mod_list_entries(self, channel, action_filter=None):
        self.calls.append((channel, action_filter))
        return {"entries": [{"username": u, "action": "ban"} for u in self._banned]}


class _Config:
    channel = "testchannel"


class _Ctx:
    def __init__(self, db, api_gate):
        self.db = db
        self.api_gate = api_gate
        self.config = _Config()


async def _give_key(db, username: str) -> None:
    k = generate_api_key()
    await db.create_device_key(
        username, "dev", api_key_display_prefix(k), hash_api_key(k)
    )


async def test_ban_reconcile_revokes_banned_key_holders(db):
    from kryten_webqueue.jobs.tasks import device_key_ban_reconcile_job

    await _give_key(db, "alice")
    await _give_key(db, "bob")
    api = _FakeApiGate(banned=["bob"])
    ctx = _Ctx(db, api)

    result = await device_key_ban_reconcile_job({}, ctx)

    assert result["revoked"] == 1
    assert result["banned_key_holders"] == 1
    assert result["targets"] == ["bob"]
    assert await db.list_device_keys("bob") == []
    assert len(await db.list_device_keys("alice")) == 1
    # Only the ban list was queried.
    assert api.calls == [("testchannel", "ban")]


async def test_ban_reconcile_is_case_insensitive(db):
    from kryten_webqueue.jobs.tasks import device_key_ban_reconcile_job

    await _give_key(db, "Alice")
    ctx = _Ctx(db, _FakeApiGate(banned=["alice"]))

    result = await device_key_ban_reconcile_job({}, ctx)

    assert result["revoked"] == 1
    assert await db.list_device_keys("Alice") == []


async def test_ban_reconcile_dry_run_makes_no_changes(db):
    from kryten_webqueue.jobs.tasks import device_key_ban_reconcile_job

    await _give_key(db, "bob")
    ctx = _Ctx(db, _FakeApiGate(banned=["bob"]))

    result = await device_key_ban_reconcile_job({"dry_run": True}, ctx)

    assert result["dry_run"] is True
    assert result["revoked"] == 1  # would-be count
    assert len(await db.list_device_keys("bob")) == 1  # nothing actually removed


async def test_ban_reconcile_skips_moderator_call_when_no_keys(db):
    from kryten_webqueue.jobs.tasks import device_key_ban_reconcile_job

    api = _FakeApiGate(banned=["bob"])
    ctx = _Ctx(db, api)

    result = await device_key_ban_reconcile_job({}, ctx)

    assert result == {
        "key_holders": 0,
        "banned_key_holders": 0,
        "revoked": 0,
        "dry_run": False,
    }
    # No point querying the ban list when nobody holds a key.
    assert api.calls == []
