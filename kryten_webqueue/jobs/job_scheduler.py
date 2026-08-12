"""Cron-based background job scheduler.

Persists schedules to the ``job_schedules`` table and fires them via
APScheduler's ``CronTrigger``. Completely separate from ``PlaylistScheduler``,
which fires playlists onto the live queue.

Chain support: when a job finishes with status ``completed`` and was triggered
by the scheduler, the scheduler checks ``run_next_job`` on that schedule and
immediately launches the next job using its own saved params.
"""

from __future__ import annotations

import json
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


class JobScheduler:
    """Cron-driven scheduler for registered background jobs."""

    def __init__(self, db, job_manager) -> None:
        self._db = db
        self._jm = job_manager
        self._aps = AsyncIOScheduler(timezone="UTC")

    async def start(self) -> None:
        self._aps.start()
        self._jm.set_post_run_hook(self._on_job_complete)
        await self._reload()
        logger.info("JobScheduler started")

    async def stop(self) -> None:
        self._aps.shutdown(wait=False)
        logger.info("JobScheduler stopped")

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _reload(self) -> None:
        schedules = await self._db.get_job_schedules()
        for sched in schedules:
            if sched.get("is_active"):
                self._register(sched["job_name"], sched["cron_expression"])

    def _register(self, job_name: str, cron_expression: str) -> bool:
        """Add or replace an APScheduler cron job. Returns True on success."""
        parts = cron_expression.strip().split()
        if len(parts) != 5:
            logger.warning(
                "JobScheduler: invalid cron %r for %r (expected 5 fields)",
                cron_expression,
                job_name,
            )
            return False
        minute, hour, day, month, day_of_week = parts
        try:
            trigger = CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
                timezone="UTC",
            )
            self._aps.add_job(
                self._fire,
                trigger=trigger,
                args=[job_name],
                id=f"jsch_{job_name}",
                replace_existing=True,
            )
        except (ValueError, Exception) as exc:  # noqa: BLE001 - bad stored cron must not crash startup
            logger.warning(
                "JobScheduler: skipping invalid cron %r for %r: %s",
                cron_expression,
                job_name,
                exc,
            )
            return False
        logger.info("JobScheduler: registered %r cron=%r", job_name, cron_expression)
        return True

    def _unregister(self, job_name: str) -> None:
        try:
            self._aps.remove_job(f"jsch_{job_name}")
        except Exception:  # noqa: BLE001 - job may not be registered
            pass

    async def _fire(self, job_name: str) -> None:
        """APScheduler calls this when a cron trigger fires."""
        sched = await self._db.get_job_schedule(job_name)
        if not sched or not sched.get("is_active"):
            return
        params = json.loads(sched["params_json"]) if sched.get("params_json") else None
        try:
            await self._jm.run(job_name, triggered_by="scheduler", params=params)
            logger.info("JobScheduler: fired %r", job_name)
        except KeyError:
            logger.warning("JobScheduler: job %r is not registered", job_name)
        except ValueError as exc:
            logger.warning("JobScheduler: %r invalid params: %s", job_name, exc)

    async def _on_job_complete(
        self, job_name: str, status: str, triggered_by: str | None
    ) -> None:
        """Post-run hook: chain to run_next_job if the scheduler triggered this run."""
        if triggered_by != "scheduler" or status != "completed":
            return
        sched = await self._db.get_job_schedule(job_name)
        if not sched or not sched.get("run_next_job"):
            return
        next_job: str = sched["run_next_job"]
        next_sched = await self._db.get_job_schedule(next_job)
        params = (
            json.loads(next_sched["params_json"])
            if next_sched and next_sched.get("params_json")
            else None
        )
        try:
            await self._jm.run(next_job, triggered_by="scheduler", params=params)
            logger.info("JobScheduler: chained %r → %r", job_name, next_job)
        except KeyError:
            logger.warning("JobScheduler: chain target %r is not registered", next_job)
        except ValueError as exc:
            logger.warning(
                "JobScheduler: chain %r → %r invalid params: %s", job_name, next_job, exc
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    async def upsert(
        self,
        job_name: str,
        cron_expression: str,
        *,
        params: dict | None = None,
        label: str | None = None,
        run_next_job: str | None = None,
        is_active: bool = True,
        created_by: str | None = None,
    ) -> None:
        """Persist a schedule and (re)register it with APScheduler.

        Raises ValueError if cron_expression is rejected by APScheduler.
        """
        # Validate before persisting so a bad expression is rejected at save time.
        if is_active:
            parts = (cron_expression or "").strip().split()
            if len(parts) != 5:
                raise ValueError(
                    f"cron_expression must have exactly 5 fields (min hour dom mon dow), got: {cron_expression!r}"
                )
            minute, hour, day, month, day_of_week = parts
            try:
                CronTrigger(
                    minute=minute, hour=hour, day=day,
                    month=month, day_of_week=day_of_week, timezone="UTC",
                )
            except Exception as exc:
                raise ValueError(f"Invalid cron expression {cron_expression!r}: {exc}") from exc

        params_json = json.dumps(params) if params is not None else None
        await self._db.upsert_job_schedule(
            job_name,
            cron_expression,
            label=label,
            params_json=params_json,
            is_active=is_active,
            run_next_job=run_next_job,
            created_by=created_by,
        )
        self._unregister(job_name)
        if is_active:
            self._register(job_name, cron_expression)

    async def remove(self, job_name: str) -> None:
        """Delete the persisted schedule and deregister from APScheduler."""
        self._unregister(job_name)
        await self._db.delete_job_schedule(job_name)
