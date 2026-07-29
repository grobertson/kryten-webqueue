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
        ws_manager=ws,
        active_interval=1.5,
        idle_interval=4.0,
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
        api_gate=_FakeApiGate(
            [
                {"active": True, "frame": {"phase": "racing"}},
                {"active": False, "frame": None},
                {"active": False, "frame": None},
            ]
        ),
        ws_manager=ws,
    )
    await poller._poll_once()  # active
    await poller._poll_once()  # transitions to idle → one clear
    await poller._poll_once()  # still idle → no repeat
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
        api_gate=_FakeApiGate(
            [
                {"active": True, "frame": {"phase": "racing"}},
                RuntimeError("nats hiccup"),
            ]
        ),
        ws_manager=ws,
    )
    await poller._poll_once()  # active → caches frame
    delay = await poller._poll_once()  # error
    assert delay == poller._idle_interval
    # No spurious clear; the cached frame is retained so a reconnecting
    # spectator still sees the race in progress.
    assert poller.last_frame == {"phase": "racing"}
    assert [m["type"] for m in ws.messages] == ["race_frame"]


def _racing_frame(race_id, elapsed, n_frames=3):
    return {
        "active": True,
        "frame": {
            "race_id": race_id,
            "phase": "racing",
            "timeline": {
                "frame_dt": 0.3,
                "duration": 1.0,
                "elapsed": elapsed,
                "frames": [[0.0]] * n_frames,
                "commentary": [{"t": 0.0, "text": "go"}],
            },
        },
    }


async def test_full_timeline_sent_once_then_stripped():
    """First racing frame carries the full timeline; later frames for the same
    race carry only ``elapsed`` (the browser self-animates)."""
    ws = _FakeWs()
    poller = RacePoller(
        api_gate=_FakeApiGate(
            [
                _racing_frame("race-A", 0.0),
                _racing_frame("race-A", 0.3),
                _racing_frame("race-A", 0.6),
            ]
        ),
        ws_manager=ws,
    )
    await poller._poll_once()
    await poller._poll_once()
    await poller._poll_once()

    first = ws.messages[0]["data"]["timeline"]
    assert "frames" in first and len(first["frames"]) == 3  # full

    for later in ws.messages[1:]:
        tl = later["data"]["timeline"]
        assert tl == {"elapsed": tl["elapsed"]}  # light: elapsed only
    assert [m["data"]["timeline"]["elapsed"] for m in ws.messages] == [0.0, 0.3, 0.6]
    # last_frame retains the FULL frame for late-joiner bootstrap.
    assert "frames" in poller.last_frame["timeline"]


async def test_new_race_resends_full_timeline():
    """A different race_id resets the strip state — full timeline again."""
    ws = _FakeWs()
    poller = RacePoller(
        api_gate=_FakeApiGate(
            [
                _racing_frame("race-A", 0.0),
                _racing_frame("race-B", 0.0),
            ]
        ),
        ws_manager=ws,
    )
    await poller._poll_once()
    await poller._poll_once()
    assert "frames" in ws.messages[0]["data"]["timeline"]
    assert "frames" in ws.messages[1]["data"]["timeline"]
