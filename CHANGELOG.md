# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.5] - 2026-06-04

### Fixed

- **Unhandled `HTTPStatusError` from `playlist_add`** — Both `insert_pay_queue` and `insert_pay_playnext` now catch `httpx.HTTPStatusError` thrown when the api-gate returns a non-2xx response for `/playlist/add`. On failure the spend is refunded (best-effort) and a `{success: False, error: "Failed to add to playlist"}` dict is returned so the route can surface a 400 to the browser instead of crashing with an unhandled 500

[0.4.5]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.4.5

## [0.4.4] - 2026-06-04

### Fixed

- **`cost_z` field name** — Economy preview response uses `cost_z` not `cost`/`z_cost`; the wrong key names caused every queue/add and queue/playnext call to raise HTTP 502 "Cost preview returned no cost value"
- **Eligibility not surfaced** — Queue routes now check `preview.get("available")` before attempting the spend; when `False`, the `error_code` from the preview (`cooldown_active`, `daily_limit_reached`, `insufficient_balance`, `blackout_active`) is returned as HTTP 400 so the UI can display a meaningful message

[0.4.4]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.4.4

## [0.4.3] - 2026-06-04

### Fixed

- **Phantom `success` checks on economy responses** — `api-gate` economy routes use `_unwrap()` which strips the outer `{success, data}` envelope before returning. Both `/queue/add` and `/queue/playnext` were checking `preview.get("success")` on the already-stripped data dict, which was always `None` → always HTTP 400 "Cost preview failed". Removed the checks entirely; `raise_for_status()` in the httpx client already propagates non-2xx responses as `HTTPStatusError`
- **Spend error handling** — Replaced `spend_result.get("success")` check with `try/except httpx.HTTPStatusError` around the `queue_spend` call for a clean error dict on failure

[0.4.3]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.4.3

## [0.4.2] - 2026-06-03

### Fixed

- **`/images/` 404s in development** — Mounted `StaticFiles` at `/images` pointing to `config.image_dir` in `app.py`; previously only the nginx alias served these in production, but uvicorn had no route so any direct or dev request returned 404

[0.4.2]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.4.2

## [0.4.1] - 2026-06-03

### Added

- **Parallel TMDB movie + TV search** — Replaced `search/multi` (which mixed in person results) with concurrent `search/movie` + `search/tv` calls; picks highest-popularity result across both
- **Title cleaning** — `_clean_title()` strips year suffixes, episode tags (`S01E02`), and resolution noise (`1080p`, `BluRay`, `x264`, etc.) before retrying a failed lookup
- **Year extraction** — Parsed year passed to TMDB as the `year` filter when available
- **`w780` poster size** — Upgraded from `w500` for higher-quality cover art
- **Thumbnail fallback** — If no external art is found, falls back to the MediaCMS thumbnail URL already stored in the catalog
- **DB migration 2** — Clears all cached `cover_art_path` / `cover_art_source` rows on first startup to force a full repoll with the improved resolver

### Changed

- **OMDB lookup** — Now passes extracted year to improve match accuracy

[0.4.1]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.4.1

## [0.4.0] - 2026-06-02

### Added

- **Pagination** — Browse and search pages use page-number controls; `browse_count` / `search_count` added to db for total page calculation
- **Admin stub routes** — `/admin/playlists`, `/admin/schedules`, `/admin/queue-mgmt` page routes with stub templates
- **Queue page improvements** — `qi-info`/`qi-right` layout, total duration, remaining time, initial HTTP load, "queued by" metadata
- **CSS additions** — `.user-header`, `.user-rank`, `.user-online`, `.balance-meta`, `.tx-time`, `.qi-info`, `.qi-meta`, `.qi-right`, `.queue-summary`, `.np-meta`, `.np-times`

### Fixed

- **Cover art paths** — Static image references updated from `/static/images` to `/images` (nginx alias)
- **TMDB search** — Filter to prefer `poster_path` results over person `profile_path` matches
- **Queue button error handling** — JS now surfaces API error messages inline rather than failing silently

