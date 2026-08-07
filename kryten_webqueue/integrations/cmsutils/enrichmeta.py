#!/usr/bin/env python3
# VENDORED from d:\Devel\cmsutils\enrichmeta.py on 2026-06-09.
# Adapted for in-process use by kryten-webqueue jobs: a headless
# run(params, *, config, progress) entry point is appended at the bottom; the
# original CLI main()/argparse path is retained but unused by the service.
# Keep adapters thin so re-vendoring from upstream stays mechanical.
"""
enrichmeta - Enrich movie metadata in a MediaCMS instance.

Scans the catalog for movies (duration >= 1 hour) with missing or low-quality
descriptions, looks up metadata from TMDb and OMDb, and pushes structured
descriptions back to the CMS.

Handles hosted versions (Svengoolie, MonsterVision, The Last Drive-In, etc.)
by extracting the underlying movie title for lookup and noting the host show
in the description.

Architecture
------------
The enrichment pipeline is:

    scan  →  score  →  parse title  →  lookup(providers)  →  format  →  commit

Providers are pluggable: TMDb and OMDb are built-in.  A future AI provider
can be added by implementing the same interface (title+year → metadata dict).

Modes
-----
  --report          Scan & score only — output a table of candidates
  (default)         Dry-run — show what would change, don't touch the CMS
  --commit          Push enriched descriptions to the CMS
  --limit N         Process only the first N candidates
  --min-score N     Only enrich items scoring below N (default: 50)
  --min-duration N  Minimum duration in seconds (default: 3600 = 1 hour)

Examples
--------
  python enrichmeta.py --token CMS_TOKEN --tmdb-key KEY --omdb-key KEY --report
  python enrichmeta.py --token CMS_TOKEN --tmdb-key KEY --omdb-key KEY --limit 10
  python enrichmeta.py --token CMS_TOKEN --tmdb-key KEY --omdb-key KEY --commit --limit 5
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import re
import sys
import time
from dataclasses import dataclass, field

import requests

# ── Defaults ───────────────────────────────────────────────────────────────────

API_BASE = "https://www.dropsugar.co/api/v1"
DEFAULT_TIMEOUT = 30
REQUEST_DELAY = 0.25  # polite delay between API calls
MIN_DURATION = 3600  # 1 hour in seconds
MIN_SCORE_THRESHOLD = 50  # items scoring below this need enrichment
TMDB_BASE = "https://api.themoviedb.org/3"
OMDB_BASE = "http://www.omdbapi.com/"


# ══════════════════════════════════════════════════════════════════════════════
#  Quality scoring  (adapted from yt-pipe QUALITY_MARKERS)
# ══════════════════════════════════════════════════════════════════════════════

QUALITY_MARKERS = [
    "Cast & Crew:",
    "Director(s):",
    "Cast:",
    "Genres:",
    "Content Rating:",
    "Release Year:",
    "Synopsis:",
    "Ratings:",
    "Original URL:",
    "Hosted Version:",
]


def score_description(description: str) -> dict:
    """
    Score an item's description quality.  Higher = richer metadata.

    Scoring:
      - 10 points per quality marker section found
      - Up to 30 points for description length (1 pt per 100 chars, capped)
      - Total possible: ~130

    Returns dict with score, sections_present, sections_missing, description_length.
    """
    description = description or ""
    sections_present = []
    sections_missing = []
    score = 0

    for marker in QUALITY_MARKERS:
        if marker in description:
            sections_present.append(marker)
            score += 10
        else:
            sections_missing.append(marker)

    # Length bonus
    desc_len = len(description)
    score += min(desc_len // 100, 30)

    return {
        "score": score,
        "sections_present": sections_present,
        "sections_missing": sections_missing,
        "description_length": desc_len,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Tubi metadata detection
# ══════════════════════════════════════════════════════════════════════════════

_TUBI_URL_RE = re.compile(r"Original URL:\s*https?://(?:www\.)?tubitv\.com/", re.I)
_TUBI_VIDEO_INFO_RE = re.compile(r"Video Information:", re.I)
_TUBI_ORIG_DESC_RE = re.compile(r"Original Description:", re.I)
_TUBI_RELEASE_YEAR_RE = re.compile(r"Release Year:\s*(\d{4})")


def is_tubi_metadata(description: str) -> bool:
    """Return True if the description carries Tubi-sourced metadata.

    Tubi descriptions are recognisable by their ``Original URL:`` pointing
    at ``tubitv.com``, and/or the ``Original Description:`` + ``Video
    Information:`` structure that the yt-pipe importer writes for Tubi
    downloads.  These items score 50-70 on our quality scale, which is
    high enough to dodge normal enrichment, but they lack Synopsis:,
    Ratings:, and detailed crew data that our TMDb+OMDb enrichment
    provides.
    """
    if not description:
        return False
    # Primary signal: the Original URL is a Tubi link
    if _TUBI_URL_RE.search(description):
        return True
    # Secondary signal: the distinctive Tubi importer structure
    if _TUBI_ORIG_DESC_RE.search(description) and _TUBI_VIDEO_INFO_RE.search(
        description
    ):
        return True
    return False


def _extract_tubi_year(description: str) -> str | None:
    """Pull the release year from Tubi's 'Video Information:' block."""
    m = _TUBI_RELEASE_YEAR_RE.search(description)
    return m.group(1) if m else None


# ══════════════════════════════════════════════════════════════════════════════
#  Hosted-version detection
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class HostedInfo:
    show_name: str  # e.g. "MonsterVision with Joe Bob Briggs"
    movie_title: str  # extracted movie name
    movie_year: str | None  # extracted year or None


# Patterns ordered longest-first to avoid partial matches
_HOSTED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"Joe\s*Bob'?s?\s+Drive[\s-]*In\s+Theater", re.I),
        "Joe Bob's Drive-In Theater",
    ),
    (
        re.compile(r"JBBTLDI|Joe\s*Bob\s+TLDI", re.I),
        "The Last Drive-In with Joe Bob Briggs",
    ),
    (
        re.compile(r"(?:The\s+)?Last\s+Drive[\s\-]*In", re.I),
        "The Last Drive-In with Joe Bob Briggs",
    ),
    (re.compile(r"Monster\s*Vision", re.I), "MonsterVision with Joe Bob Briggs"),
    (re.compile(r"Svengoolie", re.I), "Svengoolie"),
]

# Rifftrax is handled separately from _HOSTED_PATTERNS because its
# filenames use scene-style dot-separated conventions that need the
# heavier cleaning in parse_standard_title rather than detect_hosted.
_RIFFTRAX_RE = re.compile(r"Riff\s*Trax(?:\s+Live)?", re.I)

_EXT_RE = re.compile(r"\.\w{2,5}$")
_YEAR_PAREN_RE = re.compile(r"\((\d{4})\)")
_YEAR_BRACKET_RE = re.compile(r"[\[\]{}]\s*(\d{4})\s*[\[\]{}]")
_YEAR_BARE_RE = re.compile(r"\b((?:19|20)\d{2})\b")

# Scene / quality tags that appear in hosted-version filenames
_SCENE_RE_HOSTED = re.compile(
    r"\[?(?:720p|480p|1080p|who(?:do)?dude\w*|CG|Hybrid)\]?",
    re.I,
)

# Date patterns: MM-DD-YY, MM-DD-YYYY, or YYYY-MM-DD (ISO)
_DATE_RE = re.compile(
    r"(?:(?:19|20)\d{2}-\d{1,2}-\d{1,2})"  # YYYY-MM-DD (ISO)
    r"|"
    r"(?:\d{1,2}-\d{1,2}-(?:19|20)?\d{2})"  # MM-DD-YY or MM-DD-YYYY
)

# Episode numbering: S04e11, W06e02, etc.
_EPISODE_CODE_RE = re.compile(r"[SW]\d+[eE]\d+", re.I)

