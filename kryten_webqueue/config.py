import json
import os
from pathlib import Path
from typing import Any, Literal
import urllib.parse
from pydantic import BaseModel, Field, PrivateAttr, model_validator


class EmoteRehostConfig(BaseModel):
    """Settings for the emote rehost job."""

    enabled: bool = True
    # Emotes whose image URL contains this string are considered already rehosted.
    rehost_domain: str = "dropsugar.co"
    # Local directory where rehosted image files are written.
    static_dir: str = "/var/lib/kryten-webqueue/emotes/images"
    # Public base URL served from static_dir; final URL: {base_url}/{bare_name}{ext}
    base_url: str = "https://queue.dropsugar.co/emotes/images"
    # Disk-derived export (``[{\"name\": \"#emote\", \"image\": \"...\"}]``)
    # used to restore the channel emote list after a service migration.
    manifest_path: str = "/var/lib/kryten-webqueue/emotes/emotes.json"
    # Replace CyTube's emote list from the disk-derived manifest on each run.
    sync_disk_manifest: bool = True
    # Directory for timestamped backup JSON files (created if absent).
    backup_dir: str = "/var/lib/kryten-webqueue/emotes/backups"
    # Background check interval in hours; 0 disables the periodic loop.
    check_interval_hours: float = 24.0
    download_max_retries: int = 5
    inter_emote_delay_sec: float = 2.0


class MOTDLink(BaseModel):
    """A footer link rendered under the MOTD poster grid."""

    label: str
    url: str


class MOTDConfig(BaseModel):
    """Settings for the MOTD poster generator and publisher jobs."""

    # Filesystem path where poster images are written.
    poster_dir: str = "/home/mediacms.io/mediacms/static/motd_boxes"
    # Public base URL corresponding to poster_dir.
    poster_base_url: str = "https://www.dropsugar.co/static/motd_boxes"
    # Directory where the generated HTML snippet is written.
    output_dir: str = "~/kryten"

    # --- motd_publish ---
    # Jinja template under kryten_webqueue/templates/motd/.
    template: str = "channel_z.html"
    # Grid size; unresolved positions are filled with mystery boxes.
    slots: int = 12
    # Nights the grid covers, in display order (1=Fri, 2=Sat, 3=Sun). Sunday has
    # no schedule yet, so its workbook titles are ignored until it's added here.
    nights: list[int] = Field(default_factory=lambda: [1, 2])
    banner_url: str = "https://i.postimg.cc/jdr2mR8Y/1562479186-8-channel-Ztitlenew.png"
    # {dates} is substituted with the weekend showtimes (e.g. "8/14 @ 6pm ET & ...").
    headline: str = "CHANNEL Z WEEKEND MOVIE PLAYLIST ({dates})"
    showtime: str = "6pm ET"
    # Mystery boxes are drawn from the same branded placeholder pool the browse
    # view uses (served from the webqueue /images mount); this absolutizes them
    # for CyTube, which renders the MOTD off-site.
    mystery_box_base_url: str = "https://queue.dropsugar.co"
    # Single fallback used only when no branded placeholders are installed.
    mystery_box_url: str = ""
    mystery_box_href: str = "https://queue.dropsugar.co/"
    links: list[MOTDLink] = Field(
        default_factory=lambda: [
            MOTDLink(
                label="Join us on Reddit!",
                url="https://www.reddit.com/r/Channel_Z/",
            ),
            MOTDLink(
                label="See the queue and decide what's next! Our Webqueue app is in open beta!",
                url="https://queue.dropsugar.co/",
            ),
        ]
    )
    # The next-event line is carried in the render context but stays hidden until
    # it's deliberately debuted; the hand-built MOTD has no such line.
    show_next_event: bool = False
    # Max size accepted by the admin alternate-art upload endpoint.
    upload_max_bytes: int = 5 * 1024 * 1024


