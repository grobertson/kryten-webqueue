import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from datetime import datetime, UTC

from dateutil.rrule import rrulestr

from .fire import fire_schedule

logger = logging.getLogger(__name__)


def _next_occurrence(rrule_str: str, dtstart: datetime, after: datetime) -> datetime | None:
    """Return the next RRULE occurrence strictly after ``after``.

    ``dtstart`` anchors the recurrence pattern (typically the schedule's current
    fire time). Returns None when the rule is exhausted or unparseable.
    """
    try:
        rule = rrulestr(rrule_str, dtstart=dtstart)
        return rule.after(after, inc=False)
    except Exception as e:
        logger.warning(f"Could not compute next occurrence for rrule {rrule_str!r}: {e}")
        return None


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

    @staticmethod
    def _parse_fire_at(value: str) -> datetime:
        """Parse a stored fire_at ISO string into a UTC-aware datetime."""
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt

    async def _load_schedules(self):
        """Load active schedules from DB and register jobs.

        Recurring schedules whose fire time has already passed (e.g. while the
        service was down) are advanced to their next future occurrence.
        """
        schedules = await self._db.get_schedules()
        now = datetime.now(UTC)
        for sched in schedules:
            if not sched.get("is_active"):
                continue
            fire_at = self._parse_fire_at(sched["fire_at"])
            if fire_at > now:
                self._add_job(sched["id"], fire_at)
            elif sched.get("is_recurring") and sched.get("rrule"):
                nxt = _next_occurrence(sched["rrule"], fire_at, now)
                if nxt:
                    nxt_utc = nxt.astimezone(UTC)
                    await self._db.update_schedule(
                        sched["id"], fire_at=nxt_utc.isoformat(), fired_at=None
                    )
                    self._add_job(sched["id"], nxt_utc)
                    logger.info(
                        f"Advanced missed recurring schedule {sched['id']} to {nxt_utc}"
                    )

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
        # After an automatic timed fire, advance recurring schedules to their
        # next occurrence and re-arm. (Manual "Fire Now" does NOT advance the
        # recurrence — the originally scheduled occurrence stays armed.)
        await self._reschedule_if_recurring(schedule_id)

    async def _reschedule_if_recurring(self, schedule_id: int):
        sched = await self._db.get_schedule(schedule_id)
        if not sched or not sched.get("is_active"):
            return
        if not sched.get("is_recurring") or not sched.get("rrule"):
            return
        fired_from = self._parse_fire_at(sched["fire_at"])
        nxt = _next_occurrence(sched["rrule"], fired_from, fired_from)
        if not nxt:
            logger.info(f"Recurring schedule {schedule_id} has no further occurrences")
            return
        nxt_utc = nxt.astimezone(UTC)
        await self._db.update_schedule(
            schedule_id, fire_at=nxt_utc.isoformat(), fired_at=None
        )
        self._add_job(schedule_id, nxt_utc)
        logger.info(f"Recurring schedule {schedule_id} re-armed for {nxt_utc}")

    async def add_schedule(self, schedule_id: int, fire_at: datetime):
        self._add_job(schedule_id, fire_at)

    async def remove_schedule(self, schedule_id: int):
        job_id = f"schedule_{schedule_id}"
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass
