import aiosqlite
from pathlib import Path


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
    # v9: Optional fallback (mutable) playlist appended to the live queue after a
    # scheduled event's items, so the queue isn't left empty when the event is
    # exhausted. NULL = no fallback (legacy behaviour).
    """
    ALTER TABLE playlist_schedules ADD COLUMN fallback_playlist_id INTEGER REFERENCES saved_playlists(id) ON DELETE SET NULL;
    """,
    # v10: Tag a saved playlist as a promo pool. A non-NULL promo_type marks the
    # playlist as a reserved pool of promo clips for that type; such playlists
    # are hidden from public browse/search and excluded from pay-to-play (same
    # treatment as is_immutable). NULL = a normal playlist.
    """
    ALTER TABLE saved_playlists ADD COLUMN promo_type TEXT;
    CREATE INDEX IF NOT EXISTS idx_saved_playlists_promo ON saved_playlists(promo_type);
    """,
    # v11: Annotate live promo items inserted into the queue shadow. is_promo
    # marks a system-inserted promo clip; promo_type is its pool; lead_in_for_uid
    # links a Feature-Presentation / Viewer's-Choice lead-in to the content uid
    # it immediately precedes (NULL for general cadence promos).
    """
    ALTER TABLE queue_shadow ADD COLUMN is_promo BOOLEAN NOT NULL DEFAULT 0;
    ALTER TABLE queue_shadow ADD COLUMN promo_type TEXT;
    ALTER TABLE queue_shadow ADD COLUMN lead_in_for_uid INTEGER;
    """,
    # v12: Media-id fallback for the event-lock auto-lift. Stores the media_id
    # of the last scheduled item so _maybe_lift_event_lock can detect when it
    # begins playing even when last_item_uid was never captured (e.g. a CyTube
    # queue-event timeout during bulk add left the uid null).
    """
    ALTER TABLE active_schedule ADD COLUMN last_item_media_id TEXT;
    """,
    # v13: Viewer feedback + movie-title suggestions, each with a simple
    # admin-triage queue (status new|read). Title suggestions record the
    # resolved match against TMDB/OMDB (NULL when unresolved) and whether we
    # already have the title in the catalog (resolution + catalog_token).
    """
    CREATE TABLE IF NOT EXISTS feedback (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        username    TEXT NOT NULL,
        body        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'new',
        created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status, created_at);

    CREATE TABLE IF NOT EXISTS title_suggestions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        username        TEXT NOT NULL,
        query           TEXT NOT NULL,
        resolved_title  TEXT,
        resolved_year   TEXT,
        resolved_source TEXT,
        resolved_id     TEXT,
        poster_url      TEXT,
        resolution      TEXT NOT NULL DEFAULT 'unresolved',
        catalog_token   TEXT,
        status          TEXT NOT NULL DEFAULT 'new',
        created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_title_suggestions_status ON title_suggestions(status, created_at);
    """,
    # v14: Play-completion tracking that drives hiding recently-played catalog
    # items from regular users.
    #
    # play_completions records a *genuine* completion (an item that reached the
    # now-playing slot and played past a threshold), keyed by media_id
    # (== catalog friendly_token for 'cm'). completed_at powers the time-based
    # hide window. Items queued-then-refunded never reach now-playing, so they
    # never land here.
    #
    # playlist_item_played tracks, per mutable (TV-show) playlist, which short
    # (<1h) episodes have played in the *current pass*. Rows are cleared for a
    # playlist the moment its last item (MAX position) plays, releasing the whole
    # collection at once — so appending S2/S3 never re-hides S1 piecemeal.
    """
    CREATE TABLE IF NOT EXISTS play_completions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        media_type   TEXT NOT NULL,
        media_id     TEXT NOT NULL,
        completed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_play_completions_media ON play_completions(media_id);
    CREATE INDEX IF NOT EXISTS idx_play_completions_at ON play_completions(completed_at);

    CREATE TABLE IF NOT EXISTS playlist_item_played (
        playlist_id INTEGER NOT NULL REFERENCES saved_playlists(id) ON DELETE CASCADE,
        position    INTEGER NOT NULL,
        media_type  TEXT NOT NULL,
        media_id    TEXT NOT NULL,
        played_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (playlist_id, position)
    );
    CREATE INDEX IF NOT EXISTS idx_playlist_item_played_media ON playlist_item_played(media_id);
    """,
    # v15: One-time cleanup of recently-played hide state wrongly recorded for
    # promo-pool clips. Promos are excluded from the public catalog and must
    # never be treated like normal playlist items, so any hide state recorded for
    # a promo clip (before the exemption in record_play_completion) is purged.
    """
    DELETE FROM play_completions
    WHERE media_id IN (
        SELECT spi.media_id FROM saved_playlist_items spi
        JOIN saved_playlists sp ON sp.id = spi.playlist_id
        WHERE sp.promo_type IS NOT NULL AND spi.media_type = 'cm'
    );
    DELETE FROM playlist_item_played
    WHERE media_id IN (
        SELECT spi.media_id FROM saved_playlist_items spi
        JOIN saved_playlists sp ON sp.id = spi.playlist_id
        WHERE sp.promo_type IS NOT NULL AND spi.media_type = 'cm'
    );
    """,
    # v16: Cron-based job schedule persistence (separate from playlist_schedules,
    # which fires playlists onto the queue). One row per job; UNIQUE on job_name.
    # run_next_job chains another job to run immediately after this one completes.
    """
    CREATE TABLE IF NOT EXISTS job_schedules (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        job_name         TEXT NOT NULL UNIQUE,
        label            TEXT,
        cron_expression  TEXT NOT NULL,
        params_json      TEXT,
        is_active        INTEGER NOT NULL DEFAULT 1,
        run_next_job     TEXT,
        created_by       TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    # v17: Per-user watchlist ("My List").
    """
    CREATE TABLE IF NOT EXISTS user_watchlist (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        username       TEXT NOT NULL,
        friendly_token TEXT NOT NULL,
        added_at       TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (username, friendly_token)
    );
    CREATE INDEX IF NOT EXISTS idx_user_watchlist_username ON user_watchlist(username);
    """,
    # v18: Per-item cast / crew / studio facets (M-to-many, same model as tags).
    """
    CREATE TABLE IF NOT EXISTS people (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    );
    CREATE INDEX IF NOT EXISTS idx_people_name ON people(name);

    CREATE TABLE IF NOT EXISTS catalog_people (
        friendly_token TEXT NOT NULL,
        person_id      INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        role           TEXT NOT NULL,
        position       INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (friendly_token, person_id, role)
    );
    CREATE INDEX IF NOT EXISTS idx_catalog_people_token  ON catalog_people(friendly_token);
    CREATE INDEX IF NOT EXISTS idx_catalog_people_person ON catalog_people(person_id);

    CREATE TABLE IF NOT EXISTS studios (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    );

    CREATE TABLE IF NOT EXISTS catalog_studios (
        friendly_token TEXT NOT NULL,
        studio_id      INTEGER NOT NULL REFERENCES studios(id) ON DELETE CASCADE,
        PRIMARY KEY (friendly_token, studio_id)
    );
    CREATE INDEX IF NOT EXISTS idx_catalog_studios_token ON catalog_studios(friendly_token);
    """,
    # v19: Per-item enrichment pipeline state cache.
    """
    CREATE TABLE IF NOT EXISTS item_enrichment_state (
        friendly_token    TEXT PRIMARY KEY,
        content_type      TEXT,
        hosted_show       TEXT,
        lookup_title      TEXT,
        lookup_year       TEXT,
        tv_show           TEXT,
        tv_season         INTEGER,
        tv_episode_num    INTEGER,
        description_score INTEGER,
        tmdb_id           TEXT,
        imdb_id           TEXT,
        meta_json         TEXT,
        last_classify_at  TEXT,
        last_title_at     TEXT,
        last_meta_at      TEXT,
        last_art_at       TEXT,
        last_tags_at      TEXT
    );
    """,
    # v20: Add last_categories_at for categories enrichment step
    """
    ALTER TABLE item_enrichment_state ADD COLUMN last_categories_at TEXT;
    """,
    # v21: Item edit audit log (track manual admin edits)
    """
    CREATE TABLE IF NOT EXISTS item_edit_log (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        friendly_token TEXT NOT NULL,
        username       TEXT NOT NULL,
        field_name     TEXT NOT NULL,
        old_value      TEXT,
        new_value      TEXT,
        edited_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_item_edit_log_token ON item_edit_log(friendly_token, edited_at DESC);
    """,
]


class _DBBase:
    """Connection, migrations, and low-level query helpers."""

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
        """Apply pending migrations sequentially, then reconcile known drift."""
        await self._executescript(MIGRATIONS[0])
        row = await self._fetch_one("SELECT MAX(version) as v FROM _migrations")
        current_version = (row["v"] or 0) if row else 0

        for version, sql in enumerate(MIGRATIONS[1:], start=1):
            if version > current_version:
                await self._executescript(sql)
                await self._execute(
                    "INSERT INTO _migrations (version) VALUES (?)", [version]
                )

        await self._reconcile_schema()

    async def _reconcile_schema(self):
        """Idempotently repair known column drift the migration tracking missed.

        Some databases recorded a migration version whose ``ALTER`` never actually
        landed (a migration was reordered before forward-only was enforced). The
        event-lock path depends on these ``active_schedule`` columns; a missing one
        makes ``set_active_schedule`` raise, which silently disables the
        scheduled-event lock and lets promos leak into a non-preemptable event.
        Add any that are absent — a no-op on healthy databases.
        """
        expected = {
            "last_item_uid": "INTEGER",
            "lock_disabled": "INTEGER NOT NULL DEFAULT 0",
            "last_item_media_id": "TEXT",
        }
        existing = {
            r["name"]
            for r in await self._fetch_all("PRAGMA table_info(active_schedule)")
        }
        for name, decl in expected.items():
            if name not in existing:
                await self._execute(
                    f"ALTER TABLE active_schedule ADD COLUMN {name} {decl}"
                )

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
