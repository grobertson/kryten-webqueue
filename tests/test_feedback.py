"""Feedback + movie-title suggestion feature (webqueue).

Covers three layers, matching the repo's fixture + direct-call test style:
  * DB:        feedback/suggestion CRUD and catalog title matching.
  * Resolver:  CoverArtResolver.search_titles candidate parsing/dedup.
  * Routes:    the user + admin handlers called directly with a fake Request.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.catalog.images import CoverArtResolver
from kryten_webqueue.auth.rate_limit import RateLimiter
from kryten_webqueue.routes.feedback import (
    FeedbackSubmit,
    SuggestResolve,
    SuggestSubmit,
    SuggestChoice,
    submit_feedback,
    resolve_suggestion,
    submit_suggestion,
)
from kryten_webqueue.routes.admin_feedback import (
    StatusUpdate,
    list_feedback as admin_list_feedback,
    set_feedback_status as admin_set_feedback_status,
    delete_feedback as admin_delete_feedback,
    list_suggestions as admin_list_suggestions,
    set_suggestion_status as admin_set_suggestion_status,
    delete_suggestion as admin_delete_suggestion,
)

MEDIACMS = "https://www.dropsugar.com"
ADMIN = {"username": "admin", "rank": 3}
USER = {"username": "Bob"}


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "feedback.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


async def _add_catalog(db, token, title):
    await db.insert_catalog(
        {
            "friendly_token": token,
            "title": title,
            "description": "",
            "duration_sec": 600,
            "manifest_url": f"{MEDIACMS}/api/v1/media/cytube/{token}.json?format=json",
            "thumbnail_url": "",
            "synced_at": "2026-01-01T00:00:00+00:00",
        }
    )


def _request(**state):
    """Minimal stand-in for a FastAPI Request exposing app.state.<attr>."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def _lenient_limiter():
    return RateLimiter(max_requests=100, window_seconds=300)


class _FakeCover:
    """Stub cover-art resolver returning preset suggestion candidates."""

    def __init__(self, candidates):
        self._candidates = candidates

    async def search_titles(self, query, *, limit=8):
        return [dict(c) for c in self._candidates]


# ── DB: feedback ────────────────────────────────────────────────────────────


async def test_feedback_crud(db):
    fid = await db.add_feedback(username="Bob", body="Love the channel!")
    assert isinstance(fid, int)

    items = await db.list_feedback()
    assert len(items) == 1
    assert items[0]["username"] == "Bob"
    assert items[0]["body"] == "Love the channel!"
    assert items[0]["status"] == "new"

    assert await db.count_feedback() == 1
    assert await db.count_feedback(status="new") == 1
    assert await db.count_feedback(status="read") == 0

    assert await db.set_feedback_status(fid, "read") is True
    assert (await db.list_feedback(status="read"))[0]["id"] == fid
    assert await db.count_feedback(status="new") == 0

    # Unknown id is a no-op (False), known id deletes (True).
    assert await db.set_feedback_status(99999, "read") is False
    assert await db.delete_feedback(fid) is True
    assert await db.delete_feedback(fid) is False
    assert await db.count_feedback() == 0


async def test_feedback_list_ordering_newest_first(db):
    first = await db.add_feedback(username="a", body="1")
    second = await db.add_feedback(username="b", body="2")
    items = await db.list_feedback()
    assert [i["id"] for i in items] == [second, first]


# ── DB: suggestions ─────────────────────────────────────────────────────────


async def test_title_suggestion_crud(db):
    sid = await db.add_title_suggestion(
        username="Bob",
        query="the matrix",
        resolved_title="The Matrix",
        resolved_year="1999",
        resolved_source="tmdb",
        resolved_id="603",
        poster_url="http://x/p.jpg",
        resolution="resolved",
    )
    rows = await db.list_title_suggestions()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == sid
    assert row["resolved_title"] == "The Matrix"
    assert row["resolved_year"] == "1999"
    assert row["resolved_source"] == "tmdb"
    assert row["resolution"] == "resolved"
    assert row["status"] == "new"

    assert await db.count_title_suggestions(status="new") == 1
    assert await db.set_title_suggestion_status(sid, "read") is True
    assert await db.count_title_suggestions(status="new") == 0
    assert await db.delete_title_suggestion(sid) is True
    assert await db.delete_title_suggestion(sid) is False


