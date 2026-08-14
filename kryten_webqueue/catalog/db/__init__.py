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


class Database(
    _CatalogMixin,
    _PlaylistsMixin,
    _QueueMixin,
    _FeedbackMixin,
    _WatchlistMixin,
    _DBBase,
):
    """Async SQLite database wrapper."""


__all__ = [
    "Database",
    "HIDDEN_ITEM_TAG",
    "HIDDEN_CATEGORY_NAMES",
    "HIDDEN_TAG_NAMES",
]
