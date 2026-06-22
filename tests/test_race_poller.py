"""RacePoller — adaptive race-frame polling + WebSocket broadcast.

Drives ``_poll_once()`` directly (the loop is just sleep + retry) to assert the
broadcast/cache/back-off behaviour without real timers or sockets.
"""

from kryten_webqueue.queue.race_poller import RacePoller


class _FakeApiGate:
    def __init__(self, results):
        # results: list of dicts to return, or Exception instances to raise.
        self._results = list(results)
        self.calls = 0

    async def get_race_state(self):
        self.calls += 1
        val = self._results[min(self.calls - 1, len(self._results) - 1)]
        if isinstance(val, Exception):
            raise val
        return val


class _FakeWs:
    def __init__(self):
        self.messages = []

    async def broadcast(self, message):
        self.messages.append(message)


async def test_active_frame_broadcasts_and_caches():
    ws = _FakeWs()
    frame = {"phase": "racing", "tick": 1}
    poller = RacePoller(
        api_gate=_FakeApiGate([{"active": True, "frame": frame}]),
        ws_manager=ws, active_interval=1.5, idle_interval=4.0,
    )
    delay = await poller._poll_once()
    assert delay == 1.5
    assert poller.last_frame == frame
    assert ws.messages == [{"type": "race_frame", "data": frame}]


async def test_idle_without_prior_does_not_broadcast():
    ws = _FakeWs()
    poller = RacePoller(
        api_gate=_FakeApiGate([{"active": False, "frame": None}]),
        ws_manager=ws,
    )
    delay = await poller._poll_once()
    assert delay == poller._idle_interval
    assert ws.messages == []
    assert poller.last_frame is None


async def test_clear_broadcast_once_after_active():
    ws = _FakeWs()
    poller = RacePoller(
        api_gate=_FakeApiGate([
            {"active": True, "frame": {"phase": "racing"}},
            {"active": False, "frame": None},
            {"active": False, "frame": None},
        ]),
        ws_manager=ws,
    )
    await poller._poll_once()   # active
    await poller._poll_once()   # transitions to idle → one clear
    await poller._poll_once()   # still idle → no repeat
    assert [m["type"] for m in ws.messages] == ["race_frame", "race_clear"]
    assert poller.last_frame is None


async def test_active_false_with_stale_frame_is_idle():
    """``active: true`` but a null frame is treated as no race (defensive)."""
    ws = _FakeWs()
    poller = RacePoller(
        api_gate=_FakeApiGate([{"active": True, "frame": None}]),
        ws_manager=ws,
    )
    delay = await poller._poll_once()
    assert delay == poller._idle_interval
    assert ws.messages == []
    assert poller.last_frame is None


async def test_error_backs_off_without_clearing():
    ws = _FakeWs()
    poller = RacePoller(
        api_gate=_FakeApiGate([
            {"active": True, "frame": {"phase": "racing"}},
            RuntimeError("nats hiccup"),
        ]),
        ws_manager=ws,
    )
    await poller._poll_once()            # active → caches frame
    delay = await poller._poll_once()    # error
    assert delay == poller._idle_interval
    # No spurious clear; the cached frame is retained so a reconnecting
    # spectator still sees the race in progress.
    assert poller.last_frame == {"phase": "racing"}
    assert [m["type"] for m in ws.messages] == ["race_frame"]
