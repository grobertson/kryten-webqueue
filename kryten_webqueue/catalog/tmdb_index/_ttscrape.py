"""IMDb ``tt#`` extraction from arbitrary item text.

The highest-accuracy, zero-matching identity signal: many items (especially
YouTube full-movie rips with garbage titles) paste an IMDb link or bare id in
their description or source URL.
"""

from __future__ import annotations

import re

# IMDb id in a URL, or a bare id not glued to surrounding word/number chars.
_URL_RE = re.compile(r"imdb\.com/title/(tt\d{7,8})", re.IGNORECASE)
_BARE_RE = re.compile(r"(?<![a-z0-9])(tt\d{7,8})(?!\d)", re.IGNORECASE)


def extract_imdb_tt(*texts: str | None) -> str | None:
    """Return the first IMDb ``tt#`` found across ``texts``, or ``None``.

    Prefers a full ``imdb.com/title/tt…`` URL, then a bare ``tt…`` token. The
    returned id is normalised to lowercase ``tt`` + digits. Never raises.
    """
    for text in texts:
        if not text:
            continue
        m = _URL_RE.search(text)
        if m:
            return m.group(1).lower()
    for text in texts:
        if not text:
            continue
        m = _BARE_RE.search(text)
        if m:
            return m.group(1).lower()
    return None