class FetchUrlsConfig(BaseModel):
    """Settings for the fetchurls job.

    Reads the Channel Z workbook from SharePoint (Microsoft Graph) when the
    SharePoint fields are configured, otherwise falls back to a local ``.xlsx``
    at ``workbook_path``. SharePoint auth uses a pre-seeded MSAL token cache
    (see ``python -m kryten_webqueue.jobs.fetchurls_auth``); the service only
    acquires tokens *silently* from that cache and never prompts interactively.
    """

    workbook_path: str = ""  # local .xlsx fallback (used when SharePoint unset)

    # SharePoint / Microsoft Graph (read workbook + write resolved URLs to col F)
    sharepoint_tenant_id: str = ""
    sharepoint_client_id: str = ""
    sharepoint_sharing_url: str = ""
    token_cache_path: str = ""  # MSAL cache file, pre-seeded out-of-band


class FetchQueueConfig(BaseModel):
    """Settings for the persistent fetch-queue drain job.

    The drain processes queued downloads one at a time and waits a randomized
    cooldown *between* items so it doesn't hammer the source and trip bot
    detection (the MediaCMS encoder is the real bottleneck anyway, so a long
    gap costs little). Each wait is drawn uniformly from
    ``cooldown_mean_minutes ± cooldown_jitter_minutes`` (clamped at >= 0).

    These are re-read from the config file before every wait, so editing the
    config and reloading retunes a *running* drain without a restart.
    """

    # Randomized inter-item cooldown, centered on ~42 min with jitter.
    cooldown_mean_minutes: float = 42.0
    cooldown_jitter_minutes: float = 8.0
    # Set false to drain back-to-back (e.g. for a one-off bulk import).
    cooldown_enabled: bool = True


class PresenceRefundConfig(BaseModel):
    """Settings for presence-based cancel/refund of pending paid items.

    When a viewer who paid to queue an item leaves the channel or goes AFK,
    cancel and refund their not-yet-played paid items after a grace period.
    The currently-playing item is never cancelled; free/scheduled items are
    left alone.

    ``on_afk`` relies on the Robot tracking CyTube's ``setAFK`` event (shipped
    in Kryten-Robot v1.10.0). It defaults on now that v1.10.0 is released; set it
    off if running against an older Robot whose ``meta.afk`` goes stale.

    ``notify_user`` PMs the owner when a pending paid item is cancelled & refunded
    so the cancellation isn't silent. This only applies to **AFK** owners — a
    user who left the channel is no longer connected and cannot receive a PM.
    """

    enabled: bool = True
    on_leave: bool = True
    on_afk: bool = True  # needs Kryten-Robot >= 1.10.0 deployed
    grace_seconds: float = 60.0  # wait before acting; re-check after grace
    check_interval_seconds: float = 15.0  # how often to evaluate owners
    notify_user: bool = True  # PM the AFK owner on cancel/refund


class PromoTypeConfig(BaseModel):
    """Per-type promo settings.

    ``order`` is ``random`` (uniform over the pool) or ``sequential`` (rotate
    through the pool in stored order, resuming where it left off). ``weight`` is
    the relative frequency among the *general* types when a cadence slot fires
    (ignored for the lead-in types).
    """

    enabled: bool = True
    order: str = "random"  # "random" | "sequential"
    weight: int = 1


class GeneralPromoConfig(BaseModel):
    """Cadence for the general (between-content) promos."""

    every_n_items: int = 4  # insert a general promo every N content items
    every_m_minutes: float = 20.0  # ...or roughly every M minutes, whichever first
    no_repeat: bool = True  # don't play the same clip twice in a row


class PromoConfig(BaseModel):
    """Settings for the promo insertion system (see PromoDirector).

    Promo clips live in saved playlists tagged with a ``promo_type``. General
    promos (types 1-3) are inserted on a cadence between mutable content;
    Feature-Presentation (movies) and Viewer's-Choice (pay items) lead-ins
    (types 4-5) are attached immediately before a qualifying upcoming item.
    """

    enabled: bool = True
    movie_threshold_seconds: float = 3600.0
    general: GeneralPromoConfig = GeneralPromoConfig()
    types: dict[str, PromoTypeConfig] = {
        "channel_identity": PromoTypeConfig(order="random", weight=3),
        "event": PromoTypeConfig(order="random", weight=2),
        "mod_shoutout": PromoTypeConfig(order="sequential", weight=1),
        "feature_presentation": PromoTypeConfig(order="random"),
        "viewers_choice": PromoTypeConfig(order="random"),
    }


