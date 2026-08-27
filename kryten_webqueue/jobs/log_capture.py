"""Per-run capture of Python log records emitted during a background job.

A single :class:`logging.Handler` is installed on the root logger. It is gated
by a :class:`contextvars.ContextVar`, so a record is captured only when it is
emitted from within a job run's async context — the buffer is set on the run's
task and inherited by any child tasks / ``asyncio.to_thread`` calls it spawns.
Concurrent web requests (and other jobs) run in their own contexts with their
own buffer, so their logs never bleed into a run's log.

The captured records are drained by :class:`~kryten_webqueue.jobs.manager.JobManager`
when a run finishes and persisted one row per line to ``job_run_logs``.
"""

import contextvars
import logging
from datetime import datetime, UTC

# One in-flight log buffer per run context. Each entry is
# ``(logged_at_iso, level, logger_name, message)``.
_current_buffer: contextvars.ContextVar[list[tuple[str, str, str, str]] | None] = (
    contextvars.ContextVar("kryten_job_run_log_buffer", default=None)
)

_handler: "logging.Handler | None" = None
_formatter = logging.Formatter()


class _RunLogHandler(logging.Handler):
    """Append formatted records to the current context's run buffer, if any."""

    def emit(self, record: logging.LogRecord) -> None:
        buffer = _current_buffer.get()
        if buffer is None:
            return
        try:
            logged_at = datetime.fromtimestamp(record.created, UTC).isoformat()
            buffer.append(
                (logged_at, record.levelname, record.name, record.getMessage())
            )
            if record.exc_info:
                buffer.append(
                    (
                        logged_at,
                        record.levelname,
                        record.name,
                        _formatter.formatException(record.exc_info),
                    )
                )
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


def install() -> None:
    """Attach the capture handler to the root logger (idempotent)."""
    global _handler
    if _handler is not None:
        return
    handler = _RunLogHandler()
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)
    _handler = handler


def start_capture() -> list[tuple[str, str, str, str]]:
    """Begin capturing in the current context; returns the run's log buffer."""
    buffer: list[tuple[str, str, str, str]] = []
    _current_buffer.set(buffer)
    return buffer


def stop_capture() -> None:
    """Stop capturing in the current context."""
    _current_buffer.set(None)
