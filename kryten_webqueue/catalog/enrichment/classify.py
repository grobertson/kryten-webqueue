"""Item classification — hosted-show detection, content-type determination.

Single authoritative source for HOSTED_SHOW_REGISTRY and all classification
logic, replacing scattered _HOSTED_PATTERNS lists in enrichmeta.py and images.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .normalise import normalize_movie_title

# ---------------------------------------------------------------------------
# Hosted show registry
# ---------------------------------------------------------------------------


@dataclass
class HostedShowEntry:
    pattern: re.Pattern
    show_name: str
    content_type: str  # "hosted_movie" | "riffed_movie"
    title_treatment: str  # "keep" | "reformat"
    reformat_template: str | None
    cms_tag: str


HOSTED_SHOW_REGISTRY: list[HostedShowEntry] = [
    HostedShowEntry(
        re.compile(r"Joe\s*Bob'?s?\s+Drive[\s\-]*In\s+Theater", re.I),
        "Joe Bob's Drive-In Theater",
        "hosted_movie",
        "keep",
        None,
        "joebobbriggs",
    ),
    # "JBB" shorthand for Joe Bob Briggs
    HostedShowEntry(
        re.compile(r"\bJBB\b", re.I),
        "Joe Bob Briggs",
        "hosted_movie",
        "keep",
        None,
        "joebobbriggs",
    ),
    HostedShowEntry(
        re.compile(r"JBBTLDI|Joe\s*Bob\s+TLDI", re.I),
        "The Last Drive-In with Joe Bob Briggs",
        "hosted_movie",
        "keep",
        None,
        "lastdrivein",
    ),
    # "TLDI" shorthand for The Last Drive-In
    HostedShowEntry(
        re.compile(r"\bTLDI\b", re.I),
        "The Last Drive-In with Joe Bob Briggs",
        "hosted_movie",
        "keep",
        None,
        "lastdrivein",
    ),
    HostedShowEntry(
        re.compile(r"(?:The\s+)?Last\s+Drive[\s\-]*In", re.I),
        "The Last Drive-In with Joe Bob Briggs",
        "hosted_movie",
        "keep",
        None,
        "lastdrivein",
    ),
    HostedShowEntry(
        re.compile(r"Monster\s*Vision", re.I),
        "MonsterVision with Joe Bob Briggs",
        "hosted_movie",
        "keep",
        None,
        "monstervision",
    ),
    HostedShowEntry(
        re.compile(r"Svengoolie", re.I),
        "Svengoolie",
        "hosted_movie",
        "keep",
        None,
        "svengoolie",
    ),
    HostedShowEntry(
        re.compile(r"13\s+Nights?\s+of\s+Elvira", re.I),
        "13 Nights of Elvira",
        "hosted_movie",
        "keep",
        None,
        "elvira",
    ),
    HostedShowEntry(
        re.compile(r"Riff\s*Trax(?:\s+Live)?", re.I),
        "RiffTrax",
        "riffed_movie",
        "reformat",
        "RiffTrax Presents: {title} ({year})",
        "rifftrax",
    ),
    HostedShowEntry(
        re.compile(r"MST3K|Mystery\s*Science\s*Theater", re.I),
        "Mystery Science Theater 3000",
        "riffed_movie",
        "keep",
        None,
        "mst3k",
    ),
]

# ---------------------------------------------------------------------------
# TV episode detection
# ---------------------------------------------------------------------------

_TV_RE = re.compile(
    r"(?:[Ss]\d{1,2}\s*[Ee]\d{1,2}"
    r"|\b\d{1,2}[Xx]\d{2,3}\b"
    r"|\b[Ss]eason\s*\d+"
    r"|\b[Ee]pisode\s*\d+"
    r"|\b[Ee][Pp]\.?\s*\d+"
    r")"
)
_SXE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")

# ---------------------------------------------------------------------------
# Archive detection (broadcast dates, wrestling, etc.)
# ---------------------------------------------------------------------------

_ARCHIVE_RE = re.compile(
    r"(?:\b(?:19|20)\d{2}-\d{2}-\d{2}\b"  # ISO date
    r"|\b\d{1,2}-\d{1,2}-(?:19|20)?\d{2}\b"  # MM-DD-YY
    r"|\bWWF\b|\bWCW\b|\bNWA\b|\bAWA\b"  # wrestling promotions
    r"|Complete\s+Broadcast"
    r"|Original\s+Broadcast"
    r")",
    re.I,
)

# ---------------------------------------------------------------------------
# Hosted extraction helpers (adapted from enrichmeta.py detect_hosted)
# ---------------------------------------------------------------------------

_EXT_RE = re.compile(r"\.\w{2,5}$")
_YEAR_PAREN_RE = re.compile(r"\((\d{4})\)")
_YEAR_BRACKET_RE = re.compile(r"[\[\]{}]\s*(\d{4})\s*[\[\]{}]")
_YEAR_BARE_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_DATE_RE = re.compile(
    r"(?:(?:19|20)\d{2}-\d{1,2}-\d{1,2})|(?:\d{1,2}-\d{1,2}-(?:19|20)?\d{2})"
)
_EPISODE_CODE_RE = re.compile(r"[SW]\d+[eE]\d+", re.I)
_SHOW_YEAR_PREFIX_RE = re.compile(
    r"^\s*\(\d{4}\)\s*S\d+-Wk\s*\d+\s*Film\s*\d+\s*[-\u2013\u2014]?\s*", re.I
)
_WEEK_PREFIX_RE = re.compile(
    r"(?:S\d+-)?(?:Wk|Week)\s*\d+\s*[-\u2013\u2014]?\s*(?:(?:Film|Movie)\s*\d+\s*[-\u2013\u2014]?\s*)?",
    re.I,
)
_LEADING_EPNUM_RE = re.compile(r"^\s*\d{1,2}\s*[-\u2013\u2014]?\s+")
_SCENE_HOSTED_RE = re.compile(
    r"\[?(?:720p|480p|1080p|who(?:do)?dude\w*|CG|Hybrid)\]?", re.I
)


@dataclass
class HostedInfo:
    show_name: str
    movie_title: str
    movie_year: str | None
    cms_tag: str = ""


def _extract_hosted_title(raw: str, entry: HostedShowEntry) -> HostedInfo:
    """Strip show name from raw title and extract the underlying movie title + year."""
    title = _EXT_RE.sub("", raw.strip())

    # remove the matched show pattern
    m = entry.pattern.search(title)
    if m:
        title = title[: m.start()] + title[m.end() :]

    # remove any remaining registry patterns (handles double-show mentions)
    for e in HOSTED_SHOW_REGISTRY:
        title = e.pattern.sub("", title)

    title = _SCENE_HOSTED_RE.sub("", title)
    title = title.replace("_", " ")
    title = _DATE_RE.sub("", title)
    title = _SHOW_YEAR_PREFIX_RE.sub("", title)
    title = _EPISODE_CODE_RE.sub("", title)
    title = _WEEK_PREFIX_RE.sub("", title)
    title = re.sub(r"^[\s\-\u2013\u2014:,]+", "", title)
    title = _LEADING_EPNUM_RE.sub("", title)
    title = re.sub(r"^[\s\-\u2013\u2014:,]+", "", title)
    title = _LEADING_EPNUM_RE.sub("", title)

    year: str | None = None
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
            m = _YEAR_BARE_RE.search(title)
            if m and 1920 <= int(m.group(1)) <= 2030:
                year = m.group(1)
                title = title[: m.start()] + title[m.end() :]

    title = re.sub(r"\[\s*\]|\(\s*\)", "", title)
    title = re.sub(r"^[\s\-\u2013\u2014:,]+", "", title)
    title = re.sub(r"[\s\-\u2013\u2014:,]+$", "", title)
    title = re.sub(r"\s*-\s*\d+$", "", title)
    title = re.sub(r"\s+", " ", title).strip()

    return HostedInfo(
        show_name=entry.show_name,
        movie_title=title or raw,
        movie_year=year,
        cms_tag=entry.cms_tag,
    )


# ---------------------------------------------------------------------------
# ItemClassification
# ---------------------------------------------------------------------------


@dataclass
class ItemClassification:
    friendly_token: str
    raw_title: str
    content_type: (
        str  # movie | tv_episode | hosted_movie | riffed_movie | archive | unknown
    )
    hosted: HostedInfo | None
    lookup_title: str  # used for TMDB/OMDB queries and art resolution
    lookup_year: str | None
    genre_hints: list[str] = field(default_factory=list)
    tv_show: str | None = None
    tv_season: int | None = None
    tv_episode: int | None = None
    duration_sec: int = 0
    description_score: int = 0
    has_real_art: bool = False
    imdb_tt: str | None = None  # admin-set canonical IMDB tt identifier
    tmdb_id: str | None = None  # cached TMDB id from a prior identify run
    description: str | None = None  # item description (scanned for an IMDb tt#)
    source_url: str | None = None  # source/manifest URL (scanned for an IMDb tt#)


def classify_item(
    friendly_token: str,
    raw_title: str,
    duration_sec: int,
    *,
    cover_art_source: str | None = None,
    description: str | None = None,
    description_score: int = 0,
    imdb_tt: str | None = None,
    source_url: str | None = None,
) -> ItemClassification:
    """Classify a single catalog item.  Pure function; no DB access."""
    has_real_art = (cover_art_source or "") in ("tmdb", "omdb")

    # 1. Hosted / riffed — check registry first
    for entry in HOSTED_SHOW_REGISTRY:
        if entry.pattern.search(raw_title):
            info = _extract_hosted_title(raw_title, entry)
            return ItemClassification(
                friendly_token=friendly_token,
                raw_title=raw_title,
                content_type=entry.content_type,
                hosted=info,
                lookup_title=info.movie_title,
                lookup_year=info.movie_year,
                duration_sec=duration_sec,
                description_score=description_score,
                has_real_art=has_real_art,
                imdb_tt=imdb_tt,
                description=description,
                source_url=source_url,
            )

    # 2. TV episode
    if _TV_RE.search(raw_title):
        tv_show: str | None = None
        tv_season: int | None = None
        tv_ep: int | None = None
        m = _SXE_RE.search(raw_title)
        if m:
            tv_season = int(m.group(1))
            tv_ep = int(m.group(2))
            tv_show = raw_title[: m.start()].strip(" -–—")
        return ItemClassification(
            friendly_token=friendly_token,
            raw_title=raw_title,
            content_type="tv_episode",
            hosted=None,
            lookup_title=tv_show or raw_title,
            lookup_year=None,
            tv_show=tv_show,
            tv_season=tv_season,
            tv_episode=tv_ep,
            duration_sec=duration_sec,
            description_score=description_score,
            has_real_art=has_real_art,
            imdb_tt=imdb_tt,
            description=description,
            source_url=source_url,
        )

    # 3. Archive (broadcast recordings, wrestling)
    if duration_sec > 1800 and _ARCHIVE_RE.search(raw_title):
        return ItemClassification(
            friendly_token=friendly_token,
            raw_title=raw_title,
            content_type="archive",
            hosted=None,
            lookup_title=raw_title,
            lookup_year=None,
            duration_sec=duration_sec,
            description_score=description_score,
            has_real_art=has_real_art,
            imdb_tt=imdb_tt,
            description=description,
            source_url=source_url,
        )

    # 4. Movie (>= 30 min)
    if duration_sec >= 1800:
        # Handles YouTube pipe titles and dub/sub markers; year is pulled from
        # the whole string so a trailing "(1961)" after pipes is still captured.
        clean_title, year = normalize_movie_title(raw_title, for_search=True)
        return ItemClassification(
            friendly_token=friendly_token,
            raw_title=raw_title,
            content_type="movie",
            hosted=None,
            lookup_title=clean_title,
            lookup_year=year,
            duration_sec=duration_sec,
            description_score=description_score,
            has_real_art=has_real_art,
            imdb_tt=imdb_tt,
            description=description,
            source_url=source_url,
        )

    # 5. Unknown / short
    return ItemClassification(
        friendly_token=friendly_token,
        raw_title=raw_title,
        content_type="unknown",
        hosted=None,
        lookup_title=raw_title,
        lookup_year=None,
        duration_sec=duration_sec,
        description_score=description_score,
        has_real_art=has_real_art,
        imdb_tt=imdb_tt,
        description=description,
        source_url=source_url,
    )
