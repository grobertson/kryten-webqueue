# SPEC — Sortie 3: FTS5 → `tsvector`+GIN with `pg_trgm` Fuzzy Matching

**Sprint**: `postgres-migration`
**PRD**: [PRD-postgres-migration.md](PRD-postgres-migration.md)
**Depends on**: Sortie 2 (schema/connection on Postgres)
**Estimated**: 4–5 h

---

## 1. Overview

Replace the SQLite FTS5 virtual table (`catalog_fts`) with Postgres full-text search: a `tsvector` generated column on `catalog.catalog` with a GIN index for ranked matching, plus a `pg_trgm` GIN index on `title` for typo-tolerant fuzzy matching. Rewrite the search route and delete the FTS5-specific query sanitising that has historically caused parse-error bugs.

## 2. Scope and Non-Goals

**In scope**
- `tsvector` search column (title + description), GIN index, `websearch_to_tsquery('english', :query)` + `ts_rank` ranking.
- `pg_trgm` extension + GIN index on `catalog.catalog(title)`; `similarity()` fuzzy fallback for near-miss titles.
- Search route rewrite; removal of FTS5 sanitisation logic.

**Non-goals**
- Vector/embedding similarity search (`pgvector` - reserved for future recommender sprint).

## 3. Requirements

- Search returns ranked title/description matches without requiring exact word boundary syntax.
- Injection-safe: use `websearch_to_tsquery('english', :query)` with parameterized inputs.
- Fuzzy: a misspelled title (e.g. `blayd runer`) surfaces `Blade Runner` via trigram similarity score above threshold (0.3).
- Complete retirement of the derived `catalog_fts` virtual table and its manual maintenance paths.

## 4. Design

### 4.1 Full-Text Search (`tsvector` + GIN)
```sql
ALTER TABLE catalog.catalog ADD COLUMN search_vector tsvector
  GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(description, '')), 'B')
  ) STORED;
CREATE INDEX idx_catalog_search_vector ON catalog.catalog USING GIN (search_vector);
```

### 4.2 Trigram Fuzzy Search (`pg_trgm`)
```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX idx_catalog_title_trgm ON catalog.catalog USING GIN (title gin_trgm_ops);
```

### 4.3 Hybrid Query Execution
```sql
WITH fts_matches AS (
    SELECT c.friendly_token, c.title, ts_rank(c.search_vector, q) AS rank_score, 1 AS match_type
    FROM catalog.catalog c, websearch_to_tsquery('english', :query) q
    WHERE c.search_vector @@ q
),
trgm_matches AS (
    SELECT c.friendly_token, c.title, similarity(c.title, :query) AS rank_score, 2 AS match_type
    FROM catalog.catalog c
    WHERE similarity(c.title, :query) > 0.3
      AND c.friendly_token NOT IN (SELECT friendly_token FROM fts_matches)
)
SELECT * FROM fts_matches
UNION ALL
SELECT * FROM trgm_matches
ORDER BY match_type ASC, rank_score DESC
LIMIT :limit;
```

## 5. Implementation Plan

1. **Add** `search_vector` generated column, GIN index, and `pg_trgm` extension into `sql/` schema files.
2. **Rewrite** search method in `_catalog` mixin using SQLAlchemy async session with parameterized query.
3. **Delete** FTS5 table initialization, manual FTS5 insert/delete maintenance, and the `_sanitize_fts_query` helper.

`catalog_fts` is derived data and is not migrated. The Postgres generated vector rebuilds from
`catalog.catalog`, and the migration test suite validates the required GIN and trigram indexes.

## 6. Testing Strategy

- Unit tests verifying exact match, multi-word full-text query, and misspelled fuzzy match ("Terminatr" $\to$ "The Terminator").
- Syntax error fuzzing testing quotes, brackets, and symbols to verify injection-safety.

## 7. Acceptance Criteria

- [ ] `search_vector` generated column and `pg_trgm` extension created in `catalog` schema.
- [ ] Typo queries return relevant catalog items.
- [ ] All FTS5 virtual table dependencies removed.
- [ ] Tests pass green.

- Fixture corpus of ~30 titles; assert:
  - exact/substring queries rank the right item first,
  - multi-word queries work (`websearch_to_tsquery` handles quotes/AND/OR),
  - a deliberately misspelled query surfaces the intended title via trigram,
  - queries that previously broke FTS5 (`(`, `:`, `"`, unbalanced quotes) return results, not
    errors.
- Regression: the search route no longer raises on adversarial input.

## 7. Acceptance Criteria

- [ ] `catalog_fts` and its manual FTS5 maintenance removed; no FTS5 remnants.
- [ ] `tsvector`+GIN ranked search returns sensible ordering on the fixture corpus.
- [ ] `pg_trgm` fuzzy surfaces misspelled-title matches.
- [ ] Adversarial input returns results without errors (parse-error class eliminated).
- [ ] `black`, `ruff`, `mypy`, `pytest` green.

## 8. Rollout

- Ships with the connection port; validated in staging against a real catalog copy.

## 9. Documentation

- CHANGELOG `feat:` — better search + fuzzy; note the search-behavior change (ranking differs
  from FTS5). Update any user-facing search docs.
