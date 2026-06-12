from pathlib import Path
from pydantic import BaseModel
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


class Config(BaseModel):
    """Application configuration loaded from JSON file."""

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

    # Database
    db_path: str = "/var/lib/kryten-webqueue/webqueue.db"

    # Images
    image_dir: str = "/var/lib/kryten-webqueue/images"
    placeholder_dir: str = "/var/lib/kryten-webqueue/images/placeholders"

    # Scheduling
    catalog_sync_interval_hours: int = 4
    pre_fire_lock_minutes_default: int = 15
    state_poll_interval_sec: float = 3.0

    # Monitoring
    prometheus_port: int = 28292

    @classmethod
    def from_file(cls, path: str | Path) -> "Config":
        with open(path) as f:
            return cls(**json.load(f))
