from pathlib import Path
from pydantic import BaseModel, PrivateAttr
import json


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
    so the cancellation isn't silent.
    """

    enabled: bool = True
    on_leave: bool = True
    on_afk: bool = True               # needs Kryten-Robot >= 1.10.0 deployed
    grace_seconds: float = 60.0       # wait before acting; re-check after grace
    check_interval_seconds: float = 15.0  # how often to evaluate owners
    notify_user: bool = True          # PM the owner on cancel/refund


class PromoTypeConfig(BaseModel):
    """Per-type promo settings.

    ``order`` is ``random`` (uniform over the pool) or ``sequential`` (rotate
    through the pool in stored order, resuming where it left off). ``weight`` is
    the relative frequency among the *general* types when a cadence slot fires
    (ignored for the lead-in types).
    """

    enabled: bool = True
    order: str = "random"   # "random" | "sequential"
    weight: int = 1


class GeneralPromoConfig(BaseModel):
    """Cadence for the general (between-content) promos."""

    every_n_items: int = 4       # insert a general promo every N content items
    every_m_minutes: float = 20.0  # ...or roughly every M minutes, whichever first
    no_repeat: bool = True       # don't play the same clip twice in a row


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

    # API Gate
    api_gate_url: str = "http://127.0.0.1:24444"
    api_gate_token: str

    # MediaCMS
    mediacms_url: str = "https://www.dropsugar.com"
    mediacms_token: str

    # Cover art APIs
    tmdb_api_key: str = ""
    omdb_api_key: str = ""

    # Jobs (optional; jobs whose config/deps are absent fail fast at run time)
    fetch_cookies_path: str = ""          # optional yt-dlp cookies for gated sources
    fetchurls: FetchUrlsConfig = FetchUrlsConfig()

    # Presence-based cancel/refund of pending paid items
    presence_refund: PresenceRefundConfig = PresenceRefundConfig()

    # Promo insertion system
    promos: PromoConfig = PromoConfig()

    # Database
    db_path: str = "/var/lib/kryten-webqueue/webqueue.db"

    # Images
    image_dir: str = "/var/lib/kryten-webqueue/images"
    placeholder_dir: str = "/var/lib/kryten-webqueue/images/placeholders"

    # Scheduling
    catalog_sync_interval_hours: int = 4
    pre_fire_lock_minutes_default: int = 15
    state_poll_interval_sec: float = 3.0

    # Bulk playlist loading (manual import + scheduled fire). CyTube validates
    # each queued item server-side (fetching custom manifests); adding faster
    # than it can validate triggers a transient queueFail (surfaced by api-gate
    # as HTTP 422). Throttle consecutive adds and retry the transient 422.
    playlist_bulk_add_delay_sec: float = 0.5   # pause between consecutive adds
    playlist_bulk_add_max_retries: int = 2     # retries on transient 422

    # Monitoring
    prometheus_port: int = 28292

    @classmethod
    def from_file(cls, path: str | Path) -> "Config":
        with open(path, encoding="utf-8") as f:
            cfg = cls(**json.load(f))
        cfg._source_path = Path(path)
        return cfg

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