class PostgresConfig(BaseModel):
    """PostgreSQL connection configuration."""

    dsn_env: str | None = None
    dsn: str | None = None
    host: str = "host.containers.internal"
    port: int = 5432
    user: str = "kryten"
    dbname: str = "webqueue"
    password_env: str | None = "KRYTEN_WEBQUEUE_PG_PASSWORD"
    pool_size: int = 10
    max_overflow: int = 20

    def get_password(self) -> str | None:
        if self.password_env:
            return os.getenv(self.password_env)
        return None

    def get_asyncpg_dsn(self) -> str:
        """Return the DSN accepted by asyncpg.create_pool()/connect()."""
        if self.dsn_env:
            env_dsn = os.getenv(self.dsn_env)
            if env_dsn:
                return env_dsn

        password = self.get_password() or ""
        escaped_password = urllib.parse.quote_plus(password)

        if self.dsn:
            parsed = urllib.parse.urlparse(self.dsn)
            if parsed.password:
                raise ValueError(
                    "Plaintext passwords in 'database.postgres.dsn' are forbidden. "
                    "Use 'password_env' or 'dsn_env' instead."
                )
            user_part = parsed.username or self.user
            escaped_user = urllib.parse.quote_plus(user_part)
            host_part = parsed.hostname or self.host
            port_part = (
                f":{parsed.port}"
                if parsed.port
                else (f":{self.port}" if self.port else "")
            )
            netloc = f"{escaped_user}:{escaped_password}@{host_part}{port_part}"
            path = parsed.path if parsed.path else f"/{self.dbname}"
            return f"postgresql://{netloc}{path}"

        escaped_user = urllib.parse.quote_plus(self.user)
        return (
            f"postgresql://{escaped_user}:{escaped_password}@{self.host}:{self.port}/"
            f"{self.dbname}"
        )

    def get_async_url(self) -> str:
        # Precedence: dsn_env -> password-free dsn + password_env -> assembled components + password_env
        if self.dsn_env:
            env_dsn = os.getenv(self.dsn_env)
            if env_dsn:
                if env_dsn.startswith("postgresql://"):
                    return env_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
                return env_dsn

        password = self.get_password() or ""
        escaped_password = urllib.parse.quote_plus(password)

        if self.dsn:
            parsed = urllib.parse.urlparse(self.dsn)
            if parsed.password:
                raise ValueError(
                    "Plaintext passwords in 'database.postgres.dsn' are forbidden. "
                    "Use 'password_env' or 'dsn_env' instead."
                )
            user_part = parsed.username or self.user
            escaped_user = urllib.parse.quote_plus(user_part)
            host_part = parsed.hostname or self.host
            port_part = (
                f":{parsed.port}"
                if parsed.port
                else (f":{self.port}" if self.port else "")
            )
            netloc = f"{escaped_user}:{escaped_password}@{host_part}{port_part}"
            path = parsed.path if parsed.path else f"/{self.dbname}"
            return f"postgresql+asyncpg://{netloc}{path}"

        escaped_user = urllib.parse.quote_plus(self.user)
        return f"postgresql+asyncpg://{escaped_user}:{escaped_password}@{self.host}:{self.port}/{self.dbname}"

    @model_validator(mode="after")
    def validate_postgres(self) -> "PostgresConfig":
        if self.dsn:
            parsed = urllib.parse.urlparse(self.dsn)
            if parsed.password:
                raise ValueError(
                    "Plaintext passwords in 'database.postgres.dsn' are forbidden. "
                    "Use 'password_env' or 'dsn_env' instead."
                )
        return self


