# SPEC — Sortie 3: Cross-Domain Query Decoupling

**Sprint**: `sqlite-domain-separation`  
**PRD**: [PRD-sqlite-domain-separation.md](PRD-sqlite-domain-separation.md)  
**Depends on**: Sortie 2  
**Estimated Effort**: 4 hours  

---

## 1. Overview

Because SQLite cannot perform SQL `JOIN`s across distinct database files without `ATTACH DATABASE`, this sortie decouples the small number of cross-domain queries in the codebase into clean application-layer sequential queries.

---

## 2. Cross-Domain Query Analysis & Solutions

### 2.1 Recently-Played Suppression Filter

**Previous Implementation** (`_catalog.py`):
```sql
-- In catalog browse query:
AND c.friendly_token NOT IN (
    SELECT media_id FROM play_completions WHERE completed_at > datetime('now', '-7 days')
    UNION
    SELECT spi.media_id FROM playlist_item_played pip
    JOIN saved_playlists sp ON sp.id = spi.playlist_id
    WHERE sp.is_immutable = 0
)
```

**Decoupled Implementation**:
1. Add `get_active_hidden_media_ids(window_days: int = 7) -> set[str]` to `_queue_db.py`.
2. In `_catalog_db.py`, accept `excluded_tokens: set[str] | list[str] | None`.
3. In `routes/catalog.py` / `routes/pages.py`:
   ```python
   hidden_tokens = await db.queue.get_active_hidden_media_ids()
   items = await db.catalog.get_browse_items(..., exclude_tokens=hidden_tokens)
   ```

The catalog repository must bind the exclusion collection safely and preserve the empty-set case.
It must not construct unbounded SQL text from tokens. The implementation also needs a defined
upper bound or chunking strategy for SQLite parameter limits.

---

### 2.2 User Watchlist ("My List")

**Previous Implementation** (`_watchlist.py`):
```sql
SELECT c.*, wl.added_at as watchlist_added_at
FROM user_watchlist wl
JOIN catalog c ON c.friendly_token = wl.friendly_token
WHERE wl.username = ?
ORDER BY wl.added_at DESC
```

**Decoupled Implementation**:
1. In `_users_db.py`:
   ```python
   async def get_user_watchlist_tokens(self, username: str) -> list[dict]:
       # Returns [{"friendly_token": "...", "added_at": "..."}]
   ```
2. In `_catalog_db.py`:
   ```python
   async def get_items_by_tokens(self, tokens: list[str]) -> list[dict]:
       # Fetches catalog items for given tokens
   ```
3. Combine in `Database.get_user_watchlist(username)` by mapping catalog items with their `watchlist_added_at` timestamp.

Pagination happens in `users.db` first: fetch the ordered page of watchlist rows, hydrate only
that page from `catalog.db`, then restore the original watchlist order in memory. A deleted
catalog item is omitted from the hydrated response but remains an auditable watchlist row until
the normal cleanup policy removes it. This preserves the legacy ordering and page boundary.

---

### 2.3 Job Run Logging Isolation

**Previous Implementation**:
- Job logging inserted into `job_run_logs` inside the same database where `item_enrichment_state` was updated.

**Decoupled Implementation**:
- `JobManager` and `log_capture.py` write logs strictly to `db.jobs`.
- Pipeline enrichment steps write metadata strictly to `db.catalog`.
- Neither locks or contends with the other.

---

## 3. Implementation Plan

1. **Refactor** `_catalog.py` to extract `_recently_played_filter` into an explicit parameter passed from route handlers.
2. **Refactor** `_watchlist.py` to fetch tokens from `db.users` and item records from `db.catalog`.
3. **Verify** all routes in `routes/` that interact with watchlist, browse, and jobs.

---

## 4. Acceptance Criteria

- [ ] Zero SQL `JOIN`s spanning across tables residing in different database files.
- [ ] Catalog browse with recently-played filtering returns identical results to legacy single-DB queries.
- [ ] User watchlist returns correctly ordered, fully hydrated catalog items.
- [ ] No regression in API response latency or data shapes.
- [ ] Empty, large, and deleted-token watchlists are covered by tests; catalog hydration preserves the users-domain page order.
