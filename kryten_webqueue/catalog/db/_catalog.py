import aiosqlite
import re
from datetime import datetime, UTC


# Categories and tags whose items are hidden from the public catalog (dropdowns
# and search results). Admins can opt to reveal them. Matched by exact name.
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

# Tag applied by the admin "Hide Item" action. Source of truth is MediaCMS;
# this is mirrored into the local catalog_tags join so the hidden-tag filter
# applies immediately (before the next sync confirms it).
HIDDEN_ITEM_TAG = "kryten-hidden"


def _sanitize_fts_query(q: str) -> str:
    """Strip FTS5 metacharacters from a user-supplied search string.

    FTS5 treats ``"``, ``*``, ``^``, ``(``, ``)``, ``:`` and bare AND/OR/NOT as
    syntax; an unexpected one raises a SQLite parse error (500).  We discard all
    non-word characters and collapse whitespace, so "C.H.U.D. (1984)" becomes
    "C H U D 1984" which matches correctly since the FTS tokenizer also strips
    punctuation when indexing.
    """
    tokens = re.sub(r"[^\w\s]", " ", q, flags=re.UNICODE).split()
    return " ".join(tokens)


def _duration_filter(alias: str, min_sec: int) -> tuple[str, list]:
    """SQL AND-fragment filtering out items shorter than ``min_sec`` seconds."""
    return f" AND {alias}.duration_sec >= ? ", [min_sec]


