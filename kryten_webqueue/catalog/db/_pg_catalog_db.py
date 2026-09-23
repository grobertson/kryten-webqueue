"""Postgres-backed catalog domain: catalog items, facets, people/studios, enrichment, MOTD.

Only the ``is_partitioned=True`` code path from the SQLite catalog mixin applies here —
Postgres always runs the 4-schema decoupled layout (there is no "monolith Postgres" mode),
so cross-domain exclusions (reserved/blackout/recently-played) are computed by
``Database``'s facade orchestration and passed in via ``exclude_tokens``, exactly like the
SQLite partitioned layout.
"""

import json
import logging
import re
from datetime import datetime, timezone

import asyncpg

from ._pg_base_domain import _PgDomainDB, parse_dt

logger = logging.getLogger(__name__)

# Categories and tags whose items are hidden from the public catalog. Mirrors
# kryten_webqueue/catalog/db/_catalog.py exactly (kept in sync manually).
HIDDEN_CATEGORY_NAMES = [
    "Z Channel Promos",
    "Z Event Movies",
    "Weekday Z Promos",
]
HIDDEN_TAG_NAMES = [
    "grindhousebumper",
    "commercialsforbumpers",
    "bumpers",
    "channelz",
    "channelzpromo",
    "promo",
    "commercial",
    "Commercials",
    "donotplay",
    "grindhousetrailer",
    "publicaccess",
    "religioustv",
    "kryten-hidden",
    "hidden",
    "hide",
    "Halifax",
]

HIDDEN_ITEM_TAG = "kryten-hidden"


def _sanitize_fts_query(q: str) -> str:
    """Strip search metacharacters from a user-supplied search string."""
    tokens = re.sub(r"[^\w\s]", " ", q, flags=re.UNICODE).split()
    return " ".join(tokens)


def _duration_range_filter(
    alias: str, min_sec: int | None = None, max_sec: int | None = None
) -> tuple[str, list]:
    if min_sec is not None and max_sec is not None:
        return f" AND {alias}.duration_sec >= ? AND {alias}.duration_sec < ? ", [
            min_sec,
            max_sec,
        ]
    elif min_sec is not None:
        return f" AND {alias}.duration_sec >= ? ", [min_sec]
    elif max_sec is not None:
        return f" AND {alias}.duration_sec < ? ", [max_sec]
    else:
        return "", []


def _slugify(text: str) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-") or "untitled"


