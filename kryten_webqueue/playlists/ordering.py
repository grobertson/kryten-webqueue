"""Season/episode aware ordering for playlists built from search results (0.14.2).

When an admin saves a whole result set to a playlist we try to lay episodic
content out in natural watch order: grouped by series, then by season, then by
episode. Items with no detectable season/episode marker fall back to an
alphabetical-by-title placement, and ties always preserve the original input
order (a stable sort) so the behaviour is deterministic.
"""

import re

# Ordered most-specific first. Each pattern must expose season as group 1 and
# episode as group 2.
_SE_PATTERNS = [
    # S01E02, s1e2, S01 E02, S01.E02, S01-E02
    re.compile(r"[Ss](\d{1,2})\s*[._\- ]?\s*[Ee](\d{1,3})"),
    # Season 1 Episode 2 / Season 1, Episode 02
    re.compile(r"[Ss]eason\s*(\d{1,2}).*?[Ee]pisode\s*(\d{1,3})"),
    # 1x02, 01x003
    re.compile(r"\b(\d{1,2})[Xx](\d{1,3})\b"),
]


def parse_season_episode(title: str | None) -> tuple[int, int, int, int] | None:
    """Parse a season/episode marker from ``title``.

    Returns ``(season, episode, match_start, match_end)`` or ``None`` when no
    recognised marker is present.
    """
    if not title:
        return None
    for pattern in _SE_PATTERNS:
        m = pattern.search(title)
        if not m:
            continue
        try:
            season = int(m.group(1))
            episode = int(m.group(2))
        except (TypeError, ValueError):
            continue
        return season, episode, m.start(), m.end()
    return None


def _series_base(title: str, match_start: int) -> str:
    """Best-effort series name: the text before the season/episode marker.

    Falls back to the whole title when the marker sits at the very start.
    """
    base = title[:match_start].strip(" \t-–—_.:|")
    return (base or title).strip().lower()


def episode_sort_key(item: dict, index: int) -> tuple:
    """Sort key for a catalog item.

    Episodic items group under their series name then sort by season/episode.
    Non-episodic items sort by their (lowercased) title. ``index`` is appended
    so equal keys retain the caller's original ordering.
    """
    title = (item.get("title") or "").strip()
    parsed = parse_season_episode(title)
    if parsed:
        season, episode, start, _ = parsed
        return (_series_base(title, start), season, episode, index)
    return (title.lower(), 0, 0, index)


def order_for_playlist(items: list[dict]) -> list[dict]:
    """Return ``items`` ordered for a playlist (season/episode aware, stable)."""
    return [
        item
        for _, item in sorted(
            enumerate(items),
            key=lambda pair: episode_sort_key(pair[1], pair[0]),
        )
    ]
