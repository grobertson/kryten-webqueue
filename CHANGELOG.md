# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
## [0.6.6] - 2026-06-08

### Changed

- **Queue view polish.** Three refinements to the Queue page: (1) **Predicted start times** now render reliably in the viewer's local timezone — the frontend defensively treats timezone-less timestamps as UTC before converting, so ETAs no longer risk being misread as server time. (2) **Now Playing card** is larger and easier to read — bigger cover art (128px), a larger title that wraps cleanly, and the elapsed/remaining times no longer wrap. (3) **The currently-playing item is now highlighted** in the queue list with an accent tint and ring. The now-playing playlist `uid` is resolved server-side (matching media id/type against the shadow playlist when CyTube's `changeMedia` payload omits it) so the matching queue item is identified reliably.

## [0.6.5] - 2026-06-08

### Changed

- **Refined catalog browse ordering** — Tightened the quality-weighted sort introduced in 0.6.4 based on feedback. (1) Dropped the popularity (times-queued) tier, which discouraged discovery. (2) Replaced the weak "has cover art" test with a real-poster signal: items are led by `cover_art_source IN ('tmdb', 'omdb')` rather than mere presence of a `cover_art_path`/`thumbnail_url` — nearly every item carries a MediaCMS thumbnail, and the resolver also caches that thumbnail as a last-resort cover (`cover_art_source = 'thumbnail'`), so the old test was almost always true. A genuine TMDB/OMDB poster match also implies a well-formed, matchable title. Letter-first-then-alphabetical ordering (which correctly sinks `"02 - Episode"` style entries) is retained.

[0.6.5]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.6.5

## [0.6.4] - 2026-06-08

### Changed

- **Quality-weighted catalog browse ordering** — The catalog landing no longer leads with alphabetical junk (art-less, number-prefixed "02 - Episode" entries). Browse results are now ranked by signals derived entirely from existing data — no curation or featured-item maintenance required: (1) items with box art (cover or thumbnail) first, (2) then by real popularity (times queued, from `queue_history`), (3) titles beginning with a letter before number/symbol-prefixed titles, (4) then alphabetical for a stable tail. Applies to both the unfiltered landing and category-filtered browse; search continues to use full-text relevance.

[0.6.4]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.6.4

## [0.6.3] - 2026-06-08

### Fixed

- **Queue items missing title/duration for externally-queued media** — CyTube playlist items nest their metadata under a `media` key (`{uid, temp, queueby, media: {id, title, seconds, type}}`), but the shadow reconciler read flat fields, so any item not added by the running webqueue instance showed "Unknown" with a `0:00` duration. The reconciler now reads the nested `media` object (with a flat-key fallback) and backfills title/duration/media-id onto known items. Externally-queued items now also show their CyTube `queueby` as the requester.
- **Now Playing runtime showing `NaN:NaN`** — The now-playing total used the preformatted `duration` string instead of the numeric `seconds`, producing `NaN:NaN` for the total and remaining time. It now uses `seconds` (with a numeric fallback).
- **Estimated start times** — Now derived from the numeric now-playing `seconds`/`currentTime`, so the queue ETAs are correct now that item durations are read properly.

### Added

- **Sticky, highlighted Now Playing card** — The Now Playing card is pinned below the navbar while scrolling a long queue, is highlighted with a subtle accent ring/glow, and the page auto-scrolls it into view on load.
- **Case-sensitivity notice on login** — The OTP login view now warns that the CyTube username is case-sensitive and must match exactly (e.g. `TacoBelmont` ≠ `tacobelmont`); the username field no longer auto-capitalizes or autocorrects.

### Changed

- **Sticky navbar hardening** — Added `scroll-padding-top` (via a `--nav-height` variable) so scrolled-to content clears the sticky header.

[0.6.3]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.6.3
## [0.6.2] - 2026-06-07

### Fixed

- **Queue position resolution when something is playing** — CyTube's now-playing payload (`changeMedia`) carries the media `{id, type, title, seconds}` but no playlist `uid`, so `_now_playing_uid()` always returned `None`, causing Queue / Play Next / Queue-as-Admin to fail with "Queue position unavailable (now-playing unknown)" (HTTP 400) even while a video was clearly playing. The now-playing uid is now recovered by matching the media `id`/`type` against the shadow playlist (which does carry uids). Fixes all queue insertion paths.

[0.6.2]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.6.2

## [0.6.1] - 2026-06-05

### Added

- **Receipt confirmation modal for Queue & Play Next** — Clicking **Queue** or **Play Next** now opens a receipt-style modal before spending. It calls `/queue/preview` and shows the item title, price, any rank discount, total cost, current balance, and balance after the transaction, then asks the user to confirm. Unavailable purchases (insufficient balance, cooldown, daily limit, blackout) disable the confirm button with an explanatory message.
- **Enriched `/queue/preview` receipt data** — The preview endpoint now returns the catalog `title`, `base_cost`, `discount_amount`, current `balance`, and `balance_after` in addition to the existing cost/discount fields. `base_cost` comes from the economy service when available and is otherwise derived from the discount percentage.
- **Shared modal styling** — Added CSS for `.modal-overlay`/`.modal-box`/`.modal-actions` (also styling the existing admin queue modal) plus a dedicated receipt table layout.

[0.6.1]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.6.1

## [0.6.0] - 2026-06-05

### Added

