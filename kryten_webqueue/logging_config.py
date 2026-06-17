"""Centralised logging configuration.

The app previously relied on ``uvicorn.run(log_level="info")`` which only
configures uvicorn's *own* loggers (``uvicorn``/``uvicorn.access``/
``uvicorn.error``). Application loggers in the ``kryten_webqueue`` hierarchy had
no handler, so Python's "last resort" handler emitted only ``WARNING`` and
above — silently dropping every ``logger.info(...)`` call (e.g. all promo
insertion diagnostics). This module builds a single ``dictConfig`` that installs
a console handler for both uvicorn and the application loggers, with an
independently tunable level for the promo subsystem.
"""

from __future__ import annotations


def _normalize_level(level: str | None, default: str) -> str:
    if not level:
        return default
    candidate = str(level).strip().upper()
    valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
    return candidate if candidate in valid else default


def build_log_config(log_level: str = "INFO", promo_log_level: str | None = None) -> dict:
    """Return a ``logging.config.dictConfig`` dict for uvicorn + the app.

    Args:
        log_level: Level for the root and ``kryten_webqueue`` loggers.
        promo_log_level: Level for ``kryten_webqueue.promos`` (the promo
            director). Falls back to ``log_level`` when not provided. Set to
            ``DEBUG`` for a full per-poll trace of promo decisions.
    """
    app_level = _normalize_level(log_level, "INFO")
    promo_level = _normalize_level(promo_log_level, app_level)

    return {
        "version": 1,
        # Never tear down loggers created at import time (module-level
        # ``getLogger`` calls); we only attach handlers/levels.
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "access": {
                "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "class": "logging.StreamHandler",
                "formatter": "access",
                "stream": "ext://sys.stdout",
            },
        },
        "root": {"handlers": ["console"], "level": app_level},
        "loggers": {
            "kryten_webqueue": {
                "level": app_level,
                "handlers": ["console"],
                "propagate": False,
            },
            # Promo subsystem: independently tunable so operators can crank it to
            # DEBUG for a deep dive without flooding the rest of the app.
            "kryten_webqueue.promos": {
                "level": promo_level,
                "handlers": ["console"],
                "propagate": False,
            },
            "uvicorn": {"level": "INFO", "handlers": ["console"], "propagate": False},
            "uvicorn.error": {"level": "INFO", "handlers": ["console"], "propagate": False},
            "uvicorn.access": {"level": "INFO", "handlers": ["access"], "propagate": False},
        },
    }