# TV episode indicators — any of these in a raw title means "skip, it's TV".
# Covers: S01E02, S1E2, s01e02, 1x03, 01x03, Season 1, Episode 3,
#         Ep 3, Ep.3, Ep03, "S01 E02" (space-separated)
_TV_EPISODE_RE = re.compile(
    r"(?:"
    r"[Ss]\d{1,2}\s*[Ee]\d{1,2}"  # S01E02, S1 E2
    r"|\b\d{1,2}[Xx]\d{2,3}\b"  # 1x03, 01x03
    r"|\b[Ss]eason\s*\d+"  # Season 1, Season 02
    r"|\b[Ee]pisode\s*\d+"  # Episode 3, Episode 03
    r"|\b[Ee][Pp]\.?\s*\d+"  # Ep 3, Ep.3, Ep03
    r")"
)

# Show-year in parens at start of remaining text: "(2020) S2-Wk 2 Film 2-"
_SHOW_YEAR_PREFIX_RE = re.compile(
    r"^\s*\(\d{4}\)\s*S\d+-Wk\s*\d+\s*Film\s*\d+\s*[-–—]?\s*",
    re.I,
)

# "Week N - Movie N -" or "S2-Wk 2 Film 1 -"
_WEEK_PREFIX_RE = re.compile(
    r"(?:S\d+-)?(?:Wk|Week)\s*\d+\s*[-–—]?\s*(?:(?:Film|Movie)\s*\d+\s*[-–—]?\s*)?",
    re.I,
)

# Bare leading episode number: "01 ", "01 - ", "02 - "
_LEADING_EPNUM_RE = re.compile(r"^\s*\d{1,2}\s*[-–—]?\s+")


def detect_hosted(raw_title: str) -> HostedInfo | None:
    """
    If the title is a hosted version, return HostedInfo with the host show,
    extracted movie name, and year.  Returns None if not a hosted version.
    """
    title = raw_title.strip()
    title = _EXT_RE.sub("", title)

    host_show = None
    for pattern, show_name in _HOSTED_PATTERNS:
        m = pattern.search(title)
        if m:
            host_show = show_name
            # Remove host show from title
            title = title[: m.start()] + title[m.end() :]
            break

    if host_show is None:
        return None

    # Strip any remaining host-show mentions (handles double-Svengoolie etc.)
    for pattern, _ in _HOSTED_PATTERNS:
        title = pattern.sub("", title)

    # Strip scene tags
    title = _SCENE_RE_HOSTED.sub("", title)

    # Strip leading/trailing underscores and replace internal underscores with spaces
    title = title.replace("_", " ")

    # Strip date stamps
    title = _DATE_RE.sub("", title)

    # Strip show-year + season/week/film prefix
    title = _SHOW_YEAR_PREFIX_RE.sub("", title)

    # Strip episode codes (S04e11, W06e02)
    title = _EPISODE_CODE_RE.sub("", title)

    # Strip week/movie prefixes
    title = _WEEK_PREFIX_RE.sub("", title)

    # Strip leading separators before episode-number check
    title = re.sub(r"^[\s\-\u2013\u2014:,]+", "", title)

    # Strip leading episode numbers (may need two passes for "04 - 01 - Title")
    title = _LEADING_EPNUM_RE.sub("", title)
    title = re.sub(r"^[\s\-\u2013\u2014:,]+", "", title)
    title = _LEADING_EPNUM_RE.sub("", title)

    # Extract year — prefer (YYYY), then [YYYY], then bare YYYY
    year = None
    m = _YEAR_PAREN_RE.search(title)
    if m:
        year = m.group(1)
        title = title[: m.start()] + title[m.end() :]
    else:
        m = _YEAR_BRACKET_RE.search(title)
        if m:
            year = m.group(1)
            title = title[: m.start()] + title[m.end() :]
        else:
            # Bare year — only grab it if it looks like a year, not a movie number
            m = _YEAR_BARE_RE.search(title)
            if m:
                candidate = int(m.group(1))
                if 1920 <= candidate <= 2030:
                    year = m.group(1)
                    title = title[: m.start()] + title[m.end() :]

    # Clean up empty brackets/parens, separators, trailing junk
    title = re.sub(r"\[\s*\]", "", title)
    title = re.sub(r"\(\s*\)", "", title)
    title = re.sub(r"^[\s\-–—:,]+", "", title)
    title = re.sub(r"[\s\-–—:,]+$", "", title)
    title = re.sub(r"\s*-\s*\d+$", "", title)  # trailing "-1" from filenames
    title = re.sub(r"\s+", " ", title).strip()

    return HostedInfo(show_name=host_show, movie_title=title, movie_year=year)


# ══════════════════════════════════════════════════════════════════════════════
#  Title parsing for standard (non-hosted) movies
# ══════════════════════════════════════════════════════════════════════════════

# YouTube playlist pattern:  "Title | English Full Movie | Genre1 Genre2"
_FULL_MOVIE_RE = re.compile(
    r"^(?P<title>.+?)\s*\|\s*"
    r"(?:\w+\s+)?Full\s+Movie\s*"
    r"(?:\|\s*(?P<genres>.+))?$",
    re.I,
)


def parse_youtube_title(
    raw_title: str,
) -> tuple[str, list[str]] | None:
    """Detect YouTube playlist titles like 'Road House | English Full Movie | Action Drama'.

    Returns ``(clean_title, genre_hints)`` or ``None`` if the pattern
    doesn't match.
    """
    m = _FULL_MOVIE_RE.match(raw_title.strip())
    if not m:
        return None
    title = m.group("title").strip()
    genre_str = (m.group("genres") or "").strip()
    genres = genre_str.split() if genre_str else []
    return title, genres


_STD_YEAR_RE = re.compile(r"\((\d{4})\)")

# Scene / quality tags commonly found in downloaded filenames.
_SCENE_TAGS_RE = re.compile(
    r"\b(?:"
    r"480[pi]|720[pi]|1080[pi]|2160[pi]|4[Kk]"
    r"|[Bb]lu[Rr]ay|[Bb][Dd][Rr]ip|[Bb]r[Rr]ip|[Dd][Vv][Dd][Rr]ip|[Hh][Dd][Rr]ip"
    r"|[Ww][Ee][Bb]-?[Dd][Ll]|[Ww][Ee][Bb][Rr]ip|[Dd][Vv][Dd][Ss]cr"
    r"|[Xx]264|[Xx]265|[Hh]\.?264|[Hh]\.?265|[Hh][Ee][Vv][Cc]"
    r"|[Xx][Vv][Ii][Dd]|[Dd][Ii][Vv][Xx]"
    r"|[Aa][Cc]3|[Aa][Aa][Cc]|[Dd][Tt][Ss]|[Ff][Ll][Aa][Cc]|[Mm][Pp]3"
    r"|[Yy][Ii][Ff][Yy]|[Yy][Tt][Ss]|[Ss][Hh][Aa][Nn][Ii][Gg]"
    r"|[Kk][Ii][Nn][Gg][Dd][Oo][Mm]|[Dd][Ii][Ss][Ss][Oo][Ll][Vv][Ee]"
    r"|[Aa][Nn][Oo][Xx][Mm][Oo][Uu][Ss]|[Cc]irculatethetapes?"
    r"|[Ff]lux[Cc]apacitor|whodude\w*"
    r"|6ch|2ch|10bit|[Rr]emux|[Rr]epack"
    r")\b",
)


