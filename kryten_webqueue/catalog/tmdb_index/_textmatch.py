"""Leaf text-normalisation + similarity helpers shared by the resolver, index
builder, and the enrichment providers.

Kept dependency-free (only stdlib) and outside the ``enrichment`` package so both
``enrichment.providers`` and ``tmdb_index`` can import it without a circular import.
"""

from __future__ import annotations

import difflib
import re

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
