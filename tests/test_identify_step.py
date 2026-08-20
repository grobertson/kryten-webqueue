"""Tests for the identify enrichment step and identity-first art/meta wiring."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.catalog.enrichment.classify import ItemClassification
from kryten_webqueue.catalog.enrichment.providers import MovieMetadata
from kryten_webqueue.catalog.enrichment.steps.art import ArtStep
from kryten_webqueue.catalog.enrichment.steps.identify import IdentifyStep
from kryten_webqueue.catalog.enrichment.steps.meta import MetaStep
from kryten_webqueue.catalog.tmdb_index import ResolveResult

MEDIACMS = "https://www.dropsugar.com"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "identify.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


async def _add_item(
    db: Database, token: str, title: str, description: str = ""
) -> None:
    await db.insert_catalog(
        {
            "friendly_token": token,
            "title": title,
            "description": description,
            "duration_sec": 7200,
            "manifest_url": f"{MEDIACMS}/api/v1/media/cytube/{token}.json?format=json",
            "thumbnail_url": "",
            "synced_at": "2026-01-01T00:00:00+00:00",
        }
    )


def _cls(token: str, **kw) -> ItemClassification:
    base = dict(
        friendly_token=token,
        raw_title=kw.get("raw_title", "Some Movie"),
        content_type=kw.get("content_type", "movie"),
        hosted=None,
        lookup_title=kw.get("lookup_title", "Some Movie"),
        lookup_year=kw.get("lookup_year"),
    )
    for k in ("description", "source_url", "imdb_tt", "tmdb_id"):
        if k in kw:
            base[k] = kw[k]
    return ItemClassification(**base)


class FakeIndex:
    def __init__(self, result: ResolveResult | None = None):
        self._result = result

    async def resolve(self, title, year=None, kind="movie", *, original_title=None):
        return self._result

    async def close(self):
        pass


class FakeTMDB:
    def __init__(self, **meta):
        self._meta = meta
        self.search_movie_calls = 0
        self.by_imdb_calls: list[str] = []
        self.by_tmdb_calls: list[int] = []

    async def search_by_imdb_id(self, imdb_id):
        self.by_imdb_calls.append(imdb_id)
        return MovieMetadata(**self._meta)

    async def fetch_by_tmdb_id(self, tmdb_id):
        self.by_tmdb_calls.append(tmdb_id)
        return MovieMetadata(**self._meta)

    async def search_movie(self, title, year=None):
        self.search_movie_calls += 1
        return MovieMetadata(**self._meta)

    async def close(self):
        pass


def _make_identify(db, *, index=None, tmdb=None) -> IdentifyStep:
    cfg = SimpleNamespace(tmdb_api_key="", tmdb_index_path="/nonexistent.db")
    step = IdentifyStep(db=db, config=cfg)
    if index is not None:
        step._index = index
    if tmdb is not None:
        step._tmdb = tmdb
    return step


# --------------------------------------------------------------------------- #
# IdentifyStep waterfall
# --------------------------------------------------------------------------- #


async def test_scraped_tt_from_description_promotes(db):
    await _add_item(db, "yt01", "FULL MOVIE HD 1080p", "watch imdb.com/title/tt0088247")
    tmdb = FakeTMDB(tmdb_id="218", imdb_id="tt0088247", synopsis="x")
    step = _make_identify(db, index=FakeIndex(None), tmdb=tmdb)
    try:
        cls = _cls(
            "yt01",
            raw_title="FULL MOVIE HD 1080p",
            description="watch imdb.com/title/tt0088247",
        )
        r = await step.run(classifications=[cls])
    finally:
        await step.close()

    assert r.changed == 1
    assert tmdb.by_imdb_calls == ["tt0088247"]
    item = await db.get_item_by_imdb_tt("tt0088247")
    assert item is not None and item["friendly_token"] == "yt01"
    state = await db.get_enrichment_state("yt01")
    assert state["identify_source"] == "scraped_desc"
    assert state["tmdb_id"] == "218"


async def test_english_title_exact_promotes(db):
    await _add_item(db, "m01", "The Terminator (1984)")
    idx = FakeIndex(
        ResolveResult(
            tmdb_id=218,
            matched_title="The Terminator",
            popularity=40.0,
            confidence="exact",
            matched_on="title",
        )
    )
    tmdb = FakeTMDB(tmdb_id="218", imdb_id="tt0088247", synopsis="x")
    step = _make_identify(db, index=idx, tmdb=tmdb)
    try:
        cls = _cls("m01", lookup_title="The Terminator", lookup_year="1984")
        r = await step.run(classifications=[cls])
    finally:
        await step.close()

    assert r.changed == 1
    assert tmdb.by_tmdb_calls == [218]
    assert tmdb.search_movie_calls == 0
    state = await db.get_enrichment_state("m01")
    assert state["identify_source"] == "english_title"
    item = await db.get_item_by_imdb_tt("tt0088247")
    assert item["friendly_token"] == "m01"


async def test_tt_collision_not_promoted(db):
    await _add_item(db, "owner", "First")
    await db.set_imdb_tt("owner", "tt0088247")
    await _add_item(db, "dupe", "Second")
    tmdb = FakeTMDB(tmdb_id="218", imdb_id="tt0088247")
    step = _make_identify(db, index=FakeIndex(None), tmdb=tmdb)
    try:
        cls = _cls("dupe", description="tt0088247")
        await step.run(classifications=[cls])
    finally:
        await step.close()

    # dupe must not steal the tt#
    item = await db.get_item_by_imdb_tt("tt0088247")
    assert item["friendly_token"] == "owner"
    state = await db.get_enrichment_state("dupe")
    assert state["identify_reason"] == "ambiguous"


async def test_low_confidence_not_promoted(db):
    await _add_item(db, "low01", "Blurry Title")
    idx = FakeIndex(
        ResolveResult(
            tmdb_id=999,
            matched_title="Blurred",
            popularity=1.0,
            confidence="low",
            matched_on="title",
        )
    )
    tmdb = FakeTMDB()  # search_movie returns empty MovieMetadata (not found)
    step = _make_identify(db, index=idx, tmdb=tmdb)
    try:
        cls = _cls("low01", lookup_title="Blurry Title")
        r = await step.run(classifications=[cls])
    finally:
        await step.close()

    assert r.skipped == 1
    state = await db.get_enrichment_state("low01")
    assert state["identify_reason"] == "low_confidence"
    assert (
        await db.get_item_by_imdb_tt(None) is None
        or state.get("identify_source") is None
    )


async def test_no_match(db):
    await _add_item(db, "no01", "Unresolvable")
    step = _make_identify(db, index=FakeIndex(None), tmdb=FakeTMDB())
    try:
        cls = _cls("no01", lookup_title="Unresolvable")
        r = await step.run(classifications=[cls])
    finally:
        await step.close()
    assert r.skipped == 1
    state = await db.get_enrichment_state("no01")
    assert state["identify_reason"] == "no_local_match"


async def test_non_movie_skipped(db):
    await _add_item(db, "tv01", "Show S01E01")
    step = _make_identify(db, index=FakeIndex(None), tmdb=FakeTMDB())
    try:
        cls = _cls("tv01", content_type="tv_episode")
        await step.run(classifications=[cls])
    finally:
        await step.close()
    state = await db.get_enrichment_state("tv01")
    assert state["identify_reason"] == "non_movie"


async def test_dry_run_writes_nothing(db):
    await _add_item(db, "d01", "The Terminator")
    idx = FakeIndex(
        ResolveResult(
            tmdb_id=218,
            matched_title="The Terminator",
            popularity=40.0,
            confidence="exact",
            matched_on="title",
        )
    )
    step = _make_identify(db, index=idx, tmdb=FakeTMDB(imdb_id="tt0088247"))
    try:
        cls = _cls("d01", lookup_title="The Terminator")
        await step.run(classifications=[cls], dry_run=True)
    finally:
        await step.close()
    assert await db.get_item_by_imdb_tt("tt0088247") is None
    assert await db.get_enrichment_state("d01") is None


# --------------------------------------------------------------------------- #
# Identity-first art / meta
# --------------------------------------------------------------------------- #


async def test_art_uses_cached_tmdb_id_no_search(db):
    cfg = SimpleNamespace(tmdb_api_key="", omdb_api_key="", image_dir="/tmp/imgs")
    step = ArtStep(db=db, config=cfg)
    tmdb = FakeTMDB(poster_url="http://img/p.jpg")
    step._tmdb = tmdb
    try:
        cls = _cls("a01", tmdb_id="218")
        poster = await step._resolve_poster(cls)
    finally:
        await step.close()
    assert poster == "http://img/p.jpg"
    assert tmdb.by_tmdb_calls == [218]
    assert tmdb.search_movie_calls == 0


async def test_meta_uses_cached_tmdb_id_no_search(db):
    cfg = SimpleNamespace(
        tmdb_api_key="",
        omdb_api_key="",
        mediacms_url=MEDIACMS,
        mediacms_token="x",
    )
    step = MetaStep(db=db, config=cfg)
    tmdb = FakeTMDB(tmdb_id="218", imdb_id="tt0088247", synopsis="x")

    class OMDBNoop:
        async def search_movie(self, title, year=None, imdb_id=None):
            return MovieMetadata()

        async def close(self):
            pass

    step._tmdb = tmdb
    step._omdb = OMDBNoop()
    try:
        cls = _cls("me01", tmdb_id="218")
        meta = await step._lookup_movie(cls)
    finally:
        await step.close()
    assert tmdb.by_tmdb_calls == [218]
    assert tmdb.search_movie_calls == 0
    assert meta.tmdb_id == "218"


# --------------------------------------------------------------------------- #
# Coverage report
# --------------------------------------------------------------------------- #


async def test_coverage_report_reasons(db):
    from kryten_webqueue.catalog.tmdb_index import build_coverage_report

    await _add_item(db, "res01", "Resolved One")
    await db.save_enrichment_state(
        "res01", last_identify_at="now", identify_reason="resolved", tmdb_id="218"
    )
    await _add_item(db, "amb01", "Ambiguous One")
    await db.save_enrichment_state(
        "amb01", last_identify_at="now", identify_reason="ambiguous"
    )
    await _add_item(db, "new01", "Never Identified")

    report = await build_coverage_report(db)
    assert report.total == 3
    assert report.summary["resolved"] == 1
    assert report.summary["ambiguous"] == 1
    assert report.summary["not_identified"] == 1
    assert sum(report.summary.values()) == report.total
    resolved = {r.friendly_token: r.resolved for r in report.rows}
    assert resolved["res01"] is True
    assert resolved["new01"] is False
