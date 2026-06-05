import asyncio
import logging

logger = logging.getLogger(__name__)


class StatePoller:
    """Polls api-gate at a fixed interval to keep QueueShadow in sync."""

    def __init__(self, *, api_gate, shadow, ws_manager, db=None, interval: float = 3.0):
        self._api_gate = api_gate
        self._shadow = shadow
        self._ws_manager = ws_manager
        self._db = db
        self._interval = interval
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(self._loop())
        logger.info(f"StatePoller started (interval={self._interval}s)")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("StatePoller stopped")

    async def _loop(self):
        while True:
            try:
                playlist = await self._api_gate.get_playlist()
                now_playing = await self._api_gate.get_now_playing()
                await self._shadow.apply_poll_result(playlist, now_playing)
                # Broadcast updated state
                if self._db is not None:
                    state = await self._shadow.get_enriched_state(self._db)
                else:
                    state = self._shadow.get_queue_state()
                await self._ws_manager.broadcast({"type": "queue_state", "data": state})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Poll error: {e}")
            await asyncio.sleep(self._interval)