def _duration_range_filter(
    alias: str, min_sec: int | None = None, max_sec: int | None = None
) -> tuple[str, list]:
    """SQL AND-fragment filtering items by duration range."""
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
    """Derive a URL-safe slug from a category title."""
    s = (text or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-") or "untitled"


def _normalize_title(title: str) -> str:
    """Normalize a media title for equality comparison.

    Lowercases, drops parenthetical/bracketed chunks and any 4-digit year, and
    collapses remaining non-alphanumerics to single spaces. Lets a clean
    database title ("The Matrix") match a catalog title ("The Matrix (1999)").
    """
    s = (title or "").lower()
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", s)
    s = re.sub(r"\b(?:19|20)\d{2}\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _hidden_exclusion(alias: str = "c") -> tuple[str, list]:
    """SQL fragment (+ params) excluding items in hidden categories/tags.

    The fragment is prefixed with ``AND`` so it can be appended to an existing
    WHERE clause that references the catalog row under ``alias``.
    """
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


def _promo_hidden_media_subquery() -> tuple[str, list]:
    """Subquery (+ params) yielding media_ids that are promos/bumpers.

    "Promo/bumper" content is anything hidden from the public catalog by a promo
    pool OR a hidden category OR a hidden tag (e.g. ``Z Channel Promos`` /
    ``channelz``). These items never appear to regular users, so they must never
    be recorded as recently-played. Immutable *event* playlists are intentionally
    excluded here — they are their own class, not promos.
    """
    cat_ph = ",".join("?" * len(HIDDEN_CATEGORY_NAMES))
    tag_ph = ",".join("?" * len(HIDDEN_TAG_NAMES))
    sql = f"""
        SELECT spi.media_id FROM saved_playlist_items spi
        JOIN saved_playlists sp ON sp.id = spi.playlist_id
        WHERE sp.promo_type IS NOT NULL AND spi.media_type = 'cm'
        UNION
        SELECT cc.friendly_token FROM catalog_categories cc
        JOIN categories cat ON cc.category_id = cat.id
        WHERE cat.name IN ({cat_ph})
        UNION
        SELECT ct.friendly_token FROM catalog_tags ct
        JOIN tags t ON ct.tag_id = t.id
        WHERE t.name IN ({tag_ph})
    """
    return sql, [*HIDDEN_CATEGORY_NAMES, *HIDDEN_TAG_NAMES]


def _recently_played_exclusion(alias: str, days: int) -> tuple[str, list]:
    """SQL fragment (+ params) hiding recently-played items from regular users.

    Items with a ``play_completions`` row within the last ``days`` days are
    hidden. A completion is only recorded when an item played past a threshold
    (see ``CompletionRecorder``), so refunded/skipped items never hide.

    The fragment is prefixed with ``AND`` so it can be appended to an existing
    WHERE clause referencing the catalog row under ``alias``. Caller must ensure
    ``days > 0`` before applying (a non-positive window disables this).
    """
    sql = f"""
            AND {alias}.friendly_token NOT IN (
                SELECT pc.media_id FROM play_completions pc
                WHERE pc.media_type = 'cm'
                  AND pc.completed_at >= datetime('now', ?)
            )
    """
    return sql, [f"-{int(days)} days"]


def _facet_filter(
    alias: str,
    category: str | None,
    tag: str | None,
    person: str | None = None,
    studio: str | None = None,
) -> tuple[str, list]:
    """SQL fragment (+ params) AND-filtering by category, tag, person, and/or studio.

    Each filter is an ``AND friendly_token IN (...)`` subquery on the catalog row
    under ``alias``; an absent filter contributes nothing. Shared by browse() and
    search() so the two paths narrow results identically.
    """
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


# Default quality-weighted ordering (see browse() for rationale).
_DEFAULT_ORDER = """
    ORDER BY
        (c.cover_art_source IN ('tmdb', 'omdb')) DESC,
        (CASE WHEN c.title GLOB '[A-Za-z]*' THEN 0 ELSE 1 END) ASC,
        c.title ASC
"""

# Map a user-facing sort key to an ORDER BY clause referencing the catalog row
# under alias ``c``. Unknown keys fall back to the default quality ordering.
_SORT_CLAUSES = {
    "default": _DEFAULT_ORDER,
    "title_asc": " ORDER BY c.title ASC ",
    "title_desc": " ORDER BY c.title DESC ",
    "newest": " ORDER BY c.added_at DESC, c.synced_at DESC ",
    "oldest": " ORDER BY c.added_at ASC, c.synced_at ASC ",
}


def _browse_order_clause(sort: str | None) -> str:
    return _SORT_CLAUSES.get(sort or "default", _DEFAULT_ORDER)


class _CatalogMixin:
    """Catalog browse/search, sync log, job runs, and OTP methods."""

    # --- Catalog ---

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
    ) -> list[dict]:
        query = """
            SELECT c.friendly_token, c.title, c.duration_sec, c.cover_art_path, c.cover_art_source, c.thumbnail_url, c.manifest_url
            FROM catalog c
            WHERE c.friendly_token NOT IN (
                SELECT spi.media_id FROM saved_playlist_items spi
                JOIN saved_playlists sp ON spi.playlist_id = sp.id
                WHERE (sp.is_immutable = 1 OR sp.promo_type IS NOT NULL) AND spi.media_type = 'cm'
            )
        """
        params: list = []
        if not show_hidden:
            excl_sql, excl_params = _hidden_exclusion("c")
            query += excl_sql
            params.extend(excl_params)
        if recently_played_days > 0:
            rp_sql, rp_params = _recently_played_exclusion("c", recently_played_days)
            query += rp_sql
            params.extend(rp_params)
        if min_duration_sec > 0:
            dur_sql, dur_params = _duration_filter("c", min_duration_sec)
            query += dur_sql
            params.extend(dur_params)
        if category:
            query += """
                AND c.friendly_token IN (
                    SELECT cc.friendly_token FROM catalog_categories cc
                    JOIN categories cat ON cc.category_id = cat.id
                    WHERE cat.slug = ?
                )
            """
            params.append(category)
        if tag:
            query += """
                AND c.friendly_token IN (
                    SELECT ct.friendly_token FROM catalog_tags ct
                    JOIN tags t ON ct.tag_id = t.id
                    WHERE t.name = ?
                )
            """
            params.append(tag)
        if person:
            query += """
                AND c.friendly_token IN (
                    SELECT cp.friendly_token FROM catalog_people cp
                    JOIN people p ON cp.person_id = p.id
                    WHERE p.name = ?
                )
            """
            params.append(person)
        if studio:
            query += """
                AND c.friendly_token IN (
                    SELECT cs.friendly_token FROM catalog_studios cs
                    JOIN studios s ON cs.studio_id = s.id
                    WHERE s.name = ?
                )
            """
            params.append(studio)
        # Quality-weighted ordering so the landing page leads with presentable
        # items instead of alphabetical junk. No curation required — every signal
        # is derived from existing data:
        #   1. Items with REAL box art first. The strong signal is a poster match
        #      from TMDB/OMDB (cover_art_source), NOT mere presence of a
        #      cover_art_path/thumbnail_url — almost every item carries a MediaCMS
        #      thumbnail, and the resolver also caches that thumbnail as a
        #      last-resort cover (cover_art_source = 'thumbnail'). A genuine
        #      poster match also implies a well-formed, matchable title.
        #   2. Titles beginning with a letter before number/symbol-prefixed
        #      "02 - Episode" style entries.
        #   3. Finally alphabetical for a stable, predictable tail.
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
    ) -> int:
        query = """
            SELECT COUNT(*) as cnt FROM catalog c
            WHERE c.friendly_token NOT IN (
                SELECT spi.media_id FROM saved_playlist_items spi
                JOIN saved_playlists sp ON spi.playlist_id = sp.id
                WHERE (sp.is_immutable = 1 OR sp.promo_type IS NOT NULL) AND spi.media_type = 'cm'
            )
        """
        params: list = []
        if not show_hidden:
            excl_sql, excl_params = _hidden_exclusion("c")
            query += excl_sql
            params.extend(excl_params)
        if recently_played_days > 0:
            rp_sql, rp_params = _recently_played_exclusion("c", recently_played_days)
            query += rp_sql
            params.extend(rp_params)
        if min_duration_sec > 0:
            dur_sql, dur_params = _duration_filter("c", min_duration_sec)
            query += dur_sql
            params.extend(dur_params)
        if category:
            query += """
                AND c.friendly_token IN (
                    SELECT cc.friendly_token FROM catalog_categories cc
                    JOIN categories cat ON cc.category_id = cat.id
                    WHERE cat.slug = ?
                )
            """
            params.append(category)
        if tag:
            query += """
                AND c.friendly_token IN (
                    SELECT ct.friendly_token FROM catalog_tags ct
                    JOIN tags t ON ct.tag_id = t.id
                    WHERE t.name = ?
                )
            """
            params.append(tag)
        if person:
            query += """
                AND c.friendly_token IN (
                    SELECT cp.friendly_token FROM catalog_people cp
                    JOIN people p ON cp.person_id = p.id
                    WHERE p.name = ?
                )
            """
            params.append(person)
        if studio:
            query += """
                AND c.friendly_token IN (
                    SELECT cs.friendly_token FROM catalog_studios cs
                    JOIN studios s ON cs.studio_id = s.id
                    WHERE s.name = ?
                )
            """
            params.append(studio)
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
    ) -> list[dict]:
        sanitized = _sanitize_fts_query(query_text)
        if not sanitized:
            return []
        sql = """
            SELECT c.friendly_token, c.title, c.duration_sec, c.cover_art_path, c.cover_art_source, c.thumbnail_url, c.manifest_url,
                   rank AS relevance
            FROM catalog_fts fts
            JOIN catalog c ON c.rowid = fts.rowid
            WHERE catalog_fts MATCH ?
              AND c.friendly_token NOT IN (
                  SELECT spi.media_id FROM saved_playlist_items spi
                  JOIN saved_playlists sp ON spi.playlist_id = sp.id
                  WHERE (sp.is_immutable = 1 OR sp.promo_type IS NOT NULL) AND spi.media_type = 'cm'
              )
        """
        params: list = [sanitized]
        if not show_hidden:
            excl_sql, excl_params = _hidden_exclusion("c")
            sql += excl_sql
            params.extend(excl_params)
        if recently_played_days > 0:
            rp_sql, rp_params = _recently_played_exclusion("c", recently_played_days)
            sql += rp_sql
            params.extend(rp_params)
        if min_duration_sec > 0 or max_duration_sec is not None:
            dur_sql, dur_params = _duration_range_filter(
                "c", min_duration_sec or None, max_duration_sec
            )
            sql += dur_sql
            params.extend(dur_params)
        # Category/tag/person/studio facets AND with the text match.
        facet_sql, facet_params = _facet_filter("c", category, tag, person, studio)
        sql += facet_sql
        params.extend(facet_params)
        # Relevance is the natural default for a text query; other sort keys let
        # the user reorder the matched set explicitly.
        sql += (
            " ORDER BY rank "
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
    ) -> int:
        sanitized = _sanitize_fts_query(query_text)
        if not sanitized:
            return 0
        sql = """
            SELECT COUNT(*) as cnt
            FROM catalog_fts fts
            JOIN catalog c ON c.rowid = fts.rowid
            WHERE catalog_fts MATCH ?
              AND c.friendly_token NOT IN (
                  SELECT spi.media_id FROM saved_playlist_items spi
                  JOIN saved_playlists sp ON spi.playlist_id = sp.id
                  WHERE (sp.is_immutable = 1 OR sp.promo_type IS NOT NULL) AND spi.media_type = 'cm'
              )
        """
        params: list = [sanitized]
        if not show_hidden:
            excl_sql, excl_params = _hidden_exclusion("c")
            sql += excl_sql
            params.extend(excl_params)
        if recently_played_days > 0:
            rp_sql, rp_params = _recently_played_exclusion("c", recently_played_days)
            sql += rp_sql
            params.extend(rp_params)
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

    async def get_item(self, friendly_token: str) -> dict | None:
        sql = """
            SELECT * FROM catalog
            WHERE friendly_token = ?
              AND friendly_token NOT IN (
                  SELECT spi.media_id FROM saved_playlist_items spi
                  JOIN saved_playlists sp ON spi.playlist_id = sp.id
                  WHERE (sp.is_immutable = 1 OR sp.promo_type IS NOT NULL) AND spi.media_type = 'cm'
              )
        """
        return await self._fetch_one(sql, [friendly_token])

    async def get_item_admin(self, friendly_token: str) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM catalog WHERE friendly_token = ?", [friendly_token]
        )

    async def resolve_media(self, media_id: str) -> dict | None:
        """Resolve a now-playing identifier to its catalog row (token + duration).

        The live now-playing payload carries the manifest URL as ``id`` for 'cm'
        items (not the friendly_token), so match on either column.
        """
        if not media_id:
            return None
        return await self._fetch_one(
            "SELECT friendly_token, duration_sec FROM catalog "
            "WHERE friendly_token = ? OR manifest_url = ? LIMIT 1",
            [media_id, media_id],
        )

    async def record_play_completion(
        self,
        *,
        friendly_token: str,
        duration_sec: int | None = None,
        media_type: str = "cm",
    ) -> None:
        """Record a genuine play-completion for catalog-hide purposes.

        Promo-pool clips and hidden-category/tag items are exempt. Everything
        else gets a time-boxed ``play_completions`` row that hides the item from
        regular users until the configured window expires.
        """
        sub, sub_params = _promo_hidden_media_subquery()
        promo = await self._fetch_one(
            f"SELECT 1 WHERE ? IN ({sub})",
            [friendly_token, *sub_params],
        )
        if promo:
            return
        await self._execute(
            "INSERT INTO play_completions (media_type, media_id) VALUES (?, ?)",
            [media_type, friendly_token],
        )

    async def clear_play_state(
        self, friendly_token: str, *, media_type: str = "cm"
    ) -> dict:
        """Remove recently-played hide state for an item (admin helper).

        Deletes ``play_completions`` rows so the item reappears in the public
        catalog immediately. Returns the count removed.
        """
        pc = await self._fetch_one(
            "SELECT COUNT(*) AS c FROM play_completions WHERE media_id = ? AND media_type = ?",
            [friendly_token, media_type],
        )
        await self._execute(
            "DELETE FROM play_completions WHERE media_id = ? AND media_type = ?",
            [friendly_token, media_type],
        )
        return {"completions": pc["c"] if pc else 0}

    async def delete_catalog_item(self, friendly_token: str) -> bool:
        """Permanently delete a catalog item and all facet associations.

        Removes the item from the catalog plus its FTS row and its tag, category,
        people, and studio associations. Destructive, with no recovery path.
        Returns True if the item was deleted, False if it didn't exist.
        """
        # Check if item exists
        item = await self._fetch_one(
            "SELECT friendly_token FROM catalog WHERE friendly_token = ?",
            [friendly_token],
        )
        if not item:
            return False

        # FTS and the people/studio join tables are not FK-cascaded from catalog,
        # so remove them explicitly.
        await self._execute(
            "DELETE FROM catalog_fts WHERE friendly_token = ?", [friendly_token]
        )
        await self._execute(
            "DELETE FROM catalog_people WHERE friendly_token = ?", [friendly_token]
        )
        await self._execute(
            "DELETE FROM catalog_studios WHERE friendly_token = ?", [friendly_token]
        )

        # Deleting the catalog row cascades catalog_tags / catalog_categories
        # (ON DELETE CASCADE on friendly_token).
        await self._execute(
            "DELETE FROM catalog WHERE friendly_token = ?", [friendly_token]
        )

        return True

    async def purge_promo_hide_state(self) -> dict:
        """Remove any recently-played hide state recorded for promos/bumpers.

        Promos must never be subject to recently-played hiding. New completions
        are already skipped in ``record_play_completion``; this purges rows
        written before that exemption existed (run once at startup). Covers promo
        pools plus hidden-category/tag promos (the usual station-promo case).
        Returns the row count removed.
        """
        sub, sub_params = _promo_hidden_media_subquery()
        pc = await self._fetch_one(
            f"SELECT COUNT(*) AS c FROM play_completions WHERE media_id IN ({sub})",
            sub_params,
        )
        await self._execute(
            f"DELETE FROM play_completions WHERE media_id IN ({sub})", sub_params
        )
        return {"completions": pc["c"] if pc else 0}

    async def get_recently_played_debug(self, days: int) -> dict:
        """Snapshot of what the recently-played rules currently hide (admin test)."""
        by_completion = await self._fetch_all(
            "SELECT pc.media_id, c.title, MAX(pc.completed_at) AS last_completed "
            "FROM play_completions pc "
            "LEFT JOIN catalog c ON c.friendly_token = pc.media_id "
            "WHERE pc.media_type = 'cm' AND pc.completed_at >= datetime('now', ?) "
            "GROUP BY pc.media_id ORDER BY last_completed DESC",
            [f"-{int(days)} days"],
        )
        return {
            "window_days": days,
            "by_completion": by_completion,
        }

    async def get_catalog_brief(
        self, tokens: list[str], manifest_urls: list[str]
    ) -> dict[str, dict]:
        """Return a lookup of catalog metadata keyed by BOTH friendly_token and
        manifest_url, for enriching queue-shadow items that may only carry one.
        """
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
        """Return description + category/tag/people/studio names for a catalog item."""
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

    async def is_restricted(self, friendly_token: str) -> bool:
        sql = """
            SELECT 1 FROM saved_playlist_items spi
            JOIN saved_playlists sp ON spi.playlist_id = sp.id
            WHERE (sp.is_immutable = 1 OR sp.promo_type IS NOT NULL)
              AND spi.media_type = 'cm'
              AND spi.media_id = ?
            LIMIT 1
        """
        row = await self._fetch_one(sql, [friendly_token])
        return row is not None

    async def get_categories(self, *, show_hidden: bool = False) -> list[dict]:
        """Distinct categories that have at least one catalog item, for facets."""
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
        sql += """
            GROUP BY c.id, c.name, c.slug
            ORDER BY c.name
        """
        return await self._fetch_all(sql, params)

    async def get_tags(
        self,
        *,
        limit: int = 100,
        show_hidden: bool = False,
        min_duration_sec: int = 0,
        max_duration_sec: int | None = None,
    ) -> list[dict]:
        """Most-used tags that have at least one catalog item, for facets.

        Only returns tags with > 2 items matching the duration filter.
        """
        sql = """
            SELECT t.id, t.name, COUNT(ct.friendly_token) AS cnt
            FROM tags t
            JOIN catalog_tags ct ON ct.tag_id = t.id
            JOIN catalog c ON ct.friendly_token = c.friendly_token
            WHERE c.friendly_token NOT IN (
                SELECT spi.media_id FROM saved_playlist_items spi
                JOIN saved_playlists sp ON spi.playlist_id = sp.id
                WHERE (sp.is_immutable = 1 OR sp.promo_type IS NOT NULL) AND spi.media_type = 'cm'
            )
        """
        params: list = []
        if not show_hidden:
            ph = ",".join("?" * len(HIDDEN_TAG_NAMES))
            sql += f" AND t.name NOT IN ({ph})"
            params.extend(HIDDEN_TAG_NAMES)
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
        """Insert a category by name (deriving a unique slug) and return its id."""
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
        cursor = await self._db.execute(
            "INSERT INTO categories (name, slug) VALUES (?, ?)", [name, slug]
        )
        await self._db.commit()
        return cursor.lastrowid

    async def upsert_tag(self, name: str) -> int:
        """Insert a tag by name and return its id."""
        existing = await self._fetch_one("SELECT id FROM tags WHERE name = ?", [name])
        if existing:
            return existing["id"]
        cursor = await self._db.execute("INSERT INTO tags (name) VALUES (?)", [name])
        await self._db.commit()
        return cursor.lastrowid

    async def set_catalog_categories(
        self, friendly_token: str, category_ids: list[int]
    ):
        """Replace the category memberships for a catalog item."""
        await self._db.execute(
            "DELETE FROM catalog_categories WHERE friendly_token = ?", [friendly_token]
        )
        for cid in category_ids:
            await self._db.execute(
                "INSERT OR IGNORE INTO catalog_categories (friendly_token, category_id) VALUES (?, ?)",
                [friendly_token, cid],
            )
        await self._db.commit()

    async def set_catalog_tags(self, friendly_token: str, tag_ids: list[int]):
        """Replace the tag memberships for a catalog item."""
        await self._db.execute(
            "DELETE FROM catalog_tags WHERE friendly_token = ?", [friendly_token]
        )
        for tid in tag_ids:
            await self._db.execute(
                "INSERT OR IGNORE INTO catalog_tags (friendly_token, tag_id) VALUES (?, ?)",
                [friendly_token, tid],
            )
        await self._db.commit()

    async def add_catalog_tag(self, friendly_token: str, tag_name: str):
        """Add a single tag to a catalog item (idempotent), creating it if new."""
        tag_id = await self.upsert_tag(tag_name)
        await self._db.execute(
            "INSERT OR IGNORE INTO catalog_tags (friendly_token, tag_id) VALUES (?, ?)",
            [friendly_token, tag_id],
        )
        await self._db.commit()

    async def remove_catalog_tag(self, friendly_token: str, tag_name: str):
        """Remove a single tag from a catalog item (no-op if absent)."""
        await self._db.execute(
            "DELETE FROM catalog_tags WHERE friendly_token = ? AND tag_id IN "
            "(SELECT id FROM tags WHERE name = ?)",
            [friendly_token, tag_name],
        )
        await self._db.commit()

    async def insert_catalog(self, row: dict):
        sql = """
            INSERT INTO catalog (friendly_token, title, description, duration_sec,
                                 manifest_url, thumbnail_url, added_at, synced_at)
            VALUES (:friendly_token, :title, :description, :duration_sec,
                    :manifest_url, :thumbnail_url, :added_at, :synced_at)
        """
        row = {"added_at": row.get("synced_at"), **row}
        await self._db.execute(sql, row)
        # Update FTS index
        await self._db.execute(
            "INSERT INTO catalog_fts(rowid, friendly_token, title, description) "
            "SELECT rowid, friendly_token, title, description FROM catalog WHERE friendly_token = ?",
            [row["friendly_token"]],
        )
        await self._db.commit()

    async def update_catalog(self, friendly_token: str, row: dict):
        """Update catalog item with partial or full field set.

        Only fields present in *row* are updated. Always adds friendly_token
        and updates updated_at to current time when any field changes.
        """
        if not row:
            return

        # Build SET clause from provided fields
        allowed = {
            "title",
            "description",
            "duration_sec",
            "manifest_url",
            "thumbnail_url",
            "added_at",
            "synced_at",
        }
        updates = []
        params = {"friendly_token": friendly_token}

        for key in row:
            if key in allowed:
                # added_at uses COALESCE to preserve existing if None
                if key == "added_at":
                    updates.append("added_at=COALESCE(:added_at, added_at)")
                else:
                    updates.append(f"{key}=:{key}")
                params[key] = row[key]

        # Always update updated_at when modifying
        updates.append("updated_at=:updated_at")
        params["updated_at"] = row.get("synced_at") or params.get("synced_at")
        if not params["updated_at"]:
            from datetime import datetime, UTC

            params["updated_at"] = datetime.now(UTC).isoformat()

        if not updates:
            return

        sql = f"UPDATE catalog SET {', '.join(updates)} WHERE friendly_token=:friendly_token"
        await self._db.execute(sql, params)

        # Rebuild FTS for this row if title or description changed
        if "title" in row or "description" in row:
            await self._db.execute(
                "DELETE FROM catalog_fts WHERE friendly_token = ?", [friendly_token]
            )
            await self._db.execute(
                "INSERT INTO catalog_fts(rowid, friendly_token, title, description) "
                "SELECT rowid, friendly_token, title, description FROM catalog WHERE friendly_token = ?",
                [friendly_token],
            )
        await self._db.commit()

    async def update_cover_art(self, friendly_token: str, path: str, source: str):
        await self._execute(
            "UPDATE catalog SET cover_art_path=?, cover_art_source=? WHERE friendly_token=?",
            [path, source, friendly_token],
        )

    async def delete_stale_catalog_items(self, sync_started_at: str) -> int:
        """Delete catalog items that weren't updated during the sync.

        Items with synced_at older than sync_started_at are no longer present
        in MediaCMS and should be removed. Returns count of deleted items.

        Foreign key CASCADE will automatically remove related entries from:
        - catalog_fts
        - catalog_categories
        - catalog_tags
        - catalog_people
        - catalog_studios
        """
        cursor = await self._db.execute(
            "SELECT friendly_token FROM catalog WHERE synced_at < ? OR synced_at IS NULL",
            [sync_started_at],
        )
        tokens = [row[0] for row in await cursor.fetchall()]

        if not tokens:
            return 0

        # Delete from FTS first (not automatically cascaded)
        placeholders = ",".join("?" * len(tokens))
        await self._db.execute(
            f"DELETE FROM catalog_fts WHERE friendly_token IN ({placeholders})",
            tokens,
        )

        # Delete from catalog (cascades to join tables via ON DELETE CASCADE)
        await self._db.execute(
            f"DELETE FROM catalog WHERE friendly_token IN ({placeholders})",
            tokens,
        )

        await self._db.commit()
        return len(tokens)

    async def find_catalog_by_title(self, title: str) -> dict | None:
        """Best-effort lookup of a catalog item whose title matches ``title``.

        Used to tell a user we already have a suggested movie. Runs an FTS
        phrase match to gather candidates, then confirms with a normalized
        (year/punctuation-stripped, case-insensitive) equality check so loose
        token overlaps don't produce false "already have" hits. Returns
        ``{friendly_token, title}`` of the best match, or None.
        """
        norm_target = _normalize_title(title)
        if not norm_target:
            return None
        phrase = '"' + (title or "").replace('"', '""') + '"'
        try:
            rows = await self._fetch_all(
                "SELECT c.friendly_token, c.title FROM catalog_fts fts "
                "JOIN catalog c ON c.rowid = fts.rowid "
                "WHERE catalog_fts MATCH ? LIMIT 25",
                [phrase],
            )
        except aiosqlite.Error:
            rows = []
        for row in rows:
            if _normalize_title(row["title"]) == norm_target:
                return {"friendly_token": row["friendly_token"], "title": row["title"]}
        return None

    # --- Sync log ---

    async def start_sync_log(self) -> int:
        cursor = await self._db.execute(
            "INSERT INTO sync_log (started_at, status) VALUES (?, 'running')",
            [datetime.now(UTC).isoformat()],
        )
        await self._db.commit()
        return cursor.lastrowid

    async def finish_sync_log(self, log_id: int, stats: dict, status: str):
        await self._execute(
            "UPDATE sync_log SET ended_at=?, items_seen=?, items_new=?, items_updated=?, errors=?, status=? WHERE id=?",
            [
                datetime.now(UTC).isoformat(),
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

    # --- Generic job runs ---

    async def start_job_run(
        self, job_name: str, triggered_by: str | None = None, params: str | None = None
    ) -> int:
        cursor = await self._db.execute(
            "INSERT INTO job_runs (job_name, started_at, status, triggered_by, params) "
            "VALUES (?, ?, 'running', ?, ?)",
            [job_name, datetime.now(UTC).isoformat(), triggered_by, params],
        )
        await self._db.commit()
        return cursor.lastrowid

    async def finish_job_run(self, run_id: int, status: str, detail: str | None = None):
        await self._execute(
            "UPDATE job_runs SET ended_at=?, status=?, detail=? WHERE id=?",
            [datetime.now(UTC).isoformat(), status, detail, run_id],
        )

    async def update_job_run_detail(self, run_id: int, detail: str | None):
        """Update only a running job's detail column (used for live progress)."""
        await self._execute("UPDATE job_runs SET detail=? WHERE id=?", [detail, run_id])

    async def get_job_runs(
        self, job_name: str | None = None, limit: int = 10
    ) -> list[dict]:
        if job_name:
            return await self._fetch_all(
                "SELECT * FROM job_runs WHERE job_name=? ORDER BY id DESC LIMIT ?",
                [job_name, limit],
            )
        return await self._fetch_all(
            "SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", [limit]
        )

    async def reconcile_orphaned_job_runs(self) -> int:
        """Mark any job run still flagged ``running`` as ``interrupted``.

        The ``running`` flag lives only in memory on the JobManager, so a
        service restart (or a killed worker) mid-run leaves the row stuck at
        ``running`` forever. Called once on startup to clean up such orphans.
        Returns the number of rows reconciled.
        """
        cursor = await self._db.execute(
            "UPDATE job_runs SET status='interrupted', "
            "ended_at = COALESCE(ended_at, ?) WHERE status='running'",
            [datetime.now(UTC).isoformat()],
        )
        await self._db.commit()
        return cursor.rowcount or 0

    # --- Job schedules (cron-based, persisted) ---

    async def get_job_schedules(self) -> list[dict]:
        return await self._fetch_all("SELECT * FROM job_schedules ORDER BY job_name")

    async def get_job_schedule(self, job_name: str) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM job_schedules WHERE job_name=?", [job_name]
        )

    async def upsert_job_schedule(
        self,
        job_name: str,
        cron_expression: str,
        *,
        label: str | None = None,
        params_json: str | None = None,
        is_active: bool = True,
        created_by: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO job_schedules
                (job_name, label, cron_expression, params_json, is_active, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_name) DO UPDATE SET
                label            = excluded.label,
                cron_expression  = excluded.cron_expression,
                params_json      = excluded.params_json,
                is_active        = excluded.is_active,
                updated_at       = datetime('now')
            """,
            [job_name, label, cron_expression, params_json, int(is_active), created_by],
        )

    async def delete_job_schedule(self, job_name: str) -> None:
        await self._execute("DELETE FROM job_schedules WHERE job_name=?", [job_name])

    # --- OTP ---

    async def store_otp(self, username: str, code: str, expires_at: str):
        await self._execute(
            "INSERT INTO otps (username, code, expires_at) VALUES (?, ?, ?)",
            [username, code, expires_at],
        )

    async def verify_otp(self, username: str, code: str) -> bool:
        row = await self._fetch_one(
            "SELECT rowid FROM otps WHERE username=? AND code=? AND used=0 AND expires_at > datetime('now')",
            [username, code],
        )
        if row:
            await self._execute("UPDATE otps SET used=1 WHERE rowid=?", [row["rowid"]])
            return True
        return False

    # --- Item edit audit log -----------------------------------------------

    async def log_item_edit(
        self,
        friendly_token: str,
        username: str,
        field_name: str,
        old_value: str | int | None,
        new_value: str | int | None,
    ) -> None:
        """Log a manual admin edit to the audit trail."""
        # Convert to strings for storage
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
        """Retrieve edit history for an item, most recent first."""
        return await self._fetch_all(
            "SELECT * FROM item_edit_log WHERE friendly_token = ? "
            "ORDER BY edited_at DESC LIMIT ?",
            [friendly_token, limit],
        )

    async def cleanup_expired_otps(self):
        await self._execute(
            "DELETE FROM otps WHERE expires_at < datetime('now') OR used=1"
        )
