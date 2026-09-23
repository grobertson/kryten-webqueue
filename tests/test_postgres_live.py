"""Integration test suite verifying live PostgreSQL webqueue database on chandra-1."""

import os
import pytest
import asyncpg


CHANDRA_PG_DSN = os.getenv(
    "KRYTEN_WEBQUEUE_PG_DSN",
    "postgresql://kryten:kryten_secret_password@chandra-1.local:5432/webqueue",
)


@pytest.fixture
async def pg_conn():
    try:
        conn = await asyncpg.connect(CHANDRA_PG_DSN, timeout=5)
        yield conn
        await conn.close()
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Chandra-1 PostgreSQL not reachable from this environment: {exc}")


async def test_live_postgres_schemas_and_extensions(pg_conn):
    """Verify webqueue database contains all 5 schemas and pg_trgm."""
    schemas = await pg_conn.fetch(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name IN ('catalog', 'queue', 'jobs', 'users', 'tmdb')"
    )
    found_schemas = {r["schema_name"] for r in schemas}
    assert found_schemas == {"catalog", "queue", "jobs", "users", "tmdb"}

    ext = await pg_conn.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
    assert ext == 1


async def test_live_postgres_table_counts(pg_conn):
    """Verify row counts in live migrated schemas."""
    catalog_count = await pg_conn.fetchval("SELECT count(*) FROM catalog.catalog")
    assert catalog_count >= 9900

    people_count = await pg_conn.fetchval("SELECT count(*) FROM catalog.people")
    assert people_count >= 36000

    job_logs_count = await pg_conn.fetchval("SELECT count(*) FROM jobs.job_run_logs")
    assert job_logs_count >= 500000

    playlists_count = await pg_conn.fetchval(
        "SELECT count(*) FROM queue.saved_playlists"
    )
    assert playlists_count >= 30

    watchlist_count = await pg_conn.fetchval(
        "SELECT count(*) FROM users.user_watchlist"
    )
    assert watchlist_count >= 1700


async def test_live_postgres_full_text_search(pg_conn):
    """Verify tsvector full-text search with GIN index on catalog.catalog."""
    rows = await pg_conn.fetch(
        """
        SELECT friendly_token, title, ts_rank(search_vector, websearch_to_tsquery('english', 'Terminator')) AS rank
        FROM catalog.catalog
        WHERE search_vector @@ websearch_to_tsquery('english', 'Terminator')
        ORDER BY rank DESC
        LIMIT 5;
        """
    )
    assert len(rows) > 0
    titles = [r["title"] for r in rows]
    assert any("Terminator" in t for t in titles)


async def test_live_postgres_trigram_fuzzy_matching(pg_conn):
    """Verify pg_trgm fuzzy matching handles typos like 'Terminatr'."""
    rows = await pg_conn.fetch(
        """
        SELECT friendly_token, title, similarity(title, 'Terminatr') AS sim
        FROM catalog.catalog
        WHERE similarity(title, 'Terminatr') > 0.3
        ORDER BY sim DESC
        LIMIT 5;
        """
    )
    assert len(rows) > 0
    top_title = rows[0]["title"]
    assert "Terminator" in top_title
