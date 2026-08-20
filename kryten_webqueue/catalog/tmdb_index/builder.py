"""Builder for the standalone TMDB local index.

Streams the TMDB daily ID-export dumps (newline-delimited JSON) into a fresh
SQLite index, one line at a time (bounded memory), then atomically swaps it into
place. Synchronous by design — callers run it off the event loop via
``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Callable, Iterable

from ._schema import FTS_POPULATE_SQL, SCHEMA_SQL
from ._textmatch import _norm

logger = logging.getLogger(__name__)

_BATCH = 5000

# kind -> (dump filename prefix, target table). The dump files are named
# ``{prefix}_YYYY_..._MM_DD.json`` (e.g. ``movie_ids_08_19_2026.json``).
_KINDS: dict[str, tuple[str, str]] = {
    "movies": ("movie_ids", "movies"),
    "tv": ("tv_series_ids", "tv"),
    "people": ("person_ids", "people"),
    "keywords": ("keyword_ids", "keywords"),
    "companies": ("production_company_ids", "companies"),
    "networks": ("tv_network_ids", "networks"),
}

_ALL_KINDS = list(_KINDS)
_DATE_RE = re.compile(r"(\d{2})_(\d{2})_(\d{4})")


@dataclass
class BuildStats:
    source_date: str | None = None
    built_at: str = ""
    elapsed_sec: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)


def parse_kinds(kinds: str) -> list[str]:
    """Turn a ``kinds`` param (``"all"`` or a comma list) into concrete kinds."""
    if not kinds or kinds == "all":
        return list(_ALL_KINDS)
    out = [k.strip() for k in kinds.split(",") if k.strip()]
    unknown = [k for k in out if k not in _KINDS]
    if unknown:
        raise ValueError(f"Unknown kinds: {unknown}; valid: {_ALL_KINDS}")
    return out


def _latest_dump(dump_dir: Path, prefix: str) -> Path | None:
    """Return the newest dump file for ``prefix`` in ``dump_dir`` (by mtime)."""
    matches = sorted(
        dump_dir.glob(f"{prefix}_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _source_date_from(path: Path) -> str | None:
    m = _DATE_RE.search(path.name)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}-{mm}-{dd}"
    return None


def _iter_records(path: Path) -> Iterable[dict]:
    """Yield one parsed JSON object per line, skipping blanks and bad lines."""
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _rows_for(kind: str, records: Iterable[dict]) -> Iterable[tuple]:
    """Map raw dump records to insert tuples for the kind's table."""
    if kind == "movies":
        for r in records:
            title = r.get("original_title") or ""
            yield (
                r["id"],
                title,
                _norm(title),
                r.get("popularity"),
                int(bool(r.get("adult"))),
            )
    elif kind == "tv":
        for r in records:
            name = r.get("original_name") or ""
            yield (r["id"], name, _norm(name), r.get("popularity"))
    elif kind == "people":
        for r in records:
            yield (r["id"], r.get("name") or "", r.get("popularity"))
    else:  # keywords, companies, networks
        for r in records:
            yield (r["id"], r.get("name") or "")


_INSERT_SQL: dict[str, str] = {
    "movies": "INSERT OR IGNORE INTO movies(tmdb_id, original_title, norm_title, popularity, adult) VALUES (?,?,?,?,?)",
    "tv": "INSERT OR IGNORE INTO tv(tmdb_id, original_name, norm_title, popularity) VALUES (?,?,?,?)",
    "people": "INSERT OR IGNORE INTO people(tmdb_id, name, popularity) VALUES (?,?,?)",
    "keywords": "INSERT OR IGNORE INTO keywords(tmdb_id, name) VALUES (?,?)",
    "companies": "INSERT OR IGNORE INTO companies(tmdb_id, name) VALUES (?,?)",
    "networks": "INSERT OR IGNORE INTO networks(tmdb_id, name) VALUES (?,?)",
}


def _load_kind(
    conn: sqlite3.Connection,
    kind: str,
    path: Path,
    progress: Callable[[dict], None] | None,
) -> int:
    table = _KINDS[kind][1]
    sql = _INSERT_SQL[kind]
    batch: list[tuple] = []
    count = 0
    for row in _rows_for(kind, _iter_records(path)):
        batch.append(row)
        if len(batch) >= _BATCH:
            conn.executemany(sql, batch)
            count += len(batch)
            batch.clear()
            if progress:
                progress({"detail": f"index: {table} {count:,} rows"})
    if batch:
        conn.executemany(sql, batch)
        count += len(batch)
    conn.commit()
    if kind in FTS_POPULATE_SQL:
        conn.execute(FTS_POPULATE_SQL[kind])
        conn.commit()
    logger.info("[tmdb_index] loaded %s: %d rows from %s", table, count, path.name)
    return count


def build_index(
    dump_dir: str | Path,
    index_path: str | Path,
    kinds: str = "movies,tv",
    *,
    progress: Callable[[dict], None] | None = None,
) -> BuildStats:
    """Build the TMDB local index from dump files, atomically replacing ``index_path``.

    Streams each JSONL dump line-by-line into a temporary database, then swaps it
    into place so a partial build never corrupts a live index. Returns build stats.
    """
    dump_dir = Path(dump_dir)
    index_path = Path(index_path)
    if not dump_dir.is_dir():
        raise ValueError(f"dump_dir does not exist: {dump_dir}")

    kind_list = parse_kinds(kinds)
    start = time.monotonic()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = index_path.with_name(index_path.name + ".tmp")
    for leftover in (
        tmp_path,
        tmp_path.with_name(tmp_path.name + "-wal"),
        tmp_path.with_name(tmp_path.name + "-shm"),
    ):
        leftover.unlink(missing_ok=True)

    counts: dict[str, int] = {}
    source_date: str | None = None

    conn = sqlite3.connect(tmp_path)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript(SCHEMA_SQL)
        for kind in kind_list:
            prefix = _KINDS[kind][0]
            path = _latest_dump(dump_dir, prefix)
            if path is None:
                logger.warning(
                    "[tmdb_index] no dump found for %s (%s_*.json)", kind, prefix
                )
                counts[kind] = 0
                continue
            if source_date is None:
                source_date = _source_date_from(path)
            counts[kind] = _load_kind(conn, kind, path, progress)

        built_at = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO index_meta(id, source_date, built_at, counts_json) VALUES (1, ?, ?, ?)",
            (source_date, built_at, json.dumps(counts)),
        )
        conn.commit()
    finally:
        conn.close()

    os.replace(tmp_path, index_path)
    for leftover in (
        index_path.with_name(index_path.name + "-wal"),
        index_path.with_name(index_path.name + "-shm"),
    ):
        leftover.unlink(missing_ok=True)

    stats = BuildStats(
        source_date=source_date,
        built_at=built_at,
        elapsed_sec=time.monotonic() - start,
        counts=counts,
    )
    logger.info(
        "[tmdb_index] built %s in %.1fs: %s",
        index_path.name,
        stats.elapsed_sec,
        ", ".join(f"{k}={v:,}" for k, v in counts.items()),
    )
    return stats
