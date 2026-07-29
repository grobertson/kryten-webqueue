#!/usr/bin/env python3
# VENDORED from d:\Devel\cmsutils\enrichtv.py on 2026-06-09.
# Adapted for in-process use by kryten-webqueue jobs: a headless
# run(params, *, config, progress) entry point is appended at the bottom; the
# original CLI main()/argparse path is retained but unused by the service.
# Keep adapters thin so re-vendoring from upstream stays mechanical.
"""
enrichtv — Enrich TV episode metadata in a MediaCMS instance.

Scans the catalog for TV episodes (duration 10–59 minutes) with missing or
low-quality descriptions, looks up metadata from TMDb and OMDb, and pushes
structured descriptions back to the CMS.

Architecture
------------
The enrichment pipeline is:

    scan → score → parse title → lookup show (cached) → lookup episode → format → commit

Show lookups are cached so that all episodes of the same series share a
single TMDb/OMDb search, dramatically reducing API calls.

Canonical title format
----------------------
    Show Name - S01E02 - Episode Title

Modes
-----
  --report          Scan & score only — output a table of candidates
  (default)         Dry-run — show what would change, don't touch the CMS
  --commit          Push enriched descriptions to the CMS
  --interactive     Prompt for corrected show names on misses
  --limit N         Process only the first N candidates

Examples
--------
  python enrichtv.py --token CMS --tmdb-key KEY --omdb-key KEY --report
  python enrichtv.py --token CMS --tmdb-key KEY --omdb-key KEY --limit 20
  python enrichtv.py --token CMS --tmdb-key KEY --omdb-key KEY -i --limit 50
  python enrichtv.py --token CMS --tmdb-key KEY --omdb-key KEY --commit
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
REQUEST_DELAY = 0.25
MIN_DURATION = 600  # 10 minutes in seconds
MAX_DURATION = 3599  # just under 60 minutes
MIN_SCORE_THRESHOLD = 50
TMDB_BASE = "https://api.themoviedb.org/3"
OMDB_BASE = "http://www.omdbapi.com/"

_TITLE_SIM_THRESHOLD = 0.50


# ══════════════════════════════════════════════════════════════════════════════
#  Quality scoring
# ══════════════════════════════════════════════════════════════════════════════

QUALITY_MARKERS = [
    "Cast & Crew:",
    "Director(s):",
    "Directed by:",
    "Written by:",
    "Cast:",
    "Guest Stars:",
    "Genres:",
    "Content Rating:",
    "Release Year:",
    "Air Date:",
    "Synopsis:",
    "Season ",
    "Ratings:",
    "IMDb:",
    "TMDb:",
    "Original URL:",
    "Hosted Version:",
]


def score_description(description: str) -> dict:
    """Score an item's description quality. Higher = richer metadata."""
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

    desc_len = len(description)
    score += min(desc_len // 100, 30)

    return {
        "score": score,
        "sections_present": sections_present,
        "sections_missing": sections_missing,
        "description_length": desc_len,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Data classes
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class TVParsedTitle:
    """Result of parsing a TV episode filename."""

    show_name: str
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
    show_year: str | None = None  # year in show name for disambiguation


@dataclass
class ShowInfo:
    """Cached info about a TV show (from TMDb + OMDb)."""

    tmdb_id: int
    name: str
    first_air_date: str = ""
    last_air_date: str = ""
    overview: str = ""
    genres: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    status: str = ""
    num_seasons: int = 0
    num_episodes: int = 0
    imdb_id: str = ""
    content_rating: str = ""  # from OMDb (TV-MA, TV-14, etc.)
    omdb_rating: str = ""  # show-level IMDb rating from OMDb
    year_range: str = ""  # e.g. "1989–1996"
    cast: list[str] = field(default_factory=list)  # series regular actors


@dataclass
class EpisodeMetadata:
    """Merged episode metadata from TMDb + OMDb."""

    found: bool = False
    show_name: str = ""
    episode_title: str = ""
    season: int = 0
    episode: int = 0
    air_date: str = ""
    synopsis: str = ""
    runtime_min: int | None = None
    guest_stars: list[str] = field(default_factory=list)
    director: list[str] = field(default_factory=list)
    writer: list[str] = field(default_factory=list)
    tmdb_rating: str = ""
    imdb_rating: str = ""
    imdb_id: str = ""  # episode-level IMDb ID


@dataclass
class TVCandidate:
    """A CMS item identified as needing TV enrichment."""

    friendly_token: str
    raw_title: str
    description: str
    duration: int
    score: int
    parsed: TVParsedTitle | None
    cms_user: str = (
        ""  # original uploader — captured before any PUT so we can restore it
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Title parsing
# ══════════════════════════════════════════════════════════════════════════════

_EXT_RE = re.compile(r"\.\w{2,4}$")

# Scene / quality tags (shared with enrichmeta)
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
    r"|HDTV|PDTV|SDTV|DSR|PROPER"
    r")\b",
)

# YouTube video ID in brackets: [xAmEDgP-h6k]
_YOUTUBE_ID_RE = re.compile(r"\[[a-zA-Z0-9_-]{11}\]")

# S01E02, S1E2, S01 E02, s01e02, S04X01, S 4 E 11
_SXEX_RE = re.compile(r"\b[Ss]\s*(?P<s>\d{1,2})\s*[EeXx]\s*(?P<e>\d{1,3})")

# 1x03, 01x03
_NXNN_RE = re.compile(r"\b(?P<s>\d{1,2})[Xx](?P<e>\d{2,3})\b")

# "Season N" (text) — case-insensitive for SEASON / Season
_SEASON_TEXT_RE = re.compile(r"Season\s*(?P<s>\d{1,2})", re.IGNORECASE)

# "Episode N", "Ep N", "Episode #07" — case-insensitive
_EPISODE_TEXT_RE = re.compile(
    r"(?:Episode|Ep\.?)\s*#?\s*(?P<e>\d{1,3})",
    re.IGNORECASE,
)

# Bare "E14" (uppercase only) — used as fallback when Season text present
_BARE_E_RE = re.compile(r"\bE(?P<e>\d{1,3})\b")

# 3-digit episode code: first digit = season, last two = episode
# e.g.  "Morel Orel 101" → S01E01,  "Parker Lewis 225" → S02E25
_THREE_DIGIT_RE = re.compile(r"\b(?P<s>[1-9])(?P<e>\d{2})\b")

# Full-width pipe (U+FF5C) used in YouTube rips
_FULLWIDTH_PIPE = "\uff5c"

# Year in parentheses: (2003)
_YEAR_PAREN_RE = re.compile(r"\((\d{4})\)")

# Trailing bare year in show name: "The Twilight Zone 1959"
_SHOW_YEAR_TAIL_RE = re.compile(r"\s+((?:19|20)\d{2})\s*$")

# "airvideo" suffix
_AIRVIDEO_RE = re.compile(r"\s*-?\s*airvideo\s*$", re.I)


def _extract_show_year(show_part: str) -> tuple[str, str | None]:
    """Extract a disambiguation year from the show name.

    Returns (cleaned_show_name, year_or_None).
    Handles: 'Arrested Development (2003)' and 'The Twilight Zone 1959'.
    """
    show = show_part.strip()
    # Try (YYYY) first
    m = _YEAR_PAREN_RE.search(show)
    if m:
        year = m.group(1)
        show = (show[: m.start()] + show[m.end() :]).strip()
        return show, year
    # Try trailing bare year
    m = _SHOW_YEAR_TAIL_RE.search(show)
    if m:
        candidate = int(m.group(1))
        if 1920 <= candidate <= 2030:
            year = m.group(1)
            show = show[: m.start()].strip()
            return show, year
    return show, None


def _clean_episode_title(ep_part: str) -> str:
    """Clean up the episode title portion of a parsed filename."""
    title = ep_part.strip()
    # Strip fullwidth pipe and everything after (YouTube title noise)
    fw_idx = title.find(_FULLWIDTH_PIPE)
    if fw_idx != -1:
        title = title[:fw_idx]
    # Strip wrapping single quotes from episode names
    title = re.sub(r"^'(.+)'$", r"\1", title)
    # Strip scene tags
    title = _SCENE_TAGS_RE.sub("", title)
    # Strip YouTube IDs
    title = _YOUTUBE_ID_RE.sub("", title)
    # Strip [group] tags
    title = re.sub(r"\[\s*[A-Za-z0-9 _.+-]+\s*\]", "", title)
    # Strip (Without intro song) style parentheticals about file quality
    title = re.sub(r"\(Without\s+intro\s+song\)", "", title, flags=re.I)
    # Strip airvideo suffix
    title = _AIRVIDEO_RE.sub("", title)
    # Strip leading/trailing separators
    title = re.sub(r"^[\s\-–—:,.]+", "", title)
    title = re.sub(r"[\s\-–—:,.]+$", "", title)
    # Collapse whitespace
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _clean_show_name(show_part: str) -> str:
    """Clean up the show name portion of a parsed filename."""
    name = show_part.strip()
    # Truncate at tilde (show~episode separator in YouTube rips)
    # e.g. "Mr Belvedere ~ Gorgeous George - Season 1 Episode 4"
    if "~" in name:
        name = name.split("~", 1)[0].strip()
    # Strip parenthetical alt-names that aren't years: (MXC), (UK), etc.
    name = re.sub(r"\s*\([^)]*[a-zA-Z][^)]*\)", "", name)
    # Expand standalone "w" → "with" (lowercase only)
    # e.g. "I Think You Should Leave w Tim Robinson"
    name = re.sub(r"\bw\b", "with", name)
    # Strip leading "Watch" (YouTube title prefix)
    name = re.sub(r"^Watch\s+", "", name, flags=re.I)
    # Strip scene tags
    name = _SCENE_TAGS_RE.sub("", name)
    # Strip [group] tags
    name = re.sub(r"\[\s*[A-Za-z0-9 _.+-]+\s*\]", "", name)
    # Strip airvideo suffix
    name = _AIRVIDEO_RE.sub("", name)
    # Strip leading/trailing separators
    name = re.sub(r"^[\s\-–—:,.]+", "", name)
    name = re.sub(r"[\s\-–—:,.]+$", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def parse_tv_title(raw_title: str) -> TVParsedTitle | None:
    """Parse a TV episode filename into show name, season, episode, and title.

    Returns ``None`` if no season/episode pattern is detected.
    """
    title = raw_title.strip()
    title = _EXT_RE.sub("", title)

    # Normalise separators
    title = title.replace(".", " ").replace("_", " ")

    # Strip "(FULL EPISODE)" junk
    title = re.sub(r"\(FULL\s+EPISODE\)", "", title, flags=re.I)

    # Strip YouTube video IDs early: [xAmEDgP-h6k]
    title = _YOUTUBE_ID_RE.sub("", title)

    # Collapse whitespace
    title = re.sub(r"\s+", " ", title).strip()

    # ── Full-width pipe format (YouTube rips) ──────────────────────────────
    # e.g. "Show Name ｜ Season 2 ｜ Episode 3 [videoID]"
    if _FULLWIDTH_PIPE in title:
        parts = title.split(_FULLWIDTH_PIPE)
        show_part = parts[0].strip()
        season = episode = None
        for part in parts[1:]:
            sm = _SEASON_TEXT_RE.search(part)
            if sm:
                season = int(sm.group("s"))
            em = _EPISODE_TEXT_RE.search(part)
            if em:
                episode = int(em.group("e"))
        if season is not None and episode is not None:
            show_part = _YOUTUBE_ID_RE.sub("", show_part).strip()
            show_clean = _clean_show_name(show_part)
            show_name, show_year = _extract_show_year(show_clean)
            return TVParsedTitle(show_name, season, episode, None, show_year)

    # ── SxxExx pattern ─────────────────────────────────────────────────────
    m = _SXEX_RE.search(title)
    if m:
        show_part = title[: m.start()]
        ep_part = title[m.end() :]
        season = int(m.group("s"))
        episode = int(m.group("e"))

        # If "Season N" text appears inside show_part, the real show name
        # ends *before* that "Season" keyword.  Example:
        # "Small Wonder Season 3 E13 Oooga Mooga S3 E13 (...)"
        #  → show = "Small Wonder", S3E13, ep = "Oooga Mooga"
        sm_in_show = _SEASON_TEXT_RE.search(show_part)
        if sm_in_show:
            # Treat everything between "Season" and SxxExx as episode title
            extra_ep = show_part[sm_in_show.end() :].strip()
            show_part = show_part[: sm_in_show.start()]
            # Strip leading "E13" / "Ep 3" that is part of the episode marker
            extra_ep = re.sub(r"^[Ee](?:p\.?\s*)?\d{1,3}\s*", "", extra_ep).strip()
            # Prepend any extra text to ep_part
            if extra_ep:
                ep_part = extra_ep + " " + ep_part

        show_clean = _clean_show_name(show_part)
        show_name, show_year = _extract_show_year(show_clean)
        ep_title = _clean_episode_title(ep_part) or None

        # Discard episode title if it's just another SxxExx reference
        if ep_title and _SXEX_RE.fullmatch(ep_title.replace(" ", "")):
            ep_title = None

        return TVParsedTitle(show_name, season, episode, ep_title, show_year)

    # ── NxNN pattern ───────────────────────────────────────────────────────
    m = _NXNN_RE.search(title)
    if m:
        show_part = title[: m.start()]
        ep_part = title[m.end() :]
        season = int(m.group("s"))
        episode = int(m.group("e"))

        show_clean = _clean_show_name(show_part)
        show_name, show_year = _extract_show_year(show_clean)
        ep_title = _clean_episode_title(ep_part) or None

        return TVParsedTitle(show_name, season, episode, ep_title, show_year)

    # ── "Season N" + "Episode/Ep N" text format ────────────────────────────
    sm = _SEASON_TEXT_RE.search(title)
    em = _EPISODE_TEXT_RE.search(title)
    # Also accept bare "E14" (uppercase) when a Season marker exists
    if sm and not em:
        em = _BARE_E_RE.search(title)
    if sm and em:
        season = int(sm.group("s"))
        episode = int(em.group("e"))
        first_pos = min(sm.start(), em.start())
        last_end = max(sm.end(), em.end())

        show_part = title[:first_pos]
        ep_part = title[last_end:]

        show_clean = _clean_show_name(show_part)
        show_name, show_year = _extract_show_year(show_clean)
        ep_title = _clean_episode_title(ep_part) or None

        return TVParsedTitle(show_name, season, episode, ep_title, show_year)

    # ── 3-digit episode code (NNN → season = N, episode = NN) ─────────────
    # e.g. "Morel Orel 101 The Lords Greatest Gift" → S01E01
    #      "Parker Lewis Can't Lose 225 Diner 75"  → S02E25
    m = _THREE_DIGIT_RE.search(title)
    if m:
        before = title[: m.start()]
        # Only match if there's actual show text before the number
        if before.strip() and re.search(r"[a-zA-Z]", before):
            show_part = before
            ep_part = title[m.end() :]
            season = int(m.group("s"))
            episode = int(m.group("e"))

            show_clean = _clean_show_name(show_part)
            show_name, show_year = _extract_show_year(show_clean)
            ep_title = _clean_episode_title(ep_part) or None

            return TVParsedTitle(show_name, season, episode, ep_title, show_year)

    # ── Bare episode marker without season → default season 1 ─────────────
    # e.g. "Nightmares and Dreamscapes ep1 Battlegrounds" → S01E01
    #      "Star Trek The Animated Series E14 The Slaver Weapon" → S01E14
    em_only = _EPISODE_TEXT_RE.search(title)
    if not em_only:
        em_only = _BARE_E_RE.search(title)
    if em_only:
        before = title[: em_only.start()]
        if before.strip() and re.search(r"[a-zA-Z]", before):
            show_part = before
            ep_part = title[em_only.end() :]
            season = 1
            episode = int(em_only.group("e"))

            show_clean = _clean_show_name(show_part)
            show_name, show_year = _extract_show_year(show_clean)
            ep_title = _clean_episode_title(ep_part) or None

            return TVParsedTitle(show_name, season, episode, ep_title, show_year)

    # ── Could not parse ────────────────────────────────────────────────────
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  Similarity matching
# ══════════════════════════════════════════════════════════════════════════════

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
    "thirteen": "13",
}


def _normalise_for_compare(s: str) -> str:
    """Lower-case, strip articles/punctuation/extra whitespace,
    normalise number words to digits."""
    s = s.lower()
    s = s.replace("&", "and")
    s = _STRIP_ARTICLES_RE.sub("", s)
    s = _NON_ALNUM_RE.sub(" ", s)
    words = s.split()
    words = [_NUMBER_WORDS.get(w, w) for w in words]
    return " ".join(words)


def _titles_similar(query: str, result_title: str) -> bool:
    """Return True if *result_title* is a plausible match for *query*."""
    a = _normalise_for_compare(query)
    b = _normalise_for_compare(result_title)
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b and len(a) / len(b) >= 0.5:
        return True
    if b in a and len(b) / len(a) >= 0.5:
        return True
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio >= _TITLE_SIM_THRESHOLD


def _pick_best_show_result(
    results: list[dict],
    query: str,
    year: str | None,
) -> dict | None:
    """Return the first TMDb TV result whose name passes the similarity check.

    Prefers results whose first_air_date year matches *year* (if given).
    """
    ordered: list[dict] = []
    rest: list[dict] = []
    for r in results:
        fad = r.get("first_air_date", "")
        if year and fad and fad[:4] == year:
            ordered.append(r)
        else:
            rest.append(r)
    ordered.extend(rest)

    for show in ordered:
        if _titles_similar(query, show.get("name", "")):
            return show
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  API helpers
# ══════════════════════════════════════════════════════════════════════════════


def _api_call_with_backoff(
    session: requests.Session,
    method: str,
    url: str,
    delay: float,
    **kwargs,
) -> requests.Response:
    """Make an API call with automatic retry on 429 (rate limit)."""
    max_retries = 4
    backoff = max(delay, 1.0)

    for attempt in range(max_retries + 1):
        r = session.request(method, url, **kwargs)
        if r.status_code != 429:
            return r

        retry_after = r.headers.get("Retry-After")
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = backoff
        else:
            wait = backoff

        wait = min(wait, 60)
        print(
            f"         ** 429 rate-limited — waiting {wait:.0f}s "
            f"(attempt {attempt + 1}/{max_retries}) **"
        )
        time.sleep(wait)
        backoff *= 2

    return r  # return last response even if still 429


def _tmdb_auth(tmdb_token: str) -> tuple[dict, dict]:
    """Return (headers, extra_params) for TMDb API calls.

    If *tmdb_token* looks like a short v3 API key (hex, ≤40 chars) it is
    sent as ``?api_key=`` query parameter.  Otherwise it is treated as the
    long JWT read-access token used in the ``Authorization: Bearer`` header.
    """
    if len(tmdb_token) <= 40 and all(c in "0123456789abcdefABCDEF" for c in tmdb_token):
        return {}, {"api_key": tmdb_token}
    return {"Authorization": f"Bearer {tmdb_token}"}, {}


# ══════════════════════════════════════════════════════════════════════════════
#  CMS catalog fetcher
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


def _fetch_media_fallback(
    session: requests.Session,
    api_base: str,
) -> list[dict]:
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
#  TMDb TV provider
# ══════════════════════════════════════════════════════════════════════════════


def _search_tmdb_show(
    show_name: str,
    show_year: str | None,
    tmdb_token: str,
    session: requests.Session,
    delay: float,
) -> tuple[ShowInfo | None, list[dict]]:
    """Search TMDb for a TV show.

    Returns (ShowInfo_or_None, raw_search_results) so callers can offer
    interactive picking when similarity rejects all results.
    """
    headers, auth_params = _tmdb_auth(tmdb_token)
    params: dict = {"query": show_name, **auth_params}
    if show_year:
        params["first_air_date_year"] = int(show_year)

    try:
        r = _api_call_with_backoff(
            session,
            "GET",
            f"{TMDB_BASE}/search/tv",
            delay=delay,
            headers=headers,
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception:
        return None, []

    if r.status_code != 200:
        return None, []

    results = r.json().get("results", [])

    # Retry without year if no results
    if not results and show_year:
        params.pop("first_air_date_year", None)
        try:
            r = _api_call_with_backoff(
                session,
                "GET",
                f"{TMDB_BASE}/search/tv",
                delay=delay,
                headers=headers,
                params=params,
                timeout=DEFAULT_TIMEOUT,
            )
            if r.status_code == 200:
                results = r.json().get("results", [])
        except Exception:
            pass

    if not results:
        return None, []

    best = _pick_best_show_result(results, show_name, show_year)
    if best is None:
        return None, results  # results exist but none similar enough

    return _fetch_show_details(best["id"], tmdb_token, session, delay), results


def _fetch_show_details(
    show_id: int,
    tmdb_token: str,
    session: requests.Session,
    delay: float,
) -> ShowInfo:
    """Fetch full TMDb show details + external IDs."""
    headers, auth_params = _tmdb_auth(tmdb_token)
    info = ShowInfo(tmdb_id=show_id, name="")

    # Show details
    try:
        time.sleep(delay)
        r = _api_call_with_backoff(
            session,
            "GET",
            f"{TMDB_BASE}/tv/{show_id}",
            delay=delay,
            headers=headers,
            params=auth_params,
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            info.name = d.get("name", "")
            info.overview = d.get("overview", "")
            info.first_air_date = d.get("first_air_date", "")
            info.last_air_date = d.get("last_air_date", "")
            info.genres = [g["name"] for g in d.get("genres", [])]
            info.networks = [n["name"] for n in d.get("networks", [])]
            info.status = d.get("status", "")
            info.num_seasons = d.get("number_of_seasons", 0)
            info.num_episodes = d.get("number_of_episodes", 0)
    except Exception:
        pass

    # External IDs (for IMDb)
    try:
        time.sleep(delay)
        r = _api_call_with_backoff(
            session,
            "GET",
            f"{TMDB_BASE}/tv/{show_id}/external_ids",
            delay=delay,
            headers=headers,
            params=auth_params,
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code == 200:
            info.imdb_id = r.json().get("imdb_id", "") or ""
    except Exception:
        pass

    # Aggregate credits (series regulars)
    try:
        time.sleep(delay)
        r = _api_call_with_backoff(
            session,
            "GET",
            f"{TMDB_BASE}/tv/{show_id}/aggregate_credits",
            delay=delay,
            headers=headers,
            params=auth_params,
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code == 200:
            cast_list = r.json().get("cast", [])
            # Sort by total episode count descending, take top billed
            cast_list.sort(
                key=lambda c: c.get("total_episode_count", 0),
                reverse=True,
            )
            info.cast = [c["name"] for c in cast_list[:12]]
    except Exception:
        pass

    return info


def _fetch_tmdb_episode(
    show_id: int,
    season: int,
    episode: int,
    tmdb_token: str,
    session: requests.Session,
    delay: float,
) -> EpisodeMetadata:
    """Fetch a specific episode from TMDb.

    If the primary season lookup fails (e.g. the file is numbered S01E05 but
    TMDb's Season 1 only has 4 episodes), automatically retries Season 0.
    This covers shows that started as web/online series whose early episodes
    TMDb catalogues as specials (Season 0).
    """
    headers, auth_params = _tmdb_auth(tmdb_token)
    meta = EpisodeMetadata(season=season, episode=episode)

    # Build a small list of seasons to try: requested season first,
    # then Season 0 as a fallback — but only when the requested season is 1.
    # Season 0 on TMDb is the "pre-broadcast / web specials" bucket.  Shows
    # that started online often have early episodes there rather than in S01.
    # For Season 2+, a missing episode is genuinely absent from TMDb.
    seasons_to_try = [season]
    if season == 1:
        seasons_to_try.append(0)

    for try_season in seasons_to_try:
        try:
            time.sleep(delay)
            r = _api_call_with_backoff(
                session,
                "GET",
                f"{TMDB_BASE}/tv/{show_id}/season/{try_season}/episode/{episode}",
                delay=delay,
                headers=headers,
                params=auth_params,
                timeout=DEFAULT_TIMEOUT,
            )
        except Exception:
            continue

        if r.status_code == 200:
            if try_season != season:
                # Let the caller know we found it in a different season
                meta.season = try_season
            break
    else:
        # All attempts failed
        return meta

    if r.status_code != 200:
        return meta

    ep = r.json()
    meta.found = True
    meta.episode_title = ep.get("name", "")
    meta.air_date = ep.get("air_date", "")
    meta.synopsis = ep.get("overview", "")
    meta.runtime_min = ep.get("runtime")

    vote = ep.get("vote_average")
    if vote and vote > 0:
        meta.tmdb_rating = f"{vote:.1f}/10"

    # Crew
    crew = ep.get("crew", [])
    meta.director = [c["name"] for c in crew if c.get("job") == "Director"]
    meta.writer = [
        c["name"] for c in crew if c.get("job") in ("Writer", "Teleplay", "Story")
    ][:5]

    # Guest stars
    guests = ep.get("guest_stars", [])
    meta.guest_stars = [g["name"] for g in guests[:10]]

    return meta


# ══════════════════════════════════════════════════════════════════════════════
#  OMDb TV provider
# ══════════════════════════════════════════════════════════════════════════════


def _fetch_omdb_show(
    show_imdb_id: str,
    show_name: str,
    omdb_key: str,
    session: requests.Session,
    delay: float,
) -> dict:
    """Fetch show-level data from OMDb. Returns raw dict or {}."""
    if show_imdb_id:
        params: dict = {"apikey": omdb_key, "i": show_imdb_id}
    else:
        params = {"apikey": omdb_key, "t": show_name, "type": "series"}

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
            data = r.json()
            if data.get("Response") == "True":
                return data
    except Exception:
        pass
    return {}


def _enrich_show_from_omdb(info: ShowInfo, omdb_data: dict) -> None:
    """Merge OMDb show data into ShowInfo."""
    info.content_rating = omdb_data.get("Rated", "")
    if info.content_rating == "N/A":
        info.content_rating = ""
    info.omdb_rating = omdb_data.get("imdbRating", "")
    if info.omdb_rating == "N/A":
        info.omdb_rating = ""
    yr = omdb_data.get("Year", "")
    if yr and yr != "N/A":
        info.year_range = yr.replace("\u2013", "–")
    if not info.imdb_id:
        info.imdb_id = omdb_data.get("imdbID", "")


def _fetch_omdb_episode(
    show_imdb_id: str,
    show_name: str,
    season: int,
    episode: int,
    omdb_key: str,
    session: requests.Session,
    delay: float,
) -> dict:
    """Fetch a specific episode from OMDb. Returns raw dict or {}."""
    if show_imdb_id:
        params: dict = {
            "apikey": omdb_key,
            "i": show_imdb_id,
            "Season": season,
            "Episode": episode,
        }
    else:
        params = {
            "apikey": omdb_key,
            "t": show_name,
            "Season": season,
            "Episode": episode,
        }

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
            data = r.json()
            if data.get("Response") == "True":
                return data
    except Exception:
        pass
    return {}


# ══════════════════════════════════════════════════════════════════════════════
#  Description formatter
# ══════════════════════════════════════════════════════════════════════════════


def _format_year_range(show: ShowInfo) -> str:
    """Build year range string like '1989–1996' or '2014–'."""
    if show.year_range:
        return show.year_range
    start = show.first_air_date[:4] if show.first_air_date else ""
    end = show.last_air_date[:4] if show.last_air_date else ""
    if start and end and start != end:
        return f"{start}–{end}"
    if start:
        if show.status and show.status != "Ended":
            return f"{start}–"
        return start
    return ""


def format_tv_description(
    show: ShowInfo,
    ep_meta: EpisodeMetadata,
    omdb_ep: dict,
    existing_desc: str = "",
) -> str:
    """Build a structured description for a TV episode."""
    lines: list[str] = []

    # ── Show header ────────────────────────────────────────────────────────
    year_range = show.year_range or _format_year_range(show)
    if year_range:
        lines.append(f"{show.name} ({year_range})")
    else:
        lines.append(show.name)

    # Metadata line: genres | network | content rating
    meta_parts: list[str] = []
    if show.genres:
        meta_parts.append(", ".join(show.genres[:4]))
    if show.networks:
        meta_parts.append(show.networks[0])
    if show.content_rating:
        meta_parts.append(show.content_rating)
    if meta_parts:
        lines.append(" | ".join(meta_parts))

    lines.append("")

    # ── Episode header ─────────────────────────────────────────────────────
    ep_title = ep_meta.episode_title or omdb_ep.get("Title", "") or ""
    if ep_title and ep_title != "N/A":
        lines.append(
            f"Season {ep_meta.season}, Episode {ep_meta.episode}: " f'"{ep_title}"'
        )
    else:
        lines.append(f"Season {ep_meta.season}, Episode {ep_meta.episode}")

    # Air date
    air_date = ep_meta.air_date or omdb_ep.get("Released", "")
    if air_date and air_date != "N/A":
        lines.append(f"Air Date: {air_date}")

    lines.append("")

    # ── Synopsis ───────────────────────────────────────────────────────────
    synopsis = ep_meta.synopsis or ""
    omdb_plot = omdb_ep.get("Plot", "")
    if omdb_plot and omdb_plot != "N/A" and len(omdb_plot) > len(synopsis):
        synopsis = omdb_plot
    if synopsis:
        lines.append(synopsis)
        lines.append("")

    # ── Credits ────────────────────────────────────────────────────────────
    has_credits = False

    # Series cast (regulars from show-level data)
    if show.cast:
        lines.append(f"Cast: {', '.join(show.cast[:8])}")
        has_credits = True

    # Guest stars (episode-specific)
    guests = ep_meta.guest_stars
    if not guests:
        actors_str = omdb_ep.get("Actors", "")
        if actors_str and actors_str != "N/A":
            guests = [a.strip() for a in actors_str.split(",")]
    if guests:
        # Filter out anyone already in the series cast
        cast_set = set(show.cast) if show.cast else set()
        episode_guests = [g for g in guests if g not in cast_set][:8]
        if episode_guests:
            lines.append(f"Guest Stars: {', '.join(episode_guests)}")
            has_credits = True

    # Director
    directors = ep_meta.director
    if not directors:
        d = omdb_ep.get("Director", "")
        if d and d != "N/A":
            directors = [x.strip() for x in d.split(",")]
    if directors:
        lines.append(f"Directed by: {', '.join(directors)}")
        has_credits = True

    # Writer
    writers = ep_meta.writer
    if not writers:
        w = omdb_ep.get("Writer", "")
        if w and w != "N/A":
            writers = [x.strip() for x in w.split(",")]
    if writers:
        lines.append(f"Written by: {', '.join(writers[:5])}")
        has_credits = True

    if has_credits:
        lines.append("")

    # ── Ratings ────────────────────────────────────────────────────────────
    ratings: list[str] = []
    imdb = omdb_ep.get("imdbRating", "")
    if imdb and imdb != "N/A":
        ep_imdb_id = omdb_ep.get("imdbID", "")
        label = f"IMDb: {imdb}/10"
        if ep_imdb_id:
            label += f" (https://www.imdb.com/title/{ep_imdb_id}/)"
        ratings.append(label)
    if ep_meta.tmdb_rating:
        ratings.append(f"TMDb: {ep_meta.tmdb_rating}")

    if ratings:
        lines.append(" | ".join(ratings))

    # ── Preserve existing sections ─────────────────────────────────────────
    if existing_desc:
        for section_header in ("Original URL:", "Original Description:"):
            idx = existing_desc.find(section_header)
            if idx != -1:
                chunk = existing_desc[idx:]
                end = chunk.find("\n\n")
                if end != -1:
                    chunk = chunk[:end]
                lines.append(chunk.strip())
                lines.append("")

    return "\n".join(lines).strip()


# ══════════════════════════════════════════════════════════════════════════════
#  Show cache
# ══════════════════════════════════════════════════════════════════════════════


def _cache_key(name: str) -> str:
    """Normalise a show name for cache lookup.

    Strips articles, punctuation, normalises '&' → 'and', collapses
    whitespace.  'The Kids in the Hall' and 'Kids in the Hall' map to
    the same key.
    """
    s = name.lower().strip()
    s = re.sub(r"^the\s+", "", s)
    s = s.replace("&", "and")
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Show-name aliases for known mismatches between filenames and TMDb.
# Keys must be _cache_key()-normalised (lowercase, no articles/punctuation).
_SHOW_ALIASES: dict[str, str] = {
    "cosmos a spacetime odyssey": "Cosmos: A Spacetime Odyssey",
}


def _lookup_show(
    show_name: str,
    show_year: str | None,
    tmdb_token: str | None,
    omdb_key: str | None,
    session: requests.Session,
    delay: float,
    cache: dict[str, ShowInfo | None],
) -> tuple[ShowInfo | None, list[dict]]:
    """Look up a show, using the cache when possible.

    Returns (ShowInfo_or_None, raw_tmdb_search_results).
    """
    key = _cache_key(show_name)
    if key in cache:
        return cache[key], []

    # Use alias name for the TMDb search when available
    search_name = _SHOW_ALIASES.get(key, show_name)

    info: ShowInfo | None = None
    search_results: list[dict] = []

    if tmdb_token:
        info, search_results = _search_tmdb_show(
            search_name,
            show_year,
            tmdb_token,
            session,
            delay,
        )

    # Enrich with OMDb show-level data
    if info and omdb_key:
        omdb_show = _fetch_omdb_show(
            info.imdb_id,
            info.name,
            omdb_key,
            session,
            delay,
        )
        if omdb_show:
            _enrich_show_from_omdb(info, omdb_show)

    # Build year_range from TMDb dates if OMDb didn't provide one
    if info and not info.year_range:
        info.year_range = _format_year_range(info)

    cache[key] = info
    return info, search_results


# ══════════════════════════════════════════════════════════════════════════════
#  Candidate finder
# ══════════════════════════════════════════════════════════════════════════════


def find_tv_candidates(
    all_media: list[dict],
    min_duration: int,
    max_duration: int,
    min_score: int,
) -> list[TVCandidate]:
    """Scan catalog for TV episodes needing enrichment."""
    candidates: list[TVCandidate] = []

    for item in all_media:
        dur = item.get("duration") or 0
        if dur < min_duration or dur > max_duration:
            continue

        raw_title = item.get("title", "")

        desc = item.get("description", "") or ""
        quality = score_description(desc)
        if quality["score"] >= min_score:
            continue

        parsed = parse_tv_title(raw_title)

        candidates.append(
            TVCandidate(
                friendly_token=item.get("friendly_token", ""),
                raw_title=raw_title,
                description=desc,
                duration=dur,
                score=quality["score"],
                parsed=parsed,
                cms_user=item.get("user", ""),
            )
        )

    # Sort by show name then season/episode for efficient caching
    def sort_key(c: TVCandidate) -> tuple:
        if c.parsed and c.parsed.show_name:
            return (
                0,
                _cache_key(c.parsed.show_name),
                c.parsed.season or 0,
                c.parsed.episode or 0,
            )
        return (1, "~~~unparsed", 0, 0)

    candidates.sort(key=sort_key)
    return candidates


# ══════════════════════════════════════════════════════════════════════════════
#  Report
# ══════════════════════════════════════════════════════════════════════════════


def run_report(candidates: list[TVCandidate]) -> None:
    """Print a table of TV candidates with scores and parsed info."""
    parseable = sum(1 for c in candidates if c.parsed)
    unparseable = len(candidates) - parseable
    empty = sum(1 for c in candidates if c.score == 0)
    low = sum(1 for c in candidates if 0 < c.score < 50)

    # Count unique shows
    shows = set()
    for c in candidates:
        if c.parsed:
            shows.add(_cache_key(c.parsed.show_name))

    print(f"\n  Candidates: {len(candidates)}")
    print(f"    Parseable (have S/E):  {parseable}")
    print(f"    Unparseable:           {unparseable}")
    print(f"    Empty description:     {empty}")
    print(f"    Low-scoring:           {low}")
    print(f"    Unique shows:          {len(shows)}")
    print()

    col_t = 55
    col_s = 30
    print(f"  {'SCORE':>5}  {'DUR':>5}  {'TITLE':<{col_t}}  " f"{'SHOW':<{col_s}}  S/E")
    print(f"  {'─'*5}  {'─'*5}  {'─'*col_t}  {'─'*col_s}  {'─'*10}")

    limit = min(len(candidates), 150)
    for c in candidates[:limit]:
        dur_m = c.duration // 60
        t_disp = c.raw_title[:col_t]
        if c.parsed:
            s_disp = c.parsed.show_name[:col_s]
            se = (
                f"S{c.parsed.season:02d}E{c.parsed.episode:02d}"
                if c.parsed.season is not None and c.parsed.episode is not None
                else "?"
            )
        else:
            s_disp = "(unparsed)"
            se = "--"
        print(
            f"  {c.score:5d}  {dur_m:4d}m  {t_disp:<{col_t}}  "
            f"{s_disp:<{col_s}}  {se}"
        )

    if len(candidates) > limit:
        print(f"  ... and {len(candidates) - limit} more.")


# ══════════════════════════════════════════════════════════════════════════════
#  Interactive helpers
# ══════════════════════════════════════════════════════════════════════════════


def _interactive_pick_show(results: list[dict]) -> dict | None:
    """Show numbered TMDb TV results for the user to pick from."""
    show_results = results[:8]
    for i, show in enumerate(show_results, 1):
        name = show.get("name", "?")
        year = (show.get("first_air_date") or "?")[:4]
        overview = (show.get("overview") or "")[:80]
        if overview:
            print(f"           {i}. {name} ({year})  {overview}")
        else:
            print(f"           {i}. {name} ({year})")
    print("           0. Skip")
    while True:
        try:
            choice = input("         Pick [0 to skip]: ").strip()
        except EOFError:
            return None
        if choice == "0" or choice == "":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(show_results):
            return show_results[int(choice) - 1]


def _interactive_show_miss(
    prefix: str,
    title_disp: str,
    show_name: str,
    show_year: str | None,
    tmdb_token: str | None,
    omdb_key: str | None,
    session: requests.Session,
    delay: float,
    cache: dict[str, ShowInfo | None],
) -> ShowInfo | None | str:
    """Prompt user for corrected show name on MISS.

    Returns ShowInfo if found, None if skipped, or the string ``'quit'``
    to disable interactive prompting.
    """
    print(f"{prefix} ???   {title_disp}")
    print(f'         Show "{show_name}" not found.')
    print("         Type corrected show name, Enter to skip, 'q' to stop:")
    while True:
        try:
            hint = input("         >> ").strip()
        except EOFError:
            return None
        if hint == "":
            return None
        if hint.lower() == "q":
            return "quit"

        if not tmdb_token:
            print("         (no TMDb key — cannot search)")
            return None

        headers, auth_params = _tmdb_auth(tmdb_token)
        try:
            r = _api_call_with_backoff(
                session,
                "GET",
                f"{TMDB_BASE}/search/tv",
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
                f'         No results for "{hint}" — try again '
                "or press Enter to skip."
            )
            continue

        picked = _interactive_pick_show(results)
        if picked is None:
            print("         Skipped.")
            return None

        # Fetch full show details
        info = _fetch_show_details(
            picked["id"],
            tmdb_token,
            session,
            delay,
        )
        if info and omdb_key:
            omdb_show = _fetch_omdb_show(
                info.imdb_id,
                info.name,
                omdb_key,
                session,
                delay,
            )
            if omdb_show:
                _enrich_show_from_omdb(info, omdb_show)
        if info and not info.year_range:
            info.year_range = _format_year_range(info)

        # Cache under both the original key and the hint key
        cache[_cache_key(show_name)] = info
        cache[_cache_key(hint)] = info

        return info


# ══════════════════════════════════════════════════════════════════════════════
#  Enrichment loop
# ══════════════════════════════════════════════════════════════════════════════


def _fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def run_enrichment(
    candidates: list[TVCandidate],
    cms_session: requests.Session,
    api_base: str,
    tmdb_token: str | None,
    omdb_key: str | None,
    commit: bool = False,
    limit: int | None = None,
    delay: float = REQUEST_DELAY,
    interactive: bool = False,
) -> tuple[int, int, int]:
    """Run the TV enrichment pipeline.

    Returns (enriched_count, skipped_count, failed_count).
    """
    enriched = 0
    skipped = 0
    failed = 0
    skipped_items: list[tuple[str, str]] = []  # (title, reason)
    show_cache: dict[str, ShowInfo | None] = {}
    lookup_session = requests.Session()
    api_calls = 0
    t0 = time.time()

    to_process = candidates[:limit] if limit else candidates
    total = len(to_process)

    try:
        for idx, c in enumerate(to_process, 1):
            prefix = f"  [{idx}/{total}]"
            title_disp = c.raw_title[:60]

            # ── Unparseable title ──────────────────────────────────────────
            if c.parsed is None:
                print(f"{prefix} SKIP  {title_disp}  (no season/episode)")
                skipped += 1
                skipped_items.append((c.raw_title, "no season/episode"))
                continue

            p = c.parsed

            if p.season is None or p.episode is None:
                print(f"{prefix} SKIP  {title_disp}  (no season/episode)")
                skipped += 1
                skipped_items.append((c.raw_title, "no season/episode"))
                continue

            # ── Show lookup (cached) ───────────────────────────────────────
            show_info, search_results = _lookup_show(
                p.show_name,
                p.show_year,
                tmdb_token,
                omdb_key,
                lookup_session,
                delay,
                show_cache,
            )

            # Track API calls (rough: search=1, details=1, external_ids=1,
            # omdb_show=1 for first episode of each show)
            key = _cache_key(p.show_name)
            # We only made API calls if this was a cache miss
            # (the lookup function handles caching internally)

            if show_info is None:
                # Interactive: if search returned results but similarity
                # rejected them, let user pick
                if interactive and search_results:
                    print(f"{prefix} ???   {title_disp}")
                    print(
                        f'         Show "{p.show_name}" — no good match. '
                        "Pick from results:"
                    )
                    picked = _interactive_pick_show(search_results)
                    if picked is not None:
                        show_info = _fetch_show_details(
                            picked["id"],
                            tmdb_token or "",
                            lookup_session,
                            delay,
                        )
                        if show_info and omdb_key:
                            omdb_show = _fetch_omdb_show(
                                show_info.imdb_id,
                                show_info.name,
                                omdb_key,
                                lookup_session,
                                delay,
                            )
                            if omdb_show:
                                _enrich_show_from_omdb(show_info, omdb_show)
                        if show_info and not show_info.year_range:
                            show_info.year_range = _format_year_range(
                                show_info,
                            )
                        show_cache[key] = show_info

                # Interactive: no search results at all → prompt for name
                if show_info is None and interactive:
                    result = _interactive_show_miss(
                        prefix,
                        title_disp,
                        p.show_name,
                        p.show_year,
                        tmdb_token,
                        omdb_key,
                        lookup_session,
                        delay,
                        show_cache,
                    )
                    if result == "quit":
                        interactive = False
                    elif isinstance(result, ShowInfo):
                        show_info = result
                        show_cache[key] = show_info

                if show_info is None:
                    print(
                        f"{prefix} MISS  {title_disp}  "
                        f'<- show "{p.show_name}" not found'
                    )
                    skipped += 1
                    skipped_items.append(
                        (c.raw_title, f'show "{p.show_name}" not found')
                    )
                    continue

            # ── Episode lookup ─────────────────────────────────────────────
            ep_meta = EpisodeMetadata(
                season=p.season,
                episode=p.episode,
                show_name=show_info.name,
            )
            omdb_ep: dict = {}

            if tmdb_token and show_info.tmdb_id:
                ep_meta = _fetch_tmdb_episode(
                    show_info.tmdb_id,
                    p.season,
                    p.episode,
                    tmdb_token,
                    lookup_session,
                    delay,
                )
                ep_meta.season = p.season
                ep_meta.episode = p.episode
                ep_meta.show_name = show_info.name
                api_calls += 1

            if omdb_key:
                omdb_ep = _fetch_omdb_episode(
                    show_info.imdb_id,
                    show_info.name,
                    p.season,
                    p.episode,
                    omdb_key,
                    lookup_session,
                    delay,
                )
                api_calls += 1

            time.sleep(delay)

            # Check if we got anything useful
            has_synopsis = bool(
                ep_meta.synopsis or (omdb_ep.get("Plot") and omdb_ep["Plot"] != "N/A")
            )
            has_any_data = has_synopsis or ep_meta.found or omdb_ep

            if not has_any_data:
                print(
                    f"{prefix} MISS  {title_disp}  "
                    f"<- S{p.season:02d}E{p.episode:02d} not found"
                )
                skipped += 1
                skipped_items.append(
                    (c.raw_title, f"episode S{p.season:02d}E{p.episode:02d} not found")
                )
                continue

            # ── Format description ─────────────────────────────────────────
            desc = format_tv_description(
                show_info,
                ep_meta,
                omdb_ep,
                c.description,
            )

            # ── Canonical title ────────────────────────────────────────────
            ep_title = (
                ep_meta.episode_title
                or omdb_ep.get("Title", "")
                or p.episode_title
                or ""
            )
            if ep_title and ep_title != "N/A":
                canonical = (
                    f"{show_info.name} - "
                    f"S{p.season:02d}E{p.episode:02d} - "
                    f"{ep_title}"
                )
            else:
                canonical = f"{show_info.name} - " f"S{p.season:02d}E{p.episode:02d}"

            # CMS has a 100-char title limit; truncate gracefully
            if len(canonical) > 100:
                canonical = canonical[:97] + "..."

            # ── Score before / after ────────────────────────────────────
            score_before = c.score
            score_after = score_description(desc)["score"]

            # ── Output ─────────────────────────────────────────────────────
            tag = "ENRICH" if commit else "MATCH"
            print(f"{prefix} {tag}  {title_disp}")
            print(f"          -> {canonical}")
            print(f"          score: {score_before} -> {score_after}")

            print()
            for line in desc.splitlines():
                print(f"          | {line}")
            print()

            if commit:
                try:
                    payload: dict = {
                        "description": desc,
                        "title": canonical,
                    }
                    r = _api_call_with_backoff(
                        cms_session,
                        "PUT",
                        f"{api_base}/media/{c.friendly_token}",
                        delay=delay,
                        data=payload,
                        timeout=DEFAULT_TIMEOUT,
                    )
                    if r.status_code in (200, 201):
                        enriched += 1
                        # MediaCMS bug: PUT /media/{token} overwrites the
                        # owner with request.user (the admin token).  Restore
                        # the original uploader immediately after each commit.
                        if c.cms_user:
                            _restore_owner(
                                cms_session,
                                api_base,
                                c.friendly_token,
                                c.cms_user,
                                delay,
                            )
                    else:
                        detail = ""
                        try:
                            detail = f" -- {r.text[:200]}"
                        except Exception:
                            pass
                        print(f"          !! CMS returned {r.status_code}{detail}")
                        failed += 1
                except Exception as exc:
                    print(f"          !! CMS error: {exc}")
                    failed += 1
            else:
                enriched += 1

    except KeyboardInterrupt:
        elapsed = time.time() - t0
        print(
            f"\n\n  *** Interrupted after {_fmt_elapsed(elapsed)} "
            f"({api_calls} API calls) ***"
        )
        print(
            f"  Processed so far: enriched={enriched}  "
            f"skipped={skipped}  failed={failed}"
        )
        return enriched, skipped, failed

    elapsed = time.time() - t0
    print(
        f"\n  *** Completed in {_fmt_elapsed(elapsed)} " f"({api_calls} API calls) ***"
    )

    if skipped_items:
        # Group by reason
        reason_groups: dict[str, list[str]] = {}
        for title, reason in skipped_items:
            reason_groups.setdefault(reason, []).append(title)

        print(f"\n  Skipped items ({len(skipped_items)}):")
        for reason, titles in sorted(reason_groups.items()):
            print(f"\n    {reason} ({len(titles)}):")
            for t in titles[:20]:
                print(f"      - {t[:80]}")
            if len(titles) > 20:
                print(f"      ... and {len(titles) - 20} more")

    return enriched, skipped, failed


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Enrich TV episode metadata in MediaCMS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s --token CMS --tmdb-key KEY --omdb-key KEY --report
  %(prog)s --token CMS --tmdb-key KEY --omdb-key KEY --limit 20
  %(prog)s --token CMS --tmdb-key KEY --omdb-key KEY -i --limit 50
  %(prog)s --token CMS --tmdb-key KEY --omdb-key KEY --commit
""",
    )
    p.add_argument("--token", required=True, help="MediaCMS API token.")
    p.add_argument(
        "--tmdb-key", default=None, help="TMDb API key or read-access token (Bearer)."
    )
    p.add_argument("--omdb-key", default=None, help="OMDb API key.")
    p.add_argument(
        "--report",
        action="store_true",
        help="Scan & score only — don't look up metadata.",
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help="Push enriched data to CMS (default: dry-run).",
    )
    p.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Prompt for corrections on missed shows.",
    )
    p.add_argument(
        "--limit", type=int, default=None, help="Process at most N candidates."
    )
    p.add_argument(
        "--min-score",
        type=int,
        default=MIN_SCORE_THRESHOLD,
        help="Only enrich items scoring below N (default 50).",
    )
    p.add_argument(
        "--min-duration",
        type=int,
        default=MIN_DURATION,
        help="Min duration in seconds (default 600 = 10 min).",
    )
    p.add_argument(
        "--max-duration",
        type=int,
        default=MAX_DURATION,
        help="Max duration in seconds (default 3599 = ~59 min).",
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
        help="Delay between API calls (default 0.25s).",
    )
    p.add_argument("--api-url", default=API_BASE, help="MediaCMS API base URL.")
    return p


def main(argv: list[str] | None = None) -> int:
    # Fix encoding on Windows
    if sys.platform == "win32":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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
    min_m = args.min_duration // 60
    max_m = args.max_duration // 60

    print(f"\n{'='*60}")
    print(f"  enrichtv  --  Mode: {mode}")
    print(
        f"  TV episodes: {min_m}–{max_m} min  |  "
        f"Score threshold: < {args.min_score}"
    )
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
    print(f"{'='*60}")

    # ── CMS session ────────────────────────────────────────────────────────
    cms_session = requests.Session()
    cms_session.headers["Authorization"] = f"Token {args.token}"

    # ── Fetch catalog ──────────────────────────────────────────────────────
    print("\n  Fetching media catalog ...")
    all_media = fetch_all_media(cms_session, api_base)

    if not all_media:
        print("  No media found in CMS.")
        return 1

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
    print("\n  Scanning for TV enrichment candidates ...")
    candidates = find_tv_candidates(
        all_media,
        args.min_duration,
        args.max_duration,
        args.min_score,
    )

    if not candidates:
        print("\n  All TV episodes are already enriched. Nothing to do!")
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
    print(f"  {action}: {enriched}  |  Skipped: {skipped}  |  " f"Failed: {failed}")
    print(f"{'='*60}\n")

    return 0


# ── Headless entry point for the webqueue job runner ───────────────────────────


def run(params: dict, *, config, progress=None) -> dict:
    """Run TV-episode enrichment headlessly (no argparse/interactive).

    ``params`` keys: ``dry_run`` (bool), ``limit`` (int|None), ``days`` (int|None),
    ``min_score`` (int), ``min_duration`` (int), ``max_duration`` (int),
    ``delay`` (float). TMDb/OMDb keys + MediaCMS creds come from ``config``.
    Returns a counts dict for ``job_runs.detail``.
    """
    api_base = f"{config.mediacms_url.rstrip('/')}/api/v1"
    dry_run = bool(params.get("dry_run", False))
    limit = params.get("limit")
    days = params.get("days")
    min_score = params.get("min_score", MIN_SCORE_THRESHOLD)
    min_duration = params.get("min_duration", MIN_DURATION)
    max_duration = params.get("max_duration", MAX_DURATION)
    delay = params.get("delay", REQUEST_DELAY)

    tmdb_key = getattr(config, "tmdb_api_key", "") or ""
    omdb_key = getattr(config, "omdb_api_key", "") or ""
    if not tmdb_key and not omdb_key:
        raise RuntimeError("enrichtv requires a TMDb or OMDb API key (none configured)")

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

    candidates = find_tv_candidates(
        all_media,
        min_duration,
        max_duration,
        min_score,
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
