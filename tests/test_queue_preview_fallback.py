"""Graceful degradation when economy pricing is unreachable.

`_queue_preview_or_503` wraps the api-gate cost preview so that an economy /
api-gate outage surfaces as a clean HTTP 503 instead of an unhandled 500.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException

from kryten_webqueue.routes.queue import _queue_preview_or_503


class _FakeApiGate:
    def __init__(self, exc: Exception | None = None, result: dict | None = None):
        self._exc = exc
        self._result = result or {"available": True, "cost_z": 100}

    async def queue_preview(
        self, *, username: str, duration_sec: int, tier: str
    ) -> dict:
        if self._exc is not None:
            raise self._exc
        return self._result


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://api-gate/economy/queue-preview")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError("upstream", request=request, response=response)


async def test_passthrough_on_success():
    api = _FakeApiGate(result={"available": True, "cost_z": 250})
    out = await _queue_preview_or_503(
        api, username="alice", duration_sec=600, tier="queue"
    )
    assert out["cost_z"] == 250


@pytest.mark.parametrize("code", [500, 502, 503])
async def test_upstream_5xx_becomes_503(code: int):
    api = _FakeApiGate(exc=_status_error(code))
    with pytest.raises(HTTPException) as excinfo:
        await _queue_preview_or_503(
            api, username="alice", duration_sec=600, tier="queue"
        )
    assert excinfo.value.status_code == 503


async def test_transport_error_becomes_503():
    request = httpx.Request("POST", "http://api-gate/economy/queue-preview")
    api = _FakeApiGate(exc=httpx.ConnectError("refused", request=request))
    with pytest.raises(HTTPException) as excinfo:
        await _queue_preview_or_503(
            api, username="alice", duration_sec=600, tier="queue"
        )
    assert excinfo.value.status_code == 503


async def test_upstream_4xx_propagates():
    api = _FakeApiGate(exc=_status_error(404))
    with pytest.raises(httpx.HTTPStatusError):
        await _queue_preview_or_503(
            api, username="alice", duration_sec=600, tier="queue"
        )
