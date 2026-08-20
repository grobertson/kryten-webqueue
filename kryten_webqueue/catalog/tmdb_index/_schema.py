"""Schema for the standalone TMDB local index database.

This DB is **separate** from the operational ``webqueue.db`` and is fully rebuilt
from the TMDB daily ID-export dumps on each refresh, so it uses a plain ``CREATE``
schema rather than the versioned migration chain in ``catalog/db/_connection.py``.
"""

from __future__ import annotations

# Base tables. tmdb_id is declared INTEGER PRIMARY KEY so it aliases the rowid,
# letting the FTS5 external-content tables key directly off it.
SCHEMA_SQL = """
CREATE TABLE movies (
    tmdb_id        INTEGER PRIMARY KEY,
    original_title TEXT,
    norm_title     TEXT,
    popularity     REAL,
    adult          INTEGER
);
CREATE INDEX idx_movies_norm ON movies(norm_title);

CREATE VIRTUAL TABLE movies_fts USING fts5(
    norm_title,
    content='movies',
    content_rowid='tmdb_id'
);

CREATE TABLE tv (
    tmdb_id       INTEGER PRIMARY KEY,
    original_name TEXT,
    norm_title    TEXT,
    popularity    REAL
);
CREATE INDEX idx_tv_norm ON tv(norm_title);

CREATE VIRTUAL TABLE tv_fts USING fts5(
    norm_title,
    content='tv',
    content_rowid='tmdb_id'
);

CREATE TABLE people (
    tmdb_id    INTEGER PRIMARY KEY,
    name       TEXT,
    popularity REAL
);

CREATE TABLE keywords (
    tmdb_id INTEGER PRIMARY KEY,
    name    TEXT
);

CREATE TABLE companies (
    tmdb_id INTEGER PRIMARY KEY,
    name    TEXT
);

CREATE TABLE networks (
    tmdb_id INTEGER PRIMARY KEY,
    name    TEXT
);

CREATE TABLE index_meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    source_date TEXT,
    built_at    TEXT,
    counts_json TEXT
);
"""

# SQL to populate an FTS table from its external-content base table after load.
FTS_POPULATE_SQL = {
    "movies": "INSERT INTO movies_fts(rowid, norm_title) SELECT tmdb_id, norm_title FROM movies;",
    "tv": "INSERT INTO tv_fts(rowid, norm_title) SELECT tmdb_id, norm_title FROM tv;",
}