- **Item detail page** — Clicking a tile's cover art or title now opens `/catalog/item/{friendly_token}`, a dedicated view showing the cover art, full MediaCMS description, duration, and the same action buttons as the tile (Queue, Play Next, and Queue as Admin for admins). Unknown tokens render a 404 "Item not found" page.
- **Queue page metadata & reorder handle** — Up-next and now-playing items are now enriched with catalog metadata (cover-art thumbnail, correct title, duration) by matching on `friendly_token` or `manifest_url`. Each queue row gains a drag handle affordance, cover thumbnail, paid-by/tier badges, and a correct ETA. Enrichment is applied to both the `/queue/state` response and the WebSocket broadcast.
- **Generic background job runner** — New `JobManager` runs registered async jobs as background tasks and records each run (start, end, status, detail) to a new `job_runs` table. The Admin page lists registered jobs with **Run** buttons and shows a recent-run history table styled like the catalog sync log. Catalog Sync is now wired through this framework.
- **Admin job routes** — `GET /admin/jobs`, `GET /admin/jobs/runs`, and `POST /admin/jobs/{name}/run`.

### Changed

- **Top-bar rebrand to "Channel-Z"** — The navigation brand and page-title suffix now read "Channel-Z" instead of "DropSugar Queue"/"DropSugar".
- **Footer credit links to GitHub** — "kryten-webqueue" in the footer now links to the project's GitHub repository.
- **All admin time displays are human-readable and timezone-aware** — Sync-log and job-run timestamps are rendered in the browser's local timezone via shared `formatLocalDateTime`/`formatLocalTime` helpers.

### Fixed

- **Empty now-playing UID is handled safely** — When the robot KV is uninitialised and the now-playing UID is unavailable, Queue and Play Next purchases are cancelled and refunded instead of landing in an undefined position.
- **Admin "Queue as Admin" position prompt** — Admins are now prompted to resolve the inserted item's position: *Play next & refund pending* (refunds money and removes pending paid items from the queue), *Play after all purchased items*, or *Cancel*.

[0.6.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.6.0

## [0.5.2] - 2026-06-05

### Fixed

- **Queue items now land in the correct playlist position** — Positioning is computed relative to the currently-playing item and the persistent pay-queue list (`queue_shadow` rows with `is_pay = 1`):
  - **Play Next** — moved to immediately *after* the currently-playing item (previously `prepend`, which placed it before the active item). Existing pay items shift down one position.
  - **Queue** — moved to immediately after the *last* item in the persistent pay-queue list, or after the currently-playing item when no pay items exist (previously left at the end of the playlist when the pay list was empty).
  - **Queue as Admin** — same target as Queue, but the item is *not* added to the persistent pay list (`is_pay = 0`).
  - All paths now add to CyTube with `position="end"` and then issue a single `move` to the resolved target UID, refunding (where applicable) and removing the orphaned item if the move fails.

[0.5.2]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.5.2

## [0.5.1] - 2026-06-05

### Added

- **Version logged at startup** — Service version is now read from package metadata (`importlib.metadata`) and logged when the lifespan starts: `kryten-webqueue v<version> started on <host>:<port>`. The same version is exposed in the FastAPI OpenAPI schema (replacing the stale hard-coded `"0.1.0"`)

[0.5.1]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.5.1

## [0.5.0] - 2026-06-04

### Added

- **Queue as Admin** — Admin users (rank ≥ 3) now see a "Queue as Admin" button on catalog cards. It queues the item at zero cost in the first available non-pay slot (top of the free section, below any paid items), treating it exactly like a non-paid item and skipping all economy interaction. New route `POST /admin/queue/add` and `insert_admin_queue()` ordering helper
- **Channel announcement on successful queue** — After an item is successfully queued and positioned, the channel chat receives `"<title> has been queued in position <N> by <user>"`, where position counts from the currently-playing item (position 0), so position 1 is the next item to play. Added `ApiGateClient.send_chat()`

### Fixed

- **Wrong manifest URL stored during catalog sync** — `_build_manifest_url()` was producing the MediaCMS watch/detail page URL (`/view?m=TOKEN`, which returns HTTP 302) instead of the real CyTube manifest (`/api/v1/media/cytube/TOKEN.json?format=json`). CyTube rejected the 302 with "Expected HTTP 200 OK, not 302 Found", causing every queue attempt to fail. Existing rows are corrected on the next startup sync
- **queueFail now surfaced from the command response** — Instead of relying on a timeout, the Robot's `addvideo` command response now carries the CyTube `queueFail` reason. The api-gate returns it as HTTP 422 and webqueue refunds the spend and reports the actual reason. webqueue no longer needs to subscribe to the `kryten.events.cytube.channel-z.queuefail` events channel
- **Refund when an item cannot be positioned** — If `playlist_move` fails after a successful add, the spend is now refunded and the mis-placed item is removed, rather than leaving a paid item in the wrong slot

[0.5.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.5.0

## [0.4.6] - 2026-06-04

### Fixed

- **Wrong media ID sent to CyTube** — `/queue/add` and `/queue/playnext` were passing `friendly_token` (the MediaCMS slug, e.g. `"my-movie-2024"`) as the `id` field for CyTube custom media type `"cm"`. CyTube requires the manifest URL as the ID; the slug was silently rejected, the `queue` confirmation event never fired, the Robot waited 8 seconds, and kryten-py's matching 8-second timeout fired first giving a 504. Fixed by passing `item["manifest_url"]` as `media_id` to CyTube and threading `friendly_token` separately through `insert_pay_queue` / `insert_pay_playnext` for spend/history records

[0.4.6]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.4.6

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
