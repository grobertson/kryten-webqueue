"""Tests for the local TMDB index: builder, resolver, and tt# scraper."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kryten_webqueue.catalog.tmdb_index import (
    TMDBLocalIndex,
    build_index,
    extract_imdb_tt,
    parse_kinds,
)
from kryten_webqueue.catalog.tmdb_index.builder import _source_date_from


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


@pytest.fixture()
def dump_dir(tmp_path: Path) -> Path:
    d = tmp_path / "dumps"
    d.mkdir()
    _write_jsonl(
        d / "movie_ids_08_19_2026.json",
        [
            {
                "id": 218,
                "original_title": "The Terminator",
                "popularity": 40.0,
                "adult": False,
            },
            {
                "id": 8077,
                "original_title": "The Thing",
                "popularity": 12.0,
                "adult": False,
            },
            {
                "id": 1091,
                "original_title": "The Thing",
                "popularity": 30.0,
                "adult": False,
            },
            {"id": 807, "original_title": "Se7en", "popularity": 55.0, "adult": False},
            {
                "id": 2,
                "original_title": "L'Amour à vingt ans",
                "popularity": 1.4,
                "adult": False,
            },
        ],
    )
    _write_jsonl(
        d / "tv_series_ids_08_19_2026.json",
        [{"id": 2, "original_name": "Clerks", "popularity": 5.3}],
    )
    _write_jsonl(
        d / "keyword_ids_08_19_2026.json",
        [{"id": 378, "name": "prison"}],
    )
    return d


@pytest.fixture()
def index_path(dump_dir: Path, tmp_path: Path) -> Path:
    idx = tmp_path / "tmdb_index.db"
    build_index(dump_dir, idx, kinds="all")
    return idx


# --------------------------------------------------------------------------- #
# extract_imdb_tt
# --------------------------------------------------------------------------- #


def test_extract_tt_from_url():
    assert extract_imdb_tt("see https://www.imdb.com/title/tt0083658/") == "tt0083658"


def test_extract_tt_bare():
    assert extract_imdb_tt("full movie tt0088247 uploaded") == "tt0088247"


def test_extract_tt_url_preferred_over_bare():
    text = "tt1111111 imdb.com/title/tt0083658"
    assert extract_imdb_tt(text) == "tt0083658"


def test_extract_tt_rejects_glued_token():
    assert extract_imdb_tt("button tt12x") is None
    assert extract_imdb_tt("xtt0083658") is None


def test_extract_tt_none():
    assert extract_imdb_tt("no id here", None, "") is None


def test_extract_tt_multiple_texts_first_wins():
    assert extract_imdb_tt(None, "nothing", "tt0083658") == "tt0083658"


# --------------------------------------------------------------------------- #
# build_index
# --------------------------------------------------------------------------- #


def test_parse_kinds():
    assert parse_kinds("all")[:2] == ["movies", "tv"]
    assert parse_kinds("movies,tv") == ["movies", "tv"]
    with pytest.raises(ValueError):
        parse_kinds("bogus")


def test_source_date_parsed():
    assert _source_date_from(Path("movie_ids_08_19_2026.json")) == "2026-08-19"


def test_build_index_counts_and_norm(index_path: Path):
    conn = sqlite3.connect(index_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM tv").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] == 1
        # norm_title drops the article + lowercases
        norm = conn.execute(
            "SELECT norm_title FROM movies WHERE tmdb_id = 218"
        ).fetchone()[0]
        assert norm == "terminator"
        meta = conn.execute(
            "SELECT source_date, counts_json FROM index_meta"
        ).fetchone()
        assert meta[0] == "2026-08-19"
        assert json.loads(meta[1])["movies"] == 5
        # FTS searchable
        rows = conn.execute(
            "SELECT rowid FROM movies_fts WHERE movies_fts MATCH ?", ('"terminator"',)
        ).fetchall()
        assert rows == [(218,)]
    finally:
        conn.close()


def test_build_index_atomic_rebuild(dump_dir: Path, tmp_path: Path):
    idx = tmp_path / "tmdb_index.db"
    build_index(dump_dir, idx, kinds="movies")
    # rebuild over an existing index leaves a valid DB
    build_index(dump_dir, idx, kinds="movies")
    conn = sqlite3.connect(idx)
    try:
        assert conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0] == 5
    finally:
        conn.close()
    assert not idx.with_name(idx.name + ".tmp").exists()


def test_build_index_bad_dir(tmp_path: Path):
    with pytest.raises(ValueError):
        build_index(tmp_path / "nope", tmp_path / "out.db")


# --------------------------------------------------------------------------- #
# TMDBLocalIndex.resolve
# --------------------------------------------------------------------------- #


async def test_resolve_exact(index_path: Path):
    idx = TMDBLocalIndex(index_path)
    try:
        r = await idx.resolve("The Terminator")
        assert r is not None
        assert r.tmdb_id == 218
        assert r.confidence == "exact"
        assert r.matched_on == "title"
    finally:
        await idx.close()


async def test_resolve_article_and_case(index_path: Path):
    idx = TMDBLocalIndex(index_path)
    try:
        r = await idx.resolve("terminator")
        assert r is not None and r.tmdb_id == 218
    finally:
        await idx.close()


async def test_resolve_popularity_tiebreak(index_path: Path):
    idx = TMDBLocalIndex(index_path)
    try:
        # two "The Thing" rows — the more popular (1091) wins
        r = await idx.resolve("The Thing")
        assert r is not None and r.tmdb_id == 1091 and r.confidence == "exact"
    finally:
        await idx.close()


async def test_resolve_fuzzy_typo(index_path: Path):
    idx = TMDBLocalIndex(index_path)
    try:
        r = await idx.resolve("Terminater")
        assert r is not None and r.tmdb_id == 218
        assert r.confidence in ("high", "low")
    finally:
        await idx.close()


async def test_resolve_original_title(index_path: Path):
    idx = TMDBLocalIndex(index_path)
    try:
        r = await idx.resolve("Love at Twenty", original_title="L'Amour à vingt ans")
        assert r is not None and r.tmdb_id == 2
        assert r.matched_on == "original_title"
    finally:
        await idx.close()


async def test_resolve_miss(index_path: Path):
    idx = TMDBLocalIndex(index_path)
    try:
        assert await idx.resolve("Completely Unrelated Nonexistent Film") is None
    finally:
        await idx.close()


async def test_resolve_missing_index(tmp_path: Path):
    idx = TMDBLocalIndex(tmp_path / "does_not_exist.db")
    try:
        assert await idx.resolve("The Terminator") is None
    finally:
        await idx.close()
