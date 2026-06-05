"""Generic background job runner with run-history tracking.

Registers named async job functions and runs them as background tasks,
recording each run (start, end, status, detail) to the ``job_runs`` table
so the admin UI can display recent history — the same pattern used for
catalog sync.
"""

import asyncio
import json
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# A job is an async callable returning an optional dict of result detail.
JobFunc = Callable[[], Awaitable[dict | None]]


class JobManager:
    def __init__(self, db):
        self._db = db
        self._jobs: dict[str, dict] = {}
        self._running: dict[str, asyncio.Task] = {}

    def register(self, name: str, func: JobFunc, *, label: str | None = None):
        """Register a named job."""
        self._jobs[name] = {"func": func, "label": label or name}

    def list_jobs(self) -> list[dict]:
        return [
            {"name": name, "label": meta["label"], "running": name in self._running}
            for name, meta in self._jobs.items()
        ]

    def is_running(self, name: str) -> bool:
        return name in self._running

    async def run(self, name: str, *, triggered_by: str | None = None) -> dict:
        """Start a job in the background. Returns immediately.

        Raises KeyError if the job is unknown; returns ``already_running``
        status if a run is in progress.
        """
        if name not in self._jobs:
            raise KeyError(name)
        if name in self._running:
            return {"started": False, "reason": "already_running"}

        run_id = await self._db.start_job_run(name, triggered_by=triggered_by)
        task = asyncio.create_task(self._execute(name, run_id))
        self._running[name] = task
        return {"started": True, "run_id": run_id}

    async def _execute(self, name: str, run_id: int):
        func = self._jobs[name]["func"]
        try:
            result = await func()
            detail = json.dumps(result) if result is not None else None
            await self._db.finish_job_run(run_id, "completed", detail)
        except asyncio.CancelledError:
            await self._db.finish_job_run(run_id, "cancelled", None)
            raise
        except Exception as exc:  # noqa: BLE001 - record any failure
            logger.exception("Job '%s' failed", name)
            await self._db.finish_job_run(
                run_id, "failed", json.dumps({"error": f"{type(exc).__name__}: {exc}"})
            )
        finally:
            self._running.pop(name, None)