class DatabaseConfig(BaseModel):
    """Database layout and path configuration."""

    backend: Literal["sqlite", "postgres"] = "sqlite"
    layout: Literal["monolith", "partitioned"] = "monolith"
    data_dir: str = "./data"
    catalog_db_path: str | None = None
    queue_db_path: str | None = None
    jobs_db_path: str | None = None
    users_db_path: str | None = None
    db_path: str = "/var/lib/kryten-webqueue/webqueue.db"
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)

    def get_catalog_path(self) -> str:
        return self.catalog_db_path or str(Path(self.data_dir) / "catalog.sqlite3")

    def get_queue_path(self) -> str:
        return self.queue_db_path or str(Path(self.data_dir) / "queue.sqlite3")

    def get_jobs_path(self) -> str:
        return self.jobs_db_path or str(Path(self.data_dir) / "jobs.sqlite3")

    def get_users_path(self) -> str:
        return self.users_db_path or str(Path(self.data_dir) / "users.sqlite3")

    @model_validator(mode="after")
    def validate_layout(self) -> "DatabaseConfig":
        if self.backend == "sqlite":
            if self.layout == "monolith":
                if not self.db_path or not self.db_path.strip():
                    raise ValueError(
                        "Monolith database layout requires a non-empty 'db_path'"
                    )
            elif self.layout == "partitioned":
                domain_paths = [
                    self.catalog_db_path,
                    self.queue_db_path,
                    self.jobs_db_path,
                    self.users_db_path,
                ]
                any_domain_path = any(p is not None for p in domain_paths)
                all_domain_paths = all(p is not None for p in domain_paths)
                if any_domain_path and not all_domain_paths and not self.data_dir:
                    raise ValueError(
                        "Partitioned layout with explicit domain paths requires all 4 paths "
                        "(catalog_db_path, queue_db_path, jobs_db_path, users_db_path) or a base 'data_dir'"
                    )
                # Guard against silently bypassing an existing monolithic database:
                if self.db_path and Path(self.db_path).is_file():
                    catalog_path = Path(self.get_catalog_path())
                    if not catalog_path.is_file():
                        raise ValueError(
                            f"Legacy monolith database exists at '{self.db_path}', but partitioned "
                            f"database '{catalog_path}' does not exist. Run split_databases.py "
                            "before switching database layout to 'partitioned' to prevent starting with an empty database."
                        )
        return self


