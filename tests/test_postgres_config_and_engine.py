"""Unit tests for Sortie 1: PostgreSQL Configuration and SQLAlchemy 2.0 Async Engine."""

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from kryten_webqueue.config import DatabaseConfig, PostgresConfig
from kryten_webqueue.catalog.db.engine import create_pg_engine


def test_postgres_config_defaults():
    cfg = PostgresConfig()
    assert cfg.host == "host.containers.internal"
    assert cfg.port == 5432
    assert cfg.user == "kryten"
    assert cfg.dbname == "webqueue"
    assert cfg.password_env == "KRYTEN_WEBQUEUE_PG_PASSWORD"
    assert cfg.pool_size == 10
    assert cfg.max_overflow == 20


def test_postgres_config_assembled_url(monkeypatch):
    monkeypatch.setenv("KRYTEN_WEBQUEUE_PG_PASSWORD", "super_secret_pw!@#")
    cfg = PostgresConfig(
        host="chandra-1.local",
        port=5433,
        user="custom_user",
        dbname="test_db",
    )
    url = cfg.get_async_url()
    assert url.startswith("postgresql+asyncpg://custom_user:")
    assert "chandra-1.local:5433/test_db" in url
    assert "super_secret_pw%21%40%23" in url


def test_postgres_config_dsn_env_precedence(monkeypatch):
    monkeypatch.setenv(
        "CUSTOM_PG_DSN", "postgresql://app_user:pw@pg-server:5432/app_db"
    )
    cfg = PostgresConfig(dsn_env="CUSTOM_PG_DSN")
    url = cfg.get_async_url()
    assert url == "postgresql+asyncpg://app_user:pw@pg-server:5432/app_db"


def test_postgres_config_asyncpg_dsn_strips_sqlalchemy_suffix(monkeypatch):
    monkeypatch.setenv("KRYTEN_WEBQUEUE_PG_PASSWORD", "super_secret_pw!@#")
    cfg = PostgresConfig(
        host="chandra-1.local",
        port=5433,
        user="custom_user",
        dbname="test_db",
    )
    assert cfg.get_asyncpg_dsn() == (
        "postgresql://custom_user:super_secret_pw%21%40%23@chandra-1.local:5433/test_db"
    )
    assert cfg.get_async_url().startswith("postgresql+asyncpg://custom_user:")


def test_postgres_config_rejects_embedded_password_in_dsn():
    with pytest.raises(
        ValidationError,
        match="Plaintext passwords in 'database.postgres.dsn' are forbidden",
    ):
        PostgresConfig(dsn="postgresql://user:insecure_pw@host:5432/db")


def test_postgres_config_password_free_dsn_with_env(monkeypatch):
    monkeypatch.setenv("KRYTEN_WEBQUEUE_PG_PASSWORD", "env_pw")
    cfg = PostgresConfig(
        dsn="postgresql://myuser@myhost:5432/mydb",
        password_env="KRYTEN_WEBQUEUE_PG_PASSWORD",
    )
    url = cfg.get_async_url()
    assert url == "postgresql+asyncpg://myuser:env_pw@myhost:5432/mydb"


def test_create_pg_engine(monkeypatch):
    monkeypatch.setenv("KRYTEN_WEBQUEUE_PG_PASSWORD", "mock_pass")
    cfg = PostgresConfig(host="localhost", port=5432, dbname="mockdb")
    engine, session_factory = create_pg_engine(cfg)

    assert isinstance(engine, AsyncEngine)
    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "asyncpg"
    assert isinstance(session_factory, async_sessionmaker)


def test_database_config_postgres_backend():
    db_cfg = DatabaseConfig(backend="postgres")
    assert db_cfg.backend == "postgres"
    assert isinstance(db_cfg.postgres, PostgresConfig)


def test_database_initializes_postgres_schema_domains(monkeypatch):
    monkeypatch.setenv("KRYTEN_WEBQUEUE_PG_PASSWORD", "mock_pass")
    cfg = DatabaseConfig(backend="postgres")
    db_module = __import__("kryten_webqueue.catalog.db", fromlist=["Database"])
    db = db_module.Database(cfg)

    assert db.db_config.backend == "postgres"
    assert isinstance(db.catalog, db_module._PgCatalogDB)
    assert isinstance(db.queue, db_module._PgQueueDB)
    assert isinstance(db.jobs, db_module._PgJobsDB)
    assert isinstance(db.users, db_module._PgUsersDB)
    assert db.catalog.domain_name == "catalog"
    assert db.queue.domain_name == "queue"
    assert db.jobs.domain_name == "jobs"
    assert db.users.domain_name == "users"
    assert db.catalog._dsn.startswith("postgresql://")
    # backend=="postgres" must route cross-domain facade methods (browse/search/etc.)
    # through the domain-dispatch path even though layout defaults to "monolith" —
    # regression guard for a bug where only layout=="partitioned" was checked.
    assert db._domain_dispatch is True