async def test_unresolved_suggestion_defaults(db):
    sid = await db.add_title_suggestion(username="Bob", query="some obscure film")
    row = (await db.list_title_suggestions())[0]
    assert row["id"] == sid
    assert row["resolution"] == "unresolved"
    assert row["resolved_title"] is None
    assert row["catalog_token"] is None


# ── DB: catalog title matching ──────────────────────────────────────────────


async def test_find_catalog_by_title_matches_despite_year(db):
    await _add_catalog(db, "tokmatrix", "The Matrix (1999)")
    match = await db.find_catalog_by_title("The Matrix")
    assert match is not None
    assert match["friendly_token"] == "tokmatrix"


async def test_find_catalog_by_title_no_false_positive(db):
    await _add_catalog(db, "tokmatrix", "The Matrix (1999)")
    assert await db.find_catalog_by_title("Inception") is None
    # A loose token overlap must not register as 'already have'.
    assert await db.find_catalog_by_title("Matrix Reloaded") is None


async def test_find_catalog_by_title_handles_punctuation(db):
    # Punctuation in the query must not break the FTS phrase query.
    await _add_catalog(db, "tokwall", "WALL-E (2008)")
    match = await db.find_catalog_by_title('WALL-E "special"')
    assert match is None or match["friendly_token"] == "tokwall"


# ── Resolver: search_titles ─────────────────────────────────────────────────


def _make_resolver(tmp_path, *, tmdb="", omdb=""):
    return CoverArtResolver(
        image_dir=str(tmp_path / "img"),
        placeholder_dir=str(tmp_path / "img" / "ph"),
        tmdb_api_key=tmdb,
        omdb_api_key=omdb,
    )


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _router(url, params=None):
    if "search/movie" in url:
        return _FakeResp(
            {
                "results": [
                    {
                        "id": 603,
                        "title": "The Matrix",
                        "release_date": "1999-03-30",
                        "poster_path": "/matrix.jpg",
                        "popularity": 80.0,
                    },
                ]
            }
        )
    if "search/tv" in url:
        return _FakeResp({"results": []})
    if "omdbapi" in url:
        return _FakeResp(
            {
                "Search": [
                    {
                        "Title": "The Matrix",
                        "Year": "1999",
                        "imdbID": "tt0133093",
                        "Poster": "https://img/omdb.jpg",
                        "Type": "movie",
                    },
                    {
                        "Title": "The Matrix Reloaded",
                        "Year": "2003",
                        "imdbID": "tt0234215",
                        "Poster": "N/A",
                        "Type": "movie",
                    },
                ]
            }
        )
    return _FakeResp({}, status_code=404)


class _FakeClient:
    def __init__(self, router):
        self._router = router

    async def get(self, url, params=None):
        return self._router(url, params)

    async def aclose(self):
        pass


async def test_search_titles_no_keys_returns_empty(tmp_path):
    resolver = _make_resolver(tmp_path)
    resolver._client = _FakeClient(_router)
    assert await resolver.search_titles("anything") == []
    await resolver.close()


async def test_search_titles_dedupes_tmdb_and_omdb(tmp_path):
    resolver = _make_resolver(tmp_path, tmdb="k", omdb="k2")
    resolver._client = _FakeClient(_router)
    results = await resolver.search_titles("the matrix")
    # TMDB + OMDB both return "The Matrix (1999)" -> deduped to one, TMDB wins
    # (higher popularity); OMDB's "Reloaded" remains as a separate candidate.
    titles = [(c["title"], c["year"], c["source"]) for c in results]
    assert ("The Matrix", "1999", "tmdb") in titles
    assert sum(1 for c in results if c["title"] == "The Matrix") == 1
    matrix = next(c for c in results if c["title"] == "The Matrix")
    assert matrix["poster_url"].startswith("https://image.tmdb.org/t/p/")
    assert "_popularity" not in matrix  # internal field stripped
    await resolver.close()


