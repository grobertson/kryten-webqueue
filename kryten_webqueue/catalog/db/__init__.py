import asyncio
import logging
from pathlib import Path
from typing import Any

from ...config import Config, DatabaseConfig
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
from ._motd import _MOTDMixin

from ._catalog_db import _CatalogDB
from ._queue_db import _QueueDB
from ._jobs_db import _JobsDB
from ._users_db import _UsersDB

logger = logging.getLogger(__name__)

_DOMAIN_METHOD_MAP: dict[str, str] = {
    # queue
    "get_shadow_items": "queue",
    "upsert_shadow_item": "queue",
    "remove_shadow_items": "queue",
    "update_shadow_position": "queue",
    "update_shadow_estimated_start": "queue",
    "get_last_pay_uid": "queue",
    "get_shadow_position_after": "queue",
    "get_pay_items": "queue",
    "get_request_id_for_uid": "queue",
    "save_spend_request": "queue",
    "get_spend_request": "queue",
    "mark_spend_refunded": "queue",
    "add_queue_history": "queue",
    "get_user_queue_history": "queue",
    "create_saved_playlist": "queue",
    "get_saved_playlist": "queue",
    "get_saved_playlists": "queue",
    "list_saved_playlists": "queue",
    "update_saved_playlist": "queue",
    "delete_saved_playlist": "queue",
    "save_playlist_items": "queue",
    "get_saved_playlist_items": "queue",
    "replace_playlist_items": "queue",
    "append_playlist_item": "queue",
    "append_playlist_items": "queue",
    "rotate_playlist_item_to_bottom": "queue",
    "get_most_recent_playlist": "queue",
    "get_playlist_by_name": "queue",
    "get_playlist_by_name_any": "queue",
    "get_promo_pools": "queue",
    "get_promo_pool_items": "queue",
    "create_playlist_schedule": "queue",
    "get_playlist_schedule": "queue",
    "list_playlist_schedules": "queue",
    "update_playlist_schedule": "queue",
    "delete_playlist_schedule": "queue",
    "get_schedules": "queue",
    "get_schedule": "queue",
    "create_schedule": "queue",
    "update_schedule": "queue",
    "delete_schedule": "queue",
    "mark_schedule_fired": "queue",
    "get_active_schedule": "queue",
    "set_active_schedule": "queue",
    "clear_active_schedule": "queue",
    "disable_active_lock": "queue",
    "is_event_lock_active": "queue",
    "record_play_completion": "queue",
    "unrecord_play_completion": "queue",
    "clear_play_state": "queue",
    "upsert_blackout": "queue",
    "prune_expired_blackouts": "queue",
    "is_blackout": "queue",
    "count_active_blackouts": "queue",
    "list_active_blackouts": "queue",
    "get_active_hidden_media_ids": "queue",
    "get_reserved_media_ids": "queue",
    "get_active_blackout_tokens": "queue",
    "is_media_restricted": "queue",
    "get_played_at_for_tokens": "queue",
    "get_promo_pool_media_ids": "queue",
    "purge_promo_completions": "queue",
    "get_recently_played_completions": "queue",
    # jobs
    "start_job_run": "jobs",
    "finish_job_run": "jobs",
    "update_job_run_detail": "jobs",
    "add_job_run_logs": "jobs",
    "get_job_run_logs": "jobs",
    "get_job_run": "jobs",
    "get_job_runs": "jobs",
    "reconcile_orphaned_job_runs": "jobs",
    "get_job_schedules": "jobs",
    "get_job_schedule": "jobs",
    "upsert_job_schedule": "jobs",
    "delete_job_schedule": "jobs",
    "fetch_queue_add": "jobs",
    "fetch_queue_get": "jobs",
    "fetch_queue_update": "jobs",
    "fetch_queue_delete": "jobs",
    "fetch_queue_list": "jobs",
    "fetch_queue_count": "jobs",
    "fetch_queue_get_next_pending": "jobs",
    "fetch_queue_mark_started": "jobs",
    "fetch_queue_mark_finished": "jobs",
    "fetch_queue_requeue": "jobs",
    "fetch_queue_cleanup": "jobs",
    # users
    "watchlist_add": "users",
    "watchlist_remove": "users",
    "watchlist_tokens": "users",
    "watchlist_count": "users",
    "get_user_watchlist_tokens": "users",
    "store_otp": "users",
    "verify_otp": "users",
    "cleanup_expired_otps": "users",
    "create_link_code": "users",
    "get_valid_link_code": "users",
    "delete_link_code": "users",
    "link_code_exists": "users",
    "purge_expired_link_codes": "users",
    "create_device_key": "users",
    "get_device_key_by_hash": "users",
    "touch_device_key": "users",
    "list_device_keys": "users",
    "delete_device_key": "users",
    "revoke_user_device_keys": "users",
    "device_key_usernames": "users",
    "add_feedback": "users",
    "create_feedback": "users",
    "list_feedback": "users",
    "update_feedback_status": "users",
    "set_feedback_status": "users",
    "delete_feedback": "users",
    "count_feedback": "users",
    "add_title_suggestion": "users",
    "create_title_suggestion": "users",
    "list_title_suggestions": "users",
    "count_title_suggestions": "users",
    "update_title_suggestion_status": "users",
    "set_title_suggestion_status": "users",
    "delete_title_suggestion": "users",
    # catalog
    "get_item": "catalog",
    "get_item_admin": "catalog",
    "resolve_media": "catalog",
    "delete_catalog_item": "catalog",
    "get_catalog_brief": "catalog",
    "get_item_facets": "catalog",
    "get_categories": "catalog",
    "get_tags": "catalog",
    "upsert_category": "catalog",
    "upsert_tag": "catalog",
    "set_catalog_categories": "catalog",
    "set_catalog_tags": "catalog",
    "add_catalog_tag": "catalog",
    "remove_catalog_tag": "catalog",
    "insert_catalog": "catalog",
    "update_catalog": "catalog",
    "update_cover_art": "catalog",
    "set_imdb_tt": "catalog",
    "get_item_by_imdb_tt": "catalog",
    "delete_stale_catalog_items": "catalog",
    "find_catalog_by_title": "catalog",
    "start_sync_log": "catalog",
    "finish_sync_log": "catalog",
    "get_sync_logs": "catalog",
    "log_item_edit": "catalog",
    "get_item_edit_history": "catalog",
    "get_items_by_tokens": "catalog",
    "get_hidden_category_and_tag_tokens": "catalog",
    "get_people": "catalog",
    "get_studios": "catalog",
    "upsert_person": "catalog",
    "upsert_studio": "catalog",
    "set_catalog_people": "catalog",
    "set_catalog_studios": "catalog",
    "get_enrichment_state": "catalog",
    "update_enrichment_state": "catalog",
    "get_enrichment_candidates": "catalog",
    "get_identify_coverage": "catalog",
    "get_motd_override": "catalog",
    "get_motd_overrides_for_week": "catalog",
    "upsert_motd_override": "catalog",
    "delete_motd_override": "catalog",
}


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
    _MOTDMixin,
    _DBBase,
):
    """Database facade coordinating catalog, queue, jobs, and users SQLite databases."""

    catalog: _CatalogDB
    queue: _QueueDB
    jobs: _JobsDB
    users: _UsersDB

    def __init__(
        self, config_or_path: Config | DatabaseConfig | str | Path | None = None
    ):
        if isinstance(config_or_path, Config):
            self._db_config = config_or_path.database
        elif isinstance(config_or_path, DatabaseConfig):
            self._db_config = config_or_path
        elif isinstance(config_or_path, (str, Path)):
            self._db_config = DatabaseConfig(
                layout="monolith",
                db_path=str(config_or_path),
            )
        elif config_or_path is None:
            self._db_config = DatabaseConfig()
        else:
            raise TypeError(
                f"Invalid database configuration source: {type(config_or_path)}"
            )

        self._layout = self._db_config.layout

        if self._layout == "partitioned":
            _DBBase.__init__(self, self._db_config.get_catalog_path())
            self.catalog = _CatalogDB(self._db_config.get_catalog_path())
            self.queue = _QueueDB(self._db_config.get_queue_path())
            self.jobs = _JobsDB(self._db_config.get_jobs_path())
            self.users = _UsersDB(self._db_config.get_users_path())
        else:
            _DBBase.__init__(self, self._db_config.db_path)
            self.catalog = self  # type: ignore[assignment]
            self.queue = self  # type: ignore[assignment]
            self.jobs = self  # type: ignore[assignment]
            self.users = self  # type: ignore[assignment]

    @property
    def layout(self) -> str:
        return self._layout

    @property
    def db_config(self) -> DatabaseConfig:
        return self._db_config

    async def connect(self):
        if self._layout == "partitioned":
            await asyncio.gather(
                self.catalog.connect(),
                self.queue.connect(),
                self.jobs.connect(),
                self.users.connect(),
            )
            self._db = self.catalog._db
        else:
            await _DBBase.connect(self)

    async def run_migrations(self):
        if self._layout == "partitioned":
            await asyncio.gather(
                self.catalog.run_migrations(),
                self.queue.run_migrations(),
                self.jobs.run_migrations(),
                self.users.run_migrations(),
            )
        else:
            await _DBBase.run_migrations(self)

    async def close(self):
        if self._layout == "partitioned":
            await asyncio.gather(
                self.catalog.close(),
                self.queue.close(),
                self.jobs.close(),
                self.users.close(),
            )
            self._db = None
        else:
            await _DBBase.close(self)

    def __getattribute__(self, name: str) -> Any:
        try:
            layout = object.__getattribute__(self, "_layout")
        except AttributeError:
            layout = "monolith"

        if layout == "partitioned" and name in _DOMAIN_METHOD_MAP:
            domain_name = _DOMAIN_METHOD_MAP[name]
            domain_obj = object.__getattribute__(self, domain_name)
            return getattr(domain_obj, name)

        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        if self._layout == "partitioned":
            for domain in (self.catalog, self.queue, self.jobs, self.users):
                if hasattr(domain, name):
                    return getattr(domain, name)
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    # --- Decoupled Cross-Domain Orchestrations ---

    async def browse(
        self,
        *,
        category: str | None = None,
        tag: str | None = None,
        person: str | None = None,
        studio: str | None = None,
        page: int = 1,
        per_page: int = 24,
        show_hidden: bool = False,
        sort: str = "default",
        recently_played_days: int = 0,
        min_duration_sec: int = 0,
        max_duration_sec: int | None = None,
        exclude_tokens: set[str] | list[str] | None = None,
        **kwargs: Any,
    ) -> list[dict]:
        if self._layout == "partitioned":
            exclude_set = set(exclude_tokens) if exclude_tokens else set()
            if recently_played_days > 0:
                hidden = await self.queue.get_active_hidden_media_ids(
                    recently_played_days
                )
                exclude_set.update(hidden)
            reserved = await self.queue.get_reserved_media_ids()
            exclude_set.update(reserved)
            blackouts = await self.queue.get_active_blackout_tokens()
            if not show_hidden:
                exclude_set.update(blackouts)

            items = await self.catalog.browse(
                category=category,
                tag=tag,
                person=person,
                studio=studio,
                page=page,
                per_page=per_page,
                show_hidden=show_hidden,
                sort=sort,
                recently_played_days=0,
                min_duration_sec=min_duration_sec,
                max_duration_sec=max_duration_sec,
                exclude_tokens=exclude_set,
                is_partitioned=True,
            )
            if items:
                tokens = [i["friendly_token"] for i in items]
                played_map = await self.queue.get_played_at_for_tokens(tokens)
                for i in items:
                    tok = i["friendly_token"]
                    i["played_at"] = played_map.get(tok)
                    i["blackout_active"] = 1 if tok in blackouts else 0
            return items

        return await _CatalogMixin.browse(
            self,
            category=category,
            tag=tag,
            person=person,
            studio=studio,
            page=page,
            per_page=per_page,
            show_hidden=show_hidden,
            sort=sort,
            recently_played_days=recently_played_days,
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
            exclude_tokens=exclude_tokens,
            is_partitioned=False,
        )

    async def browse_count(
        self,
        *,
        category: str | None = None,
        tag: str | None = None,
        person: str | None = None,
        studio: str | None = None,
        show_hidden: bool = False,
        recently_played_days: int = 0,
        min_duration_sec: int = 0,
        max_duration_sec: int | None = None,
        exclude_tokens: set[str] | list[str] | None = None,
        **kwargs: Any,
    ) -> int:
        if self._layout == "partitioned":
            exclude_set = set(exclude_tokens) if exclude_tokens else set()
            if recently_played_days > 0:
                hidden = await self.queue.get_active_hidden_media_ids(
                    recently_played_days
                )
                exclude_set.update(hidden)
            reserved = await self.queue.get_reserved_media_ids()
            exclude_set.update(reserved)
            if not show_hidden:
                blackouts = await self.queue.get_active_blackout_tokens()
                exclude_set.update(blackouts)

            return await self.catalog.browse_count(
                category=category,
                tag=tag,
                person=person,
                studio=studio,
                show_hidden=show_hidden,
                recently_played_days=0,
                min_duration_sec=min_duration_sec,
                max_duration_sec=max_duration_sec,
                exclude_tokens=exclude_set,
                is_partitioned=True,
            )

        return await _CatalogMixin.browse_count(
            self,
            category=category,
            tag=tag,
            person=person,
            studio=studio,
            show_hidden=show_hidden,
            recently_played_days=recently_played_days,
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
            exclude_tokens=exclude_tokens,
            is_partitioned=False,
        )

    async def search(
        self,
        query_text: str,
        *,
        category: str | None = None,
        tag: str | None = None,
        person: str | None = None,
        studio: str | None = None,
        page: int = 1,
        per_page: int = 24,
        show_hidden: bool = False,
        sort: str = "default",
        recently_played_days: int = 0,
        min_duration_sec: int = 0,
        max_duration_sec: int | None = None,
        exclude_tokens: set[str] | list[str] | None = None,
        **kwargs: Any,
    ) -> list[dict]:
        if self._layout == "partitioned":
            exclude_set = set(exclude_tokens) if exclude_tokens else set()
            if recently_played_days > 0:
                hidden = await self.queue.get_active_hidden_media_ids(
                    recently_played_days
                )
                exclude_set.update(hidden)
            reserved = await self.queue.get_reserved_media_ids()
            exclude_set.update(reserved)
            blackouts = await self.queue.get_active_blackout_tokens()
            if not show_hidden:
                exclude_set.update(blackouts)

            items = await self.catalog.search(
                query_text,
                category=category,
                tag=tag,
                person=person,
                studio=studio,
                page=page,
                per_page=per_page,
                show_hidden=show_hidden,
                sort=sort,
                recently_played_days=0,
                min_duration_sec=min_duration_sec,
                max_duration_sec=max_duration_sec,
                exclude_tokens=exclude_set,
                is_partitioned=True,
            )
            if items:
                tokens = [i["friendly_token"] for i in items]
                played_map = await self.queue.get_played_at_for_tokens(tokens)
                for i in items:
                    tok = i["friendly_token"]
                    i["played_at"] = played_map.get(tok)
                    i["blackout_active"] = 1 if tok in blackouts else 0
            return items

        return await _CatalogMixin.search(
            self,
            query_text,
            category=category,
            tag=tag,
            person=person,
            studio=studio,
            page=page,
            per_page=per_page,
            show_hidden=show_hidden,
            sort=sort,
            recently_played_days=recently_played_days,
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
            exclude_tokens=exclude_tokens,
            is_partitioned=False,
        )

    async def search_count(
        self,
        query_text: str,
        *,
        category: str | None = None,
        tag: str | None = None,
        person: str | None = None,
        studio: str | None = None,
        show_hidden: bool = False,
        recently_played_days: int = 0,
        min_duration_sec: int = 0,
        max_duration_sec: int | None = None,
        exclude_tokens: set[str] | list[str] | None = None,
        **kwargs: Any,
    ) -> int:
        if self._layout == "partitioned":
            exclude_set = set(exclude_tokens) if exclude_tokens else set()
            if recently_played_days > 0:
                hidden = await self.queue.get_active_hidden_media_ids(
                    recently_played_days
                )
                exclude_set.update(hidden)
            reserved = await self.queue.get_reserved_media_ids()
            exclude_set.update(reserved)
            if not show_hidden:
                blackouts = await self.queue.get_active_blackout_tokens()
                exclude_set.update(blackouts)

            return await self.catalog.search_count(
                query_text,
                category=category,
                tag=tag,
                person=person,
                studio=studio,
                show_hidden=show_hidden,
                recently_played_days=0,
                min_duration_sec=min_duration_sec,
                max_duration_sec=max_duration_sec,
                exclude_tokens=exclude_set,
                is_partitioned=True,
            )

        return await _CatalogMixin.search_count(
            self,
            query_text,
            category=category,
            tag=tag,
            person=person,
            studio=studio,
            show_hidden=show_hidden,
            recently_played_days=recently_played_days,
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
            exclude_tokens=exclude_tokens,
            is_partitioned=False,
        )

    async def watchlist_get(
        self, username: str, *, page: int = 1, per_page: int = 24
    ) -> list[dict]:
        if self._layout == "partitioned":
            entries = await self.users.get_user_watchlist_tokens(
                username, limit=per_page, offset=(page - 1) * per_page
            )
            if not entries:
                return []
            tokens = [e["friendly_token"] for e in entries]
            items = await self.catalog.get_items_by_tokens(tokens)
            item_map = {item["friendly_token"]: item for item in items}
            result: list[dict] = []
            for e in entries:
                tok = e["friendly_token"]
                if tok in item_map:
                    result.append(dict(item_map[tok]))
            return result

        return await _WatchlistMixin.watchlist_get(
            self, username, page=page, per_page=per_page
        )

    async def get_item(self, friendly_token: str) -> dict | None:
        if self._layout == "partitioned":
            item = await self.catalog.get_item(friendly_token, is_partitioned=True)
            if not item:
                return None
            manifest_url = item.get("manifest_url") or ""
            if await self.queue.is_media_restricted([friendly_token, manifest_url]):
                return None
            if await self.queue.is_blackout(friendly_token):
                return None
            return item

        return await _CatalogMixin.get_item(self, friendly_token)

    async def is_restricted(self, friendly_token: str) -> bool:
        if self._layout == "partitioned":
            item = await self.catalog.get_item(friendly_token, is_partitioned=True)
            if not item:
                return False
            manifest_url = item.get("manifest_url") or ""
            return await self.queue.is_media_restricted([friendly_token, manifest_url])

        return await _CatalogMixin.is_restricted(self, friendly_token)

    async def purge_promo_hide_state(self) -> dict:
        if self._layout == "partitioned":
            promo_pool = await self.queue.get_promo_pool_media_ids()
            cat_tags = await self.catalog.get_hidden_category_and_tag_tokens()
            all_media = promo_pool | cat_tags
            count = await self.queue.purge_promo_completions(all_media)
            return {"completions": count}

        return await _CatalogMixin.purge_promo_hide_state(self)

    async def get_recently_played_debug(self, days: int) -> dict:
        if self._layout == "partitioned":
            completions = await self.queue.get_recently_played_completions(days)
            tokens = [c["media_id"] for c in completions]
            items = await self.catalog.get_items_by_tokens(tokens)
            title_map = {i["friendly_token"]: i.get("title") for i in items}
            by_completion = [
                {
                    "media_id": c["media_id"],
                    "title": title_map.get(c["media_id"]),
                    "last_completed": c.get("last_completed"),
                }
                for c in completions
            ]
            return {
                "window_days": days,
                "by_completion": by_completion,
            }

        return await _CatalogMixin.get_recently_played_debug(self, days)


__all__ = [
    "Database",
    "_CatalogDB",
    "_QueueDB",
    "_JobsDB",
    "_UsersDB",
    "HIDDEN_ITEM_TAG",
    "HIDDEN_CATEGORY_NAMES",
    "HIDDEN_TAG_NAMES",
]
