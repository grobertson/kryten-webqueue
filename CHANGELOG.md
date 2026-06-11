# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
## [Unreleased]

## [0.9.3] — 2026-06-11

### Fixed

- **Queue ETAs are computed from the remainder of the *currently-playing* item and wrap around the playlist.** Previously the schedule was built as if the item at list index 0 always played next, so whenever the now-playing item was not at the head, every ETA was wrong. ETAs now start from the time left on the current item and walk the rest of the list in true play order (item after current → end → wrap to the front), matching CyTube's looping playlist.
- **Paid items keep their purchase order (FIFO).** New paid items were being anchored against a stale `queue_shadow.position` read from the DB (poll reconciliation only re-indexed positions in memory), which could drop every new item directly after the now-playing item and scramble the order. The FIFO anchor is now derived from the in-memory shadow (the authoritative play order), and reconciliation persists positions back to the DB so DB-backed queries no longer drift.

### Added

- **One-click playlist Reserve/Release.** The admin Playlists list now has a direct Reserve/Release action (and a Mutable/Immutable status) to toggle a saved playlist's immutability without digging into the editor. Immutable playlists' items stay hidden from public browse/search and reserved for scheduled play; releasing returns them to the catalog and pay-to-play.
- **Scheduled-event pay-to-play lock with auto-expiry + manual unlock.** While an *immutable* scheduled playlist is playing, pay-to-play is locked so the curated event can't be interrupted. The lock now **auto-lifts once the last scheduled item begins playing** (so viewers can queue content for after the event). Admins get an **Unlock now** button (on the active-event banner and during a schedule's pre-fire window) that lifts the current lock while keeping the schedule armed for future firings — no more deleting the schedule to clear a lock.

## [0.9.2] — 2026-06-09

### Fixed

- Added missing `python-slugify>=8.0` core dependency (required by the `fetch` job).

## [0.9.1] — 2026-06-09

### Fixed

- Moved `requests`, `openpyxl`, `pyyaml`, and `yt-dlp` from the optional `[jobs]` extra into core dependencies; the `jobs` extra is removed. This fixes installation under `pipx` and other tools that do not support `package[extra]` syntax.

## [0.9.0] — 2026-06-09

### Added

- **Reimplemented content jobs (vendored).** Five tools are vendored into `kryten_webqueue/integrations/` and driven in-process as parameterized jobs, replacing the Windows-script workflow:
  - `enrichtitles`, `enrichmeta`, `enrichtv` — clean titles / enrich movie & TV metadata via TMDb/OMDb, pushing to MediaCMS. Params: `dry_run`, `limit`, `days` (+ tool-specific `tubi_upgrade`, `min_score`, `min_duration`, `max_duration`, `delay`).
  - `fetch` — download a yt-dlp-supported URL (single or playlist) to MediaCMS with `quality` (`best`/`good`/`medium`) and an optional **Add to playlist** that appends the uploaded item(s) to a saved playlist.
  - `fetchurls` — read the upcoming **weekend's** Channel Z workbook from a local `.xlsx` (file-only, no SharePoint/Graph in v1), resolve off-site URLs via the in-process downloader, and import each section as a saved playlist named `{sheet}-{section}` (idempotent re-runs replace items). The target sheet is always the imminent Fri/Sat (Friday → today; Sat/Sun → next Friday).
  Each blocking tool runs off the event loop via `asyncio.to_thread`, bridging progress back to the `job_runs` row. Jobs whose optional dependencies are missing register normally but fail the run fast with a clear "dependency not installed" message. New optional extra `jobs` (`yt-dlp`, `openpyxl`, `requests`, `pyyaml`); `fetch` additionally needs `ffmpeg` on the host. New config: `fetch_cookies_path`, `fetchurls.workbook_path`.
