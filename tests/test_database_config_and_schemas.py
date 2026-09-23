"""Unit tests for Sortie 1: Schema partitioning definitions and DatabaseConfig validation."""

import pytest
from pathlib import Path
from pydantic import ValidationError

from kryten_webqueue.config import Config, DatabaseConfig
from kryten_webqueue.catalog.db.schemas import (
    CATALOG_MIGRATIONS,
    QUEUE_MIGRATIONS,
    JOBS_MIGRATIONS,
    USERS_MIGRATIONS,
)


def test_schema_migrations_exist_and_cover_domains():
    """Verify each domain has valid migrations defining its expected tables."""
    assert len(CATALOG_MIGRATIONS) >= 2
    assert len(QUEUE_MIGRATIONS) >= 2
    assert len(JOBS_MIGRATIONS) >= 2
    assert len(USERS_MIGRATIONS) >= 2

    catalog_sql = " ".join(CATALOG_MIGRATIONS).lower()
    for table in [
        "_migrations",
        "catalog",
        "catalog_fts",
        "categories",
        "catalog_categories",
        "tags",
        "catalog_tags",
        "people",
        "catalog_people",
        "studios",
        "catalog_studios",
        "item_enrichment_state",
        "item_edit_log",
        "sync_log",
        "motd_overrides",
    ]:
        assert (
            f"create table if not exists {table}" in catalog_sql
            or "using fts5" in catalog_sql
        )

    queue_sql = " ".join(QUEUE_MIGRATIONS).lower()
    for table in [
        "_migrations",
        "queue_shadow",
        "spend_requests",
        "queue_history",
        "saved_playlists",
        "saved_playlist_items",
        "playlist_schedules",
        "active_schedule",
        "play_completions",
        "playlist_item_played",
        "catalog_blackouts",
    ]:
        assert f"create table if not exists {table}" in queue_sql

    jobs_sql = " ".join(JOBS_MIGRATIONS).lower()
    for table in [
        "_migrations",
        "job_runs",
        "job_run_logs",
        "job_schedules",
        "fetch_queue",
    ]:
        assert f"create table if not exists {table}" in jobs_sql

    users_sql = " ".join(USERS_MIGRATIONS).lower()
    for table in [
        "_migrations",
        "otps",
        "device_link_codes",
        "device_api_keys",
        "user_watchlist",
        "feedback",
        "title_suggestions",
    ]:
        assert f"create table if not exists {table}" in users_sql


def test_database_config_default_paths():
    """Default config resolves paths in data_dir."""
    cfg = DatabaseConfig(data_dir="/tmp/test_wq")
    assert cfg.layout == "monolith"
    assert cfg.get_catalog_path() == str(Path("/tmp/test_wq/catalog.sqlite3"))
    assert cfg.get_queue_path() == str(Path("/tmp/test_wq/queue.sqlite3"))
    assert cfg.get_jobs_path() == str(Path("/tmp/test_wq/jobs.sqlite3"))
    assert cfg.get_users_path() == str(Path("/tmp/test_wq/users.sqlite3"))


def test_database_config_explicit_paths_override():
    """Explicit paths override data_dir defaults."""
    cfg = DatabaseConfig(
        data_dir="/tmp/test_wq",
        catalog_db_path="/custom/catalog.db",
        queue_db_path="/custom/queue.db",
        jobs_db_path="/custom/jobs.db",
        users_db_path="/custom/users.db",
    )
    assert cfg.get_catalog_path() == "/custom/catalog.db"
    assert cfg.get_queue_path() == "/custom/queue.db"
    assert cfg.get_jobs_path() == "/custom/jobs.db"
    assert cfg.get_users_path() == "/custom/users.db"


def test_monolith_rejects_empty_db_path():
    """Monolith layout requires non-empty db_path."""
    with pytest.raises(ValidationError, match="requires a non-empty 'db_path'"):
        DatabaseConfig(layout="monolith", db_path="")


def test_partitioned_rejects_silent_bypass_of_existing_monolith(tmp_path):
    """If legacy monolith database exists, switching to partitioned requires partitioned dbs to exist."""
    legacy_db = tmp_path / "webqueue.db"
    legacy_db.write_text("dummy")

    with pytest.raises(ValidationError, match="Legacy monolith database exists"):
        DatabaseConfig(
            layout="partitioned",
            db_path=str(legacy_db),
            data_dir=str(tmp_path / "partitioned_data"),
        )


def test_partitioned_allows_when_catalog_exists(tmp_path):
    """Partitioned layout is allowed when split catalog database already exists."""
    legacy_db = tmp_path / "webqueue.db"
    legacy_db.write_text("dummy")
    part_dir = tmp_path / "partitioned_data"
    part_dir.mkdir()
    (part_dir / "catalog.sqlite3").write_text("dummy")

    cfg = DatabaseConfig(
        layout="partitioned",
        db_path=str(legacy_db),
        data_dir=str(part_dir),
    )
    assert cfg.layout == "partitioned"


def test_config_db_path_sync():
    """Config maintains synchronization between top-level db_path and database.db_path."""
    cfg1 = Config(
        secret_key="secret",
        api_gate_token="gate",
        mediacms_token="cms",
        db_path="/custom/path.db",
    )
    assert cfg1.db_path == "/custom/path.db"
    assert cfg1.database.db_path == "/custom/path.db"

    cfg2 = Config(
        secret_key="secret",
        api_gate_token="gate",
        mediacms_token="cms",
        database={"layout": "monolith", "db_path": "/nested/path.db"},
    )
    assert cfg2.db_path == "/nested/path.db"
    assert cfg2.database.db_path == "/nested/path.db"
