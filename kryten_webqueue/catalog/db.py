import aiosqlite
import re
from pathlib import Path
from datetime import datetime, UTC


def _slugify(text: str) -> str:
    """Derive a URL-safe slug from a category title."""
    s = (text or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-") or "untitled"


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
    "grindhousetrailer",
    "publicaccess",
    "religioustv",
    "kryten-hidden",
]

# Tag applied by the admin "Hide Item" action. Source of truth is MediaCMS;
# this is mirrored into the local catalog_tags join so the hidden-tag filter
# applies immediately (before the next sync confirms it).
HIDDEN_ITEM_TAG = "kryten-hidden"


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


MIGRATIONS = [
    # v1: Migration tracking table
    """
    CREATE TABLE IF NOT EXISTS _migrations (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # v2: Core schema
    """
    CREATE TABLE IF NOT EXISTS catalog (
        friendly_token   TEXT PRIMARY KEY,
        title            TEXT NOT NULL,
        description      TEXT,
        duration_sec     INTEGER,
        manifest_url     TEXT NOT NULL,
        thumbnail_url    TEXT,
        cover_art_path   TEXT,
        cover_art_source TEXT,
        added_at         TIMESTAMP,
        updated_at       TIMESTAMP,
        synced_at        TIMESTAMP
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts USING fts5(
        friendly_token UNINDEXED,
        title,
        description,
        content='catalog',
        content_rowid='rowid'
    );

    CREATE TABLE IF NOT EXISTS categories (
        id    INTEGER PRIMARY KEY,
        name  TEXT NOT NULL UNIQUE,
        slug  TEXT NOT NULL UNIQUE
    );

    CREATE TABLE IF NOT EXISTS catalog_categories (
        friendly_token TEXT REFERENCES catalog(friendly_token) ON DELETE CASCADE,
        category_id    INTEGER REFERENCES categories(id) ON DELETE CASCADE,
        PRIMARY KEY (friendly_token, category_id)
    );

    CREATE TABLE IF NOT EXISTS tags (
        id   INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE
    );

    CREATE TABLE IF NOT EXISTS catalog_tags (
        friendly_token TEXT REFERENCES catalog(friendly_token) ON DELETE CASCADE,
        tag_id         INTEGER REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (friendly_token, tag_id)
    );

    CREATE TABLE IF NOT EXISTS sync_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at    TIMESTAMP NOT NULL,
        ended_at      TIMESTAMP,
        items_seen    INTEGER,
        items_new     INTEGER,
        items_updated INTEGER,
        errors        INTEGER,
        status        TEXT
    );

    CREATE TABLE IF NOT EXISTS otps (
        username    TEXT NOT NULL,
        code        TEXT NOT NULL,
        created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at  TIMESTAMP NOT NULL,
        used        BOOLEAN NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_otps_username ON otps(username);

    CREATE TABLE IF NOT EXISTS queue_shadow (
        uid              INTEGER PRIMARY KEY,
        position         INTEGER NOT NULL,
        title            TEXT,
        friendly_token   TEXT,
        media_type       TEXT NOT NULL,
        media_id         TEXT NOT NULL,
        duration_sec     INTEGER,
        is_pay           BOOLEAN NOT NULL DEFAULT 0,
        paid_by          TEXT,
        tier             TEXT,
        z_cost           INTEGER,
        schedule_id      INTEGER,
        estimated_start_at TIMESTAMP,
        added_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS saved_playlists (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,
        description  TEXT,
        is_immutable BOOLEAN NOT NULL DEFAULT 0,
        created_by   TEXT NOT NULL,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS saved_playlist_items (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        playlist_id  INTEGER NOT NULL REFERENCES saved_playlists(id) ON DELETE CASCADE,
        position     INTEGER NOT NULL,
        media_type   TEXT NOT NULL,
        media_id     TEXT NOT NULL,
        title        TEXT,
        duration_sec INTEGER,
        UNIQUE(playlist_id, position)
    );

    CREATE TABLE IF NOT EXISTS playlist_schedules (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        playlist_id             INTEGER REFERENCES saved_playlists(id) ON DELETE SET NULL,
        label                   TEXT NOT NULL,
        fire_at                 TIMESTAMP NOT NULL,
        is_recurring            BOOLEAN DEFAULT 0,
        rrule                   TEXT,
        immutability_expires_at TIMESTAMP,
        pre_fire_lock_minutes   INTEGER DEFAULT 15,
        fired_at                TIMESTAMP,
        is_active               BOOLEAN DEFAULT 1,
        created_by              TEXT NOT NULL,
        created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS active_schedule (
        id               INTEGER PRIMARY KEY DEFAULT 1,
        schedule_id      INTEGER REFERENCES playlist_schedules(id),
        playlist_id      INTEGER REFERENCES saved_playlists(id),
        is_immutable     BOOLEAN NOT NULL DEFAULT 0,
        started_at       TIMESTAMP,
        estimated_end_at TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS spend_requests (
        request_id     TEXT PRIMARY KEY,
        username       TEXT NOT NULL,
        uid            INTEGER,
        friendly_token TEXT,
        tier           TEXT,
        z_cost         INTEGER,
        created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        refunded       BOOLEAN DEFAULT 0,
        refunded_at    TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS queue_history (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        username       TEXT NOT NULL,
        friendly_token TEXT,
        title          TEXT,
        tier           TEXT,
        z_cost         INTEGER,
        queued_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status         TEXT DEFAULT 'queued'
    );
    CREATE INDEX IF NOT EXISTS idx_queue_history_user ON queue_history(username);
    """,
    # v3: Clear all cached cover art to force a full repoll (fixes TMDB poster/person
    # preference and /static/images -> /images path correction)
    """
    UPDATE catalog SET cover_art_path = NULL, cover_art_source = NULL;
    """,
    # v4: Generic background job run history
    """
    CREATE TABLE IF NOT EXISTS job_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job_name    TEXT NOT NULL,
        started_at  TIMESTAMP NOT NULL,
        ended_at    TIMESTAMP,
        status      TEXT NOT NULL DEFAULT 'running',
        detail      TEXT,
        triggered_by TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_job_runs_name ON job_runs(job_name, started_at);
    """,
    # v5: Record the parameters a job run was started with.
    """
    ALTER TABLE job_runs ADD COLUMN params TEXT;
    """,
    # v6: Stopgap backfill of catalog.added_at (previously never populated on
    # insert). Lets "Newest first" browse ordering work before the next full
    # sync overwrites these with the true MediaCMS add_date.
    """
    UPDATE catalog SET added_at = synced_at WHERE added_at IS NULL;
    """,
    # v7: Per-schedule pre-fire lock override. Lets an admin lift a currently
    # active pre-fire lock without deleting/unarming the (recurring) schedule.
    # Reset to 0 whenever a recurring schedule re-arms its next occurrence.
    """
    ALTER TABLE playlist_schedules ADD COLUMN lock_disabled INTEGER NOT NULL DEFAULT 0;
    """,
    # v8: Track the firing's last scheduled item + an in-progress lock override
    # so the scheduled-event lock can auto-lift once the last item begins
    # playing, and admins can disable it mid-event.
    """
    ALTER TABLE active_schedule ADD COLUMN last_item_uid INTEGER;
    ALTER TABLE active_schedule ADD COLUMN lock_disabled INTEGER NOT NULL DEFAULT 0;
    """,
]


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")

    async def close(self):
        if self._db:
            await self._db.close()

    async def run_migrations(self):
        """Apply pending migrations sequentially."""
        await self._executescript(MIGRATIONS[0])
        row = await self._fetch_one("SELECT MAX(version) as v FROM _migrations")
        current_version = (row["v"] or 0) if row else 0

        for version, sql in enumerate(MIGRATIONS[1:], start=1):
            if version > current_version:
                await self._executescript(sql)
                await self._execute("INSERT INTO _migrations (version) VALUES (?)", [version])

    # --- Low-level helpers ---

    async def _execute(self, sql: str, params: list | None = None):
        await self._db.execute(sql, params or [])
        await self._db.commit()

    async def _executescript(self, sql: str):
        await self._db.executescript(sql)
        await self._db.commit()

    async def _fetch_one(self, sql: str, params: list | None = None) -> dict | None:
        cursor = await self._db.execute(sql, params or [])
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def _fetch_all(self, sql: str, params: list | None = None) -> list[dict]:
        cursor = await self._db.execute(sql, params or [])
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- Catalog ---

    async def browse(self, *, category: str | None = None, tag: str | None = None, page: int = 1, per_page: int = 24, show_hidden: bool = False, sort: str = "default") -> list[dict]:
        query = """
            SELECT c.friendly_token, c.title, c.duration_sec, c.cover_art_path, c.cover_art_source, c.thumbnail_url, c.manifest_url
            FROM catalog c
            WHERE c.friendly_token NOT IN (
                SELECT spi.media_id FROM saved_playlist_items spi
                JOIN saved_playlists sp ON spi.playlist_id = sp.id
                WHERE sp.is_immutable = 1 AND spi.media_type = 'cm'
            )
        """
        params: list = []
        if not show_hidden:
            excl_sql, excl_params = _hidden_exclusion("c")
            query += excl_sql
            params.extend(excl_params)
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

    async def browse_count(self, *, category: str | None = None, tag: str | None = None, show_hidden: bool = False) -> int:
        query = """
            SELECT COUNT(*) as cnt FROM catalog c
            WHERE c.friendly_token NOT IN (
                SELECT spi.media_id FROM saved_playlist_items spi
                JOIN saved_playlists sp ON spi.playlist_id = sp.id
                WHERE sp.is_immutable = 1 AND spi.media_type = 'cm'
            )
        """
        params: list = []
        if not show_hidden:
            excl_sql, excl_params = _hidden_exclusion("c")
            query += excl_sql
            params.extend(excl_params)
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
        row = await self._fetch_one(query, params)
        return row["cnt"] if row else 0

    async def search(self, query_text: str, *, page: int = 1, per_page: int = 24, show_hidden: bool = False, sort: str = "default") -> list[dict]:
        sql = """
            SELECT c.friendly_token, c.title, c.duration_sec, c.cover_art_path, c.cover_art_source, c.thumbnail_url, c.manifest_url,
                   rank AS relevance
            FROM catalog_fts fts
            JOIN catalog c ON c.rowid = fts.rowid
            WHERE catalog_fts MATCH ?
              AND c.friendly_token NOT IN (
                  SELECT spi.media_id FROM saved_playlist_items spi
                  JOIN saved_playlists sp ON spi.playlist_id = sp.id
                  WHERE sp.is_immutable = 1 AND spi.media_type = 'cm'
              )
        """
        params: list = [query_text]
        if not show_hidden:
            excl_sql, excl_params = _hidden_exclusion("c")
            sql += excl_sql
            params.extend(excl_params)
        # Relevance is the natural default for a text query; other sort keys let
        # the user reorder the matched set explicitly.
        sql += " ORDER BY rank " if (sort or "default") == "default" else _browse_order_clause(sort)
        sql += " LIMIT ? OFFSET ? "
        params.extend([per_page, (page - 1) * per_page])
        return await self._fetch_all(sql, params)

    async def search_count(self, query_text: str, *, show_hidden: bool = False) -> int:
        sql = """
            SELECT COUNT(*) as cnt
            FROM catalog_fts fts
            JOIN catalog c ON c.rowid = fts.rowid
            WHERE catalog_fts MATCH ?
              AND c.friendly_token NOT IN (
                  SELECT spi.media_id FROM saved_playlist_items spi
                  JOIN saved_playlists sp ON spi.playlist_id = sp.id
                  WHERE sp.is_immutable = 1 AND spi.media_type = 'cm'
              )
        """
        params: list = [query_text]
        if not show_hidden:
            excl_sql, excl_params = _hidden_exclusion("c")
            sql += excl_sql
            params.extend(excl_params)
        row = await self._fetch_one(sql, params)
        return row["cnt"] if row else 0

    async def get_item(self, friendly_token: str) -> dict | None:
        sql = """
            SELECT * FROM catalog
            WHERE friendly_token = ?
              AND friendly_token NOT IN (
                  SELECT spi.media_id FROM saved_playlist_items spi
                  JOIN saved_playlists sp ON spi.playlist_id = sp.id
                  WHERE sp.is_immutable = 1 AND spi.media_type = 'cm'
              )
        """
        return await self._fetch_one(sql, [friendly_token])

    async def get_item_admin(self, friendly_token: str) -> dict | None:
        return await self._fetch_one("SELECT * FROM catalog WHERE friendly_token = ?", [friendly_token])

    async def get_catalog_brief(self, tokens: list[str], manifest_urls: list[str]) -> dict[str, dict]:
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
        """Return description + category/tag names for a single catalog item.

        Used to enrich the now-playing card. Returns empty values when the
        token is unknown.
        """
        if not friendly_token:
            return {"description": None, "categories": [], "tags": []}
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
        return {
            "description": (row or {}).get("description"),
            "categories": [{"name": c["name"], "slug": c["slug"]} for c in cats],
            "tags": [t["name"] for t in tags],
        }

    async def is_restricted(self, friendly_token: str) -> bool:
        sql = """
            SELECT 1 FROM saved_playlist_items spi
            JOIN saved_playlists sp ON spi.playlist_id = sp.id
            WHERE sp.is_immutable = 1
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

    async def get_tags(self, *, limit: int = 100, show_hidden: bool = False) -> list[dict]:
        """Most-used tags that have at least one catalog item, for facets."""
        sql = """
            SELECT t.id, t.name, COUNT(ct.friendly_token) AS cnt
            FROM tags t
            JOIN catalog_tags ct ON ct.tag_id = t.id
        """
        params: list = []
        if not show_hidden:
            ph = ",".join("?" * len(HIDDEN_TAG_NAMES))
            sql += f" WHERE t.name NOT IN ({ph})"
            params.extend(HIDDEN_TAG_NAMES)
        sql += """
            GROUP BY t.id, t.name
            ORDER BY cnt DESC, t.name ASC
            LIMIT ?
        """
        params.append(limit)
        return await self._fetch_all(sql, params)

    async def upsert_category(self, name: str) -> int:
        """Insert a category by name (deriving a unique slug) and return its id."""
        existing = await self._fetch_one("SELECT id FROM categories WHERE name = ?", [name])
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

    async def set_catalog_categories(self, friendly_token: str, category_ids: list[int]):
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
        sql = """
            UPDATE catalog SET title=:title, description=:description,
                   duration_sec=:duration_sec, manifest_url=:manifest_url,
                   thumbnail_url=:thumbnail_url,
                   added_at=COALESCE(:added_at, added_at),
                   synced_at=:synced_at, updated_at=:synced_at
            WHERE friendly_token=:friendly_token
        """
        row = {"added_at": None, **row}
        await self._db.execute(sql, row)
        # Rebuild FTS for this row
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
            [datetime.now(UTC).isoformat(), stats["seen"], stats["new"], stats["updated"], stats["errors"], status, log_id],
        )

    async def get_sync_logs(self, limit: int = 10) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", [limit]
        )

    # --- Generic job runs ---

    async def start_job_run(self, job_name: str, triggered_by: str | None = None,
                            params: str | None = None) -> int:
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
        await self._execute(
            "UPDATE job_runs SET detail=? WHERE id=?", [detail, run_id]
        )

    async def get_job_runs(self, job_name: str | None = None, limit: int = 10) -> list[dict]:
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

    async def cleanup_expired_otps(self):
        await self._execute("DELETE FROM otps WHERE expires_at < datetime('now') OR used=1")

    # --- Queue shadow ---

    async def get_shadow_items(self) -> list[dict]:
        return await self._fetch_all("SELECT * FROM queue_shadow ORDER BY position ASC")

    async def upsert_shadow_item(self, item: dict):
        sql = """
            INSERT OR REPLACE INTO queue_shadow
                (uid, position, title, media_type, media_id, duration_sec, is_pay, paid_by, tier, z_cost, schedule_id, added_at)
            VALUES (:uid, :position, :title, :media_type, :media_id, :duration_sec, :is_pay,
                    :paid_by, :tier, :z_cost, :schedule_id, :added_at)
        """
        defaults = {"paid_by": None, "tier": None, "z_cost": None, "schedule_id": None,
                    "friendly_token": None, "added_at": datetime.now(UTC).isoformat()}
        row = {**defaults, **item}
        await self._db.execute(sql, row)
        await self._db.commit()

    async def remove_shadow_items(self, uids: set[int]):
        placeholders = ",".join("?" * len(uids))
        await self._execute(f"DELETE FROM queue_shadow WHERE uid IN ({placeholders})", list(uids))

    async def update_shadow_position(self, uid: int, position: int):
        await self._db.execute("UPDATE queue_shadow SET position=? WHERE uid=?", [position, uid])
        await self._db.commit()

    async def update_shadow_estimated_start(self, uid: int, estimated: str):
        await self._db.execute(
            "UPDATE queue_shadow SET estimated_start_at=? WHERE uid=?", [estimated, uid]
        )
        await self._db.commit()

    async def get_last_pay_uid(self) -> int | None:
        row = await self._fetch_one(
            "SELECT uid FROM queue_shadow WHERE is_pay = 1 ORDER BY position DESC LIMIT 1"
        )
        return row["uid"] if row else None

    async def get_shadow_position_after(self, after_uid: int) -> int:
        row = await self._fetch_one("SELECT position FROM queue_shadow WHERE uid = ?", [after_uid])
        return (row["position"] + 1) if row else 0

    async def get_pay_items(self) -> list[dict]:
        return await self._fetch_all("SELECT * FROM queue_shadow WHERE is_pay = 1 ORDER BY position ASC")

    # --- Spend requests ---

    async def save_spend_request(self, request_id: str, *, username: str, uid: int | None,
                                 friendly_token: str | None = None, tier: str | None = None,
                                 z_cost: int | None = None):
        sql = """
            INSERT OR IGNORE INTO spend_requests (request_id, username, uid, friendly_token, tier, z_cost)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        await self._execute(sql, [request_id, username, uid, friendly_token, tier, z_cost])

    async def get_request_id_for_uid(self, uid: int) -> str | None:
        row = await self._fetch_one(
            "SELECT request_id FROM spend_requests WHERE uid = ? AND refunded = 0 LIMIT 1", [uid]
        )
        return row["request_id"] if row else None

    async def mark_spend_refunded(self, request_id: str):
        await self._execute(
            "UPDATE spend_requests SET refunded=1, refunded_at=datetime('now') WHERE request_id=?",
            [request_id],
        )

    # --- Queue history ---

    async def add_queue_history(self, *, username: str, friendly_token: str | None,
                                title: str | None, tier: str, z_cost: int):
        await self._execute(
            "INSERT INTO queue_history (username, friendly_token, title, tier, z_cost) VALUES (?, ?, ?, ?, ?)",
            [username, friendly_token, title, tier, z_cost],
        )

    async def get_user_queue_history(self, username: str, limit: int = 50) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM queue_history WHERE username=? ORDER BY id DESC LIMIT ?",
            [username, limit],
        )

    # --- Saved playlists ---

    async def get_saved_playlists(self) -> list[dict]:
        return await self._fetch_all("SELECT * FROM saved_playlists ORDER BY name")

    async def get_saved_playlist(self, playlist_id: int) -> dict | None:
        return await self._fetch_one("SELECT * FROM saved_playlists WHERE id=?", [playlist_id])

    async def create_saved_playlist(self, *, name: str, description: str | None, is_immutable: bool, created_by: str) -> int:
        cursor = await self._db.execute(
            "INSERT INTO saved_playlists (name, description, is_immutable, created_by) VALUES (?, ?, ?, ?)",
            [name, description, int(is_immutable), created_by],
        )
        await self._db.commit()
        return cursor.lastrowid

    async def update_saved_playlist(self, playlist_id: int, *, name: str, description: str | None, is_immutable: bool):
        await self._execute(
            "UPDATE saved_playlists SET name=?, description=?, is_immutable=?, updated_at=datetime('now') WHERE id=?",
            [name, description, int(is_immutable), playlist_id],
        )

    async def delete_saved_playlist(self, playlist_id: int):
        await self._execute("DELETE FROM saved_playlists WHERE id=?", [playlist_id])

    async def get_saved_playlist_items(self, playlist_id: int) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM saved_playlist_items WHERE playlist_id=? ORDER BY position", [playlist_id]
        )

    async def replace_playlist_items(self, playlist_id: int, items: list[dict]):
        await self._db.execute("DELETE FROM saved_playlist_items WHERE playlist_id=?", [playlist_id])
        for i, item in enumerate(items):
            await self._db.execute(
                "INSERT INTO saved_playlist_items (playlist_id, position, media_type, media_id, title, duration_sec) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [playlist_id, i, item["media_type"], item["media_id"], item.get("title"), item.get("duration_sec")],
            )
        await self._db.commit()

    async def append_playlist_item(self, playlist_id: int, item: dict) -> int:
        """Append a single item to the end of a playlist. Returns new item count."""
        row = await self._fetch_one(
            "SELECT COALESCE(MAX(position), -1) AS pos, COUNT(*) AS cnt "
            "FROM saved_playlist_items WHERE playlist_id=?",
            [playlist_id],
        )
        next_pos = (row["pos"] + 1) if row else 0
        count = (row["cnt"] if row else 0) + 1
        await self._db.execute(
            "INSERT INTO saved_playlist_items (playlist_id, position, media_type, media_id, title, duration_sec) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [playlist_id, next_pos, item["media_type"], item["media_id"], item.get("title"), item.get("duration_sec")],
        )
        await self._db.execute(
            "UPDATE saved_playlists SET updated_at=datetime('now') WHERE id=?", [playlist_id]
        )
        await self._db.commit()
        return count

    async def get_most_recent_playlist(self, created_by: str) -> dict | None:
        """The given admin's most recently *created* saved playlist, if any."""
        return await self._fetch_one(
            "SELECT * FROM saved_playlists WHERE created_by=? ORDER BY created_at DESC, id DESC LIMIT 1",
            [created_by],
        )

    async def get_playlist_by_name(self, name: str, created_by: str) -> dict | None:
        """Match an existing saved playlist by exact name + creator.

        Used for idempotent re-imports (e.g. the fetchurls job replacing a
        section playlist's items rather than creating a duplicate).
        """
        return await self._fetch_one(
            "SELECT * FROM saved_playlists WHERE name=? AND created_by=? ORDER BY id LIMIT 1",
            [name, created_by],
        )

    # --- Schedules ---

    async def get_schedules(self) -> list[dict]:
        return await self._fetch_all("SELECT * FROM playlist_schedules ORDER BY fire_at")

    async def get_schedule(self, schedule_id: int) -> dict | None:
        return await self._fetch_one("SELECT * FROM playlist_schedules WHERE id=?", [schedule_id])

    async def create_schedule(self, **kwargs) -> int:
        keys = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" * len(kwargs))
        cursor = await self._db.execute(
            f"INSERT INTO playlist_schedules ({keys}) VALUES ({placeholders})",
            list(kwargs.values()),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def update_schedule(self, schedule_id: int, **kwargs):
        sets = ", ".join(f"{k}=?" for k in kwargs.keys())
        await self._execute(
            f"UPDATE playlist_schedules SET {sets} WHERE id=?",
            [*kwargs.values(), schedule_id],
        )

    async def delete_schedule(self, schedule_id: int):
        await self._execute("DELETE FROM playlist_schedules WHERE id=?", [schedule_id])

    async def mark_schedule_fired(self, schedule_id: int, fired_at: str):
        await self._execute(
            "UPDATE playlist_schedules SET fired_at=? WHERE id=?", [fired_at, schedule_id]
        )

    # --- Active schedule ---

    async def get_active_schedule(self) -> dict | None:
        return await self._fetch_one("SELECT * FROM active_schedule WHERE id=1")

    async def set_active_schedule(self, *, schedule_id: int, playlist_id: int,
                                  is_immutable: bool, started_at: str, estimated_end_at: str,
                                  last_item_uid: int | None = None):
        await self._execute(
            "INSERT OR REPLACE INTO active_schedule "
            "(id, schedule_id, playlist_id, is_immutable, started_at, estimated_end_at, last_item_uid, lock_disabled) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, 0)",
            [schedule_id, playlist_id, int(is_immutable), started_at, estimated_end_at, last_item_uid],
        )

    async def clear_active_schedule(self):
        await self._execute("DELETE FROM active_schedule WHERE id=1")

    async def disable_active_lock(self):
        """Lift the in-progress scheduled-event lock without ending the event.

        Keeps the ``active_schedule`` row (so banners/state still show the event)
        and leaves the underlying schedule armed for future occurrences.
        """
        await self._execute("UPDATE active_schedule SET lock_disabled=1 WHERE id=1")

    async def is_event_lock_active(self) -> bool:
        """True while an immutable scheduled event is locking pay-to-play.

        Auto-lifts (via :meth:`disable_active_lock`, set when the last scheduled
        item begins playing) and respects an admin's manual unlock.
        """
        row = await self.get_active_schedule()
        if not row:
            return False
        if not row.get("is_immutable"):
            return False
        return not row.get("lock_disabled")

    # --- Pre-fire lock check ---

    async def is_pre_fire_lock_active(self) -> bool:
        row = await self._fetch_one("""
            SELECT 1 FROM playlist_schedules
            WHERE is_active = 1
              AND lock_disabled = 0
              AND datetime(fire_at, '-' || pre_fire_lock_minutes || ' minutes') <= datetime('now')
              AND fire_at > datetime('now')
            LIMIT 1
        """)
        return row is not None

    async def get_active_pre_fire_lock(self) -> dict | None:
        """Return the schedule whose pre-fire lock window is currently active.

        Used to give users a specific "pay-to-play closes before [event]"
        message instead of a generic locked error.
        """
        return await self._fetch_one("""
            SELECT * FROM playlist_schedules
            WHERE is_active = 1
              AND lock_disabled = 0
              AND datetime(fire_at, '-' || pre_fire_lock_minutes || ' minutes') <= datetime('now')
              AND fire_at > datetime('now')
            ORDER BY fire_at
            LIMIT 1
        """)

    async def get_next_schedule(self) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM playlist_schedules WHERE is_active=1 AND fire_at > datetime('now') ORDER BY fire_at LIMIT 1"
        )
