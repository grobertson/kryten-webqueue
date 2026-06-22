import asyncio
import logging

logger = logging.getLogger(__name__)


class RacePoller:
    """Polls api-gate's race view and pushes frames to race spectators.

    Adaptive cadence: while a race is live it polls quickly (``active_interval``)
    so the web view animates smoothly; when idle it backs off
    (``idle_interval``). The latest frame is cached so a newly-connected
    spectator can be shown the race already in progress.

    Broadcast messages (to the public race WebSocket manager):
    - ``{"type": "race_frame", "data": <frame>}`` each poll while a race is live.
    - ``{"type": "race_clear"}`` once, when a race ends and the view goes idle.
    """

    def __init__(
        self,
        *,
        api_gate,
        ws_manager,
        active_interval: float = 1.5,
        idle_interval: float = 4.0,
    ):
        self._api_gate = api_gate
        self._ws_manager = ws_manager
        self._active_interval = active_interval
        self._idle_interval = idle_interval
        self._task: asyncio.Task | None = None
        self._last_frame: dict | None = None

    @property
    def last_frame(self) -> dict | None:
        """The most recent live frame, or None when no race is active."""
        return self._last_frame

    async def start(self):
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "RacePoller started (active=%.1fs idle=%.1fs)",
            self._active_interval, self._idle_interval,
        )

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("RacePoller stopped")

    async def _poll_once(self) -> float:
        """Run one poll; broadcast as needed. Returns the next sleep interval."""
        try:
            result = await self._api_gate.get_race_state()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Race poll error: %s", e)
            # Treat an error like "idle" but don't spuriously clear an active
            # view — just back off and retry.
            return self._idle_interval

        active = bool(result.get("active")) and result.get("frame") is not None
        if active:
            self._last_frame = result["frame"]
            await self._ws_manager.broadcast(
                {"type": "race_frame", "data": self._last_frame}
            )
            return self._active_interval

        # Not active. If we were showing a race, tell spectators to clear once.
        if self._last_frame is not None:
            self._last_frame = None
            await self._ws_manager.broadcast({"type": "race_clear"})
        return self._idle_interval

    async def _loop(self):
        while True:
            try:
                delay = await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("RacePoller loop error: %s", e)
                delay = self._idle_interval
            await asyncio.sleep(delay)
