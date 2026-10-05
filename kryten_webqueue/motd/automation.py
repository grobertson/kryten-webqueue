"""Reconcile loop: keep the channel MOTD in step with the template schedules.

Every tick resolves what *should* be live and compares its composition key
with the latest publication. On a difference it starts the normal
``motd_publish`` job, so there is exactly one write path. Schedule and template
edits never publish directly; they only change what the next tick resolves,
which also makes startup and missed ticks self-healing.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from pathlib import Path

from .builder import week_context
from .composer import resolve_composition
from .templating import MOTDTemplateError

logger = logging.getLogger(__name__)

UTC = datetime.timezone.utc
# Don't hammer a composition whose publish keeps failing (e.g. bad template).
RETRY_AFTER_SECONDS = 600


class MOTDAutomation:
    def __init__(self, db, job_manager, config) -> None:
        self._db = db
        self._jobs = job_manager
        self._config = config
        self._task: asyncio.Task | None = None
        self._last_attempt: tuple[str, float] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        trigger = "startup"
        while True:
            try:
                await self.tick(trigger=trigger)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive any tick failure
                logger.exception("MOTD automation tick failed")
            trigger = "schedule"
            await asyncio.sleep(
                max(10, int(self._config.motd.reconcile_interval_seconds))
            )

    async def tick(self, *, trigger: str = "schedule") -> dict:
        if not self._config.motd.automation_enabled:
            return {"action": "skipped", "reason": "disabled"}
        if self._jobs.is_running("motd_publish"):
            return {"action": "skipped", "reason": "publish_running"}
        try:
            composition = await resolve_composition(self._db)
        except MOTDTemplateError as exc:
            logger.warning("MOTD automation cannot resolve a composition: %s", exc)
            return {"action": "skipped", "reason": str(exc)}

        latest = await self._db.get_latest_motd_publication()
        if latest and latest["composition_key"] == composition.key:
            return {"action": "none", "composition_key": composition.key}
        if self._last_attempt and self._last_attempt[0] == composition.key:
            if time.monotonic() - self._last_attempt[1] < RETRY_AFTER_SECONDS:
                return {"action": "skipped", "reason": "recent_attempt"}

        # Keep whichever weekend is live; an early Sunday debut must survive.
        next_key, _ = week_context(week_offset=1)
        week = "next" if latest and latest.get("week_key") == next_key else "current"
        self._last_attempt = (composition.key, time.monotonic())
        logger.info(
            "MOTD automation (%s): publishing %s for the %s weekend",
            trigger,
            composition.master["name"],
            week,
        )
        result = await self._jobs.run(
            "motd_publish",
            triggered_by="motd_automation",
            params={"publish": True, "week": week},
        )
        return {"action": "publish", "composition_key": composition.key, **result}


MOTD_RETENTION_PRUNE_SCHEMA: list[dict] = []


async def motd_retention_prune_job(params: dict, ctx) -> dict:
    """Drop MOTD audit rows, publications, and backups past the retention window."""
    days = int(ctx.config.motd.retention_days)
    cutoff = datetime.datetime.now(UTC) - datetime.timedelta(days=days)
    result = await ctx.db.prune_motd_history(cutoff)

    backups = (Path(ctx.config.motd.output_dir).expanduser() / "backups").resolve()
    keep = result.get("kept_backup_path")
    removed = 0

    def _sweep() -> int:
        count = 0
        if not backups.is_dir():
            return 0
        for path in backups.glob("motd-*.html"):
            if keep and path.resolve() == Path(keep).resolve():
                continue
            mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if mtime < cutoff:
                path.unlink(missing_ok=True)
                count += 1
        return count

    removed = await asyncio.to_thread(_sweep)
    summary = {
        "retention_days": days,
        "publications_removed": result["publications"],
        "audit_removed": result["audit"],
        "backups_removed": removed,
    }
    logger.info("motd_retention_prune: %s", summary)
    return summary
