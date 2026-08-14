"""Single canonical copy of all title-normalisation helpers.

Replaces the five scattered implementations in sync.py, images.py,
enrichtitles.py, enrichmeta.py (parse_standard_title), and enrichmeta.py
(detect_hosted).  All enrichment steps import from here.
"""

import re

# ---------------------------------------------------------------------------
# Extension stripping
# ---------------------------------------------------------------------------

_EXT_RE = re.compile(
    r"\s*\.(mp4|mkv|avi|mov|wmv|m4v|webm|flv|ts|mpg|mpeg|m2ts|vob|divx|xvid)$",
    re.IGNORECASE,
)


def strip_extension(title: str) -> str:
    """Strip a video file extension from the end of a title string."""
    return _EXT_RE.sub("", title)


# ---------------------------------------------------------------------------
# Leading-year normalisation
# ---------------------------------------------------------------------------

_LEADING_YEAR_RE = re.compile(r"^\s*[\(\[]((?:19|20)\d{2})[\)\]]\s*(.+)")


def normalize_leading_year(title: str) -> str:
    """Move a leading ``(YYYY)`` to trailing position.

    ``(1989) Godzilla vs. Biollante`` → ``Godzilla vs. Biollante (1989)``
    """
    m = _LEADING_YEAR_RE.match(title)
    if m:
        return f"{m.group(2).strip()} ({m.group(1)})"
    return title


# ---------------------------------------------------------------------------
# Scene / quality tag stripping
# ---------------------------------------------------------------------------

_SCENE_TAGS_RE = re.compile(
    r"\b(?:"
    r"2160p|1080p|1080i|720p|480p|480i|4[Kk]|UHD"
    r"|[Xx]\.?264|[Xx]\.?265|[Hh]\.?264|[Hh]\.?265|HEVC|AVC"
    r"|[Xx][Vv][Ii][Dd]|[Dd][Ii][Vv][Xx]"
    r"|[Bb]lu[-\s]?[Rr]ay|BRRip|BDRip|DVDRip|DVDScr|DVD-?R"
    r"|WEB-?DL|WEBRip|WEBDL|HDRip|HDTVRip|HDTV"
    r"|6ch|2ch|10bit|[Rr]emux|[Rr]epack"
    r"|[Yy][Ii][Ff][Yy]|[Yy][Tt][Ss]|[Ss][Hh][Aa][Nn][Ii][Gg]"
    r"|[Ff]lux[Cc]apacitor|whodude\w*"
    r")\b",
)

_NOISE_BRACKET_RE = re.compile(r"\[\s*[A-Za-z0-9 _.+-]+\s*\]")
_YEAR_PAREN_RE = re.compile(r"\((\d{4})\)")
_YEAR_BRACKET_RE = re.compile(r"[\[\]{}]\s*(\d{4})\s*[\[\]{}]")
_YEAR_BARE_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def normalize_and_clean(title: str) -> tuple[str, str | None]:
    """Return ``(clean_title, year_or_None)`` for a standard movie title.

    Pipeline: strip extension → normalize leading year → replace dots/underscores
    with spaces → split concatenated word+year → extract year → strip scene tags
    → strip noise brackets → final cleanup.
    """
    t = strip_extension(title)
    t = normalize_leading_year(t)
    t = t.replace(".", " ").replace("_", " ")
    # split concatenated word+year: "Deathquake1980" → "Deathquake 1980"
    t = re.sub(r"([a-zA-Z])(\d{4})\b", r"\1 \2", t)

    # extract year
    year: str | None = None
    m = _YEAR_PAREN_RE.search(t)
    if m:
        year = m.group(1)
        t = t[: m.start()] + t[m.end() :]
    else:
        m = _YEAR_BRACKET_RE.search(t)
        if m:
            year = m.group(1)
            t = t[: m.start()] + t[m.end() :]
        else:
            m = _YEAR_BARE_RE.search(t)
            if m and 1920 <= int(m.group(1)) <= 2030:
                year = m.group(1)
                t = t[: m.start()] + t[m.end() :]

    t = _SCENE_TAGS_RE.sub("", t)
    t = _NOISE_BRACKET_RE.sub("", t)
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"[\[\]{}]", "", t)
    t = re.sub(r"^\s*[-\u2013\u2014:,]+\s*", "", t)
    t = re.sub(r"\s*[-\u2013\u2014:,]+\s*$", "", t)
    # re-apply extension strip: removing the year may expose an extension
    t = strip_extension(t.strip(" .-"))
    t = re.sub(r"\s+", " ", t).strip()
    return t or title, year