### Changed

- **User dashboard** — Shows rank name, Z balance with comma formatting (`toLocaleString`), `describeTx` helper for transaction labels, transaction dates

[0.4.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.4.0

## [0.3.2] - 2026-06-02

### Added

- **Cover art resolver logging** — INFO/WARNING log entries to diagnose silent failures in TMDB/OMDB lookups

[0.3.2]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.3.2

## [0.3.1] - 2026-06-02

### Fixed

- **Cover art rate limiting** — Added 250ms delay between API calls to avoid hitting TMDB/OMDB rate limits

[0.3.1]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.3.1

## [0.3.0] - 2026-06-02

### Added

- **TMDB/OMDB cover art** — Fetches and caches box art from TMDB (primary) and OMDB (fallback) during catalog sync; MediaCMS thumbnail is last resort

[0.3.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.3.0

## [0.2.9] - 2026-06-02

### Added

- **MediaCMS thumbnail fallback** — Shows MediaCMS-sourced thumbnail as cover art when no external art is available

[0.2.9]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.2.9

## [0.2.8] - 2026-06-02

### Fixed

- **`manifest_url`** — Now uses the watch page URL instead of the raw media URL
- **Catalog search route** — Added missing template handler for `/catalog/search`

[0.2.8]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.2.8

## [0.2.7] - 2026-06-02

### Fixed

- **Full catalog sync** — Switched from `/media` (capped at 1000 items) to `/manage_media` endpoint for complete pagination

[0.2.7]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.2.7

## [0.2.6] - 2026-06-02

### Fixed

- **Catalog pagination** — Follow `next` URL from MediaCMS API response instead of constructing page numbers manually

[0.2.6]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.2.6

## [0.2.5] - 2026-06-02

### Fixed

- **Network error messages** — Clear per-host error messages; distinguish DNS failure, connection refused, and timeout

[0.2.5]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.2.5

## [0.2.4] - 2026-06-02

### Fixed

- **MediaCMS URL misconfiguration** — Strip trailing `/api/v1` from `mediacms_url` if accidentally included; log 4xx response URL and body for debugging

[0.2.4]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.2.4

## [0.2.3] - 2026-06-01

### Fixed

- **Catalog sync error logging** — Log full traceback on sync failure instead of message-only

[0.2.3]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.2.3

## [0.2.2] - 2026-06-01

### Fixed

- **Catalog sync startup timing** — Run catalog sync immediately on startup; add `INFO`-level progress logging

[0.2.2]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.2.2

## [0.2.1] - 2026-06-01

### Fixed

- **Starlette compatibility** — Updated `TemplateResponse` calls to Starlette ≥0.36 signature `(request, name, context)`

[0.2.1]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.2.1

## [0.2.0] - 2026-05-31

### Added

- **`main()` entrypoint** — Allows `kryten-webqueue` console script invocation
- **Deploy files in wheel** — nginx config and systemd unit included as `shared-data` for `pipx`-based installs

### Fixed

- **nginx TLS hardening** — `http2` directive syntax fixed; proxy keep-alive headers added

[0.2.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.2.0

## [0.1.6] - 2026-05-31

### Changed

- **CI** — Restored original two-workflow pattern (release.yml + python-publish.yml)

[0.1.6]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.1.6

## [0.1.3] - 2026-05-31

### Fixed

- **Duration parsing** — Parse `HH:MM:SS` strings to seconds
- **Duration/currentTime coercion** — Coerce API string values to `float`

[0.1.3]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.1.3

## [0.1.0] - 2026-05-31

### Added

- Initial release: FastAPI app with catalog sync from MediaCMS, SQLite catalog DB, queue/playnext routes, session auth, Jinja2 templates for browse/search/queue/user pages, nginx + systemd deploy configs

[0.1.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.1.0