- **Parameterized background jobs.** The job framework now supports declarative parameter schemas: `JobManager.register(name, func, *, label, schema)` and `run(name, *, triggered_by, params)`. Job functions receive `(params, ctx)` where `ctx` exposes `db`, `api_gate`, `config`, `triggered_by`, and an async `progress(detail)` callback that writes live progress to the run's `job_runs` row. Submitted params are validated/coerced against the schema (required, defaults, `string`/`int`/`float`/`bool`/`enum`/`playlist` types) and persisted to the new `job_runs.params` column. The admin **Run** button opens a schema-driven modal for jobs that declare parameters (and runs immediately for those that don't); each job also shows a last-run summary. New endpoint `GET /admin/jobs/{name}/schema`; `GET /admin/jobs` now includes each job's `schema` + `last_run`; `POST /admin/jobs/{name}/run` accepts a JSON `{params}` body (400 on invalid params).
- **Browse sort control.** Browse and search now offer a *Sort by* dropdown — `Default` (quality-weighted), `Title A–Z`, `Title Z–A`, `Newest first`, `Oldest first` — available to everyone. The choice carries through pagination and the facet form and is remembered per-browser via `localStorage`. `Newest`/`Oldest` order by the catalog `added_at`, which is now populated from the MediaCMS `add_date` on sync (and backfilled from `synced_at` for existing rows).
- **Branded placeholder art with hover-to-thumbnail.** Tiles without a real poster match (no TMDB/OMDB art) now show a random branded placeholder from `placeholder_dir` instead of the raw MediaCMS thumbnail; hovering reveals the real thumbnail via a CSS crossfade. The placeholder list is cached in memory and rescanned periodically.
- **Admin tile actions.** Catalog tiles now offer admins (rank ≥ 3) *Add to playlist* (picker modal), *+ Recent* (append to the admin's most recently created playlist, no modal), and *Hide* (tags the item `kryten-hidden` in MediaCMS and hides it locally immediately). New endpoints: `POST /admin/playlists/{id}/append`, `POST /admin/playlists/recent/append`, `POST /admin/catalog/{token}/hide`, and `POST /admin/catalog/{token}/unhide`.

### Changed

- **Tile action buttons stack vertically**, full-width, for clearer affordance at narrow tile widths.

### Fixed

- **Job-run history no longer shows phantom `running` rows.** A job's running flag lived only in memory, so a restart or killed worker mid-run left the `job_runs` row stuck at `running` forever. On startup such orphans are now reconciled to a new `interrupted` status (styled in the admin dashboard).

### Removed

- **Live queue page no longer shows the order number or a drag handle.** The `qi-pos` index and the non-functional `qi-drag` (☰) affordance were removed; reordering lives in the playlist editor, not the live queue.

## [0.8.2] - 2026-06-08

### Fixed

- **Queue ETAs no longer depend on the server clock/timezone.** Predicted start times were emitted as absolute UTC timestamps, so any host clock skew or timezone misconfiguration shifted every ETA by the offset (the persistent "TZ issue"). The shadow now also emits a clock-independent relative offset (`estimated_start_in_sec` = seconds-from-now until an item plays), and the queue page computes the wall-clock time from the **browser's** own clock (`Date.now() + offset`). The absolute timestamp is retained for compatibility and as a fallback. Result: ETAs are correct regardless of server clock/timezone.

## [0.8.1] - 2026-06-08

### Added

- **True RRULE-based recurring schedules.** Recurring schedules now auto-re-arm: after an automatic timed fire, the scheduler computes the next occurrence from the schedule's `rrule` (anchored on its fire time), advances `fire_at`, clears `fired_at`, and registers the next job — no manual re-arming needed. On startup, recurring schedules whose fire time elapsed while the service was down are advanced to their next future occurrence. Manual "Fire Now" intentionally does **not** advance the recurrence; the originally scheduled occurrence stays armed. Unparseable or exhausted rules are logged and left inert. Adds an explicit `python-dateutil` dependency for RRULE parsing.

## [0.8.0] - 2026-06-08

### Added

- **Admin Playlists UI.** The placeholder is replaced with a full management page: list/create/delete saved playlists, a two-column item editor (catalog search-to-add, drag-and-drop plus up/down reorder, per-item remove), bulk text import (`cm:`/`type:id`/bare-token, with unresolved-line reporting), rename + immutable toggle, and "Import to Live" to load a playlist into the CyTube queue. A new stateless `POST /admin/playlists/parse-text` endpoint exposes the existing text parser so parsed items merge into the editor and persist via the existing `PUT /{id}/items`.
- **Admin Schedules UI.** List of scheduled fires with playlist names, local fire times, lock window and status; create/edit/delete with `fire_at` (datetime-local → UTC), `pre_fire_lock_minutes`, `is_recurring`/`rrule`, and active toggle; "Fire Now"; and an active-schedule banner with "Clear Active".
- **Admin Queue Management UI.** Live `queue_shadow` table (auto-refreshing) with pay/scheduled metadata, ETA, paid-by and Z cost; remove (auto-refund), jump, an admin add-item modal (catalog search + placement mode), and the catalog sync log with a "Sync Now" trigger.
- **Upcoming-schedule announcement on the Queue page.** A public `GET /queue/next-schedule` feeds a banner with the next scheduled playlist, its fire time, and a live countdown (noting when pay-to-play is closed).
- Shared admin CSS for section headers, forms, modals, badges, the playlist editor list, drag-reorder, and catalog-add results.

### Changed

- **Specific pre-fire-lock messaging.** Submitting during a lock window now returns `Pay-to-play is closed: "[event]" starts in N min.` instead of a generic locked error (surfaced in the existing toast).

## [0.7.5] - 2026-06-08

### Added

- **Hidden categories/tags with an admin reveal toggle.** Items in the categories `Z Channel Promos`, `Z Event Movies`, `Weekday Z Promos` and the tags `grindhousebumper`, `commercialsforbumpers`, `bumpers`, `channelz`, `grindhousetrailer`, `publicaccess`, `religioustv` are now excluded from the catalog browse/search results and from the category/tag facet dropdowns. Admins (rank ≥ 3) see a notice — "Certain items are hidden from results. Show hidden items?" — that toggles them back into view via `?show_hidden=1` (ignored for non-admins).
- **Modern, readability-focused typography.** The UI now loads Inter (body/controls) and Sora (headings) from Google Fonts, with antialiasing and `optimizeLegibility` enabled.

### Removed

- **"Play Next" buttons** are no longer shown on the catalog browse cards or the item detail page (the underlying play-next queue mechanism is unchanged).

## [0.7.4] - 2026-06-08

### Changed

- **Catalog item detail page redesigned** to a two-column layout: the poster and action buttons (Queue / Play Next / Queue as Admin) stack in a sticky left column, while the right column shows the title, a divider, the formatted description, and **Category** / **Tags** facet rows. Category and tag chips link back into a filtered catalog browse. Categories/tags are sourced from the catalog join tables (populated by sync since 0.7.0).

## [0.7.3] - 2026-06-08

### Fixed

- **Description line breaks now render.** MediaCMS stores descriptions as newline-delimited plain text (`Synopsis:` / `Tagline:` / `Cast & Crew:` sections separated by `\n`), but the catalog item-detail page collapsed those newlines into a single run-on paragraph. Both the item-detail description and the Now Playing card now use `white-space: pre-line` to preserve the line/paragraph breaks; the Now Playing card also normalizes legacy `\r\n` line endings.

## [0.7.2] - 2026-06-08

### Changed

- **Now Playing card redesigned for readability.** The card is now a vertical stack: the title spans the full width across the top (with a divider), then a row pairs a 2:3 poster with the time display, the progress bar, and the **Remaining** time directly under the bar. Below that, the item **description** and **category/tag chips** are shown when available. The now-playing state is enriched server-side with the catalog description and category/tag names for the playing item.
- **"Hide Previous" now defaults to on** so the Queue page opens focused on what's still to come.

## [0.7.0] - 2026-06-08

### Added

- **Category & tag search facets on the catalog listing.** The category dropdown is now populated with the distinct MediaCMS categories that actually have items, and a new **Tags** dropdown sits beside it. Selecting either narrows the listing; both are preserved across pagination. Catalog sync now fetches per-item `categories_info`/`tags_info` from the MediaCMS media-detail endpoint (the bulk `manage_media` list omits them) and maintains the `categories`/`tags` join tables. Facet dropdowns only surface categories/tags with at least one item, and tags are ordered by usage.
- **"Hide Previous" toggle on the Queue page** — a switch in the Up Next header hides every item before the currently-playing one, for a cleaner view of what's still to come.

### Fixed

- **Queue page no longer overflows the viewport width.** The three-column queue grid now uses `minmax(0, …)` tracks (and `min-width: 0` on its columns) so long titles or the no-wrap Now Playing times can't blow the layout past 100% width. The queue column was also slimmed slightly.

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
