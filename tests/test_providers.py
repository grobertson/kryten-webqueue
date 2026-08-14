"""Tests for providers.py — MovieMetadata, merge_metadata, title similarity."""

from kryten_webqueue.catalog.enrichment.providers import (
    MovieMetadata,
    OMDBProvider,
    _titles_similar,
    merge_metadata,
)


# --- _titles_similar ---


def test_exact_match():
    assert _titles_similar("Aliens", "Aliens")


def test_article_ignored():
    assert _titles_similar("The Terminator", "Terminator")


def test_punctuation_ignored():
    assert _titles_similar("C.H.U.D.", "CHUD")


def test_clearly_different():
    assert not _titles_similar("Aliens", "Predator 2")


def test_partial_short_query():
    # short query contained in long result should still pass if coverage >= 50%
    assert _titles_similar("Alien", "Aliens")


# --- MovieMetadata.found ---


def test_found_with_synopsis():
    m = MovieMetadata(synopsis="A great film.")
    assert m.found


def test_found_with_cast():
    m = MovieMetadata(cast=["Sigourney Weaver"])
    assert m.found


def test_not_found_empty():
    assert not MovieMetadata().found


# --- merge_metadata ---


def test_merge_prefers_tmdb_synopsis_when_longer():
    tmdb = MovieMetadata(synopsis="Long TMDB synopsis " * 5)
    omdb = MovieMetadata(synopsis="Short.")
    merged = merge_metadata(tmdb, omdb)
    assert merged.synopsis == tmdb.synopsis


def test_merge_prefers_omdb_content_rating():
    tmdb = MovieMetadata(content_rating="")
    omdb = MovieMetadata(content_rating="R")
    merged = merge_metadata(tmdb, omdb)
    assert merged.content_rating == "R"


def test_merge_prefers_tmdb_poster():
    tmdb = MovieMetadata(poster_url="https://tmdb.example/poster.jpg")
    omdb = MovieMetadata(poster_url="https://omdb.example/poster.jpg")
    merged = merge_metadata(tmdb, omdb)
    assert merged.poster_url == tmdb.poster_url


def test_merge_falls_back_to_omdb_poster():
    tmdb = MovieMetadata()
    omdb = MovieMetadata(poster_url="https://omdb.example/poster.jpg")
    merged = merge_metadata(tmdb, omdb)
    assert merged.poster_url == omdb.poster_url


def test_merge_ratings_from_omdb():
    tmdb = MovieMetadata(tmdb_rating="8.1/10")
    omdb = MovieMetadata(rotten_tomatoes="94%", imdb_rating="8.3")
    merged = merge_metadata(tmdb, omdb)
    assert merged.rotten_tomatoes == "94%"
    assert merged.imdb_rating == "8.3"
    assert merged.tmdb_rating == "8.1/10"


# --- OMDBProvider._parse ---


def test_omdb_parse_basic():
    data = {
        "Response": "True",
        "Title": "Aliens",
        "Year": "1986",
        "Plot": "A colony is attacked by aliens.",
        "Director": "James Cameron",
        "Actors": "Sigourney Weaver, Michael Biehn",
        "Genre": "Action, Sci-Fi",
        "Rated": "R",
        "imdbRating": "8.3",
        "imdbID": "tt0090605",
        "Poster": "https://example.com/poster.jpg",
        "Ratings": [
            {"Source": "Rotten Tomatoes", "Value": "98%"},
        ],
    }
    meta = OMDBProvider._parse(data)
    assert meta.title == "Aliens"
    assert meta.year == "1986"
    assert meta.director == ["James Cameron"]
    assert "Sigourney Weaver" in meta.cast
    assert "Action" in meta.genres
    assert meta.content_rating == "R"
    assert meta.rotten_tomatoes == "98%"
    assert meta.poster_url == "https://example.com/poster.jpg"
