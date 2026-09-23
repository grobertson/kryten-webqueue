"""SQLAlchemy 2.0 async engine and session factory for PostgreSQL."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ...config import PostgresConfig


def create_pg_engine(
    config: PostgresConfig,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create a SQLAlchemy 2.0 async engine and sessionmaker from PostgresConfig."""
    url = config.get_async_url()
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