def parse_standard_title(raw_title: str) -> tuple[str, str | None]:
    """
    Parse a standard movie title like "Manhunter (1986)" or
    "The.Ice.Pirates.[1984].mp4" and return (clean_name, year_or_None).
    """
    title = raw_title.strip()
    title = _EXT_RE.sub("", title)

    # ── normalise separators ────────────────────────────────────────────
    # Replace dots and underscores with spaces FIRST so that regexes
    # below work on clean text.
    title = title.replace(".", " ").replace("_", " ")

    # Split concatenated word+year (e.g. "Deathquake1980" -> "Deathquake 1980")
    title = re.sub(r"([a-zA-Z])(\d{4})\b", r"\1 \2", title)

    # ── extract year (before stripping tags that might eat bracket years) ──
    year: str | None = None

    # Try (YYYY) first — the enriched format from enrichtitles.py
    m = _STD_YEAR_RE.search(title)
    if m:
        year = m.group(1)
        title = title[: m.start()]
    else:
        # Try [YYYY], ]YYYY], {YYYY}, [ 1996], etc.
        m = _YEAR_BRACKET_RE.search(title)
        if m:
            year = m.group(1)
            title = title[: m.start()]
        else:
            # Bare year — only if it looks like a plausible movie year
            m = _YEAR_BARE_RE.search(title)
            if m:
                candidate = int(m.group(1))
                if 1920 <= candidate <= 2030:
                    year = m.group(1)
                    title = title[: m.start()]

    # ── strip scene / quality tags ──────────────────────────────────────
    title = _SCENE_TAGS_RE.sub("", title)

    # ── strip [YTS MX] style group tags ─────────────────────────────────
    title = re.sub(r"\[\s*[A-Za-z0-9 _.+-]+\s*\]", "", title)

    # ── final cleanup ───────────────────────────────────────────────────
    # Remove leftover brackets / braces
    title = re.sub(r"[\[\]{}]", "", title)
    # Remove leftover empty parens and stray separators
    title = re.sub(r"\(\s*\)", "", title)
    title = re.sub(r"^\s*[-–—:,]+\s*", "", title)
    title = re.sub(r"\s*[-–—:,]+\s*$", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title, year


# ══════════════════════════════════════════════════════════════════════════════
#  Metadata providers
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class MovieMetadata:
    """Merged metadata from all providers."""

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
    # Ratings
    imdb_rating: str = ""
    imdb_id: str = ""
    rotten_tomatoes: str = ""
    metacritic: str = ""
    tmdb_rating: str = ""
    # Extra crew
    writer: list[str] = field(default_factory=list)
    cinematographer: list[str] = field(default_factory=list)
    composer: list[str] = field(default_factory=list)
    editor: list[str] = field(default_factory=list)
    special_effects: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.synopsis or self.cast or self.director)


# ── TMDb provider ─────────────────────────────────────────────────────────────

# Minimum SequenceMatcher ratio to accept a TMDb/OMDb result.
_TITLE_SIM_THRESHOLD = 0.50

_STRIP_ARTICLES_RE = re.compile(r"\b(?:the|a|an)\b", re.I)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")

# Map number words to digits for comparison
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
    "thirteen": "13",
}


def _normalise_for_compare(s: str) -> str:
    """Lower-case, strip articles/punctuation/extra whitespace,
    normalise number words to digits."""
    s = s.lower()
    s = _STRIP_ARTICLES_RE.sub("", s)
    s = _NON_ALNUM_RE.sub(" ", s)
    words = s.split()
    words = [_NUMBER_WORDS.get(w, w) for w in words]
    return " ".join(words)


def _titles_similar(query: str, result_title: str) -> bool:
    """Return True if *result_title* is a plausible match for *query*.

    Uses ``difflib.SequenceMatcher`` on normalised strings so that minor
    differences (articles, punctuation, number-vs-word) are tolerated while
    completely unrelated titles (e.g. '50 50' vs 'ZRock: 50 Years') are
    rejected.
    """
    a = _normalise_for_compare(query)
    b = _normalise_for_compare(result_title)
    if not a or not b:
        return False
    # Fast path: exact or near-exact after normalisation
    if a == b:
        return True
    # Containment check — only trust it when the query covers most of
    # the result (avoids short queries like "sum1" matching inside long
    # unrelated titles).
    if a in b and len(a) / len(b) >= 0.5:
        return True
    if b in a and len(b) / len(a) >= 0.5:
        return True
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio >= _TITLE_SIM_THRESHOLD


def _pick_best_tmdb_result(
    results: list[dict],
    query: str,
    year: str | None,
) -> dict | None:
    """Return the first TMDb result whose title passes the similarity check.

    Prefers results whose release year matches *year* (if given).
    """
    # Try year-matching results first, then the rest
    ordered = []
    rest = []
    for r in results:
        rd = r.get("release_date", "")
        if year and rd and rd[:4] == year:
            ordered.append(r)
        else:
            rest.append(r)
    ordered.extend(rest)

    for movie in ordered:
        if _titles_similar(query, movie.get("title", "")):
            return movie
    return None


def _tmdb_auth(tmdb_token: str) -> tuple[dict, dict]:
    """Return (headers, extra_params) for TMDb API calls.

    If *tmdb_token* looks like a short v3 API key (hex, ≤40 chars) it is
    sent as ``?api_key=`` query parameter.  Otherwise it is treated as the
    long JWT read-access token used in the ``Authorization: Bearer`` header.
    """
    if len(tmdb_token) <= 40 and all(c in "0123456789abcdefABCDEF" for c in tmdb_token):
        return {}, {"api_key": tmdb_token}
    return {"Authorization": f"Bearer {tmdb_token}"}, {}