def _normalize_title(title: str) -> str:
    s = (title or "").lower()
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", s)
    s = re.sub(r"\b(?:19|20)\d{2}\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _hidden_exclusion(alias: str = "c") -> tuple[str, list]:
    cat_ph = ",".join("?" * len(HIDDEN_CATEGORY_NAMES))
    tag_ph = ",".join("?" * len(HIDDEN_TAG_NAMES))
    sql = f"""
            AND {alias}.friendly_token NOT IN (
                SELECT cc.friendly_token FROM catalog_categories cc
                JOIN categories cat ON cc.category_id = cat.id
                WHERE cat.name IN ({cat_ph})
            )
            AND {alias}.friendly_token NOT IN (
                SELECT ct.friendly_token FROM catalog_tags ct
                JOIN tags t ON ct.tag_id = t.id
                WHERE t.name IN ({tag_ph})
            )
    """
    return sql, [*HIDDEN_CATEGORY_NAMES, *HIDDEN_TAG_NAMES]


def _facet_filter(
    alias: str,
    category: str | None,
    tag: str | None,
    person: str | None = None,
    studio: str | None = None,
) -> tuple[str, list]:
    sql = ""
    params: list = []
    if category:
        sql += f"""
            AND {alias}.friendly_token IN (
                SELECT cc.friendly_token FROM catalog_categories cc
                JOIN categories cat ON cc.category_id = cat.id
                WHERE cat.slug = ?
            )
        """
        params.append(category)
    if tag:
        sql += f"""
            AND {alias}.friendly_token IN (
                SELECT ct.friendly_token FROM catalog_tags ct
                JOIN tags t ON ct.tag_id = t.id
                WHERE t.name = ?
            )
        """
        params.append(tag)
    if person:
        sql += f"""
            AND {alias}.friendly_token IN (
                SELECT cp.friendly_token FROM catalog_people cp
                JOIN people p ON cp.person_id = p.id
                WHERE p.name = ?
            )
        """
        params.append(person)
    if studio:
        sql += f"""
            AND {alias}.friendly_token IN (
                SELECT cs.friendly_token FROM catalog_studios cs
                JOIN studios s ON cs.studio_id = s.id
                WHERE s.name = ?
            )
        """
        params.append(studio)
    return sql, params


# Same quality-weighted default ordering as SQLite, with GLOB translated to a
# POSIX regex match (Postgres has no GLOB operator).
_DEFAULT_ORDER = """
    ORDER BY
        (c.cover_art_source IN ('tmdb', 'omdb')) DESC,
        (CASE WHEN c.title ~ '^[A-Za-z]' THEN 0 ELSE 1 END) ASC,
        c.title ASC
"""

_SORT_CLAUSES = {
    "default": _DEFAULT_ORDER,
    "title_asc": " ORDER BY c.title ASC ",
    "title_desc": " ORDER BY c.title DESC ",
    "newest": " ORDER BY c.added_at DESC, c.synced_at DESC ",
    "oldest": " ORDER BY c.added_at ASC, c.synced_at ASC ",
}


def _browse_order_clause(sort: str | None) -> str:
    return _SORT_CLAUSES.get(sort or "default", _DEFAULT_ORDER)


VALID_ROLES = frozenset({"cast", "director", "producer", "writer"})

_ENRICHMENT_SAVE_ALLOWED = {
    "content_type",
    "hosted_show",
    "lookup_title",
    "lookup_year",
    "tv_show",
    "tv_season",
    "tv_episode_num",
    "description_score",
    "tmdb_id",
    "imdb_id",
    "meta_json",
    "last_classify_at",
    "last_title_at",
    "last_meta_at",
    "last_art_at",
    "last_tags_at",
    "last_categories_at",
    "last_identify_at",
    "identify_source",
    "identify_reason",
}
_ENRICHMENT_UPDATE_ALLOWED = {
    "content_type",
    "hosted_show",
    "lookup_title",
    "lookup_year",
    "tv_show",
    "tv_season",
    "tv_episode_num",
}

_CATALOG_UPDATE_ALLOWED = {
    "title",
    "description",
    "duration_sec",
    "manifest_url",
    "thumbnail_url",
    "added_at",
    "synced_at",
    "imdb_tt",
    "override_artwork_tt_id",
}
_CATALOG_TIMESTAMP_KEYS = {"added_at", "synced_at"}


class _PgCatalogDB(_PgDomainDB):
    """Postgres implementation of the catalog domain (schema ``catalog``)."""

    # --- Browse / search ---

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
        is_partitioned: bool = True,
    ) -> list[dict]:
        query = """
            SELECT c.friendly_token, c.title, c.duration_sec, c.cover_art_path, c.cover_art_source, c.thumbnail_url, c.manifest_url,
                   NULL AS played_at,
                   0 AS blackout_active
            FROM catalog c
            WHERE 1=1
        """
        params: list = []
        if not show_hidden:
            excl_sql, excl_params = _hidden_exclusion("c")
            query += excl_sql
            params.extend(excl_params)

        if exclude_tokens:
            tokens_list = [t for t in exclude_tokens if t]
            for i in range(0, len(tokens_list), 500):
                chunk = tokens_list[i : i + 500]
                ph = ",".join("?" * len(chunk))
                query += f" AND c.friendly_token NOT IN ({ph}) "
                params.extend(chunk)

        if min_duration_sec > 0 or max_duration_sec is not None:
            dur_sql, dur_params = _duration_range_filter(
                "c", min_duration_sec or None, max_duration_sec
            )
            query += dur_sql
            params.extend(dur_params)

        facet_sql, facet_params = _facet_filter("c", category, tag, person, studio)
        query += facet_sql
        params.extend(facet_params)

        query += _browse_order_clause(sort)
        query += " LIMIT ? OFFSET ?"
        params.extend([per_page, (page - 1) * per_page])
        return await self._fetch_all(query, params)

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
        is_partitioned: bool = True,
    ) -> int:
        query = "SELECT COUNT(*) as cnt FROM catalog c WHERE 1=1"
        params: list = []
        if not show_hidden:
            excl_sql, excl_params = _hidden_exclusion("c")
            query += excl_sql
            params.extend(excl_params)

        if exclude_tokens:
            tokens_list = [t for t in exclude_tokens if t]
            for i in range(0, len(tokens_list), 500):
                chunk = tokens_list[i : i + 500]
                ph = ",".join("?" * len(chunk))
                query += f" AND c.friendly_token NOT IN ({ph}) "
                params.extend(chunk)

        if min_duration_sec > 0 or max_duration_sec is not None:
            dur_sql, dur_params = _duration_range_filter(
                "c", min_duration_sec or None, max_duration_sec
            )
            query += dur_sql
            params.extend(dur_params)

        facet_sql, facet_params = _facet_filter("c", category, tag, person, studio)
        query += facet_sql
        params.extend(facet_params)

        row = await self._fetch_one(query, params)
        return row["cnt"] if row else 0

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
        is_partitioned: bool = True,
    ) -> list[dict]:
        sanitized = _sanitize_fts_query(query_text)
        if not sanitized:
            return []

        sql = """
            WITH fts_matches AS (
                SELECT c.friendly_token, ts_rank(c.search_vector, q) AS rank_score, 1 AS match_type
                FROM catalog c, websearch_to_tsquery('english', ?) q
                WHERE c.search_vector @@ q
            ),
            trgm_matches AS (
                SELECT c.friendly_token, similarity(c.title, ?) AS rank_score, 2 AS match_type
                FROM catalog c
                WHERE similarity(c.title, ?) > 0.3
                  AND c.friendly_token NOT IN (SELECT friendly_token FROM fts_matches)
            ),
            matched AS (
                SELECT * FROM fts_matches
                UNION ALL
                SELECT * FROM trgm_matches
            )
            SELECT c.friendly_token, c.title, c.duration_sec, c.cover_art_path, c.cover_art_source, c.thumbnail_url, c.manifest_url,
                   NULL AS played_at,
                   m.rank_score AS relevance,
                   0 AS blackout_active
            FROM matched m
            JOIN catalog c ON c.friendly_token = m.friendly_token
            WHERE 1=1
        """
        params: list = [sanitized, sanitized, sanitized]
        if not show_hidden:
            excl_sql, excl_params = _hidden_exclusion("c")
            sql += excl_sql
            params.extend(excl_params)

        if exclude_tokens:
            tokens_list = [t for t in exclude_tokens if t]
            for i in range(0, len(tokens_list), 500):
                chunk = tokens_list[i : i + 500]
                ph = ",".join("?" * len(chunk))
                sql += f" AND c.friendly_token NOT IN ({ph}) "
                params.extend(chunk)

        if min_duration_sec > 0 or max_duration_sec is not None:
            dur_sql, dur_params = _duration_range_filter(
                "c", min_duration_sec or None, max_duration_sec
            )
            sql += dur_sql
            params.extend(dur_params)

        facet_sql, facet_params = _facet_filter("c", category, tag, person, studio)
        sql += facet_sql
        params.extend(facet_params)

        sql += (
            " ORDER BY m.match_type ASC, relevance DESC "
            if (sort or "default") == "default"
            else _browse_order_clause(sort)
        )
        sql += " LIMIT ? OFFSET ? "
        params.extend([per_page, (page - 1) * per_page])
        return await self._fetch_all(sql, params)

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
        is_partitioned: bool = True,
    ) -> int:
        sanitized = _sanitize_fts_query(query_text)
        if not sanitized:
            return 0
        sql = """
            WITH fts_matches AS (
                SELECT c.friendly_token
                FROM catalog c, websearch_to_tsquery('english', ?) q
                WHERE c.search_vector @@ q
            ),
            trgm_matches AS (
                SELECT c.friendly_token
                FROM catalog c
                WHERE similarity(c.title, ?) > 0.3
                  AND c.friendly_token NOT IN (SELECT friendly_token FROM fts_matches)
            ),
            matched AS (
                SELECT * FROM fts_matches
                UNION ALL
                SELECT * FROM trgm_matches
            )
            SELECT COUNT(*) as cnt
            FROM matched m
            JOIN catalog c ON c.friendly_token = m.friendly_token
            WHERE 1=1
        """
        params: list = [sanitized, sanitized]
        if not show_hidden:
            excl_sql, excl_params = _hidden_exclusion("c")
            sql += excl_sql
            params.extend(excl_params)

        if exclude_tokens:
            tokens_list = [t for t in exclude_tokens if t]
            for i in range(0, len(tokens_list), 500):
                chunk = tokens_list[i : i + 500]
                ph = ",".join("?" * len(chunk))
                sql += f" AND c.friendly_token NOT IN ({ph}) "
                params.extend(chunk)

        if min_duration_sec > 0 or max_duration_sec is not None:
            dur_sql, dur_params = _duration_range_filter(
                "c", min_duration_sec or None, max_duration_sec
            )
            sql += dur_sql
            params.extend(dur_params)

        facet_sql, facet_params = _facet_filter("c", category, tag, person, studio)
        sql += facet_sql
        params.extend(facet_params)

        row = await self._fetch_one(sql, params)
        return row["cnt"] if row else 0

    async def get_item(
        self, friendly_token: str, is_partitioned: bool = True
    ) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM catalog WHERE friendly_token = ?", [friendly_token]
        )

    async def get_item_admin(self, friendly_token: str) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM catalog WHERE friendly_token = ?", [friendly_token]
        )

    async def resolve_media(self, media_id: str) -> dict | None:
        if not media_id:
            return None
        return await self._fetch_one(
            "SELECT friendly_token, duration_sec FROM catalog "
            "WHERE friendly_token = ? OR manifest_url = ? LIMIT 1",
            [media_id, media_id],
        )

    async def delete_catalog_item(self, friendly_token: str) -> bool:
        """Permanently delete a catalog item and all facet associations."""
        item = await self._fetch_one(
            "SELECT friendly_token FROM catalog WHERE friendly_token = ?",
            [friendly_token],
        )
        if not item:
            return False

        # catalog_people / catalog_studios are not FK-cascaded from catalog,
        # so remove them explicitly. catalog_tags / catalog_categories cascade
        # via ON DELETE CASCADE. No catalog_fts table exists in Postgres —
        # search_vector is a generated column maintained automatically.
        await self._execute(
            "DELETE FROM catalog_people WHERE friendly_token = ?", [friendly_token]
        )
        await self._execute(
            "DELETE FROM catalog_studios WHERE friendly_token = ?", [friendly_token]
        )
        await self._execute(
            "DELETE FROM catalog WHERE friendly_token = ?", [friendly_token]
        )
        return True

    async def get_catalog_brief(
        self, tokens: list[str], manifest_urls: list[str]
    ) -> dict[str, dict]:
        keys = [k for k in ({*tokens} | {*manifest_urls}) if k]
        if not keys:
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = await self._fetch_all(
            "SELECT friendly_token, manifest_url, title, duration_sec, "
            "cover_art_path, thumbnail_url FROM catalog "
            f"WHERE friendly_token IN ({placeholders}) OR manifest_url IN ({placeholders})",
            keys + keys,
        )
        lookup: dict[str, dict] = {}
        for row in rows:
            data = dict(row)
            if data.get("friendly_token"):
                lookup[data["friendly_token"]] = data
            if data.get("manifest_url"):
                lookup[data["manifest_url"]] = data
        return lookup

    async def get_item_facets(self, friendly_token: str) -> dict:
        if not friendly_token:
            return {
                "description": None,
                "categories": [],
                "tags": [],
                "people": {"cast": [], "director": [], "producer": [], "writer": []},
                "studios": [],
            }
        row = await self._fetch_one(
            "SELECT description FROM catalog WHERE friendly_token = ?", [friendly_token]
        )
        cats = await self._fetch_all(
            "SELECT cat.name, cat.slug FROM categories cat "
            "JOIN catalog_categories cc ON cc.category_id = cat.id "
            "WHERE cc.friendly_token = ? ORDER BY cat.name",
            [friendly_token],
        )
        tags = await self._fetch_all(
            "SELECT t.name FROM tags t "
            "JOIN catalog_tags ct ON ct.tag_id = t.id "
            "WHERE ct.friendly_token = ? ORDER BY t.name",
            [friendly_token],
        )
        people = await self.get_item_people(friendly_token)
        studios = await self.get_item_studios(friendly_token)
        return {
            "description": (row or {}).get("description"),
            "categories": [{"name": c["name"], "slug": c["slug"]} for c in cats],
            "tags": [t["name"] for t in tags],
            "people": people,
            "studios": studios,
        }

    async def get_categories(self, *, show_hidden: bool = False) -> list[dict]:
        sql = """
            SELECT c.id, c.name, c.slug, COUNT(cc.friendly_token) AS cnt
            FROM categories c
            JOIN catalog_categories cc ON cc.category_id = c.id
        """
        params: list = []
        if not show_hidden:
            ph = ",".join("?" * len(HIDDEN_CATEGORY_NAMES))
            sql += f" WHERE c.name NOT IN ({ph})"
            params.extend(HIDDEN_CATEGORY_NAMES)
        sql += " GROUP BY c.id, c.name, c.slug ORDER BY c.name"
        return await self._fetch_all(sql, params)

    async def get_tags(
        self,
        *,
        limit: int = 100,
        show_hidden: bool = False,
        min_duration_sec: int = 0,
        max_duration_sec: int | None = None,
        exclude_tokens: set[str] | list[str] | None = None,
        is_partitioned: bool = True,
    ) -> list[dict]:
        sql = """
            SELECT t.id, t.name, COUNT(ct.friendly_token) AS cnt
            FROM tags t
            JOIN catalog_tags ct ON ct.tag_id = t.id
            JOIN catalog c ON ct.friendly_token = c.friendly_token
            WHERE 1=1
        """
        params: list = []
        if not show_hidden:
            ph = ",".join("?" * len(HIDDEN_TAG_NAMES))
            sql += f" AND t.name NOT IN ({ph})"
            params.extend(HIDDEN_TAG_NAMES)
        if exclude_tokens:
            tokens_list = [t for t in exclude_tokens if t]
            for i in range(0, len(tokens_list), 500):
                chunk = tokens_list[i : i + 500]
                ph = ",".join("?" * len(chunk))
                sql += f" AND c.friendly_token NOT IN ({ph}) "
                params.extend(chunk)
        if min_duration_sec > 0 or max_duration_sec is not None:
            dur_sql, dur_params = _duration_range_filter(
                "c", min_duration_sec or None, max_duration_sec
            )
            sql += dur_sql
            params.extend(dur_params)
        sql += """
            GROUP BY t.id, t.name
            HAVING COUNT(ct.friendly_token) > 2
            ORDER BY cnt DESC, t.name ASC
            LIMIT ?
        """
        params.append(limit)
        return await self._fetch_all(sql, params)

    async def upsert_category(self, name: str) -> int:
        existing = await self._fetch_one(
            "SELECT id FROM categories WHERE name = ?", [name]
        )
        if existing:
            return existing["id"]
        base = _slugify(name)
        slug, n = base, 1
        while await self._fetch_one("SELECT 1 FROM categories WHERE slug = ?", [slug]):
            n += 1
            slug = f"{base}-{n}"
        return await self._execute_returning_id(
            "INSERT INTO categories (name, slug) VALUES (?, ?) RETURNING id",
            [name, slug],
        )

    async def upsert_tag(self, name: str) -> int:
        existing = await self._fetch_one("SELECT id FROM tags WHERE name = ?", [name])
        if existing:
            return existing["id"]
        return await self._execute_returning_id(
            "INSERT INTO tags (name) VALUES (?) RETURNING id", [name]
        )

    async def set_catalog_categories(
        self, friendly_token: str, category_ids: list[int]
    ):
        await self._execute(
            "DELETE FROM catalog_categories WHERE friendly_token = ?", [friendly_token]
        )
        for cid in category_ids:
            await self._execute(
                "INSERT INTO catalog_categories (friendly_token, category_id) VALUES (?, ?) "
                "ON CONFLICT DO NOTHING",
                [friendly_token, cid],
            )

    async def set_catalog_tags(self, friendly_token: str, tag_ids: list[int]):
        await self._execute(
            "DELETE FROM catalog_tags WHERE friendly_token = ?", [friendly_token]
        )
        for tid in tag_ids:
            await self._execute(
                "INSERT INTO catalog_tags (friendly_token, tag_id) VALUES (?, ?) "
                "ON CONFLICT DO NOTHING",
                [friendly_token, tid],
            )

    async def add_catalog_tag(self, friendly_token: str, tag_name: str):
        tag_id = await self.upsert_tag(tag_name)
        await self._execute(
            "INSERT INTO catalog_tags (friendly_token, tag_id) VALUES (?, ?) "
            "ON CONFLICT DO NOTHING",
            [friendly_token, tag_id],
        )

    async def remove_catalog_tag(self, friendly_token: str, tag_name: str):
        await self._execute(
            "DELETE FROM catalog_tags WHERE friendly_token = ? AND tag_id IN "
            "(SELECT id FROM tags WHERE name = ?)",
            [friendly_token, tag_name],
        )

    async def insert_catalog(self, row: dict):
        defaults = {
            "description": "",
            "duration_sec": 0,
            "thumbnail_url": "",
            "added_at": row.get("synced_at"),
        }
        row = {**defaults, **row}
        await self._execute(
            "INSERT INTO catalog (friendly_token, title, description, duration_sec, "
            "manifest_url, thumbnail_url, added_at, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                row["friendly_token"],
                row["title"],
                row["description"],
                row["duration_sec"],
                row["manifest_url"],
                row["thumbnail_url"],
                parse_dt(row.get("added_at")),
                parse_dt(row.get("synced_at")),
            ],
        )
        # search_vector is GENERATED ALWAYS AS (...) STORED — Postgres maintains
        # the full-text index automatically. No manual FTS maintenance needed.

    async def update_catalog(self, friendly_token: str, row: dict):
        """Update catalog item with partial or full field set.

        Only fields present in *row* are updated. Always updates ``updated_at``
        when any field changes.
        """
        if not row:
            return

        sets: list[str] = []
        values: list = []
        for key in row:
            if key not in _CATALOG_UPDATE_ALLOWED:
                continue
            value = row[key]
            if key in _CATALOG_TIMESTAMP_KEYS:
                value = parse_dt(value)
            if key == "added_at":
                sets.append("added_at = COALESCE(?, added_at)")
            else:
                sets.append(f"{key} = ?")
            values.append(value)

        if not sets:
            return

        updated_at = parse_dt(row.get("synced_at")) or datetime.now(timezone.utc)
        sets.append("updated_at = ?")
        values.append(updated_at)
        values.append(friendly_token)

        sql = f"UPDATE catalog SET {', '.join(sets)} WHERE friendly_token = ?"
        await self._execute(sql, values)
        # No FTS rebuild needed: search_vector regenerates automatically.

    async def update_cover_art(self, friendly_token: str, path: str, source: str):
        await self._execute(
            "UPDATE catalog SET cover_art_path=?, cover_art_source=? WHERE friendly_token=?",
            [path, source, friendly_token],
        )

    async def set_imdb_tt(self, friendly_token: str, imdb_tt: str | None) -> None:
        await self._execute(
            "UPDATE catalog SET imdb_tt = ?, updated_at = now() WHERE friendly_token = ?",
            [imdb_tt or None, friendly_token],
        )

    async def get_item_by_imdb_tt(self, imdb_tt: str) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM catalog WHERE imdb_tt = ?", [imdb_tt]
        )

    async def delete_stale_catalog_items(self, sync_started_at: str) -> int:
        """Delete catalog items that weren't updated during the sync.

        FK CASCADE removes related catalog_categories/catalog_tags rows
        automatically (no separate catalog_fts table exists in Postgres).
        """
        result = await self._execute(
            "DELETE FROM catalog WHERE synced_at < ? OR synced_at IS NULL",
            [parse_dt(sync_started_at)],
        )
        return result.rowcount or 0

    async def find_catalog_by_title(self, title: str) -> dict | None:
        """Best-effort lookup of a catalog item whose title matches ``title``."""
        norm_target = _normalize_title(title)
        if not norm_target:
            return None
        phrase = '"' + (title or "").replace('"', '""') + '"'
        try:
            rows = await self._fetch_all(
                "SELECT c.friendly_token, c.title FROM catalog c, "
                "websearch_to_tsquery('english', ?) q "
                "WHERE c.search_vector @@ q LIMIT 25",
                [phrase],
            )
        except asyncpg.PostgresError:
            rows = []
        for row in rows:
            if _normalize_title(row["title"]) == norm_target:
                return {"friendly_token": row["friendly_token"], "title": row["title"]}
        return None

    # --- Sync log ---

    async def start_sync_log(self) -> int:
        return await self._execute_returning_id(
            "INSERT INTO sync_log (started_at, status) VALUES (?, 'running') RETURNING id",
            [datetime.now(timezone.utc)],
        )

    async def finish_sync_log(self, log_id: int, stats: dict, status: str):
        await self._execute(
            "UPDATE sync_log SET ended_at=?, items_seen=?, items_new=?, items_updated=?, errors=?, status=? WHERE id=?",
            [
                datetime.now(timezone.utc),
                stats["seen"],
                stats["new"],
                stats["updated"],
                stats["errors"],
                status,
                log_id,
            ],
        )

    async def get_sync_logs(self, limit: int = 10) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", [limit]
        )

    # --- Item edit audit log ---

    async def log_item_edit(
        self,
        friendly_token: str,
        username: str,
        field_name: str,
        old_value: str | int | None,
        new_value: str | int | None,
    ) -> None:
        old_str = str(old_value) if old_value is not None else None
        new_str = str(new_value) if new_value is not None else None
        await self._execute(
            "INSERT INTO item_edit_log (friendly_token, username, field_name, old_value, new_value) "
            "VALUES (?, ?, ?, ?, ?)",
            [friendly_token, username, field_name, old_str, new_str],
        )

    async def get_item_edit_history(
        self, friendly_token: str, limit: int = 50
    ) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM item_edit_log WHERE friendly_token = ? "
            "ORDER BY edited_at DESC LIMIT ?",
            [friendly_token, limit],
        )

    # --- Cross-domain helpers (catalog_db.py extras) ---

    async def get_items_by_tokens(self, tokens: list[str]) -> list[dict]:
        """Fetch catalog rows for a list of friendly_tokens, chunked for parameter limits."""
        if not tokens:
            return []
        items: list[dict] = []
        for i in range(0, len(tokens), 500):
            chunk = tokens[i : i + 500]
            ph = ",".join("?" * len(chunk))
            sql = f"""
                SELECT friendly_token, title, duration_sec, cover_art_path,
                       cover_art_source, thumbnail_url, manifest_url
                FROM catalog
                WHERE friendly_token IN ({ph})
            """
            rows = await self._fetch_all(sql, chunk)
            items.extend(rows)
        return items

    async def get_hidden_category_and_tag_tokens(self) -> set[str]:
        cat_ph = ",".join("?" * len(HIDDEN_CATEGORY_NAMES))
        tag_ph = ",".join("?" * len(HIDDEN_TAG_NAMES))
        sql = f"""
            SELECT cc.friendly_token FROM catalog_categories cc
            JOIN categories cat ON cc.category_id = cat.id
            WHERE cat.name IN ({cat_ph})
            UNION
            SELECT ct.friendly_token FROM catalog_tags ct
            JOIN tags t ON ct.tag_id = t.id
            WHERE t.name IN ({tag_ph})
        """
        rows = await self._fetch_all(sql, [*HIDDEN_CATEGORY_NAMES, *HIDDEN_TAG_NAMES])
        return {r["friendly_token"] for r in rows if r.get("friendly_token")}

    async def resolve_friendly_tokens(
        self, identifiers: list[str] | set[str]
    ) -> set[str]:
        """Resolve a mix of bare friendly_tokens and manifest URLs to friendly_tokens."""
        ids = [i for i in identifiers if i]
        if not ids:
            return set()
        tokens: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            ph = ",".join("?" * len(chunk))
            rows = await self._fetch_all(
                f"SELECT friendly_token FROM catalog "
                f"WHERE friendly_token IN ({ph}) OR manifest_url IN ({ph})",
                [*chunk, *chunk],
            )
            tokens.update(r["friendly_token"] for r in rows if r.get("friendly_token"))
        return tokens

    # --- People / studios ---

    async def upsert_person(self, name: str) -> int:
        existing = await self._fetch_one("SELECT id FROM people WHERE name = ?", [name])
        if existing:
            return existing["id"]
        return await self._execute_returning_id(
            "INSERT INTO people (name) VALUES (?) RETURNING id", [name]
        )

    async def set_catalog_people(self, friendly_token: str, people: list[dict]) -> None:
        await self._execute(
            "DELETE FROM catalog_people WHERE friendly_token = ?", [friendly_token]
        )
        for entry in people:
            role = entry.get("role", "cast")
            if role not in VALID_ROLES:
                continue
            person_id = await self.upsert_person(entry["name"])
            await self._execute(
                "INSERT INTO catalog_people (friendly_token, person_id, role, position) "
                "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [friendly_token, person_id, role, entry.get("position", 0)],
            )

    async def get_item_people(self, friendly_token: str) -> dict[str, list[str]]:
        rows = await self._fetch_all(
            "SELECT p.name, cp.role FROM people p "
            "JOIN catalog_people cp ON cp.person_id = p.id "
            "WHERE cp.friendly_token = ? "
            "ORDER BY cp.role, cp.position, p.name",
            [friendly_token],
        )
        result: dict[str, list[str]] = {r: [] for r in VALID_ROLES}
        for row in rows:
            result[row["role"]].append(row["name"])
        return result

    async def upsert_studio(self, name: str) -> int:
        existing = await self._fetch_one(
            "SELECT id FROM studios WHERE name = ?", [name]
        )
        if existing:
            return existing["id"]
        return await self._execute_returning_id(
            "INSERT INTO studios (name) VALUES (?) RETURNING id", [name]
        )

    async def set_catalog_studios(
        self, friendly_token: str, studio_names: list[str]
    ) -> None:
        await self._execute(
            "DELETE FROM catalog_studios WHERE friendly_token = ?", [friendly_token]
        )
        for name in studio_names:
            studio_id = await self.upsert_studio(name)
            await self._execute(
                "INSERT INTO catalog_studios (friendly_token, studio_id) VALUES (?, ?) "
                "ON CONFLICT DO NOTHING",
                [friendly_token, studio_id],
            )

    async def get_item_studios(self, friendly_token: str) -> list[str]:
        rows = await self._fetch_all(
            "SELECT s.name FROM studios s "
            "JOIN catalog_studios cs ON cs.studio_id = s.id "
            "WHERE cs.friendly_token = ? ORDER BY s.name",
            [friendly_token],
        )
        return [r["name"] for r in rows]

    # --- Enrichment ---

    async def ensure_enrichment_state(self, token: str) -> None:
        await self._execute(
            "INSERT INTO item_enrichment_state (friendly_token) VALUES (?) "
            "ON CONFLICT DO NOTHING",
            [token],
        )

    async def get_enrichment_state(self, token: str) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM item_enrichment_state WHERE friendly_token = ?", [token]
        )

    async def save_enrichment_state(self, token: str, **fields) -> None:
        safe = {k: v for k, v in fields.items() if k in _ENRICHMENT_SAVE_ALLOWED}
        if not safe:
            return
        await self.ensure_enrichment_state(token)
        sets = ", ".join(f"{k} = ?" for k in safe)
        await self._execute(
            f"UPDATE item_enrichment_state SET {sets} WHERE friendly_token = ?",
            [*safe.values(), token],
        )

    async def update_enrichment_state(self, token: str, fields: dict) -> None:
        safe = {k: v for k, v in fields.items() if k in _ENRICHMENT_UPDATE_ALLOWED}
        if not safe:
            return
        await self.ensure_enrichment_state(token)
        sets = ", ".join(f"{k} = ?" for k in safe)
        await self._execute(
            f"UPDATE item_enrichment_state SET {sets} WHERE friendly_token = ?",
            [*safe.values(), token],
        )

    async def get_catalog_for_enrichment(
        self,
        *,
        step: str,
        tokens: list[str] | None = None,
        force: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        step_col = f"last_{step}_at"
        sql = """
            SELECT c.friendly_token, c.title, c.duration_sec,
                   c.cover_art_path, c.cover_art_source, c.thumbnail_url,
                   c.description, c.manifest_url, c.imdb_tt,
                   e.content_type, e.hosted_show, e.lookup_title, e.lookup_year,
                   e.tv_show, e.tv_season, e.tv_episode_num,
                   e.description_score, e.tmdb_id, e.imdb_id, e.meta_json,
                   e.last_classify_at, e.last_title_at, e.last_meta_at,
                   e.last_art_at, e.last_tags_at, e.last_categories_at,
                   e.last_identify_at, e.identify_source, e.identify_reason
            FROM catalog c
            LEFT JOIN item_enrichment_state e ON e.friendly_token = c.friendly_token
        """
        params: list = []
        conditions: list[str] = []

        if tokens:
            ph = ",".join("?" * len(tokens))
            conditions.append(f"c.friendly_token IN ({ph})")
            params.extend(tokens)

        if not force:
            conditions.append(f"(e.{step_col} IS NULL)")

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        sql += " ORDER BY c.title"

        if limit:
            sql += " LIMIT ?"
            params.append(limit)

        return await self._fetch_all(sql, params)

    async def get_identify_coverage(self) -> list[dict]:
        return await self._fetch_all(
            "SELECT c.friendly_token, c.title, c.imdb_tt, "
            "e.content_type, e.tmdb_id, e.identify_source, e.identify_reason, "
            "e.last_identify_at "
            "FROM catalog c "
            "LEFT JOIN item_enrichment_state e ON e.friendly_token = c.friendly_token "
            "ORDER BY c.title"
        )

    def parse_meta_json(self, row: dict) -> dict | None:
        """Deserialise the cached meta_json field from an enrichment row."""
        if not row or not row.get("meta_json"):
            return None
        try:
            return json.loads(row["meta_json"])
        except (json.JSONDecodeError, TypeError):
            return None

    # --- MOTD overrides ---

    async def upsert_motd_override(
        self,
        week_key: str,
        slot_key: str,
        *,
        title: str | None = None,
        poster_url: str | None = None,
        href: str | None = None,
        created_by: str | None = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO motd_overrides
                (week_key, slot_key, title, poster_url, href, created_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, now())
            ON CONFLICT(week_key, slot_key) DO UPDATE SET
                title      = COALESCE(excluded.title, motd_overrides.title),
                poster_url = COALESCE(excluded.poster_url, motd_overrides.poster_url),
                href       = COALESCE(excluded.href, motd_overrides.href),
                created_by = excluded.created_by,
                updated_at = now()
            """,
            [week_key, slot_key, title, poster_url, href, created_by],
        )

    async def list_motd_overrides(self, week_key: str) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM motd_overrides WHERE week_key = ? ORDER BY slot_key",
            [week_key],
        )

    async def delete_motd_override(self, week_key: str, slot_key: str) -> int:
        result = await self._execute(
            "DELETE FROM motd_overrides WHERE week_key = ? AND slot_key = ?",
            [week_key, slot_key],
        )
        return result.rowcount or 0

    async def clear_motd_overrides(self, week_key: str) -> int:
        result = await self._execute(
            "DELETE FROM motd_overrides WHERE week_key = ?", [week_key]
        )
        return result.rowcount or 0
