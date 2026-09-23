# SPEC — Sortie 1: Database Config + SQLAlchemy 2.0 Async Engine

**Sprint**: `postgres-migration`
**PRD**: [PRD-postgres-migration.md](PRD-postgres-migration.md)
**Depends on**: none
**Estimated**: 2–4 h

---

## 1. Overview

Add the Postgres backend selector, DSN resolution, and **SQLAlchemy 2.0 (`asyncpg`)** engine and session factory for the unified `webqueue` database on `chandra-1`, supporting both partitioned SQLite (from Part 1) and PostgreSQL backends.

## 2. Scope and Non-Goals

**In scope**: configuration models, DSN resolution, SQLAlchemy `AsyncEngine` and `async_sessionmaker` factory, example configuration updates, dependency additions (`sqlalchemy>=2.0.30`, `asyncpg>=0.29.0`, `greenlet>=3.0`).
**Non-goals**: no repository port (Sortie 2), no FTS change (Sortie 3), no TMDB schema port (Sortie 4), no ETL (Sortie 5).

## 3. Requirements

- `database.backend: sqlite | postgres` (default `sqlite` during development/transitional phase).
- `database.postgres`: `dsn_env`/password-free `dsn`/`host`/`port`/`user`/`dbname`/`password_env`/`pool_size`/`max_overflow`.
- Single database `dbname = webqueue` hosting all logical schemas (`catalog`, `queue`, `jobs`, `users`, `tmdb`).
- DSN precedence: `dsn_env` → password-free `dsn` plus `password_env` → assembled URL using `password_env`. No secrets, password field, or password-bearing DSN is permitted in `config.json`.

## 4. Design

```python
# kryten_webqueue/config.py
class PostgresConfig(BaseModel):
    dsn_env: str | None = None
    dsn: str | None = None
    host: str = "host.containers.internal"
    port: int = 5432
    user: str = "kryten"
    dbname: str = "webqueue"
    password_env: str | None = "KRYTEN_WEBQUEUE_PG_PASSWORD"
    pool_size: int = 10
    max_overflow: int = 20

class DatabaseConfig(BaseModel):
    backend: Literal["sqlite", "postgres"] = "sqlite"
    sqlite_layout: Literal["monolith", "partitioned"] = "monolith"
    data_dir: str = "./data"                  # for partitioned sqlite
    postgres: PostgresConfig = PostgresConfig()
```

```python
# kryten_webqueue/catalog/db/engine.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

def create_pg_engine(config: PostgresConfig):
    url = config.get_async_url()  # postgresql+asyncpg://...
    engine = create_async_engine(
        url,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return engine, session_factory
```

The runtime container resolves `host.containers.internal` through an explicit rootful Quadlet
host-gateway mapping. This is not a container-localhost connection. Configuration validation
rejects password-bearing `dsn` values and ambiguous SQLite layouts before startup.

## 5. Implementation Plan

- **Modify** `config.py`: add `PostgresConfig`, `DatabaseConfig` with `postgresql+asyncpg` URL builder.
- **Create** `kryten_webqueue/catalog/db/engine.py`.
- **Add dependencies** in `pyproject.toml`: `sqlalchemy>=2.0.30`, `asyncpg>=0.29.0`, `greenlet>=3.0`.
- **Modify** `config.example.json`: document Postgres connection block with `KRYTEN_WEBQUEUE_PG_PASSWORD` environment placeholder.

## 6. Testing Strategy

- Unit tests for `PostgresConfig.get_async_url()` testing precedence order, environment-only password substitution, and rejection of password-bearing file DSNs.
- Configuration loading tests for default SQLite and PostgreSQL configurations.

## 7. Acceptance Criteria

- [ ] Backend selectable; supports `sqlite` and `postgres`.
- [ ] SQLAlchemy 2.0 `AsyncEngine` initialized with connection pooling.
- [ ] Environment variable secret resolution verified without leaks.
- [ ] SQLite layout validation prevents an existing monolith from being silently replaced by empty partitioned files.
- [ ] Dependencies installed and all checks green (`black`, `ruff`, `mypy`, `pytest`).