def _parse_tmdb_search(
    search_data: dict,
    title: str,
    year: str | None,
    tmdb_token: str,
    session: requests.Session,
    delay: float,
) -> MovieMetadata:
    """
    Parse TMDb search results, fetch details + credits.
    Called from run_enrichment (which handles rate-limit retries).
    """
    meta = MovieMetadata()
    headers, auth_params = _tmdb_auth(tmdb_token)

    results = search_data.get("results", [])
    if not results:
        # Retry without year
        if year:
            try:
                r = _api_call_with_backoff(
                    session,
                    "GET",
                    f"{TMDB_BASE}/search/movie",
                    delay=delay,
                    headers=headers,
                    params={"query": title, **auth_params},
                    timeout=DEFAULT_TIMEOUT,
                )
                if r.status_code == 200:
                    results = r.json().get("results", [])
            except Exception:
                pass
    if not results:
        return meta

    movie = _pick_best_tmdb_result(results, title, year)
    if movie is None:
        return meta  # no result passed the similarity check

    movie_id = movie["id"]
    meta.title = movie.get("title", "")
    meta.synopsis = movie.get("overview", "")
    release = movie.get("release_date", "")
    if release:
        meta.year = release[:4]

    # Full details
    try:
        time.sleep(delay)
        r = _api_call_with_backoff(
            session,
            "GET",
            f"{TMDB_BASE}/movie/{movie_id}",
            delay=delay,
            headers=headers,
            params=auth_params,
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code == 200:
            detail = r.json()
            meta.genres = [g["name"] for g in detail.get("genres", [])]
            meta.runtime_min = detail.get("runtime")
            meta.tagline = detail.get("tagline", "")
            vote = detail.get("vote_average")
            if vote and vote > 0:
                meta.tmdb_rating = f"{vote:.1f}/10"
            meta.imdb_id = detail.get("imdb_id", "")
    except Exception:
        pass

    # Credits
    try:
        time.sleep(delay)
        r = _api_call_with_backoff(
            session,
            "GET",
            f"{TMDB_BASE}/movie/{movie_id}/credits",
            delay=delay,
            headers=headers,
            params=auth_params,
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code == 200:
            credits_data = r.json()
            meta.cast = [c["name"] for c in credits_data.get("cast", [])[:10]]
            crew = credits_data.get("crew", [])
            meta.director = [c["name"] for c in crew if c.get("job") == "Director"]
            meta.producer = [c["name"] for c in crew if c.get("job") == "Producer"][:5]
            meta.writer = [
                c["name"]
                for c in crew
                if c.get("job") in ("Writer", "Screenplay", "Story")
            ][:5]
            meta.cinematographer = [
                c["name"] for c in crew if c.get("job") == "Director of Photography"
            ][:3]
            meta.composer = [
                c["name"] for c in crew if c.get("job") == "Original Music Composer"
            ][:3]
            meta.editor = [c["name"] for c in crew if c.get("job") == "Editor"][:3]
            sfx_jobs = {
                "Special Effects",
                "Visual Effects Supervisor",
                "Special Effects Supervisor",
                "Visual Effects Producer",
                "Creature Design",
                "Practical Effects",
            }
            meta.special_effects = [
                f"{c['name']} ({c['job']})" for c in crew if c.get("job") in sfx_jobs
            ][:5]
    except Exception:
        pass

    return meta


def lookup_tmdb(
    title: str,
    year: str | None,
    tmdb_token: str,
    session: requests.Session,
    delay: float = REQUEST_DELAY,
) -> MovieMetadata:
    """Search TMDb for a movie and return rich metadata (standalone entry point)."""
    headers, auth_params = _tmdb_auth(tmdb_token)
    params: dict = {"query": title, **auth_params}
    if year:
        params["year"] = int(year)
    try:
        r = _api_call_with_backoff(
            session,
            "GET",
            f"{TMDB_BASE}/search/movie",
            delay=delay,
            headers=headers,
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code == 200:
            return _parse_tmdb_search(r.json(), title, year, tmdb_token, session, delay)
    except Exception:
        pass
    return MovieMetadata()


# ── OMDb provider ─────────────────────────────────────────────────────────────


def _parse_omdb_response(
    data: dict,
    title: str,
    year: str | None,
    omdb_key: str,
    session: requests.Session,
    delay: float,
) -> MovieMetadata:
    """
    Parse an OMDb JSON response into MovieMetadata.
    Called from run_enrichment (which handles rate-limit retries).
    """
    meta = MovieMetadata()

    if data.get("Response") != "True":
        # Retry without year
        if year:
            try:
                time.sleep(delay)
                r = _api_call_with_backoff(
                    session,
                    "GET",
                    OMDB_BASE,
                    delay=delay,
                    params={"apikey": omdb_key, "t": title, "type": "movie"},
                    timeout=DEFAULT_TIMEOUT,
                )
                if r.status_code == 200:
                    data = r.json()
            except Exception:
                pass
            if data.get("Response") != "True":
                return meta
        else:
            return meta

    meta.title = data.get("Title", "")
    if not _titles_similar(title, meta.title):
        return MovieMetadata()  # OMDb returned something unrelated

    meta.year = data.get("Year", "")
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

    for rating in data.get("Ratings", []):
        src = rating.get("Source", "")
        val = rating.get("Value", "")
        if "Rotten Tomatoes" in src:
            meta.rotten_tomatoes = val
        elif "Metacritic" in src:
            meta.metacritic = val

    metascore = data.get("Metascore", "")
    if metascore and metascore != "N/A" and not meta.metacritic:
        meta.metacritic = f"{metascore}/100"

    return meta


def lookup_omdb(
    title: str,
    year: str | None,
    omdb_key: str,
    session: requests.Session,
    delay: float = REQUEST_DELAY,
) -> MovieMetadata:
    """Search OMDb for a movie and return metadata (standalone entry point)."""
    params: dict = {"apikey": omdb_key, "t": title, "type": "movie"}
    if year:
        params["y"] = year
    try:
        r = _api_call_with_backoff(
            session,
            "GET",
            OMDB_BASE,
            delay=delay,
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code == 200:
            return _parse_omdb_response(r.json(), title, year, omdb_key, session, delay)
    except Exception:
        pass
    return MovieMetadata()


# ── Merge providers ───────────────────────────────────────────────────────────


def merge_metadata(tmdb: MovieMetadata, omdb: MovieMetadata) -> MovieMetadata:
    """Merge TMDb (deep credits) with OMDb (ratings) into one result."""
    merged = MovieMetadata()

    # Title & year: prefer TMDb (canonical), fall back to OMDb
    merged.title = tmdb.title or omdb.title
    merged.year = tmdb.year or omdb.year

    # Synopsis: prefer TMDb (longer/better usually)
    if len(tmdb.synopsis) >= len(omdb.synopsis):
        merged.synopsis = tmdb.synopsis
    else:
        merged.synopsis = omdb.synopsis

    # Credits: TMDb has deeper data
    merged.director = tmdb.director or omdb.director
    merged.producer = tmdb.producer
    merged.cast = tmdb.cast if len(tmdb.cast) > len(omdb.cast) else omdb.cast
    merged.genres = tmdb.genres or omdb.genres
    merged.writer = tmdb.writer or omdb.writer
    merged.cinematographer = tmdb.cinematographer
    merged.composer = tmdb.composer
    merged.editor = tmdb.editor
    merged.special_effects = tmdb.special_effects

    # Content rating: OMDb has MPAA
    merged.content_rating = omdb.content_rating

    # Ratings: OMDb is authoritative
    merged.imdb_rating = omdb.imdb_rating or tmdb.imdb_rating
    merged.imdb_id = omdb.imdb_id or tmdb.imdb_id
    merged.rotten_tomatoes = omdb.rotten_tomatoes
    merged.metacritic = omdb.metacritic
    merged.tmdb_rating = tmdb.tmdb_rating

    # Extras
    merged.runtime_min = tmdb.runtime_min
    merged.tagline = tmdb.tagline

    return merged


# ══════════════════════════════════════════════════════════════════════════════
#  Description formatter
# ══════════════════════════════════════════════════════════════════════════════


def format_description(
    meta: MovieMetadata,
    hosted: HostedInfo | None = None,
    existing_desc: str = "",
) -> str:
    """
    Build a structured description from metadata.

    Format matches the yt-pipe convention so scoring stays consistent.
    Preserves any existing 'Original URL:' or 'Original Description:' sections.
    """
    lines: list[str] = []

    # ── Hosted version banner ──────────────────────────────────────────────
    if hosted:
        lines.append(f"Hosted Version: {hosted.show_name}")
        lines.append("")

    # ── Synopsis ───────────────────────────────────────────────────────────
    if meta.synopsis:
        lines.append("Synopsis:")
        lines.append(meta.synopsis)
        lines.append("")

    if meta.tagline:
        lines.append(f"Tagline: {meta.tagline}")
        lines.append("")

    # ── Release year ───────────────────────────────────────────────────────
    if meta.year:
        lines.append(f"Release Year: {meta.year}")

    # ── Content rating ─────────────────────────────────────────────────────
    if meta.content_rating and meta.content_rating != "N/A":
        lines.append(f"Content Rating: {meta.content_rating}")

    if meta.runtime_min:
        lines.append(f"Runtime: {meta.runtime_min} min")

    if meta.year or meta.content_rating or meta.runtime_min:
        lines.append("")

    # ── Genres ─────────────────────────────────────────────────────────────
    if meta.genres:
        lines.append(f"Genres: {', '.join(meta.genres)}")
        lines.append("")

    # ── Ratings ────────────────────────────────────────────────────────────
    ratings_parts = []
    if meta.imdb_rating and meta.imdb_rating != "N/A":
        label = f"IMDb: {meta.imdb_rating}"
        if meta.imdb_id:
            label += f" (https://www.imdb.com/title/{meta.imdb_id}/)"
        ratings_parts.append(label)
    if meta.rotten_tomatoes:
        ratings_parts.append(f"Rotten Tomatoes: {meta.rotten_tomatoes}")
    if meta.metacritic:
        ratings_parts.append(f"Metacritic: {meta.metacritic}")
    if meta.tmdb_rating:
        ratings_parts.append(f"TMDb: {meta.tmdb_rating}")

    if ratings_parts:
        lines.append("Ratings:")
        for rp in ratings_parts:
            lines.append(f"  {rp}")
        lines.append("")

    # ── Cast & Crew ────────────────────────────────────────────────────────
    has_crew = (
        meta.director
        or meta.cast
        or meta.producer
        or meta.writer
        or meta.cinematographer
        or meta.composer
        or meta.editor
        or meta.special_effects
    )
    if has_crew:
        lines.append("Cast & Crew:")

        if meta.director:
            lines.append(f"  Director(s): {', '.join(meta.director)}")
        if meta.writer:
            lines.append(f"  Writer(s): {', '.join(meta.writer)}")
        if meta.producer:
            lines.append(f"  Producer(s): {', '.join(meta.producer)}")
        if meta.cinematographer:
            lines.append(f"  Cinematography: {', '.join(meta.cinematographer)}")
        if meta.composer:
            lines.append(f"  Music: {', '.join(meta.composer)}")
        if meta.editor:
            lines.append(f"  Editor(s): {', '.join(meta.editor)}")
        if meta.special_effects:
            lines.append(f"  Special Effects: {', '.join(meta.special_effects)}")
        if meta.cast:
            lines.append(f"  Cast: {', '.join(meta.cast)}")

        lines.append("")

    # ── Preserve existing Original URL / Description ───────────────────────
    if existing_desc:
        for section_header in ("Original URL:", "Original Description:"):
            idx = existing_desc.find(section_header)
            if idx != -1:
                # Grab from the header to the next blank line or end
                chunk = existing_desc[idx:]
                end = chunk.find("\n\n")
                if end != -1:
                    chunk = chunk[:end]
                lines.append(chunk.strip())
                lines.append("")

    return "\n".join(lines).strip()


# ══════════════════════════════════════════════════════════════════════════════
#  CMS API helpers
# ══════════════════════════════════════════════════════════════════════════════


def fetch_all_media(session: requests.Session, api_base: str) -> list[dict]:
    """Paginate through /manage_media to get every media item."""
    all_items: list[dict] = []
    page = 1

    resp = session.get(
        f"{api_base}/manage_media",
        params={"page": 1},
        timeout=DEFAULT_TIMEOUT,
    )

    if resp.status_code == 403:
        print("  (falling back to /media — may be capped at ~1000)", file=sys.stderr)
        return _fetch_media_fallback(session, api_base)

    resp.raise_for_status()
    data = resp.json()
    total = data.get("count", 0)
    all_items.extend(data.get("results", []))
    print(f"    Total media in CMS: {total}")

    while data.get("next"):
        page += 1
        time.sleep(REQUEST_DELAY)
        resp = session.get(
            f"{api_base}/manage_media",
            params={"page": page},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        all_items.extend(data.get("results", []))
        pct = min(100, int(len(all_items) / total * 100)) if total else 0
        print(f"\r    Fetched {len(all_items)}/{total} ({pct}%)", end="", flush=True)

    print(f"\r    Fetched {len(all_items)}/{total} (100%)    ")
    return all_items


def _fetch_media_fallback(session: requests.Session, api_base: str) -> list[dict]:
    all_items: list[dict] = []
    page = 1
    while True:
        resp = session.get(
            f"{api_base}/media",
            params={"page": page},
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code != 200:
            break
        data = resp.json()
        batch = data.get("results", [])
        if not batch:
            break
        all_items.extend(batch)
        if not data.get("next"):
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    print(f"    Fetched {len(all_items)} items (via /media fallback)")
    return all_items


def update_media(
    session: requests.Session,
    api_base: str,
    friendly_token: str,
    description: str,
    title: str | None = None,
) -> bool:
    """PUT updated fields to /media/{token}. Returns True on success."""
    payload: dict[str, str] = {"description": description}
    if title is not None:
        payload["title"] = title
    resp = session.put(
        f"{api_base}/media/{friendly_token}",
        data=payload,
        timeout=DEFAULT_TIMEOUT,
    )
    return resp.status_code in (200, 201)


def _restore_owner(
    session: requests.Session,
    api_base: str,
    friendly_token: str,
    cms_user: str,
    delay: float,
) -> None:
    """Restore original ownership after a PUT.

    MediaCMS bug: PUT /api/v1/media/{token} always calls
    ``serializer.save(user=request.user)``, silently overwriting the stored
    owner with whoever holds the API token (typically admin).  Calling this
    immediately after each commit restores the correct uploader.
    """
    if not cms_user:
        return
    try:
        time.sleep(delay)
        session.post(
            f"{api_base}/media/user/bulk_actions",
            json={
                "action": "change_owner",
                "media_ids": [friendly_token],
                "owner": cms_user,
            },
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception:
        pass  # ownership restoration is best-effort; don't fail the whole run


# ══════════════════════════════════════════════════════════════════════════════
#  Data model
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class Candidate:
    friendly_token: str
    raw_title: str
    description: str
    duration: int
    score: int
    hosted: HostedInfo | None
    lookup_title: str  # clean title for API lookup
    lookup_year: str | None  # year for API lookup
    genre_hints: list[str] = field(default_factory=list)  # from YouTube titles
    is_youtube: bool = False  # parsed from "Title | Full Movie | Genres"
    cms_user: str = (
        ""  # original uploader — captured before any PUT so we can restore it
    )
    is_tubi: bool = False  # True when --tubi-upgrade targets this item


# ══════════════════════════════════════════════════════════════════════════════
#  Main pipeline
# ══════════════════════════════════════════════════════════════════════════════


def find_candidates(
    all_media: list[dict],
    min_duration: int,
    min_score: int,
    *,
    tubi_upgrade: bool = False,
) -> list[Candidate]:
    """Scan catalog, filter to movies, score, return items needing enrichment.

    When *tubi_upgrade* is True, items whose descriptions carry Tubi-sourced
    metadata are included regardless of their quality score.  These items
    are tagged with ``is_tubi=True`` on the Candidate so the pipeline can
    differentiate them.
    """
    candidates: list[Candidate] = []

    for item in all_media:
        dur = item.get("duration") or 0
        if dur < min_duration:
            continue

        raw_title = item.get("title", "")

        # Skip TV episodes — enrichment for series comes later.
        if _TV_EPISODE_RE.search(raw_title):
            continue

        desc = item.get("description", "") or ""
        quality = score_description(desc)

        item_is_tubi = tubi_upgrade and is_tubi_metadata(desc)

        if quality["score"] >= min_score and not item_is_tubi:
            continue  # already enriched (and not a Tubi upgrade target)

        # Detect Rifftrax first — its filenames use scene-style
        # dot-separated conventions best handled by parse_standard_title.
        riff_m = _RIFFTRAX_RE.search(raw_title)
        if riff_m:
            stripped = _RIFFTRAX_RE.sub("", raw_title)
            lookup_title, lookup_year = parse_standard_title(stripped)
            hosted = HostedInfo(
                show_name="Rifftrax Presents",
                movie_title=lookup_title,
                movie_year=lookup_year,
            )
        else:
            hosted = detect_hosted(raw_title)

        genre_hints: list[str] = []
        is_youtube = False

        if hosted:
            lookup_title = hosted.movie_title
            lookup_year = hosted.movie_year
        else:
            yt = parse_youtube_title(raw_title)
            if yt:
                lookup_title, genre_hints = yt
                lookup_year = None  # YouTube titles never have years
                is_youtube = True
            else:
                lookup_title, lookup_year = parse_standard_title(raw_title)

        # For Tubi items, prefer the year from their Video Information block
        # if the title parser didn't find one.
        if item_is_tubi and not lookup_year:
            tubi_year = _extract_tubi_year(desc)
            if tubi_year:
                lookup_year = tubi_year

        candidates.append(
            Candidate(
                friendly_token=item.get("friendly_token", ""),
                raw_title=raw_title,
                description=desc,
                duration=dur,
                score=quality["score"],
                hosted=hosted,
                lookup_title=lookup_title,
                lookup_year=lookup_year,
                genre_hints=genre_hints,
                is_youtube=is_youtube,
                cms_user=item.get("user", ""),
                is_tubi=item_is_tubi,
            )
        )

    # Sort by score ascending (worst first)
    candidates.sort(key=lambda c: (c.score, c.raw_title))
    return candidates


def run_report(candidates: list[Candidate]) -> None:
    """Print a table of candidates with scores."""
    # Tally
    hosted_count = sum(1 for c in candidates if c.hosted)
    empty_count = sum(1 for c in candidates if c.score == 0)
    low_count = sum(1 for c in candidates if 0 < c.score < 50)
    tubi_count = sum(1 for c in candidates if c.is_tubi)

    print(f"\n  Candidates: {len(candidates)}")
    print(f"    Empty description:  {empty_count}")
    print(f"    Low-scoring:        {low_count}")
    print(f"    Hosted versions:    {hosted_count}")
    if tubi_count:
        print(f"    Tubi upgrades:      {tubi_count}")
    print()

    col_t = 55
    col_l = 35
    print(f"  {'SCORE':>5}  {'DUR':>5}  {'TITLE':<{col_t}}  {'LOOKUP':<{col_l}}  HOST")
    print(f"  {'─'*5}  {'─'*5}  {'─'*col_t}  {'─'*col_l}  {'─'*20}")

    for c in candidates[:100]:  # cap report at 100 rows
        dur_m = c.duration // 60
        title_disp = c.raw_title[:col_t]
        lookup_disp = c.lookup_title[:col_l]
        host_disp = c.hosted.show_name[:20] if c.hosted else ""
        if c.is_tubi:
            host_disp = (host_disp + " Tubi↑").strip()
        yr = f" ({c.lookup_year})" if c.lookup_year else ""
        print(
            f"  {c.score:>5}  {dur_m:>4}m  {title_disp:<{col_t}}  {lookup_disp}{yr:<{col_l}}  {host_disp}"
        )

    if len(candidates) > 100:
        print(f"\n  ... and {len(candidates) - 100} more.")


def _api_call_with_backoff(
    session: requests.Session,
    method: str,
    url: str,
    delay: float,
    **kwargs,
) -> requests.Response:
    """
    Make an API call with automatic retry on 429 (rate limit).
    Respects Retry-After header if present, otherwise backs off exponentially.
    """
    max_retries = 4
    backoff = max(delay, 1.0)

    for attempt in range(max_retries + 1):
        r = session.request(method, url, **kwargs)
        if r.status_code != 429:
            return r

        # Rate-limited — back off
        retry_after = r.headers.get("Retry-After")
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = backoff
        else:
            wait = backoff

        wait = min(wait, 60)  # cap at 1 minute
        print(
            f"         ** 429 rate-limited — waiting {wait:.0f}s "
            f"(attempt {attempt + 1}/{max_retries}) **"
        )
        time.sleep(wait)
        backoff *= 2  # exponential

    return r  # return last response even if still 429


def _format_eta(seconds: float) -> str:
    """Format seconds into a human-readable ETA string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _interactive_pick(
    results: list[dict],
) -> dict | None:
    """Show numbered TMDb results and let the user pick one.

    Returns the chosen result dict, or None if the user skips.
    """
    if not results:
        return None
    top = results[:8]  # show at most 8
    print("         TMDb returned these candidates:")
    for idx, r in enumerate(top, 1):
        rd = r.get("release_date", "") or "????"
        yr = rd[:4] if len(rd) >= 4 else "????"
        overview = (r.get("overview") or "")[:80]
        if len(r.get("overview") or "") > 80:
            overview += "…"
        print(f"           {idx}. {r.get('title', '?')} ({yr})  — {overview}")
    print("           0. Skip this item")
    while True:
        try:
            choice = input("         Pick [0 to skip]: ").strip()
        except EOFError:
            return None
        if choice == "" or choice == "0":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(top):
            return top[int(choice) - 1]
        print(f"         (enter 1-{len(top)} or 0)")


def _interactive_miss(
    prefix: str,
    title_disp: str,
    query: str,
    year: str | None,
    tmdb_token: str | None,
    lookup_session: requests.Session,
    delay: float,
) -> MovieMetadata | None:
    """Prompt the user for a corrected search query on a MISS.

    Returns a MovieMetadata if the user successfully finds a match, or
    None if they choose to skip.  Returns the sentinel string ``'quit'``
    when the user wants to stop interactive prompting.
    """
    print(
        f"{prefix} MISS  {title_disp}  " f'<- no results for "{query}" ({year or "?"})'
    )
    print(
        "         Type a corrected title to retry, Enter to skip, "
        "or 'q' to stop prompting:"
    )
    while True:
        try:
            hint = input("         >> ").strip()
        except EOFError:
            return None
        if hint == "":
            return None
        if hint.lower() == "q":
            return "quit"  # type: ignore[return-value]

        # Try the user's hint as a search query
        if not tmdb_token:
            print("         (no TMDb key — cannot search)")
            return None

        headers, auth_params = _tmdb_auth(tmdb_token)
        try:
            r = _api_call_with_backoff(
                lookup_session,
                "GET",
                f"{TMDB_BASE}/search/movie",
                delay=delay,
                headers=headers,
                params={"query": hint, **auth_params},
                timeout=DEFAULT_TIMEOUT,
            )
        except Exception:
            print("         (search failed — network error)")
            continue
        if r.status_code != 200:
            print(f"         (TMDb returned {r.status_code})")
            continue
        results = r.json().get("results", [])
        if not results:
            print(
                f'         No results for "{hint}" — try again or press Enter to skip.'
            )
            continue

        picked = _interactive_pick(results)
        if picked is None:
            print("         Skipped.")
            return None

        # Fetch full details for the picked result
        meta = MovieMetadata()
        meta.title = picked.get("title", "")
        meta.synopsis = picked.get("overview", "")
        release = picked.get("release_date", "")
        if release:
            meta.year = release[:4]

        movie_id = picked["id"]
        try:
            time.sleep(delay)
            r2 = _api_call_with_backoff(
                lookup_session,
                "GET",
                f"{TMDB_BASE}/movie/{movie_id}",
                delay=delay,
                headers=headers,
                params=auth_params,
                timeout=DEFAULT_TIMEOUT,
            )
            if r2.status_code == 200:
                detail = r2.json()
                meta.genres = [g["name"] for g in detail.get("genres", [])]
                meta.runtime_min = detail.get("runtime")
                meta.tagline = detail.get("tagline", "")
                vote = detail.get("vote_average")
                if vote and vote > 0:
                    meta.tmdb_rating = f"{vote:.1f}/10"
                meta.imdb_id = detail.get("imdb_id", "")
        except Exception:
            pass
        try:
            time.sleep(delay)
            r3 = _api_call_with_backoff(
                lookup_session,
                "GET",
                f"{TMDB_BASE}/movie/{movie_id}/credits",
                delay=delay,
                headers=headers,
                params=auth_params,
                timeout=DEFAULT_TIMEOUT,
            )
            if r3.status_code == 200:
                credits_data = r3.json()
                meta.cast = [c["name"] for c in credits_data.get("cast", [])[:10]]
                crew = credits_data.get("crew", [])
                meta.director = [c["name"] for c in crew if c.get("job") == "Director"]
                meta.producer = [c["name"] for c in crew if c.get("job") == "Producer"][
                    :5
                ]
                meta.writer = [
                    c["name"]
                    for c in crew
                    if c.get("job") in ("Writer", "Screenplay", "Story")
                ][:5]
                meta.cinematographer = [
                    c["name"] for c in crew if c.get("job") == "Director of Photography"
                ][:3]
                meta.composer = [
                    c["name"] for c in crew if c.get("job") == "Original Music Composer"
                ][:3]
                meta.editor = [c["name"] for c in crew if c.get("job") == "Editor"][:3]
        except Exception:
            pass

        return meta


def run_enrichment(
    candidates: list[Candidate],
    cms_session: requests.Session,
    api_base: str,
    tmdb_token: str | None,
    omdb_key: str | None,
    commit: bool,
    limit: int | None,
    delay: float = REQUEST_DELAY,
    interactive: bool = False,
) -> tuple[int, int, int]:
    """
    Look up metadata for each candidate and optionally push to CMS.

    Returns (enriched_count, skipped_count, failed_count).
    """
    lookup_session = requests.Session()
    to_process = candidates[:limit] if limit else candidates
    total = len(to_process)

    enriched = 0
    skipped = 0
    failed = 0
    api_calls = 0

    col_w = 50
    start_time = time.monotonic()

    try:
        for i, c in enumerate(to_process, 1):
            title_disp = c.raw_title[:col_w]
            prefix = f"  [{i}/{total}]"

            if not c.lookup_title:
                print(f"{prefix} SKIP  {title_disp}  (could not parse title)")
                skipped += 1
                continue

            # ── Lookup ─────────────────────────────────────────────────────
            tmdb_meta = MovieMetadata()
            omdb_meta = MovieMetadata()

            if tmdb_token:
                tmdb_search_results: list[dict] = []
                _hdrs, _aprms = _tmdb_auth(tmdb_token)
                try:
                    r = _api_call_with_backoff(
                        lookup_session,
                        "GET",
                        f"{TMDB_BASE}/search/movie",
                        delay=delay,
                        headers=_hdrs,
                        params={
                            "query": c.lookup_title,
                            **_aprms,
                            **(({"year": int(c.lookup_year)} if c.lookup_year else {})),
                        },
                        timeout=DEFAULT_TIMEOUT,
                    )
                    api_calls += 1
                    if r.status_code == 200:
                        tmdb_search_results = r.json().get("results", [])
                        tmdb_meta = _parse_tmdb_search(
                            r.json(),
                            c.lookup_title,
                            c.lookup_year,
                            tmdb_token,
                            lookup_session,
                            delay,
                        )
                except Exception:
                    pass

                # Interactive: if results existed but similarity rejected
                # them all, let the user pick from the list.
                if interactive and not tmdb_meta.found and tmdb_search_results:
                    print(
                        f"{prefix} ???   {title_disp}  "
                        f'<- no good match for "{c.lookup_title}" '
                        f'({c.lookup_year or "?"})'
                    )
                    picked = _interactive_pick(
                        tmdb_search_results,
                    )
                    if picked is not None:
                        tmdb_meta = _parse_tmdb_search(
                            {"results": [picked]},
                            picked.get("title", ""),
                            None,
                            tmdb_token,
                            lookup_session,
                            delay,
                        )

                time.sleep(delay)

            if omdb_key:
                try:
                    # Prefer IMDb ID from TMDb for exact OMDb lookup;
                    # fall back to TMDb canonical title, then raw CMS title.
                    if tmdb_meta.imdb_id:
                        params: dict = {"apikey": omdb_key, "i": tmdb_meta.imdb_id}
                    else:
                        omdb_title = tmdb_meta.title or c.lookup_title
                        params = {"apikey": omdb_key, "t": omdb_title, "type": "movie"}
                        if c.lookup_year:
                            params["y"] = c.lookup_year
                    r = _api_call_with_backoff(
                        lookup_session,
                        "GET",
                        OMDB_BASE,
                        delay=delay,
                        params=params,
                        timeout=DEFAULT_TIMEOUT,
                    )
                    api_calls += 1
                    if r.status_code == 200:
                        omdb_title_for_check = tmdb_meta.title or c.lookup_title
                        omdb_meta = _parse_omdb_response(
                            r.json(),
                            omdb_title_for_check,
                            c.lookup_year,
                            omdb_key,
                            lookup_session,
                            delay,
                        )
                except Exception:
                    pass
                time.sleep(delay)

            meta = merge_metadata(tmdb_meta, omdb_meta)

            # Use YouTube genre hints as fallback
            if not meta.genres and c.genre_hints:
                meta.genres = c.genre_hints

            if not meta.found:
                if interactive:
                    result = _interactive_miss(
                        prefix,
                        title_disp,
                        c.lookup_title,
                        c.lookup_year,
                        tmdb_token,
                        lookup_session,
                        delay,
                    )
                    if result == "quit":
                        interactive = False  # stop prompting, keep running
                        print(
                            f"{prefix} MISS  {title_disp}  "
                            f'<- no results for "{c.lookup_title}" ({c.lookup_year or "?"})'
                        )
                        skipped += 1
                        continue
                    if result is not None and result.found:
                        tmdb_meta = result
                        # Re-fetch OMDb with the interactively-selected IMDb ID
                        if omdb_key and tmdb_meta.imdb_id:
                            try:
                                r = _api_call_with_backoff(
                                    lookup_session,
                                    "GET",
                                    OMDB_BASE,
                                    delay=delay,
                                    params={"apikey": omdb_key, "i": tmdb_meta.imdb_id},
                                    timeout=DEFAULT_TIMEOUT,
                                )
                                if r.status_code == 200:
                                    omdb_meta = _parse_omdb_response(
                                        r.json(),
                                        tmdb_meta.title,
                                        tmdb_meta.year,
                                        omdb_key,
                                        lookup_session,
                                        delay,
                                    )
                            except Exception:
                                pass
                            time.sleep(delay)
                        meta = merge_metadata(tmdb_meta, omdb_meta)
                        if not meta.genres and c.genre_hints:
                            meta.genres = c.genre_hints
                    else:
                        skipped += 1
                        continue
                else:
                    print(
                        f"{prefix} MISS  {title_disp}  "
                        f'<- no results for "{c.lookup_title}" ({c.lookup_year or "?"})'
                    )
                    skipped += 1
                    continue

            # ── Format ─────────────────────────────────────────────────────
            new_desc = format_description(
                meta, hosted=c.hosted, existing_desc=c.description
            )
            new_score = score_description(new_desc)

            if new_score["score"] <= c.score:
                print(
                    f"{prefix} SKIP  {title_disp}  "
                    f"(new score {new_score['score']} <= existing {c.score})"
                )
                skipped += 1
                continue

            # ── Canonical title rename ─────────────────────────────────────
            # For non-hosted items, update the CMS title to the canonical
            # name (e.g. "310 to Yuma (2007)" -> "3:10 to Yuma (2007)").
            # For Rifftrax, use "Rifftrax Presents: Title (year)".
            new_cms_title: str | None = None
            if meta.title:
                year_suffix = f" ({meta.year})" if meta.year else ""
                if c.hosted and c.hosted.show_name == "Rifftrax Presents":
                    canonical_cms = f"{c.hosted.show_name}: {meta.title}{year_suffix}"
                elif not c.hosted:
                    canonical_cms = f"{meta.title}{year_suffix}"
                else:
                    canonical_cms = None  # other hosted shows keep original title
                if canonical_cms and canonical_cms != c.raw_title:
                    new_cms_title = canonical_cms

            # ── Display / commit ───────────────────────────────────────────
            host_tag = f" [{c.hosted.show_name}]" if c.hosted else ""
            if c.is_tubi:
                host_tag += " [Tubi↑]"
            ratings_tag = ""
            if meta.imdb_rating and meta.imdb_rating != "N/A":
                ratings_tag = f" IMDb:{meta.imdb_rating}"
            if meta.rotten_tomatoes:
                ratings_tag += f" RT:{meta.rotten_tomatoes}"

            # ETA
            elapsed = time.monotonic() - start_time
            per_item = elapsed / i if i else 1
            remaining = per_item * (total - i)
            eta = _format_eta(remaining)

            found_title = meta.title or c.lookup_title
            print(f"{prefix} MATCH {title_disp}")
            print(
                f"         -> {found_title} ({meta.year or '?'}){host_tag}{ratings_tag}"
            )
            if new_cms_title:
                print(f"         -> rename: {c.raw_title} -> {new_cms_title}")
            print(
                f"         -> score {c.score} -> {new_score['score']}  "
                f"({new_score['description_length']} chars, "
                f"{len(new_score['sections_present'])} sections)  "
                f"ETA: {eta}"
            )

            if commit:
                try:
                    ok = update_media(
                        cms_session,
                        api_base,
                        c.friendly_token,
                        new_desc,
                        title=new_cms_title,
                    )
                except Exception:
                    ok = False
                if ok:
                    enriched += 1
                    print("         -> COMMITTED")
                    # MediaCMS bug: PUT /media/{token} overwrites the owner
                    # with request.user (the admin token).  Restore the
                    # original uploader immediately after each commit.
                    if c.cms_user:
                        _restore_owner(
                            cms_session,
                            api_base,
                            c.friendly_token,
                            c.cms_user,
                            delay,
                        )
                else:
                    failed += 1
                    print("         -> FAILED to commit")
                time.sleep(delay)
            else:
                enriched += 1  # count as "would enrich" in dry-run

    except KeyboardInterrupt:
        elapsed = time.monotonic() - start_time
        print(
            f"\n\n  *** Interrupted after {_format_eta(elapsed)} "
            f"({api_calls} API calls) ***"
        )
        print(
            f"  Processed so far: enriched={enriched}  "
            f"skipped={skipped}  failed={failed}"
        )
        if commit and enriched > 0:
            print(f"  ({enriched} items were already committed before interrupt)")
        print()

    return enriched, skipped, failed


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="enrichmeta",
        description="Enrich movie metadata in DropSugar MediaCMS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
modes:
  Default is dry-run (shows what would change). Use --commit to apply.
  Use --report for a quick scan-and-score table without lookups.

examples:
  %(prog)s --token CMS --tmdb-key KEY --omdb-key KEY --report
  %(prog)s --token CMS --tmdb-key KEY --omdb-key KEY --limit 10
  %(prog)s --token CMS --tmdb-key KEY --omdb-key KEY --commit --limit 5
        """,
    )
    p.add_argument("--token", required=True, help="MediaCMS API token.")
    p.add_argument("--tmdb-key", default=None, help="TMDb read-access token (Bearer).")
    p.add_argument("--omdb-key", default=None, help="OMDb API key.")
    p.add_argument(
        "--report",
        action="store_true",
        help="Scan and score only - no lookups, no changes.",
    )
    p.add_argument(
        "--commit", action="store_true", help="Push enriched descriptions to the CMS."
    )
    p.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Prompt on misses and ambiguous matches.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Only process the first N candidates.",
    )
    p.add_argument(
        "--tubi-upgrade",
        action="store_true",
        help="Re-enrich items with Tubi-sourced metadata regardless of score.",
    )
    p.add_argument(
        "--min-score",
        type=int,
        default=MIN_SCORE_THRESHOLD,
        help=f"Enrich items scoring below this (default: {MIN_SCORE_THRESHOLD}).",
    )
    p.add_argument(
        "--min-duration",
        type=int,
        default=MIN_DURATION,
        help=f"Minimum duration in seconds (default: {MIN_DURATION}).",
    )
    p.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="Only consider items uploaded in the last N days.",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY,
        help=f"Seconds between API calls (default: {REQUEST_DELAY}).",
    )
    p.add_argument(
        "--api-url",
        default=API_BASE,
        help=f"Override the API base URL (default: {API_BASE}).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows console UTF-8 fix
    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
        import io

        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )

    parser = build_parser()
    args = parser.parse_args(argv)

    api_base = args.api_url.rstrip("/")

    if not args.report and not args.tmdb_key and not args.omdb_key:
        print(
            "Error: at least one of --tmdb-key or --omdb-key is required "
            "for enrichment (use --report for scan-only).",
            file=sys.stderr,
        )
        return 1

    mode = "REPORT" if args.report else ("COMMIT" if args.commit else "DRY-RUN")
    if args.interactive and not args.report:
        mode += " + INTERACTIVE"
    if args.tubi_upgrade:
        mode += " + TUBI-UPGRADE"
    dur_mins = args.min_duration // 60

    print(f"\n{'='*60}")
    print(f"  enrichmeta  --  Mode: {mode}")
    print(
        f"  Movies: duration >= {dur_mins} min  |  Score threshold: < {args.min_score}"
    )
    if args.tubi_upgrade:
        print("  Tubi upgrade: ON (re-enrich Tubi-sourced items regardless of score)")
    if args.days:
        print(f"  Window: last {args.days} day(s)")
    if args.limit:
        print(f"  Limit: {args.limit}")
    providers = []
    if args.tmdb_key:
        providers.append("TMDb")
    if args.omdb_key:
        providers.append("OMDb")
    if providers:
        print(f"  Providers: {', '.join(providers)}")
    print(f"  Delay: {args.delay}s between API calls")
    print(f"{'='*60}\n")

    # ── CMS session ────────────────────────────────────────────────────────
    cms_session = requests.Session()
    cms_session.headers["Authorization"] = f"Token {args.token}"

    # ── Fetch catalog ──────────────────────────────────────────────────────
    print("  Fetching media catalog ...")
    all_media = fetch_all_media(cms_session, api_base)

    # ── Filter by upload date ─────────────────────────────────────────────
    if args.days:
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=args.days)
        ).strftime("%Y-%m-%d")
        before = len(all_media)
        all_media = [i for i in all_media if (i.get("add_date") or "")[:10] >= cutoff]
        print(
            f"  Filtered to {len(all_media)}/{before} item(s) "
            f"uploaded in the last {args.days} day(s)."
        )

    # ── Find candidates ────────────────────────────────────────────────────
    print("\n  Scanning for enrichment candidates ...")
    candidates = find_candidates(
        all_media,
        args.min_duration,
        args.min_score,
        tubi_upgrade=args.tubi_upgrade,
    )

    if not candidates:
        print("\n  All movies are already enriched. Nothing to do!")
        return 0

    # ── Report mode ────────────────────────────────────────────────────────
    if args.report:
        run_report(candidates)
        return 0

    # ── Enrichment ─────────────────────────────────────────────────────────
    print(f"\n  Found {len(candidates)} candidate(s). Starting lookups ...\n")

    enriched, skipped, failed = run_enrichment(
        candidates=candidates,
        cms_session=cms_session,
        api_base=api_base,
        tmdb_token=args.tmdb_key,
        omdb_key=args.omdb_key,
        commit=args.commit,
        limit=args.limit,
        delay=args.delay,
        interactive=args.interactive,
    )

    # ── Summary ────────────────────────────────────────────────────────────
    action = "Enriched" if args.commit else "Would enrich"
    print(f"\n{'='*60}")
    print(f"  {action}: {enriched}  |  Skipped: {skipped}  |  Failed: {failed}")
    if not args.commit and enriched > 0:
        print("  (dry-run -- use --commit to apply)")
    print(f"{'='*60}\n")

    return 0 if failed == 0 else 1


# ── Headless entry point for the webqueue job runner ───────────────────────────


def run(params: dict, *, config, progress=None) -> dict:
    """Run movie-metadata enrichment headlessly (no argparse/interactive).

    ``params`` keys: ``dry_run`` (bool), ``limit`` (int|None), ``days`` (int|None),
    ``tubi_upgrade`` (bool), ``min_score`` (int), ``min_duration`` (int),
    ``delay`` (float). TMDb/OMDb keys + MediaCMS creds come from ``config``.
    Returns a counts dict for ``job_runs.detail``.
    """
    api_base = f"{config.mediacms_url.rstrip('/')}/api/v1"
    dry_run = bool(params.get("dry_run", False))
    limit = params.get("limit")
    days = params.get("days")
    tubi_upgrade = bool(params.get("tubi_upgrade", False))
    min_score = params.get("min_score", MIN_SCORE_THRESHOLD)
    min_duration = params.get("min_duration", MIN_DURATION)
    delay = params.get("delay", REQUEST_DELAY)

    tmdb_key = getattr(config, "tmdb_api_key", "") or ""
    omdb_key = getattr(config, "omdb_api_key", "") or ""
    if not tmdb_key and not omdb_key:
        raise RuntimeError(
            "enrichmeta requires a TMDb or OMDb API key (none configured)"
        )

    def _emit(detail):
        if progress:
            progress(detail)

    cms_session = requests.Session()
    cms_session.headers["Authorization"] = f"Token {config.mediacms_token}"

    _emit({"phase": "fetching"})
    all_media = fetch_all_media(cms_session, api_base)

    if days:
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        ).strftime("%Y-%m-%d")
        all_media = [i for i in all_media if (i.get("add_date") or "")[:10] >= cutoff]

    candidates = find_candidates(
        all_media,
        min_duration,
        min_score,
        tubi_upgrade=tubi_upgrade,
    )
    _emit({"phase": "scanned", "scanned": len(all_media), "matched": len(candidates)})

    if not candidates:
        return {
            "scanned": len(all_media),
            "matched": 0,
            "committed": 0,
            "skipped": 0,
            "failed": 0,
            "dry_run": dry_run,
        }

    enriched, skipped, failed = run_enrichment(
        candidates=candidates,
        cms_session=cms_session,
        api_base=api_base,
        tmdb_token=tmdb_key or None,
        omdb_key=omdb_key or None,
        commit=not dry_run,
        limit=limit,
        delay=delay,
        interactive=False,
    )

    return {
        "scanned": len(all_media),
        "matched": len(candidates),
        "committed": enriched if not dry_run else 0,
        "would_enrich": enriched if dry_run else None,
        "skipped": skipped,
        "failed": failed,
        "dry_run": dry_run,
    }


if __name__ == "__main__":
    raise SystemExit(main())
