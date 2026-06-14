"""PresenceRefundMonitor behaviour (Feature 1).

Covers the presence-based cancel/refund of pending paid items: leave/AFK
triggers, the grace window, now-playing exemption, inconclusive lookups, and
multi-item cancellation for a single owner.
"""

from kryten_webqueue.config import PresenceRefundConfig
from kryten_webqueue.queue.presence import PresenceRefundMonitor


_RAISE = object()


class _FakeApiGate:
    def __init__(self, users, np=None):
        self._users = dict(users)  # username -> response dict (or _RAISE)
        self._np = np
        self.refunds = []
        self.deleted = []
        self.pms = []

    async def get_now_playing(self):
        return self._np

    async def get_user(self, username):
        val = self._users.get(username)
        if val is _RAISE:
            raise RuntimeError("robot/NATS hiccup")
        return val

    async def playlist_delete(self, uid):
        self.deleted.append(uid)
        return {"success": True}

    async def send_pm(self, username, message):
        self.pms.append((username, message))
        return {"success": True}

    async def queue_refund(self, username, request_id, reason):
        self.refunds.append((username, request_id, reason))
        return {"success": True}


class _FakeShadow:
    def __init__(self, items, now_playing=None):
        self._items = list(items)
        self.now_playing = now_playing
        self.removed = []

    @property
    def items(self):
        return list(self._items)

    async def remove(self, uid):
        self._items = [it for it in self._items if it["uid"] != uid]
        self.removed.append(uid)

    async def get_enriched_state(self, db):
        return {"items": self._items, "now_playing": self.now_playing}


class _FakeDb:
    def __init__(self, spend_map):
        # spend_map: uid -> {"request_id": str, "username": str}
        self._by_uid = dict(spend_map)
        self.refunded = []

    async def get_request_id_for_uid(self, uid):
        rec = self._by_uid.get(uid)
        return rec["request_id"] if rec else None

    async def _fetch_one(self, sql, params):
        request_id = params[0]
        for rec in self._by_uid.values():
            if rec["request_id"] == request_id:
                return {"username": rec["username"]}
        return None

    async def mark_spend_refunded(self, request_id):
        self.refunded.append(request_id)


class _FakeWs:
    def __init__(self):
        self.messages = []

    async def broadcast(self, message):
        self.messages.append(message)


def _paid(uid, owner, **kw):
    d = {"uid": uid, "title": f"Item {uid}", "is_pay": True, "paid_by": owner}
    d.update(kw)
    return d


def _free(uid, **kw):
    d = {"uid": uid, "title": f"Item {uid}", "is_pay": False, "paid_by": None}
    d.update(kw)
    return d


def _monitor(api, shadow, db, ws, **cfg_kw):
    cfg_kw.setdefault("grace_seconds", 0.0)
    cfg = PresenceRefundConfig(**cfg_kw)
    return PresenceRefundMonitor(
        api_gate=api, shadow=shadow, db=db, ws_manager=ws, config=cfg
    )


async def test_owner_offline_cancels_paid_keeps_free():
    shadow = _FakeShadow([_paid(11, "alice"), _free(12)], now_playing={"uid": 10})
    api = _FakeApiGate({"alice": {"online": False}}, np={"uid": 10})
    db = _FakeDb({11: {"request_id": "r11", "username": "alice"}})
    ws = _FakeWs()
    mon = _monitor(api, shadow, db, ws)

    assert await mon.check_once() == 0           # first sighting: starts grace
    assert await mon.check_once() == 1           # grace elapsed: acts

    assert api.refunds == [("alice", "r11", "owner_left")]
    assert 11 in shadow.removed                  # paid item removed
    assert 12 not in shadow.removed              # free item untouched
    assert db.refunded == ["r11"]
    assert ws.messages and ws.messages[-1]["type"] == "queue_state"


async def test_afk_cancel_pms_owner_when_notify_enabled():
    # AFK owners are still connected and CAN be PM'd.
    shadow = _FakeShadow([_paid(11, "alice", title="Cool Video")], now_playing={"uid": 10})
    api = _FakeApiGate({"alice": {"online": True, "meta": {"afk": True}}}, np={"uid": 10})
    db = _FakeDb({11: {"request_id": "r11", "username": "alice"}})
    ws = _FakeWs()
    mon = _monitor(api, shadow, db, ws, notify_user=True, on_afk=True)

    await mon.check_once()                        # first sighting: starts grace
    await mon.check_once()                        # grace elapsed: acts

    assert len(api.pms) == 1
    user, msg = api.pms[0]
    assert user == "alice"
    assert "Cool Video" in msg
    assert "AFK" in msg


async def test_left_channel_cancel_does_not_pm():
    # A user who LEFT the channel is unreachable by PM — no PM attempted.
    shadow = _FakeShadow([_paid(11, "alice")], now_playing={"uid": 10})
    api = _FakeApiGate({"alice": {"online": False}}, np={"uid": 10})
    db = _FakeDb({11: {"request_id": "r11", "username": "alice"}})
    ws = _FakeWs()
    mon = _monitor(api, shadow, db, ws, notify_user=True)

    await mon.check_once()
    await mon.check_once()

    assert api.refunds == [("alice", "r11", "owner_left")]
    assert api.pms == []


