from pathlib import Path
from pydantic import BaseModel
import json


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
