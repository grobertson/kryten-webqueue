"""Throttled, retrying single-item add used by bulk playlist loaders.

CyTube validates each queued item server-side (e.g. fetching a custom MediaCMS
manifest). Adding items faster than CyTube can validate them produces a
transient ``queueFail`` — which api-gate surfaces as HTTP 422 on
``/playlist/add``. These rejections are not about a bad URL; spacing the calls
out (and retrying the 422 a couple of times) lets the queue settle.
"""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


async def add_item_throttled(
    api_gate,
    *,
    media_type: str,
    media_id: str,
    position: str = "end",
    max_retries: int = 0,
    retry_delay_sec: float = 0.5,
) -> dict:
    """Add one item to the live queue, retrying transient CyTube rejections.

    A 422 (CyTube ``queueFail``) is retried up to ``max_retries`` times with a
    linear backoff (``retry_delay_sec`` × attempt). Any other HTTP error is not
    retried. Returns the api-gate result dict; re-raises the final exception
    when retries are exhausted so callers can count/log the failure.
    """
    attempt = 0
    while True:
        try:
            return await api_gate.playlist_add(
                media_type=media_type,
                media_id=media_id,
                position=position,
            )
        except httpx.HTTPStatusError as e:
            is_transient = e.response is not None and e.response.status_code == 422
            if not is_transient or attempt >= max_retries:
                raise
            attempt += 1
            backoff = retry_delay_sec * attempt
            logger.info(
                "CyTube rejected %s (422 queueFail); retry %d/%d after %.1fs",
                media_id,
                attempt,
                max_retries,
                backoff,
            )
            await asyncio.sleep(backoff)
