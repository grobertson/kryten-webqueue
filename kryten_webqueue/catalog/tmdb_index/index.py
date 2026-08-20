"""Offline TMDB resolver over the local index.

Resolves a title (or original-language title) to a ``tmdb_id`` with no network
call, using the same normalisation + similarity logic as the live-API path so
offline matching stays consistent with ``_titles_similar``.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiosqlite

from ._textmatch import _norm

logger = logging.getLogger(__name__)

Confidence = Literal["exact", "high", "low"]
MatchedOn = Literal["original_title", "title"]

_HIGH_RATIO = 0.85
_LOW_RATIO = 0.50
_CANDIDATE_LIMIT = 50


@dataclass
class ResolveResult:
    tmdb_id: int
    matched_title: str
    popularity: float
    confidence: Confidence
    matched_on: MatchedOn


class TMDBLocalIndex:
    """Read-only accessor over the local TMDB index database."""

    def __init__(self, index_path: str | Path):
        self._path = Path(index_path)
        self._conn: aiosqlite.Connection | None = None
        self._missing_logged = False

    async def _connect(self) -> aiosqlite.Connection | None:
        if self._conn is not None:
            return self._conn
        if not self._path.exists():
            if not self._missing_logged:
                logger.debug("[tmdb_index] index not found at %s", self._path)
                self._missing_logged = True
            return None
        uri = f"file:{self._path}?mode=ro"
        self._conn = await aiosqlite.connect(uri, uri=True)
        self._conn.row_factory = aiosqlite.Row
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def resolve(
        self,
        title: str,
        year: str | None = None,
        kind: str = "movie",
        *,
        original_title: str | None = None,
    ) -> ResolveResult | None:
        """Resolve a title to a ``tmdb_id`` offline.

        Tries the original-language title first (highest offline yield, since the
        dumps are keyed on original titles), then the English title. Year is not
        present in the dumps and cannot filter here — it stays a downstream API
        disambiguator. Returns ``None`` on a confident miss or a missing index.
        """
        conn = await self._connect()
        if conn is None:
            return None
        table, fts = ("tv", "tv_fts") if kind == "tv" else ("movies", "movies_fts")

        best_low: ResolveResult | None = None
        queries: list[tuple[str, MatchedOn]] = []
        if original_title:
            queries.append((original_title, "original_title"))
        if title:
            queries.append((title, "title"))

        for raw, matched_on in queries:
            norm = _norm(raw)
            if not norm:
                continue
            result = await self._resolve_norm(conn, table, fts, norm, matched_on)
            if result is None:
                continue
            if result.confidence in ("exact", "high"):
                return result
            if best_low is None or result.popularity > best_low.popularity:
                best_low = result
        return best_low

    async def _resolve_norm(
        self,
        conn: aiosqlite.Connection,
        table: str,
        fts: str,
        norm: str,
        matched_on: MatchedOn,
    ) -> ResolveResult | None:
        title_col = "original_title" if table == "movies" else "original_name"

        # 1. Exact normalised-title match — pick the most popular.
        async with conn.execute(
            f"SELECT tmdb_id, {title_col} AS t, popularity FROM {table} "
            "WHERE norm_title = ? ORDER BY popularity DESC LIMIT 1",
            (norm,),
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            return ResolveResult(
                tmdb_id=row["tmdb_id"],
                matched_title=row["t"] or "",
                popularity=row["popularity"] or 0.0,
                confidence="exact",
                matched_on=matched_on,
            )

        # 2. FTS candidate pool, ranked by similarity then popularity.
        tokens = [tok for tok in norm.split() if tok]
        if not tokens:
            return None
        # Exact-token MATCH first; if that recalls nothing (e.g. a typo in a
        # single-word title), retry with prefix matching so difflib still gets a
        # candidate pool to score.
        exact_q = " OR ".join(f'"{tok}"' for tok in tokens)
        prefix_q = " OR ".join(
            f"{tok[:4]}*" if len(tok) >= 4 else f"{tok}*" for tok in tokens
        )
        candidates: list[aiosqlite.Row] = []
        for match_query in (exact_q, prefix_q):
            try:
                async with conn.execute(
                    f"SELECT m.tmdb_id, m.{title_col} AS t, m.norm_title, m.popularity "
                    f"FROM {fts} f JOIN {table} m ON m.tmdb_id = f.rowid "
                    f"WHERE {fts} MATCH ? ORDER BY m.popularity DESC LIMIT ?",
                    (match_query, _CANDIDATE_LIMIT),
                ) as cur:
                    candidates = list(await cur.fetchall())
            except aiosqlite.OperationalError:
                return None
            if candidates:
                break
        if not candidates:
            return None

        best: ResolveResult | None = None
        best_ratio = 0.0
        for c in candidates:
            ratio = difflib.SequenceMatcher(None, norm, c["norm_title"] or "").ratio()
            if ratio < _LOW_RATIO:
                continue
            if (
                best is None
                or ratio > best_ratio
                or (ratio == best_ratio and (c["popularity"] or 0.0) > best.popularity)
            ):
                best_ratio = ratio
                best = ResolveResult(
                    tmdb_id=c["tmdb_id"],
                    matched_title=c["t"] or "",
                    popularity=c["popularity"] or 0.0,
                    confidence="high" if ratio >= _HIGH_RATIO else "low",
                    matched_on=matched_on,
                )
        return best