async def test_cancel_silent_when_notify_disabled():
    shadow = _FakeShadow([_paid(11, "alice")], now_playing={"uid": 10})
    api = _FakeApiGate({"alice": {"online": True, "meta": {"afk": True}}}, np={"uid": 10})
    db = _FakeDb({11: {"request_id": "r11", "username": "alice"}})
    ws = _FakeWs()
    mon = _monitor(api, shadow, db, ws, notify_user=False, on_afk=True)

    await mon.check_once()
    await mon.check_once()

    assert api.refunds == [("alice", "r11", "owner_afk")]
    assert api.pms == []
    shadow = _FakeShadow([_paid(11, "bob")], now_playing={"uid": 10})
    api = _FakeApiGate({"bob": {"meta": {"afk": True}}}, np={"uid": 10})
    db = _FakeDb({11: {"request_id": "r11", "username": "bob"}})
    ws = _FakeWs()
    mon = _monitor(api, shadow, db, ws, on_afk=True, grace_seconds=60.0)

    await mon.check_once()                        # registers AFK
    assert "bob" in mon._missing_since

    api._users["bob"] = {"online": True, "meta": {"afk": False}}  # returned
    assert await mon.check_once() == 0

    assert "bob" not in mon._missing_since
    assert api.refunds == []
    assert shadow.removed == []


async def test_now_playing_owner_offline_is_exempt():
    # uid 10 is both the now-playing item and a paid item.
    shadow = _FakeShadow([_paid(10, "carol")], now_playing={"uid": 10})
    api = _FakeApiGate({"carol": {"online": False}}, np={"uid": 10})
    db = _FakeDb({10: {"request_id": "r10", "username": "carol"}})
    ws = _FakeWs()
    mon = _monitor(api, shadow, db, ws)

    await mon.check_once()
    await mon.check_once()

    assert api.refunds == []
    assert shadow.removed == []
    assert "carol" not in mon._missing_since


async def test_get_user_error_is_inconclusive():
    shadow = _FakeShadow([_paid(11, "dave")], now_playing={"uid": 10})
    api = _FakeApiGate({"dave": _RAISE}, np={"uid": 10})
    db = _FakeDb({11: {"request_id": "r11", "username": "dave"}})
    ws = _FakeWs()
    mon = _monitor(api, shadow, db, ws)

    assert await mon.check_once() == 0            # lookup raised: no tracking
    assert "dave" not in mon._missing_since

    api._users["dave"] = {"online": False}        # now a real signal
    await mon.check_once()                         # registers
    assert await mon.check_once() == 1             # acts
    assert api.refunds == [("dave", "r11", "owner_left")]


async def test_two_items_one_owner_both_cancelled():
    shadow = _FakeShadow([_paid(11, "erin"), _paid(12, "erin")], now_playing={"uid": 10})
    api = _FakeApiGate({"erin": {"online": False}}, np={"uid": 10})
    db = _FakeDb({
        11: {"request_id": "r11", "username": "erin"},
        12: {"request_id": "r12", "username": "erin"},
    })
    ws = _FakeWs()
    mon = _monitor(api, shadow, db, ws)

    await mon.check_once()                          # register
    assert await mon.check_once() == 2             # both cancelled in one window

    assert set(shadow.removed) == {11, 12}
    assert ("erin", "r11", "owner_left") in api.refunds
    assert ("erin", "r12", "owner_left") in api.refunds


async def test_afk_ignored_when_on_afk_disabled():
    shadow = _FakeShadow([_paid(11, "frank")], now_playing={"uid": 10})
    api = _FakeApiGate({"frank": {"meta": {"afk": True}}}, np={"uid": 10})
    db = _FakeDb({11: {"request_id": "r11", "username": "frank"}})
    ws = _FakeWs()
    mon = _monitor(api, shadow, db, ws, on_afk=False)

    await mon.check_once()
    await mon.check_once()

    assert api.refunds == []
    assert shadow.removed == []
    assert "frank" not in mon._missing_since


async def test_leave_ignored_when_on_leave_disabled():
    shadow = _FakeShadow([_paid(11, "gina")], now_playing={"uid": 10})
    api = _FakeApiGate({"gina": {"online": False}}, np={"uid": 10})
    db = _FakeDb({11: {"request_id": "r11", "username": "gina"}})
    ws = _FakeWs()
    mon = _monitor(api, shadow, db, ws, on_leave=False)

    await mon.check_once()
    await mon.check_once()

    assert api.refunds == []
    assert shadow.removed == []
    assert "gina" not in mon._missing_since


async def test_owner_recovers_after_partial_grace_then_leaves_again():
    shadow = _FakeShadow([_paid(11, "hank")], now_playing={"uid": 10})
    api = _FakeApiGate({"hank": {"online": False}}, np={"uid": 10})
    db = _FakeDb({11: {"request_id": "r11", "username": "hank"}})
    ws = _FakeWs()
    mon = _monitor(api, shadow, db, ws, grace_seconds=60.0)

    await mon.check_once()                          # gone: grace starts
    assert "hank" in mon._missing_since

    api._users["hank"] = {"online": True}           # returns: grace cleared
    await mon.check_once()
    assert "hank" not in mon._missing_since
    assert shadow.removed == []
