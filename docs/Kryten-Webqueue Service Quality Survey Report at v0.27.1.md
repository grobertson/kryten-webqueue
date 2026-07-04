## Kryten-Webqueue Service Survey Report

### 1. What It Does

kryten-webqueue is a standalone FastAPI web service (v0.27.1) that provides a catalog browser and pay-to-play queue management for CyTube, integrating with `kryten-api-gate` as its backend. It exposes:

- **Catalog** — A Netflix/Tubi-style browsable media catalog synced from MediaCMS, with TMDB/OMDB cover art resolution, FTS5 search, and facet filtering
- **Queue** — Live CyTube playlist mirroring (shadow), WebSocket broadcasts of queue state, estimated start times, and promo insertion
- **Auth** — OTP-based login + JWT sessions for the web UI
- **Scheduled Events** — Recurring and one-shot playlist scheduling with immutability locks
- **Promos** — Just-in-time insertion of promo clips (channel identity, events, mod shoutouts) between content, plus lead-in promos before movies/pay items
- **Jobs** — Background enrichment (title/meta/TV metadata from cmsutils), video fetching (yt-pipe downloader), and weekend workbook URL resolution (fetchurls from SharePoint)
- **Feedback** — Viewer feedback/suggestion triage
- **Presence Refund** — Auto-cancel+refund of pending paid items when the owner leaves/AFK

### 2. Service Dependencies

The webqueue service is a **consumer** of `kryten-api-gate` (via HTTP client in `api_gate/client.py`), not a Python dependency of any other service. Cross-workspace search results:

| Workspace | Imports kryten-webqueue? |
|-----------|-------------------------|
| kryten-api-gate | **No** Python imports |
| kryten-py | **No** references |
| kryten-cli | **No** references |
| Kryten-Robot | **No** Python imports (only CHANGELOG mentions) |
| kryten-economy | References via HTTP URL (`queue_url`), not Python imports |

It is self-contained — communicates with api-gate over HTTP, not via shared Python code.

### 3. Suggested Improvements

#### Remove / Clean Up

- **`config.prometheus_port` (config.py:160)** — Declared but never used. app.py mounts no Prometheus endpoint, no middleware references it. Remove the field or implement the endpoint.
- **`catalog.db._facet_filter()` (db.py:76-103)** — Defined but never called. The `browse()`, `browse_count()`, `search()`, and `search_count()` methods inline their own category/tag filtering SQL instead of using this helper. Remove as dead code.
- **`catalog.db._slugify()` (db.py:7-12)** — Only used inside `upsert_category()`. Could be inlined or left as is (low priority).
- **`integrations/` vendored code** — Contains vendored copies of `cmsutils` and `yt-pipe` tools (noted in `integrations/__init__.py`). These are duplicated from the `cmsutils` repo. Consider whether they could be installed as a proper Python dependency instead of vendored, to reduce drift. The overhead may be justified by operating in an air-gapped environment.
- **Empty `__init__.py` files** — `api_gate/__init__.py`, `auth/__init__.py`, `catalog/__init__.py`, `queue/__init__.py`, `playlists/__init__.py`, `ws/__init__.py` are empty. Not a problem per se, but if all are namespace-only they could be consolidated.

#### Architectural / Maintainability

- **`catalog/db.py` (1,393 lines)** — This file is a massive SQLite data-access layer covering catalog, OTPs, queue shadow, playlists, schedules, feedback, spend requests, and job runs. Consider splitting into sub-modules:
  - `db/__init__.py` (connection, migrations)
  - `db/catalog.py` (browse, search, facets, sync_log)
  - `db/playlists.py` (CRUD, schedules, active schedule)
  - `db/queue.py` (shadow, spend requests, history)
  - `db/feedback.py` (feedback, title suggestions)
- **`promos/director.py` (547 lines)** — Very dense single class. Consider splitting the promo decision logic (`_general_due`, `_pick_general_type`, `_select_clip`, `_leadin_type_for`) into a separate strategy module.
- **`app.py` lifespan (lines 44-240)** — The single `lifespan` function initializes 15+ services. Consider extracting startup into a dedicated `services.py` module with factory functions.
- **Duplicate order logic in `queue/ordering.py`** — Unclear from naming, but there may be overlap with shadow position logic. Verify if intended.

#### Minor Observations

- **`pygame` or missing optional deps** — `pyproject.toml` lists `openpyxl`, `msal`, `yt-dlp` as core deps, but some are only used by the fetchurls/fetch jobs. Could be moved to `[project.optional-dependencies] jobs` to keep the base install lean.
- **`promo_log_level` in config.py:116** — Well-documented and used, good practice. Commendable logging design.
- **`_thread_safe_progress` closure in jobs/tasks.py:39** — Uses `asyncio.run_coroutine_threadsafe` with fire-and-forget. Consider adding a bounded queue or callback list instead if dropped progress callbacks become an issue.

### 4. Overall Assessment

**The codebase is healthy, well-maintained, and actively developed.** The code is well-documented with docstrings, inline comments, and thoughtful edge-case handling (orphaned job recovery, auto-lifting event locks, thread-safe progress reporting). 

No dead, dangerously unused, or duplicate code was found — the two exceptions (`prometheus_port`, `_facet_filter`) are minor. The service occupies a clear architectural niche: it's the frontend-facing web service that bridges CyTube/MediaCMS with the internal api-gate, and it's not duplicated elsewhere.

The biggest actionable recommendation is **splitting `catalog/db.py`** into manageable sub-modules and either **removing or implementing the `prometheus_port` config field**.