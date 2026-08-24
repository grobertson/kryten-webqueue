from ._connection import _DBBase
from ._catalog import (
    _CatalogMixin,
    HIDDEN_ITEM_TAG,
    HIDDEN_CATEGORY_NAMES,
    HIDDEN_TAG_NAMES,
)
from ._playlists import _PlaylistsMixin
from ._queue import _QueueMixin
from ._feedback import _FeedbackMixin
from ._watchlist import _WatchlistMixin
from ._people import _PeopleMixin
from ._enrichment import _EnrichmentMixin
from ._fetch_queue import _FetchQueueMixin
from ._blackouts import _BlackoutMixin
from ._devices import _DevicesMixin


class Database(
    _CatalogMixin,
    _PlaylistsMixin,
    _QueueMixin,
    _FeedbackMixin,
    _WatchlistMixin,
    _PeopleMixin,
    _EnrichmentMixin,
    _FetchQueueMixin,
    _BlackoutMixin,
    _DevicesMixin,
    _DBBase,
):
    """Async SQLite database wrapper."""


__all__ = [
    "Database",
    "HIDDEN_ITEM_TAG",
    "HIDDEN_CATEGORY_NAMES",
    "HIDDEN_TAG_NAMES",
]