class Config(BaseModel):
    """Application configuration loaded from JSON file."""

    # Path the config was loaded from; set by ``from_file`` so editable settings
    # (e.g. the promo admin panel) can persist back to the same file.
    _source_path: Path | None = PrivateAttr(default=None)

    # Server
    channel: str = "Q_A"
    host: str = "0.0.0.0"
    port: int = 2010
    secret_key: str
    session_ttl_hours: int = 24
    # Old JWT signing keys accepted only for verification during a key rotation.
    # New sessions are always signed with ``secret_key``.
    session_previous_secret_keys: list[str] = Field(default_factory=list)

    # Logging
    # Root application log level for the ``kryten_webqueue`` logger hierarchy.
    # Without explicit configuration Python only emits WARNING+ via its
    # "last resort" handler, so INFO diagnostics (e.g. promo insertions) are
    # silently dropped. ``__main__`` installs a dictConfig using these values.
    log_level: str = "INFO"
    # Independent level for the promo subsystem (``kryten_webqueue.promos``).
    # Set to "DEBUG" for a full per-poll trace of promo decisions without
    # flooding the rest of the app. Falls back to ``log_level`` when None.
    promo_log_level: str | None = None

    # API Gate
    api_gate_url: str = "http://127.0.0.1:24444"
    api_gate_token: str

    # MediaCMS
    mediacms_url: str = "https://www.dropsugar.co"
    mediacms_token: str
    # Set true when the token can tag any media (manager/superuser on a CMS whose
    # bulk_actions is patched to allow it); pushes derived tags for all items.
    mediacms_manage_all_media: bool = False

    # Cover art APIs
    tmdb_api_key: str = ""
    omdb_api_key: str = ""

    # Local TMDB index (standalone DB rebuilt from the daily ID-export dumps).
    # tmdb_index_source_dir is the directory holding the unpacked dump files and
    # doubles as the allowed root for the refresh job's dump_dir param.
    tmdb_index_path: str = "/var/lib/kryten-webqueue/tmdb_index.db"
    tmdb_index_source_dir: str = ""

    # Jobs (optional; jobs whose config/deps are absent fail fast at run time)
    fetch_cookies_path: str = ""  # optional yt-dlp cookies for gated sources
    fetchurls: FetchUrlsConfig = FetchUrlsConfig()
    fetch_queue: FetchQueueConfig = FetchQueueConfig()
    emote_rehost: EmoteRehostConfig = EmoteRehostConfig()
    motd: MOTDConfig = MOTDConfig()
    # Presence-based cancel/refund of pending paid items
    presence_refund: PresenceRefundConfig = PresenceRefundConfig()

    # Promo insertion system
    promos: PromoConfig = PromoConfig()

    # Database
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    db_path: str = "/var/lib/kryten-webqueue/webqueue.db"

    @model_validator(mode="before")
    @classmethod
    def _sync_db_config_before(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "db_path" in data:
                if "database" not in data:
                    data["database"] = {"db_path": data["db_path"]}
                elif (
                    isinstance(data["database"], dict)
                    and "db_path" not in data["database"]
                ):
                    data["database"]["db_path"] = data["db_path"]
            elif "database" in data and isinstance(data["database"], dict):
                if "db_path" in data["database"]:
                    data["db_path"] = data["database"]["db_path"]
        return data

    @model_validator(mode="after")
    def _sync_db_config_after(self) -> "Config":
        self.db_path = self.database.db_path
        return self

    # Images
    image_dir: str = "/var/lib/kryten-webqueue/images"
    placeholder_dir: str = "/var/lib/kryten-webqueue/images/placeholders"

    # Scheduling
    catalog_sync_interval_hours: int = 4
    pre_fire_lock_minutes_default: int = 15
    state_poll_interval_sec: float = 3.0

    # Hide recently-played items from the public catalog (browse/search) for this
    # many days after they actually finished playing. Admins (rank >= 3) always
    # see them. Short (<1h) episodes of a mutable playlist are instead governed by
    # playlist position (hidden until the playlist's last item plays). Set to 0 to
    # disable recently-played hiding entirely.
    catalog_recently_played_hide_days: int = 21

    # Bulk playlist loading (manual import + scheduled fire). CyTube validates
    # each queued item server-side (fetching custom manifests); adding faster
    # than it can validate triggers a transient queueFail (surfaced by api-gate
    # as HTTP 422). Throttle consecutive adds and retry the transient 422.
    playlist_bulk_add_delay_sec: float = 0.5  # pause between consecutive adds
    playlist_bulk_add_max_retries: int = 2  # retries on transient 422

    # Monitoring
    prometheus_port: int = 28292

    @classmethod
    def from_file(cls, path: str | Path) -> "Config":
        with open(path, encoding="utf-8") as f:
            cfg = cls(**json.load(f))
        cfg._source_path = Path(path)
        return cfg

    def reload_fetch_queue(self) -> "FetchQueueConfig":
        """Re-read just the ``fetch_queue`` section from the source file.

        Enables live cooldown tuning: edit the config file and the running drain
        picks up new pacing before its next wait — no restart needed. On any
        error (no known source path, unreadable or invalid file) falls back to
        the section loaded at startup so the drain never breaks on a bad edit.
        """
        if self._source_path is None:
            return self.fetch_queue
        try:
            with open(self._source_path, encoding="utf-8") as f:
                raw = json.load(f)
            return FetchQueueConfig(**(raw.get("fetch_queue") or {}))
        except Exception:  # noqa: BLE001 - never let a bad edit break the drain
            return self.fetch_queue

    def save(self) -> None:
        """Persist the current config back to the file it was loaded from.

        Writes atomically (temp file + replace) so a crash mid-write can't leave
        a truncated config. Raises if the config has no known source path (e.g.
        constructed in-memory by a test).
        """
        if self._source_path is None:
            raise RuntimeError("Config has no source path; cannot persist changes")
        tmp = self._source_path.with_name(self._source_path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.model_dump(), f, indent=2)
            f.write("\n")
        tmp.replace(self._source_path)
