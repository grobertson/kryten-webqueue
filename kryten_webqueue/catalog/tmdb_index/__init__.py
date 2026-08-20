"""Local TMDB index built from the daily ID-export dumps.

A standalone, rebuildable SQLite index (separate from ``webqueue.db``) plus an
offline resolver and IMDb ``tt#`` scraper used by the enrichment ``identify`` step.
"""

from __future__ import annotations

from ._ttscrape import extract_imdb_tt
from .builder import BuildStats, build_index, parse_kinds
from .coverage import CoverageReport, CoverageRow, build_coverage_report
from .index import ResolveResult, TMDBLocalIndex

__all__ = [
    "BuildStats",
    "CoverageReport",
    "CoverageRow",
    "ResolveResult",
    "TMDBLocalIndex",
    "build_coverage_report",
    "build_index",
    "extract_imdb_tt",
    "parse_kinds",
]