async def test_search_titles_omdb_only(tmp_path):
    resolver = _make_resolver(tmp_path, omdb="k2")
    resolver._client = _FakeClient(_router)
    results = await resolver.search_titles("the matrix")
    assert all(c["source"] == "omdb" for c in results)
    # OMDB poster "N/A" is normalized to None.
    reloaded = next(c for c in results if c["title"] == "The Matrix Reloaded")
    assert reloaded["poster_url"] is None
    await resolver.close()


# ── User routes: feedback ───────────────────────────────────────────────────


async def test_submit_feedback_records_and_thanks(db):
    req = _request(db=db, feedback_rate_limiter=_lenient_limiter())
    res = await submit_feedback(FeedbackSubmit(body="  Great stuff!  "), req, user=USER)
    assert res["success"] is True
    assert "Bob" in res["message"]
    assert "Channel-Z" in res["message"]
    items = await db.list_feedback()
    assert items[0]["body"] == "Great stuff!"  # trimmed
    assert items[0]["username"] == "Bob"


async def test_submit_feedback_rejects_empty(db):
    req = _request(db=db, feedback_rate_limiter=_lenient_limiter())
    with pytest.raises(HTTPException) as ei:
        await submit_feedback(FeedbackSubmit(body="   "), req, user=USER)
    assert ei.value.status_code == 400
    assert await db.count_feedback() == 0


async def test_submit_feedback_rate_limited(db):
    req = _request(
        db=db, feedback_rate_limiter=RateLimiter(max_requests=1, window_seconds=300)
    )
    await submit_feedback(FeedbackSubmit(body="one"), req, user=USER)
    with pytest.raises(HTTPException) as ei:
        await submit_feedback(FeedbackSubmit(body="two"), req, user=USER)
    assert ei.value.status_code == 429


# ── User routes: suggestions ────────────────────────────────────────────────


async def test_resolve_suggestion_flags_already_owned(db):
    await _add_catalog(db, "tokmatrix", "The Matrix (1999)")
    cover = _FakeCover(
        [
            {
                "source": "tmdb",
                "id": "603",
                "title": "The Matrix",
                "year": "1999",
                "media_type": "movie",
                "poster_url": "http://x",
            },
            {
                "source": "tmdb",
                "id": "1",
                "title": "Brand New Film",
                "year": "2026",
                "media_type": "movie",
                "poster_url": None,
            },
        ]
    )
    req = _request(db=db, cover_art=cover, feedback_rate_limiter=_lenient_limiter())
    res = await resolve_suggestion(SuggestResolve(query="matrix"), req, user=USER)
    owned = next(c for c in res["candidates"] if c["title"] == "The Matrix")
    fresh = next(c for c in res["candidates"] if c["title"] == "Brand New Film")
    assert owned["catalog_token"] == "tokmatrix"
    assert "catalog_token" not in fresh


async def test_submit_suggestion_already_have(db):
    await _add_catalog(db, "tokmatrix", "The Matrix (1999)")
    req = _request(db=db, feedback_rate_limiter=_lenient_limiter())
    res = await submit_suggestion(
        SuggestSubmit(
            query="matrix",
            choice=SuggestChoice(
                source="tmdb", id="603", title="The Matrix", year="1999"
            ),
        ),
        req,
        user=USER,
    )
    assert res["resolution"] == "already_have"
    assert res["catalog_token"] == "tokmatrix"
    row = (await db.list_title_suggestions())[0]
    assert row["resolution"] == "already_have"
    assert row["catalog_token"] == "tokmatrix"
    assert row["resolved_title"] == "The Matrix"


async def test_submit_suggestion_resolved_new_title(db):
    req = _request(db=db, feedback_rate_limiter=_lenient_limiter())
    res = await submit_suggestion(
        SuggestSubmit(
            query="inception",
            choice=SuggestChoice(
                source="tmdb", id="27205", title="Inception", year="2010"
            ),
        ),
        req,
        user=USER,
    )
    assert res["resolution"] == "resolved"
    row = (await db.list_title_suggestions())[0]
    assert row["resolution"] == "resolved"
    assert row["resolved_title"] == "Inception"
    assert row["catalog_token"] is None


