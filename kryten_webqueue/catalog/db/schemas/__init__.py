"""Domain schemas and migration sets for partitioned SQLite databases."""

from .catalog_schema import CATALOG_MIGRATIONS
from .queue_schema import QUEUE_MIGRATIONS
from .jobs_schema import JOBS_MIGRATIONS
from .users_schema import USERS_MIGRATIONS

__all__ = [
    "CATALOG_MIGRATIONS",
    "QUEUE_MIGRATIONS",
    "JOBS_MIGRATIONS",
    "USERS_MIGRATIONS",
]
