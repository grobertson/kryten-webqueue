"""Generic background job runner with run-history tracking.

Registers named async job functions and runs them as background tasks,
recording each run (start, end, status, detail) to the ``job_runs`` table
so the admin UI can display recent history — the same pattern used for
catalog sync.

Jobs may declare a small parameter ``schema`` (a list of field descriptors)
that the admin UI renders into a Run modal and the manager validates before
each run. Job functions receive ``(params, ctx)`` where ``ctx`` is a
:class:`JobContext` exposing ``db``, ``api_gate``, ``config`` and an async
``progress(detail)`` callback for live progress updates.
"""

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from . import log_capture

logger = logging.getLogger(__name__)

# A job is an async callable taking validated params + a context, returning an
# optional dict of result detail.
JobFunc = Callable[[dict, "JobContext"], Awaitable[dict | None]]


class JobError(Exception):
    """An expected, user-facing job failure (bad input / config, not a bug).

    Raising this from a job records a clean ``failed`` run with the message and
    logs it at WARNING without a stack trace, so misconfiguration (e.g. a
    missing workbook sheet) reads as actionable guidance rather than a crash.
    """


def _option_values(field: dict) -> list:
    """Return the allowed values for an enum field's ``options``.

    Options may be plain scalars or ``{"value": ..., "label": ...}`` dicts.
    """
    values = []
    for opt in field.get("options", []) or []:
        if isinstance(opt, dict):
            values.append(opt.get("value"))
        else:
            values.append(opt)
    return values


def _coerce(field: dict, value: Any) -> Any:
    """Coerce a raw submitted value to the field's declared type."""
    ftype = field.get("type", "string")
    if ftype == "int":
        return int(value)
    if ftype == "float":
        return float(value)
    if ftype == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if ftype == "enum":
        allowed = _option_values(field)
        if allowed and value not in allowed:
            raise ValueError(f"must be one of {allowed}")
        return value
    # string, playlist (id passed through as-is), and any unknown type
    return value if ftype == "playlist" else str(value)


def validate_params(schema: list[dict] | None, raw: dict | None) -> dict:
    """Validate + coerce ``raw`` params against ``schema``.

    Applies defaults for absent optional fields, enforces ``required``, and
    coerces values to declared types. Raises :class:`ValueError` with a
    human-readable message listing all problems. Returns the cleaned params.
    Unknown keys in ``raw`` are ignored.
    """
    schema = schema or []
    raw = raw or {}
    cleaned: dict = {}
    errors: list[str] = []

    for field in schema:
        name = field["name"]
        label = field.get("label", name)
        required = field.get("required", False)
        present = name in raw and raw[name] not in (None, "")

        if not present:
            if required:
                errors.append(f"{label} is required")
            else:
                cleaned[name] = field.get("default")
            continue

        try:
            cleaned[name] = _coerce(field, raw[name])
        except (ValueError, TypeError) as exc:
            msg = str(exc)
            errors.append(
                f"{label} {msg}" if msg.startswith("must") else f"{label} is invalid"
            )

    if errors:
        raise ValueError("; ".join(errors))
    return cleaned


class JobContext:
    """Runtime context handed to a job function.

    Exposes shared services plus an async ``progress`` callback that persists
    incremental progress detail to the job's ``job_runs`` row so the admin UI
    can show live status for long-running jobs.
    """

    def __init__(
        self,
        *,
        db,
        api_gate,
        config,
        run_id: int,
        triggered_by: str | None = None,
        cover_art=None,
        job_manager=None,
    ):
        self.db = db
        self.api_gate = api_gate
        self.config = config
        self.cover_art = cover_art
        self.run_id = run_id
        self.triggered_by = triggered_by
        self.job_manager = job_manager

    async def progress(self, detail: dict) -> None:
        try:
            await self.db.update_job_run_detail(self.run_id, json.dumps(detail))
        except Exception:  # noqa: BLE001 - progress is best-effort
            logger.debug(
                "Failed to persist progress for run %s", self.run_id, exc_info=True
            )