async def test_submit_suggestion_unresolved(db):
    req = _request(db=db, feedback_rate_limiter=_lenient_limiter())
    res = await submit_suggestion(
        SuggestSubmit(query="a film nobody has", unresolved=True), req, user=USER
    )
    assert res["resolution"] == "unresolved"
    row = (await db.list_title_suggestions())[0]
    assert row["resolution"] == "unresolved"
    assert row["resolved_title"] is None


async def test_submit_suggestion_empty_choice_is_unresolved(db):
    # A choice with a blank title falls back to the unresolved path.
    req = _request(db=db, feedback_rate_limiter=_lenient_limiter())
    res = await submit_suggestion(
        SuggestSubmit(query="mystery", choice=SuggestChoice(title="   ")),
        req,
        user=USER,
    )
    assert res["resolution"] == "unresolved"


async def test_submit_suggestion_sanitizes_bad_source(db):
    # An unknown source from a tampered client is dropped to NULL.
    req = _request(db=db, feedback_rate_limiter=_lenient_limiter())
    await submit_suggestion(
        SuggestSubmit(
            query="x",
            choice=SuggestChoice(
                source="evil", id="1", title="Some New Movie", year="2020"
            ),
        ),
        req,
        user=USER,
    )
    row = (await db.list_title_suggestions())[0]
    assert row["resolved_source"] is None
    assert row["resolution"] == "resolved"


# ── Admin routes ────────────────────────────────────────────────────────────


async def test_admin_feedback_list_mark_delete(db):
    fid = await db.add_feedback(username="Bob", body="hello")
    req = _request(db=db)

    listing = await admin_list_feedback(req, status=None, user=ADMIN)
    assert listing["counts"]["new"] == 1
    assert listing["items"][0]["body"] == "hello"

    res = await admin_set_feedback_status(
        fid, StatusUpdate(status="read"), req, user=ADMIN
    )
    assert res["success"] is True
    assert (await db.list_feedback())[0]["status"] == "read"

    with pytest.raises(HTTPException) as ei:
        await admin_set_feedback_status(
            fid, StatusUpdate(status="bogus"), req, user=ADMIN
        )
    assert ei.value.status_code == 400

    with pytest.raises(HTTPException) as ei:
        await admin_set_feedback_status(
            99999, StatusUpdate(status="read"), req, user=ADMIN
        )
    assert ei.value.status_code == 404

    assert (await admin_delete_feedback(fid, req, user=ADMIN))["success"] is True
    with pytest.raises(HTTPException) as ei:
        await admin_delete_feedback(fid, req, user=ADMIN)
    assert ei.value.status_code == 404


async def test_admin_feedback_status_filter(db):
    a = await db.add_feedback(username="a", body="1")
    await db.add_feedback(username="b", body="2")
    await db.set_feedback_status(a, "read")
    req = _request(db=db)
    new_only = await admin_list_feedback(req, status="new", user=ADMIN)
    assert len(new_only["items"]) == 1
    assert new_only["items"][0]["username"] == "b"
    # An invalid status filter is ignored (returns all).
    all_items = await admin_list_feedback(req, status="bogus", user=ADMIN)
    assert len(all_items["items"]) == 2


async def test_admin_suggestion_list_mark_delete(db):
    sid = await db.add_title_suggestion(username="Bob", query="the matrix")
    req = _request(db=db)

    listing = await admin_list_suggestions(req, status=None, user=ADMIN)
    assert listing["counts"]["new"] == 1
    assert listing["items"][0]["query"] == "the matrix"

    assert (
        await admin_set_suggestion_status(
            sid, StatusUpdate(status="read"), req, user=ADMIN
        )
    )["success"]
    assert (await db.list_title_suggestions())[0]["status"] == "read"

    with pytest.raises(HTTPException) as ei:
        await admin_set_suggestion_status(
            99999, StatusUpdate(status="read"), req, user=ADMIN
        )
    assert ei.value.status_code == 404

    assert (await admin_delete_suggestion(sid, req, user=ADMIN))["success"] is True
    with pytest.raises(HTTPException) as ei:
        await admin_delete_suggestion(sid, req, user=ADMIN)
    assert ei.value.status_code == 404
