"""Async TMDB + OMDB metadata providers.

Replaces the sync requests-based providers in enrichmeta.py with async httpx
equivalents.  Both providers share the MovieMetadata dataclass which now
includes poster_url so art and meta steps can share a single provider call.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
OMDB_BASE = "https://www.omdbapi.com/"
_TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# MovieMetadata
# ---------------------------------------------------------------------------


@dataclass
class MovieMetadata:
    title: str = ""
    year: str | None = None
    synopsis: str = ""
    director: list[str] = field(default_factory=list)
    producer: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    content_rating: str = ""
    runtime_min: int | None = None
    tagline: str = ""
    imdb_rating: str = ""
    imdb_id: str = ""
    tmdb_id: str = ""
    rotten_tomatoes: str = ""
    metacritic: str = ""
    tmdb_rating: str = ""
    writer: list[str] = field(default_factory=list)
    cinematographer: list[str] = field(default_factory=list)
    composer: list[str] = field(default_factory=list)
    editor: list[str] = field(default_factory=list)
    special_effects: list[str] = field(default_factory=list)
    studio: list[str] = field(default_factory=list)  # production companies
    poster_url: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.synopsis or self.cast or self.director)


# ---------------------------------------------------------------------------
# Title similarity (port from enrichmeta.py)
# ---------------------------------------------------------------------------

_STRIP_ARTICLES_RE = re.compile(r"\b(?:the|a|an)\b", re.I)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")
_NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
}


def _norm(s: str) -> str:
    s = _STRIP_ARTICLES_RE.sub("", s.lower())
    s = _NON_ALNUM_RE.sub(" ", s)
    return " ".join(_NUMBER_WORDS.get(w, w) for w in s.split())


def _titles_similar(query: str, result: str, threshold: float = 0.50) -> bool:
    a, b = _norm(query), _norm(result)
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b and len(a) / len(b) >= 0.5:
        return True
    if b in a and len(b) / len(a) >= 0.5:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def _tmdb_auth(key: str) -> tuple[dict, dict]:
    if len(key) <= 40 and all(c in "0123456789abcdefABCDEF" for c in key):
        return {}, {"api_key": key}
    return {"Authorization": f"Bearer {key}"}, {}


# ---------------------------------------------------------------------------
# TMDB provider
# ---------------------------------------------------------------------------


class TMDBProvider:
    def __init__(self, api_key: str, *, delay: float = 0.25):
        self._key = api_key
        self._delay = delay
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)

    async def close(self) -> None:
        await self._client.aclose()

    async def search_movie(self, title: str, year: str | None = None) -> MovieMetadata:
        if not self._key:
            return MovieMetadata()
        headers, auth = _tmdb_auth(self._key)
        params: dict = {"query": title, **auth}
        if year:
            params["primary_release_year"] = year
        try:
            resp = await self._client.get(
                f"{TMDB_BASE}/search/movie", headers=headers, params=params
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                movie = self._pick(results, title, year)
                if not movie and year:
                    # retry without year
                    resp2 = await self._client.get(
                        f"{TMDB_BASE}/search/movie",
                        headers=headers,
                        params={"query": title, **auth},
                    )
                    if resp2.status_code == 200:
                        movie = self._pick(resp2.json().get("results", []), title, None)
                if movie:
                    return await self._fetch_details(movie, headers, auth)
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            logger.debug("TMDB search error for %r: %s", title, exc)
        return MovieMetadata()

    async def search_tv_show(self, show: str) -> MovieMetadata:
        """Search for a TV show and return its poster (for episode art)."""
        if not self._key:
            return MovieMetadata()
        headers, auth = _tmdb_auth(self._key)
        try:
            resp = await self._client.get(
                f"{TMDB_BASE}/search/tv",
                headers=headers,
                params={"query": show, **auth},
            )
            if resp.status_code != 200:
                return MovieMetadata()
            results = resp.json().get("results", [])
            if not results:
                return MovieMetadata()
            # Pick the best match by popularity
            tv_show = results[0]
            meta = MovieMetadata()
            meta.title = tv_show.get("name", "")
            meta.synopsis = tv_show.get("overview", "")
            first_air = tv_show.get("first_air_date", "")
            meta.year = first_air[:4] if first_air else None
            if tv_show.get("poster_path"):
                meta.poster_url = f"https://image.tmdb.org/t/p/w780{tv_show['poster_path']}"
            return meta
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            logger.debug("TMDB TV show search error for %r: %s", show, exc)
        return MovieMetadata()

    async def search_tv_episode(
        self, show: str, season: int, episode: int
    ) -> MovieMetadata:
        if not self._key:
            return MovieMetadata()
        headers, auth = _tmdb_auth(self._key)
        try:
            resp = await self._client.get(
                f"{TMDB_BASE}/search/tv",
                headers=headers,
                params={"query": show, **auth},
            )
            if resp.status_code != 200:
                return MovieMetadata()
            results = resp.json().get("results", [])
            if not results:
                return MovieMetadata()
            show_id = results[0]["id"]
            ep_resp = await self._client.get(
                f"{TMDB_BASE}/tv/{show_id}/season/{season}/episode/{episode}",
                headers=headers,
                params=auth,
            )
            if ep_resp.status_code == 200:
                data = ep_resp.json()
                meta = MovieMetadata()
                meta.title = data.get("name", "")
                meta.synopsis = data.get("overview", "")
                air_date = data.get("air_date", "")
                meta.year = air_date[:4] if air_date else None
                meta.cast = [c["name"] for c in data.get("guest_stars", [])[:8]]
                return meta
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            logger.debug(
                "TMDB TV search error for %r S%02dE%02d: %s", show, season, episode, exc
            )
        return MovieMetadata()

    def _pick(self, results: list[dict], query: str, year: str | None) -> dict | None:
        ordered = sorted(
            results,
            key=lambda r: (
                0 if year and (r.get("release_date") or "")[:4] == year else 1,
                -float(r.get("popularity") or 0),
            ),
        )
        for r in ordered:
            if _titles_similar(query, r.get("title", "")):
                return r
        return None

    async def _fetch_details(
        self, movie: dict, headers: dict, auth: dict
    ) -> MovieMetadata:
        meta = MovieMetadata()
        meta.title = movie.get("title", "")
        meta.synopsis = movie.get("overview", "")
        rd = movie.get("release_date", "")
        meta.year = rd[:4] if rd else None
        meta.tmdb_id = str(movie.get("id", ""))
        if movie.get("poster_path"):
            meta.poster_url = f"https://image.tmdb.org/t/p/w780{movie['poster_path']}"

        mid = movie["id"]
        try:
            resp = await self._client.get(
                f"{TMDB_BASE}/movie/{mid}", headers=headers, params=auth
            )
            if resp.status_code == 200:
                d = resp.json()
                meta.genres = [g["name"] for g in d.get("genres", [])]
                meta.runtime_min = d.get("runtime")
                meta.tagline = d.get("tagline", "")
                vote = d.get("vote_average")
                if vote:
                    meta.tmdb_rating = f"{vote:.1f}/10"
                meta.imdb_id = d.get("imdb_id", "")
                meta.studio = [c["name"] for c in d.get("production_companies", [])[:3]]
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            pass

        try:
            resp = await self._client.get(
                f"{TMDB_BASE}/movie/{mid}/credits", headers=headers, params=auth
            )
            if resp.status_code == 200:
                c = resp.json()
                meta.cast = [x["name"] for x in c.get("cast", [])[:10]]
                crew = c.get("crew", [])
                meta.director = [x["name"] for x in crew if x.get("job") == "Director"]
                meta.producer = [x["name"] for x in crew if x.get("job") == "Producer"][
                    :5
                ]
                meta.writer = [
                    x["name"]
                    for x in crew
                    if x.get("job") in ("Writer", "Screenplay", "Story")
                ][:5]
                meta.cinematographer = [
                    x["name"] for x in crew if x.get("job") == "Director of Photography"
                ][:3]
                meta.composer = [
                    x["name"] for x in crew if x.get("job") == "Original Music Composer"
                ][:3]
                meta.editor = [x["name"] for x in crew if x.get("job") == "Editor"][:3]
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            pass

        return meta


# ---------------------------------------------------------------------------
# OMDB provider
# ---------------------------------------------------------------------------


class OMDBProvider:
    def __init__(self, api_key: str, *, delay: float = 0.25):
        self._key = api_key
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)

    async def close(self) -> None:
        await self._client.aclose()

    async def search_movie(
        self,
        title: str,
        year: str | None = None,
        *,
        imdb_id: str | None = None,
    ) -> MovieMetadata:
        if not self._key:
            return MovieMetadata()
        params: dict = {"apikey": self._key}
        if imdb_id:
            params["i"] = imdb_id
        else:
            params["t"] = title
            params["type"] = "movie"
            if year:
                params["y"] = year
        try:
            resp = await self._client.get(OMDB_BASE, params=params)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("Response") != "True":
                    if year and not imdb_id:
                        # retry without year
                        p2 = {"apikey": self._key, "t": title, "type": "movie"}
                        r2 = await self._client.get(OMDB_BASE, params=p2)
                        if r2.status_code == 200:
                            data = r2.json()
                if data.get("Response") == "True":
                    if not imdb_id and not _titles_similar(
                        title, data.get("Title", "")
                    ):
                        return MovieMetadata()
                    return self._parse(data)
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            logger.debug("OMDB error for %r: %s", title, exc)
        return MovieMetadata()

    async def search_tv_show(self, show: str) -> MovieMetadata:
        """Search for a TV show and return its poster (for episode art)."""
        if not self._key:
            return MovieMetadata()
        try:
            resp = await self._client.get(
                OMDB_BASE,
                params={
                    "apikey": self._key,
                    "t": show,
                    "type": "series",
                },
            )
            if resp.status_code == 200 and resp.json().get("Response") == "True":
                return self._parse(resp.json())
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            pass
        return MovieMetadata()

    async def search_tv_episode(
        self, show: str, season: int, episode: int
    ) -> MovieMetadata:
        if not self._key:
            return MovieMetadata()
        try:
            resp = await self._client.get(
                OMDB_BASE,
                params={
                    "apikey": self._key,
                    "t": show,
                    "Season": season,
                    "Episode": episode,
                },
            )
            if resp.status_code == 200 and resp.json().get("Response") == "True":
                return self._parse(resp.json())
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            pass
        return MovieMetadata()

    @staticmethod
    def _parse(data: dict) -> MovieMetadata:
        meta = MovieMetadata()
        meta.title = data.get("Title", "")
        meta.year = (data.get("Year") or "")[:4] or None
        meta.synopsis = data.get("Plot", "")
        meta.content_rating = data.get("Rated", "")
        meta.imdb_rating = data.get("imdbRating", "")
        meta.imdb_id = data.get("imdbID", "")
        if data.get("Director") and data["Director"] != "N/A":
            meta.director = [d.strip() for d in data["Director"].split(",")]
        if data.get("Actors") and data["Actors"] != "N/A":
            meta.cast = [a.strip() for a in data["Actors"].split(",")]
        if data.get("Genre") and data["Genre"] != "N/A":
            meta.genres = [g.strip() for g in data["Genre"].split(",")]
        if data.get("Writer") and data["Writer"] != "N/A":
            meta.writer = [w.strip() for w in data["Writer"].split(",")][:5]
        if data.get("Production") and data["Production"] != "N/A":
            meta.studio = [data["Production"].strip()]
        for r in data.get("Ratings", []):
            src, val = r.get("Source", ""), r.get("Value", "")
            if "Rotten Tomatoes" in src:
                meta.rotten_tomatoes = val
            elif "Metacritic" in src:
                meta.metacritic = val
        ms = data.get("Metascore", "")
        if ms and ms != "N/A" and not meta.metacritic:
            meta.metacritic = f"{ms}/100"
        if data.get("Poster") and data["Poster"] != "N/A":
            meta.poster_url = data["Poster"]
        return meta


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def merge_metadata(tmdb: MovieMetadata, omdb: MovieMetadata) -> MovieMetadata:
    """Combine TMDB (deep credits) with OMDB (ratings, MPAA)."""
    m = MovieMetadata()
    m.title = tmdb.title or omdb.title
    m.year = tmdb.year or omdb.year
    m.synopsis = (
        tmdb.synopsis if len(tmdb.synopsis) >= len(omdb.synopsis) else omdb.synopsis
    )
    m.director = tmdb.director or omdb.director
    m.producer = tmdb.producer
    m.cast = tmdb.cast if len(tmdb.cast) >= len(omdb.cast) else omdb.cast
    m.genres = tmdb.genres or omdb.genres
    m.writer = tmdb.writer or omdb.writer
    m.cinematographer = tmdb.cinematographer
    m.composer = tmdb.composer
    m.editor = tmdb.editor
    m.special_effects = tmdb.special_effects
    m.studio = tmdb.studio or omdb.studio
    m.content_rating = omdb.content_rating
    m.imdb_rating = omdb.imdb_rating or tmdb.imdb_rating
    m.imdb_id = omdb.imdb_id or tmdb.imdb_id
    m.tmdb_id = tmdb.tmdb_id
    m.rotten_tomatoes = omdb.rotten_tomatoes
    m.metacritic = omdb.metacritic
    m.tmdb_rating = tmdb.tmdb_rating
    m.runtime_min = tmdb.runtime_min
    m.tagline = tmdb.tagline
    # prefer TMDB poster (higher resolution)
    m.poster_url = tmdb.poster_url or omdb.poster_url
    return m
