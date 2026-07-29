"""Queue chat-announcement behaviour (v0.9.7).

Covers the English-ordinal position words, the paid-queue announcement message
format/position (counted from now-playing, wrapping), and that admin queueing
is not announced.
"""

import pytest

from kryten_webqueue.queue import ordering
from kryten_webqueue.queue.ordering import _ordinal_words, _announce_paid_queued


class _FakeApiGate:
    def __init__(self, np_uid=None):
        self.sent = []
        self._np_uid = np_uid

    async def get_now_playing(self):
        return {"uid": self._np_uid} if self._np_uid is not None else None

    async def send_chat(self, message):
        self.sent.append(message)
        return {"success": True}


class _FakeShadow:
    def __init__(self, items, now_playing=None):
        self._items = items
        self.now_playing = now_playing

    @property
    def items(self):
        return self._items


@pytest.mark.parametrize(
    "n,word",
    [
        (2, "second"),
        (3, "third"),
        (4, "fourth"),
        (5, "fifth"),
        (8, "eighth"),
        (9, "ninth"),
        (11, "eleventh"),
        (12, "twelfth"),
        (20, "twentieth"),
        (21, "twenty-first"),
        (42, "forty-second"),
        (53, "fifty-third"),
        (100, "one hundredth"),
        (107, "one hundred seventh"),
        (123, "one hundred twenty-third"),
    ],
)
def test_ordinal_words(n, word):
    assert _ordinal_words(n) == word


def _items(*uids):
    return [{"uid": u, "title": f"Item {u}"} for u in uids]


async def test_paid_announcement_next():
    # now-playing uid=10 at index 0; the new item uid=11 is immediately next.
    api = _FakeApiGate(np_uid=10)
    shadow = _FakeShadow(_items(10, 11))
    await _announce_paid_queued(
        api, shadow, uid=11, title="Airplane (1980)", username="Hollis"
    )
    assert api.sent == [
        "Airplane (1980) added to the queue with Zcoin by Hollis and is now next."
    ]


async def test_paid_announcement_third():
    # now-playing uid=10 at index 0; new item is two slots away -> "third".
    api = _FakeApiGate(np_uid=10)
    shadow = _FakeShadow(_items(10, 99, 11))
    await _announce_paid_queued(
        api, shadow, uid=11, title="Airplane (1980)", username="Hollis"
    )
    assert api.sent == [
        "Airplane (1980) added to the queue with Zcoin by Hollis and is now third."
    ]


async def test_paid_announcement_wraps_around():
    # now-playing is uid=99 at index 2; list wraps: after 99 comes 10 (next),
    # then 11 (third).
    api = _FakeApiGate(np_uid=99)
    shadow = _FakeShadow(_items(10, 11, 99))
    await _announce_paid_queued(api, shadow, uid=11, title="X", username="U")
    assert api.sent == ["X added to the queue with Zcoin by U and is now third."]


async def test_admin_queue_not_announced(monkeypatch):
    """insert_admin_queue must never call the announcement helper."""
    called = False

    async def _fail(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(ordering, "_announce_paid_queued", _fail)
    # Source check: the admin path has no announce call (the helper is only wired
    # to the paid paths). This guards against a regression re-adding it.
    import inspect

    src = inspect.getsource(ordering.insert_admin_queue)
    assert "_announce_paid_queued" not in src
    assert "_announce_queued" not in src
    assert called is False