class JobManager:
    def __init__(self, db, *, api_gate=None, config=None, cover_art=None):
        self._db = db
        self._api_gate = api_gate
        self._config = config
        self._cover_art = cover_art
        self._jobs: dict[str, dict] = {}
        self._running: dict[str, asyncio.Task] = {}
        log_capture.install()

    def register(
        self,
        name: str,
        func: JobFunc,
        *,
        label: str | None = None,
        schema: list[dict] | None = None,
    ):
        """Register a named job with an optional parameter schema."""
        self._jobs[name] = {
            "func": func,
            "label": label or name,
            "schema": schema or [],
        }

    def list_jobs(self) -> list[dict]:
        return [
            {
                "name": name,
                "label": meta["label"],
                "running": name in self._running,
                "schema": meta["schema"],
            }
            for name, meta in self._jobs.items()
        ]

    def get_schema(self, name: str) -> list[dict]:
        if name not in self._jobs:
            raise KeyError(name)
        return self._jobs[name]["schema"]

    def is_running(self, name: str) -> bool:
        return name in self._running

    async def stop(self) -> None:
        """Cancel all running job tasks and wait for them to finish.

        Call this during application shutdown *before* closing shared resources
        (database, HTTP clients) so that in-progress tasks receive
        ``CancelledError`` while those resources are still usable.
        """
        if not self._running:
            return
        logger.info("Stopping %d running job(s) for shutdown", len(self._running))
        for task in list(self._running.values()):
            task.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)

    async def run(
        self, name: str, *, triggered_by: str | None = None, params: dict | None = None
    ) -> dict:
        """Start a job in the background. Returns immediately.

        Raises ``KeyError`` if the job is unknown and ``ValueError`` if the
        supplied params fail schema validation. Returns an ``already_running``
        status if a run is in progress.
        """
        if name not in self._jobs:
            raise KeyError(name)
        if name in self._running:
            return {"started": False, "reason": "already_running"}

        meta = self._jobs[name]
        validated = validate_params(meta["schema"], params)  # may raise ValueError

        params_json = json.dumps(validated) if validated else None
        run_id = await self._db.start_job_run(
            name, triggered_by=triggered_by, params=params_json
        )
        task = asyncio.create_task(self._execute(name, run_id, validated, triggered_by))
        self._running[name] = task
        return {"started": True, "run_id": run_id}

    async def _execute(
        self, name: str, run_id: int, params: dict, triggered_by: str | None = None
    ):
        func = self._jobs[name]["func"]
        ctx = JobContext(
            db=self._db,
            api_gate=self._api_gate,
            config=self._config,
            run_id=run_id,
            triggered_by=triggered_by,
            cover_art=getattr(self, "_cover_art", None),
            job_manager=self,
        )
        log_buffer = log_capture.start_capture()
        try:
            result = await func(params, ctx)
            detail = json.dumps(result) if result is not None else None
            await self._db.finish_job_run(run_id, "completed", detail)
        except asyncio.CancelledError:
            try:
                await self._db.finish_job_run(run_id, "cancelled", None)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Could not persist 'cancelled' status for job '%s' (DB unavailable)",
                    name,
                )
            raise
        except JobError as exc:
            # Expected, user-facing failure (bad input/config): record a clean
            # message and log without a stack trace.
            logger.warning("Job '%s' failed: %s", name, exc)
            try:
                await self._db.finish_job_run(
                    run_id, "failed", json.dumps({"error": str(exc)})
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Could not persist 'failed' status for job '%s' (DB unavailable)",
                    name,
                )
        except Exception as exc:  # noqa: BLE001 - record any failure
            logger.exception("Job '%s' failed", name)
            try:
                await self._db.finish_job_run(
                    run_id,
                    "failed",
                    json.dumps({"error": f"{type(exc).__name__}: {exc}"}),
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Could not persist 'failed' status for job '%s' (DB unavailable)",
                    name,
                )
        finally:
            self._running.pop(name, None)
            log_capture.stop_capture()
            if log_buffer:
                try:
                    await self._db.add_job_run_logs(run_id, log_buffer)
                except Exception:  # noqa: BLE001 - log persistence is best-effort
                    logger.debug(
                        "Could not persist %d captured log line(s) for job '%s'",
                        len(log_buffer),
                        name,
                    )
