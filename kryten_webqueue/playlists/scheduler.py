import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from datetime import datetime, UTC

from .fire import fire_schedule

logger = logging.getLogger(__name__)


class PlaylistScheduler:
    """APScheduler-based scheduler for playlist fire events."""

    def __init__(self, *, db, api_gate, shadow, ws_manager):
        self._db = db
        self._api_gate = api_gate
        self._shadow = shadow
        self._ws_manager = ws_manager
        self._scheduler = AsyncIOScheduler()

    async def start(self):
        """Start scheduler and load all pending schedules."""
        self._scheduler.start()
        await self._load_schedules()
        logger.info("PlaylistScheduler started")

    async def stop(self):
        self._scheduler.shutdown(wait=False)
        logger.info("PlaylistScheduler stopped")

    async def _load_schedules(self):
        """Load active schedules from DB and register jobs."""
        schedules = await self._db.get_schedules()
        now = datetime.now(UTC)
        for sched in schedules:
            if not sched.get("is_active"):
                continue
            fire_at_str = sched["fire_at"]
            fire_at = datetime.fromisoformat(fire_at_str)
            if fire_at <= now:
                continue
            self._add_job(sched["id"], fire_at)

    def _add_job(self, schedule_id: int, fire_at: datetime):
        job_id = f"schedule_{schedule_id}"
        self._scheduler.add_job(
            self._fire,
            trigger=DateTrigger(run_date=fire_at),
            id=job_id,
            replace_existing=True,
            kwargs={"schedule_id": schedule_id},
        )
        logger.info(f"Scheduled job {job_id} for {fire_at}")

    async def _fire(self, schedule_id: int):
        await fire_schedule(
            schedule_id=schedule_id,
            api_gate=self._api_gate,
            db=self._db,
            shadow=self._shadow,
            ws_manager=self._ws_manager,
        )

    async def add_schedule(self, schedule_id: int, fire_at: datetime):
        self._add_job(schedule_id, fire_at)

    async def remove_schedule(self, schedule_id: int):
        job_id = f"schedule_{schedule_id}"
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass
