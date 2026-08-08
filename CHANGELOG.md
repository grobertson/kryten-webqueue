# Changelog

## [0.33.5] - 2026-08-07

### Fixed
- Ensure console script entry points (`kryten-webqueue`) are present in the
  published wheel so `pipx install kryten-webqueue` exposes the executable.

## [0.33.4] - 2026-08-07

### Added
- `rehost_emotes` job: fetches the current channel emote list, downloads any
  images not yet hosted on `dropsugar.co`, places them in
  `/home/mediacms.io/mediacms/static/emotes/{bare_name}{ext}` with `www-data`
  group ownership, updates each emote URL via api-gate, and saves timestamped
  before/after JSON backups in `~/emote_backups/`. Runnable on demand from
  Admin → Jobs or automatically on a configurable interval (default 24 h).
  New config section `emote_rehost` (see `config.example.json`).
- `ApiGateClient.get_emotes()` and `ApiGateClient.update_emote()` methods.

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.33.3] — 2026-08-06

### Fixed

- **yt-dlp JS runtime and remote challenge solver enabled.** Deno 2.9.5 is now installed system-wide on the server (`/usr/local/bin/deno`), resolving the "No supported JavaScript runtime could be found" warning that caused some YouTube formats to be missing. Additionally, `remote_components = ["ejs:github", "ejs:npm"]` is now set in the central `_YoutubeDLWithJSRuntimes` wrapper so yt-dlp can fetch and cache the EJS challenge solver script and NPM package on first use.

## [0.33.3] — 2026-08-06

### Fixed

- **yt-dlp JS runtime and remote challenge solver enabled.** Deno 2.9.5 is now installed system-wide on the server (`/usr/local/bin/deno`), resolving the "No supported JavaScript runtime could be found" warning that caused some YouTube formats to be missing. Additionally, `remote_components = ["ejs:github", "ejs:npm"]` is now set in the central `_YoutubeDLWithJSRuntimes` wrapper so yt-dlp can fetch and cache the EJS challenge solver script and NPM package on first use.

## [0.33.2] — 2026-07-29

### Added

- **Per-user submission quota on feedback and title suggestions.** Each user may now submit at most **2 per day and 6 per week** for feedback *and* (independently) for suggestions. Exceeding either tier returns `429` with a message pointing at the day/week limit. Implemented as a new `QuotaLimiter` (multi-tier sliding window) wired into the two submit endpoints, in addition to the existing short-burst throttle. Search/resolve requests are unaffected.

## [0.33.1] — 2026-07-29

### Changed

- **Applied `black` formatting across the codebase.** Repository-wide `black` pass (default profile) to bring all source and test modules — including the vendored `integrations/cmsutils/*` tools — into a consistent style. No behavioral changes; purely cosmetic.

## [0.33.0] — 2026-07-29

### Added

- **Sunday schedule in the "Fetch URLs (weekend workbook)" job.** `fetchurls` now resolves two additional column-A sections from the weekend workbook: `SUNDAY MORNING` → the **"Sunday Morning"** saved playlist (slug `sunday-morning`), and `SUNDAY AFTERNOON` → the **"Sunday Daytime"** saved playlist (slug `sunday-daytime`). The two new sections join the existing Friday/Saturday sections and are resolved and imported the same way (dropsugar.co validated via HEAD; YouTube/Tubi auto-downloaded and posted to the CMS). The job's `section` parameter gains `sunday-morning` and `sunday-daytime` options (`all` still runs every section). Sunday lives in the existing `M.D-M.D` weekend sheet as extra rows — the worksheet naming scheme is unchanged (first sheet with Sunday rows is `8.7-8.8`).

## [0.32.3] — 2026-07-09

### Fixed

- **Tag/category-classified promos are now also exempt from recently-played hiding.** v0.32.2 only exempted promo-*pool* clips, but most station promos/bumpers (e.g. “CHANNEL Z …”) are classified by hidden category (`Z Channel Promos`) or tag (`channelz`, `bumpers`, …) instead, so they were still being recorded and appeared in the recently-played debug list. `record_play_completion` now skips any item hidden from the public catalog by a promo pool **or** a hidden category/tag. On startup the service self-heals by purging any such rows written by earlier builds (`purge_promo_hide_state`, now covering the tag/category case).

## [0.32.2] — 2026-07-09

### Fixed

- **Promo clips are exempt from recently-played hiding.** Playing a promo-pool clip no longer records any hide state — promos are already excluded from the public catalog and must not be treated like normal mutable/immutable playlist items. `record_play_completion` now skips promo-pool members entirely. A one-time migration (v14) purges any promo hide state recorded before this fix; the same cleanup is also available on-demand via `purge_promo_hide_state()`.

## [0.32.1] — 2026-07-09

### Added

- **Admin controls to test recently-played hiding in situ** without waiting for real playback:
  - `POST /admin/catalog/{token}/mark-played` — simulates a genuine completion via the same `record_play_completion` the poll loop uses, so it also exercises the mutable-playlist pass logic (marking episodes, resetting on the last item).
  - `POST /admin/catalog/{token}/clear-played` — removes an item's completion and playlist-pass rows so it reappears for regular users immediately.
  - `GET /admin/catalog/recently-played/debug` — read-only snapshot of exactly what regular users are not seeing and why (time-window vs mutable-playlist pass).
  - The admin catalog browser gains **Mark played** and **Clear played** buttons per item.

## [0.32.0] — 2026-07-09

### Added

- **Hide recently-played items from the public catalog.** Regular users no longer see a title in browse/search for a configurable window after it has actually finished playing. Admins (rank ≥ 3) always see every title. The window is set by `catalog_recently_played_hide_days` (default `21`; `0` disables).
  - **Based on genuine play-completion, not queue time.** A new `CompletionRecorder` runs in the poll loop and records a completion only when the now-playing pointer advances off an item that played past ~50% of its duration (`play_completions` table). This means an item that was pay-to-queued and then **refunded/removed by an admin before it played never counts as played**, and an early admin *skip* of the currently-playing item doesn't either.
  - **Mutable playlists (TV-show collections) are handled as a unit.** Short (< 1 hour) episodes of a mutable playlist are governed by playlist position instead of the day window: each hides as it plays, and reaching the playlist's **last item** releases the whole collection at once (`playlist_item_played` table). This lets admins append S2/S3 to a show without re-hiding S1 piecemeal or over-playing season 1. Longer items (e.g. a movie) inside a mutable playlist still follow the normal completion + day-window rule.
  - **Firing a mutable playlist skips episodes already played in the current pass.** `fire_schedule` (scheduled or manual "fire now"), including the appended fallback playlist, now omits already-played episodes so a re-fire continues where the pass left off instead of replaying from the start. Once the last item plays the pass resets and the next fire loads the full playlist again. Immutable/promo playlists are unaffected.
  - **Manual "import playlist to live queue" honors the same skip, with an override.** Importing a saved playlist now skips already-played episodes by default (matching automated firing); pass `?full=1` on the import endpoint to force-load the entire list regardless of pass state. Automated use (a playlist attached to a schedule as a fallback) always observes the played/non-played rules. The playlist editor exposes both as **Import to Live** (skip played) and **Load Entire List** (full) buttons.
  - Promos are unaffected — promo/immutable-playlist items are already excluded from the public catalog.

[0.32.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.32.0
[0.32.1]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.32.1
[0.32.2]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.32.2
[0.32.3]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.32.3

## [0.31.0] — 2026-07-04

### Fixed

- **Moderation list "Remove" button now works.** The button was rendered with `onclick="removeModEntry(${JSON.stringify(e.username)})"` — `JSON.stringify` wraps the value in double quotes that immediately terminate the `onclick="…"` attribute, so browsers reported *"unexpected end of input at admin:1:16"* and the function was never called. The button now uses a `data-mod-rm-username` attribute with a delegated `click` listener on the stable `#mod-entries-list` container, consistent with how the "Moderate…" buttons in the Recent Users table work. The existing `confirm()` dialog inside `removeModEntry` is unaffected and will now prompt as intended.

[0.31.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.31.0

## [0.30.0] — 2026-07-04

### Fixed

- **Clean shutdown when a catalog sync (or other job) is in progress.** Previously, shutting the server down while a background job was running caused a cascade of WARNING/ERROR log lines as the database connection and HTTP client were closed underneath the still-running job task. Three changes fix this:
  - `JobManager` gains a `stop()` method that cancels all in-flight task and awaits them.
  - The lifespan shutdown now calls `job_manager.stop()` *before* closing the database, HTTP clients, or any other shared resource, so the cancelled tasks can still record their final status cleanly.
  - Background loop tasks (`bg_tasks`) are now properly awaited after cancellation, preventing "Task exception was never retrieved" warnings.
  - `JobManager._execute` wraps every database write in a defensive `try/except` so that a race between task teardown and resource teardown never produces an unhandled exception or spurious ERROR log.
  - `CatalogSync.sync` adds an explicit `asyncio.CancelledError` handler that logs a clean info message and re-raises (letting `JobManager` record the `cancelled` status), and wraps the error-path `finish_sync_log` calls defensively for the same reason.

[0.30.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.30.0

## [0.29.0] — 2026-07-04

### Changed

- **Admin JavaScript moved to external static files.** All inline `{% block scripts %}` JavaScript has been extracted from the five admin templates into individual per-page files under `static/js/` (`admin.js`, `admin-playlists.js`, `admin-promos.js`, `admin-schedules.js`, `admin-queue-mgmt.js`). Shared utilities `showModal`/`closeModal` and `fmtDur` were promoted to `main.js` (previously only defined per-page). Duplicate `escapeHtml`/`fmtDur`/`showModal`/`closeModal` definitions are removed from per-page files. Each template now references its JS via a single `<script src="/static/js/...">` tag — no functional changes, better cacheability and maintainability.

[0.29.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.29.0

## [0.28.0] — 2026-07-04

### Added

- **Moderation tab on the admin dashboard.** A new "Moderation" tab provides full-coverage access to the kryten-moderator service without leaving the admin panel. Four sections are lazy-loaded on first open:
  - *Service Status* — compact stat grid showing live health, version, uptime, users tracked, bans enforced, and mutes enforced (with a Refresh button).
  - *Moderation List* — filterable table (All / Ban / SMute / Mute) of active entries, showing username, action badge, reason, issuing moderator, and timestamp. Entries can be removed individually (with confirmation); new entries are added via an inline "Add entry…" collapsible form (username, action, optional reason). The logged-in admin's username is recorded as the moderator automatically.
  - *Username Patterns* — table of banned username patterns with regex indicator, action badge, description, and who added them. Patterns can be removed individually; new ones are added via an inline "Add pattern…" form with a regex toggle.
  - *Recent Users* — configurable look-back window (minutes) loads a table of users recently seen in the channel, sorted by last-seen descending. Unmoderated users show a "Moderate…" button that opens a modal to pick an action and optional reason, immediately adding a moderation entry and refreshing both the recent-users and moderation-list tables.
- New `GET|POST|DELETE /admin/moderation/entries[/{username}]`, `GET|POST|DELETE /admin/moderation/patterns[/{pattern}]`, `GET /admin/moderation/recent`, and `GET /admin/moderation/status` proxy endpoints (admin-only); the configured `channel` is resolved server-side so it never needs to be specified in the UI.

### Changed

- **`catalog/db.py` split into `catalog/db/` package.** The monolithic database module has been broken into four focused mixins (`_CatalogMixin`, `_PlaylistsMixin`, `_QueueMixin`, `_FeedbackMixin`) wired together in a single `Database` class. Public API is unchanged.

[0.28.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.28.0

## [0.27.1] — 2026-06-23

### Fixed

- **"Suggest a Title" search row layout.** The Search button is now reliably pinned to the right end of the title input on the same row, instead of overlapping it on wide viewports (seen in Opera/Blink). The input inherited a `width: 100%` that conflicted with its flex sizing; the row now sizes the input with `flex` + `min-width: 0` and gives the button a fixed natural width.
- **Suggestion results now use the full width on wide screens.** The candidate cards were confined to the narrow ~760px form column and stacked into many short rows. The results grid now breaks out of the form column and centers on the viewport (up to 1100px), so wide screens show up to four cards per row; it still collapses to fewer columns (and a single column on mobile) as the viewport narrows, with no horizontal overflow.

[0.27.1]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.27.1

## [0.27.0] — 2026-06-23

### Added

- **Viewer feedback + "Suggest a Title".** A new **Feedback** page (login required, linked in the nav) lets viewers contribute to Channel-Z in two tabs. *Feedback* is a simple text box — submit and get an instant thank-you. *Suggest a Title* resolves what you type against our movie databases (TMDB + OMDB) and shows the candidate matches so you can confirm the right one; pick a match and it's recorded for the team. A title we can't match is still accepted (stored as **unresolved**), and if we already have it you're told right away with a link to watch it now.
- **Admin triage queues for feedback & suggestions.** Both feeds land in the admin dashboard with the same lightweight workflow: mark entries **read/unread** and **delete** stale ones, with a per-tab unread badge and an "unread only" filter. Suggestion rows show the resolved match (title/year/source), whether we **already have it in the catalog** (with a direct link to the item), or that it's unresolved.

### Changed

- **Admin dashboard is now tabbed**, mirroring the user profile page. The at-a-glance sections (Overview, Jobs, Sync Logs) plus the new Feedback and Suggestions queues are organized into tabs; the Playlists / Schedules / Queue Management / Promos pages remain linked as before.
- **Race odds column is now a fixed width.** The odds/bet-total column on the `/race` view no longer auto-sizes to each entry's bet total, so the lanes (and the track rails) line up cleanly instead of staggering as bets come in.
- **Race view now supports 8 cars.** Added Brown and White lane colours to match the economy's expanded 8-car grid.

[0.27.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.27.0

## [0.26.0] — 2026-06-22

### Changed

- **Race view is now smooth, with cars, drivers, and live commentary.** The economy now ships the whole race as a precomputed timeline (economy 0.13.0), so the `/race` view animates it client-side with `requestAnimationFrame` + interpolation — cars glide continuously and re-sync to the server clock instead of jumping between polls. Visual overhaul: each racer is a little CSS race **car** (cabin + wheels) that drives down a striped track from a colour start-cap to a checkered finish, with the **driver's name** (e.g. *Manuel Transmission*) under its colour, a live **position** column, and bet totals. A **commentary banner** above the field shows play-by-play (start, driver-named lead changes, close-finish, winner call) with the newest line replacing the last. Now shows all **6** cars.
- **Race WebSocket bandwidth cut.** The heavy position timeline is broadcast only once per race; subsequent frames carry just the server `elapsed` re-sync hint (the browser self-animates), so per-tick traffic stays tiny even with many spectators. Late-joiners still receive the full timeline on connect.

[0.26.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.26.0

## [0.25.0] — 2026-06-22

### Added

- **Live web race view at `/race`.** The economy's racing game play-by-play moved off public chat (which flooded the channel) and onto a visual web view anyone can watch — no login required. A new public page animates the race in real time: each racer glides along its lane toward the finish, with live positions, odds, the betting countdown, per-colour bet totals, and a winner banner + payouts when it ends. A background `RacePoller` polls api-gate's `GET /economy/race` adaptively (fast while a race is live, backing off when idle) and pushes frames to spectators over a dedicated public `/ws/race` WebSocket; a newly-opened view is immediately shown a race already in progress. The race WebSocket carries only race frames (no user or queue data), and a “Race” link was added to the nav.

[0.25.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.25.0

## [0.24.0] — 2026-06-21

### Added

- **Live readability preview for the chat-color picker.** The dashboard's chat-color editor now previews the chosen color exactly as it renders in chat — a sample chat line (gray timestamp, username, message) on the near-black chat background (`#111`) — and scores it as you pick, mirroring kryten-economy's server-side guard (combined APCA lightness contrast × red-chroma penalty). A contrast badge reports **ok / warn / reject**: colors that fail the readability threshold (very dark colors and harsh pure reds) show a **reject** badge and the **Save button is disabled**, so users can't buy a color the server would refuse; borderline colors get a non-blocking **warn**. The thresholds (`reject < 30`, `warn < 40`) and background match the economy service so the dashboard and server always agree.

[0.24.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.24.0

## [0.23.1] — 2026-06-21

### Changed

- **Release CI hardening (no runtime changes).** PyPI publishing was moved out of a reusable workflow into a top-level one. PyPI Trusted Publishing matches the OIDC `job_workflow_ref` claim — the filename of the workflow containing the running job — against the configured publisher (`python-publish.yml`); invoking that workflow via a reusable `uses:` call is unsupported by PyPI and raised a "workflow misconfiguration" warning. `python-publish.yml` now triggers via `workflow_run` after `release.yml` completes (keeping it top-level so the publisher filename stays `python-publish.yml`), and `release.yml` no longer calls it reusably. All GitHub Actions were bumped off the deprecated Node 20 runtime: `actions/checkout@v7`, `actions/setup-python@v6`, `actions/upload-artifact@v7`, `actions/download-artifact@v8`, and `astral-sh/setup-uv@v8.2.0`.

[0.23.1]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.23.1

## [0.23.0] — 2026-06-21

### Fixed

- **Scheduled-event pre-fire lock lingered until midnight.** The pay-to-play pre-fire lock lives in `playlist_schedules` and was gated on `fire_at > datetime('now')`. `fire_at` is stored as a raw ISO string with a `T` separator (e.g. `2026-06-21T15:00:00+00:00`, or `…Z` from the admin UI) while SQLite's `datetime('now')` is space-separated, so the two were compared as **strings** — `'T'` sorts after `' '`, keeping the condition true from fire time until the calendar date rolled over. A 15-minute pre-fire lock effectively lasted until midnight, the queue stayed locked with no active-schedule row to show for it, and "Clear Active" couldn't lift it. The lock queries now wrap `fire_at` in `datetime(…)` so the comparison is over normalized timestamps and the lock releases exactly at `fire_at`. `get_next_schedule` shared the same flaw (already-fired events showed as "next" until midnight) and is fixed too.

### Added

- **Clear admin lockout indicator + one-click "End lockout now".** The admin Schedules page now shows a prominent banner whenever pay-to-play is closed — covering the pre-fire window (which has no active-schedule row and was previously invisible until a queue attempt failed) as well as an in-progress immutable event. A new `GET /admin/schedules/lock-status` reports the authoritative state, and `POST /admin/schedules/unlock` now lifts an in-progress event lock **and** every active pre-fire lock in a single action (it previously lifted only one), so one click reliably reopens the queue. Schedules stay armed — a recurring event re-locks on its next firing.

[0.23.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.23.0

## [0.22.0] - 2026-06-19

### Added

- **Shoutout on the Vanity Items tab.** The dashboard's Vanity tab gains a third item — **Shoutout** — alongside Greeting and Chat color, each now with a short call-to-action. Sending a shoutout posts the user's message to public chat (`📢 <user>: …`) via the new `POST /user/vanity/shoutout` route, which proxies the api-gate `POST /economy/vanity/shoutout` endpoint. The message is the user's own input (validated server-side: trimmed, non-empty, max 200 chars); the cost/availability come from the account summary; the username is always taken from the authenticated session.

## [0.21.0] — 2026-06-17

### Changed

- **Catalog sync no longer runs on startup.** The background sync loop now waits a full interval before its first run instead of syncing immediately when the process starts, so a restart won't kick off a sync. Admins can still trigger it on demand with the "Sync Catalog" button.
- **Friendlier job buttons.** Jobs that open a parameter dialog before running now show **"Begin…"** instead of "Run…", signalling that a dialog (with a Cancel) comes next rather than an immediate action. One-click jobs keep the plain **"Run"** label.

[0.21.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.21.0

## [0.20.1] — 2026-06-17

### Fixed

- **Z-Coin dashboard layout polish.** The left account column is now a fixed 320px width (was a flexible 280–360px range that shifted with content), and the tab strip has proper folder-tab styling — filled inactive tabs with hover feedback and an active tab that visually connects to its panel — instead of the previous near-invisible underline.

[0.20.1]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.20.1

## [0.20.0] — 2026-06-17

### Added

- **Richer fetchurls / job logging.** The fetchurls job now logs a per-section resolved/failed summary and a WARNING line for every failing URL (with its Excel row and the reason), and folds a compact `failures_detail` list into the `job_runs` record so the admin "Detail" column shows a concrete example instead of just a count. The `run()` result gained `section_summary` and `failure_details`.
- **Actionable log format.** Application loggers (`kryten_webqueue.*`) now include the source `file:line` in each line via a dedicated formatter, while uvicorn keeps its leaner format.

### Fixed

- **SharePoint download failures lost their detail.** `download_sharepoint_xlsx` raised `SystemExit` (a `BaseException` the job manager's `except Exception` couldn't catch), so a failed download could bubble up uncaught with no recorded detail. It now raises `RuntimeError` with the HTTP status and a response excerpt, so the failure is caught, logged, and recorded in the job run.

[0.20.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.20.0

## [0.19.0] — 2026-06-17

### Changed

- **Z-Coin dashboard reorganized into tabs.** The account page was overcrowded with three side-by-side columns. It's now a widened account card (balance, rank, progress, perks) beside a tabbed container with **Queue History**, **Recent Transactions**, and a new **Vanity Items** tab. Each tab lazy-loads its data on first view; the vanity greeting/color editors moved out of the cramped left column into their own roomier tab. Collapses to a single column on narrow screens.

[0.19.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.19.0

## [0.18.0] — 2026-06-17

### Fixed

- **Active scheduled event lingered on the admin page after it ended.** The `active_schedule` row was only removed by the manual "Clear Active Schedule" button — the lock auto-lifted but the row (and its banner) stayed. The queue shadow now clears it automatically on the next poll once the event is genuinely over, via two signals: the last scheduled item has left the queue (event temp items auto-remove after playing), or the estimated end is more than 5 minutes in the past (safety net for a missed boundary or a restart mid-event). The schedules page also hides a well-past banner immediately and re-checks every 15s.

### Added

- **Live admin dashboard.** The admin page now subscribes to the same `/ws` feed as the public queue, so the queue item count and now-playing update without a reload. Job status refreshes every 5s while the tab is visible (jobs are DB-polled, not broadcast), and a fired schedule triggers an immediate jobs refresh.

[0.18.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.18.0

## [0.17.0] — 2026-06-17

### Added

- **Search now combines with category/tag filters.** A free-text search ANDs with the selected category and/or tag instead of ignoring them. `db.search()` / `db.search_count()` accept `category` and `tag`; the `/catalog/search` page and JSON route pass them through and keep the dropdowns populated/selected; `applyFacets()` includes the active facets when a query is present. (Shared `_facet_filter()` SQL helper keeps browse and search behavior identical.)
- **Clear empty-results messaging.** When a search and/or facet filter returns nothing, the catalog page now explains *why* — naming the active query and filters — and offers one-click escapes ("Search without filters", "Clear filters", "Back to browse") instead of a bare "No items found."

[0.17.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.17.0

## [0.16.0] — 2026-06-17

### Added

- **Light / dark theme toggle.** A navbar button switches between dark (default) and light themes. The choice persists in `localStorage`; first-time visitors follow their OS `prefers-color-scheme`. An inline pre-paint script in `base.html` applies the saved/preferred theme before first render to avoid a flash. Light/dark palettes are defined as `:root[data-theme="..."]` overrides of the existing CSS variables, so the whole UI re-themes without per-component changes.

### Changed

- **Clearer playlist terminology (display only).** Admin playlist/schedule/promo screens now label reserved playlists as **Non-preemptable** and normal ones as **Preemptable** (previously "Immutable" / "Mutable"). This is a wording change only — the `is_immutable` data field, API payloads, and config keys are unchanged.

[0.16.0]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.16.0

## [0.15.2] — 2026-06-17

### Fixed

- **Promos were inserted into immutable playlists during a schedule fire.** A scheduled event clears the queue and loads its items over several seconds (throttled adds + 422 retries), but the event lock that suppresses promos was only recorded *after* the entire load finished. Meanwhile the state poller (every ~3s) saw a partially-built queue with no lock and slotted a general promo between the freshly-added immutable items. `PromoDirector` now exposes a re-entrant `suppressed()` guard that `fire_schedule` (and manual playlist import) holds for the whole load — spanning through `set_active_schedule`, so for an immutable event the persistent lock is already live by the time suppression lifts (a clean handoff with no race window). When suppression releases, the next poll re-baselines now-playing instead of treating the bulk load as content advancing, so no promo fires on the wrong boundary. This also satisfies the general rule: never evaluate promos while a bulk queue insert/append is in progress, regardless of playlist type.

[0.15.2]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.15.2

## [0.15.1] — 2026-06-17

### Fixed

- **Application logs were silently dropped.** The app never configured Python logging — `uvicorn.run(log_level="info")` only sets up uvicorn's own loggers, leaving the `kryten_webqueue` hierarchy with no handler, so Python's "last resort" path emitted only `WARNING`+. Every `logger.info(...)` (including all promo-insertion diagnostics) was discarded. A new `logging_config.build_log_config()` installs a `dictConfig` (passed to uvicorn via `log_config`) that attaches a console handler to the application loggers.
- **Silent failure in the promo poll loop.** `PromoDirector.on_poll()` swallowed every exception from the immutable-event-lock check with a bare `except: return` and no log line, hiding faults. It now logs at `WARNING` with a traceback before skipping the cycle.

### Added

- **Configurable log levels.** New `log_level` (default `INFO`) and `promo_log_level` (default falls back to `log_level`) config fields. Set `promo_log_level` to `DEBUG` for a full per-poll trace of promo decisions without flooding the rest of the app (the `kryten_webqueue.promos` logger is independently tunable).
- **Deep promo observability.** `PromoDirector` now emits detailed diagnostics across the whole insertion path: now-playing advance + cadence counter, clip selection (order, pool size, sequential index, random no-repeat avoidance), weighted type pick (candidates/weights/skipped), cadence-due reason, idempotency-guard skips, lead-in decisions, and the add/move/uid result. It warns on single-clip pools (which always repeat) and **errors** when an add succeeds but returns no uid (an untracked insertion that would otherwise repeat every poll).

[0.15.1]: https://github.com/grobertson/kryten-webqueue/releases/tag/v0.15.1

## [0.15.0] — 2026-06-14

### Added

- **Account progression panel on the Z-Coin dashboard.** The left column now surfaces the user's economy account in full: current rank/level, a progress bar toward the next milestone (remaining Z + percent), active perks (including spend discount), and their purchased vanity items. Powered by the new api-gate `GET /economy/account/{username}` endpoint (economy `account.summary`), proxied via `GET /user/account`.
- **Vanity item editing dialogs.** Inline **Edit** buttons open dialogs to purchase/update the custom greeting (textarea, 200-char limit) and custom chat color (native color picker synced with a 6-digit hex field). Backed by `POST /user/vanity/greeting` and `POST /user/vanity/color`, which proxy the economy `vanity.set_greeting` / `vanity.set_color` commands. The username is taken from the authenticated session, never the request body.
- **Transaction credit/debit filter.** The Recent Transactions column gains an All / Credits / Debits toggle and a **Load more** control, with friendlier titles derived from each transaction's `trigger_id` (e.g. `presence.base` → "Watching reward", `spend.vanity.chat_color` → "Custom color") instead of raw `earn` labels.

### Changed

- **Queue history pagination.** `GET /queue/history` now accepts `limit`/`offset` and returns a `total` count; the dashboard's middle column paginates through the full history with Prev/Next controls instead of showing only the most recent 20 entries.

## [0.14.2] — 2026-06-13

### Added

- **Save all search/browse results to a playlist.** The Browse/search results page now shows an admin-only **Save results to playlist** button. It appends every catalog item matching the current search query or browse facets (across all pages, honouring the hidden-items toggle) to a playlist of your choosing. Where a season/episode marker is detectable in the title (`S01E02`, `1x02`, `Season 1 Episode 2`, …) items are laid out in proper series → season → episode order; everything else falls back to a stable alphabetical placement. Items already in the target playlist are skipped, so re-running is idempotent. Backend: `POST /admin/playlists/{id}/append-results` (admin-only) plus a de-duplicating bulk `Database.append_playlist_items` and a pure `playlists.ordering` helper.

## [0.14.1] — 2026-06-13

### Fixed

- **Cancel/refund PM notifications only fire for AFK owners.** A user who *leaves* the channel is no longer connected to CyTube and cannot receive a PM, so the leave path now skips the notification entirely (the refund + WS state update still happen). AFK owners — who are still present — are PM'd as before. `presence_refund.notify_user` now governs the AFK case only.
- **`no_repeat` promo selection is now deterministic.** When a `no_repeat` random draw matched the previous clip it was retried up to 8 times and could still return a repeat (a flaky guarantee for small pools). It now draws from the pool excluding the last clip, so a consecutive repeat never occurs for pools of 2+.

## [0.14.0] — 2026-06-13

### Added

- **Inline promo settings editing (plan O2).** The **Promos** admin page settings panel is now editable: toggle the system enable, set the movie threshold, general cadence (`every_n_items` / `every_m_minutes` / `no_repeat`), and per-type `enabled` / `order` / `weight`, then **Save**. Changes are persisted to the service config file and hot-applied to the running `PromoDirector` — no restart required. Backend: `PUT /admin/promos/config` (admin-only) plus `Config.save()` (atomic write back to the loaded config file) and `PromoDirector.update_config()`.
- **Cancel/refund notifications (plan O4).** When the presence monitor cancels & refunds a pending paid item, the owner now receives a PM explaining it ("… cancelled and refunded because you left the channel / you went AFK"). Controlled by `presence_refund.notify_user` (default on); best-effort so a failed PM never blocks the refund.

### Changed

- **`presence_refund.on_afk` now defaults on (plan O1).** AFK-based cancel/refund is enabled by default now that Kryten-Robot v1.10.0 (which tracks CyTube's `setAFK` event) is released. Set it off if running against an older Robot.

### Tested

- DB-level test asserting promo pool clips are hidden from browse/search and rejected by pay-to-play (`get_item`), and become visible again when the `promo_type` is cleared (plan §2.9 gap).
- Config save round-trip + no-source-path guard; presence-cancel PM (sent / suppressed) coverage.


### Added

- **Promo admin UI.** A new **Promos** admin page (`/admin/promos`, linked from the admin panel):
  - **Promo pool designation** — assign any saved playlist a promo type from a dropdown (or clear it), reusing the existing playlist editor for the clips. Immutable playlists are excluded (release them first). Promo pools are flagged with a badge on the Playlists page.
  - **Promo settings panel** — read-only display of the live `promos` configuration (system enable, movie threshold, general cadence, and a per-type table of enabled/order/weight). Inline editing is a planned follow-up; values come from the config file.
- **Live-queue promo badges.** Promo items in the public queue view now render distinctly, badged by promo type (Channel ID, Event, Mod Shoutout, Feature, Viewer's Choice) with a separate accent.
- Backend: `GET /admin/promos/config` and `GET /admin/promos/pools` (admin-only).

## [0.11.0] — 2026-06-13

### Added

- **Promo insertion system (`PromoDirector`).** Curated promo clips are now inserted between mutable content as playback advances, driven by the state poller:
  - **General promos** (`channel_identity`, `event`, `mod_shoutout`) inserted on a cadence — every N content items or every M minutes, whichever comes first — with weighted type selection and per-type `random`/`sequential` clip ordering plus an optional `no_repeat` guard.
  - **Feature Presentation** lead-in immediately before a mutable-playlist movie (`duration_sec >= movie_threshold_seconds`).
  - **Viewer's Choice** lead-in immediately before any pay-to-play item (a paid movie gets Viewer's Choice, never Feature Presentation). Inserted synchronously at pay time so it is in place before a "play next" item can begin, and removed automatically if the paid item is later cancelled/refunded (including presence-based cancels).
  - When both are due before the same item the order is `[general][lead-in][content]`. Promos are added as CyTube **temp** items (via the throttled add helper) so they auto-remove after playing and never accumulate across loops. The director is a no-op during an immutable scheduled event.
- **Promo pools.** A saved playlist can be designated a promo pool by tagging it with a `promo_type` (via the playlist create/update API). Promo pools are hidden from public browse/search and excluded from pay-to-play, the same treatment as immutable playlists.
- Config: `promos` block — global `enabled`, `movie_threshold_seconds`, a `general` cadence block (`every_n_items`, `every_m_minutes`, `no_repeat`), and per-type `enabled`/`order`/`weight` settings.
- DB: migration v10 (`promo_type` on `saved_playlists` + index) and v11 (`is_promo`, `promo_type`, `lead_in_for_uid` on `queue_shadow`).

### Note

- Backend promo designation and the full insertion engine ship in this release. A dedicated admin promo-settings panel and live-queue promo badges are a planned follow-up; pools can be designated today via the playlist API and `promos` config is file-based.

## [0.10.0] — 2026-06-13

### Added

- **Presence-based cancel/refund of pending paid items.** When a viewer who paid to queue an item leaves the channel (or goes AFK), their not-yet-played paid items are now automatically refunded and removed after a configurable grace period. The currently-playing item is never cancelled, and free/scheduled items are left untouched. If the owner returns before the grace window elapses the item is kept; transient api-gate/robot lookup failures are treated as inconclusive and never trigger a cancellation. Implemented by a new `PresenceRefundMonitor` running on its own interval (decoupled from the 3s state poll).
- Config: `presence_refund` block — `enabled` (default `true`), `on_leave` (default `true`), `on_afk` (default `false`; enable once Kryten-Robot ≥ 1.10.0, which tracks CyTube's `setAFK` event, is deployed), `grace_seconds` (default `60`), and `check_interval_seconds` (default `15`).

## [0.9.13] — 2026-06-12

### Fixed

- **Bulk playlist loads no longer fail with spurious `422 Unprocessable Entity`.** When importing a saved playlist into the live queue or firing a scheduled playlist, items were added back-to-back with no pacing. CyTube validates each queued item server-side (fetching the custom MediaCMS manifest), and adding faster than it can validate triggers a transient `queueFail` — surfaced by api-gate as HTTP 422 — even for perfectly valid URLs. The importer and scheduled-fire loops now throttle consecutive adds and retry the transient 422 with a short backoff.

### Added

- Config: `playlist_bulk_add_delay_sec` (default `0.5`) — pause between consecutive CyTube adds during bulk loads — and `playlist_bulk_add_max_retries` (default `2`) — retries on a transient 422.

## [0.9.12] — 2026-06-12

### Fixed

- **`fetch`/`fetchurls` no longer crash with "Invalid js_runtimes format".** Newer yt-dlp expects `js_runtimes` as a dict of `{runtime: {config}}`, not a list; the vendored downloader now passes `js_runtimes={'deno': {}, 'node': {}}`.

## [0.9.11] — 2026-06-12

### Fixed

- **`fetch`/`fetchurls` now enable a Node.js JavaScript runtime for yt-dlp.** yt-dlp needs an external JS runtime to solve YouTube's JS challenges; only `deno` is enabled by default, so hosts with only Node installed hit a "No supported JavaScript runtime could be found" warning and could miss formats. The vendored downloader now passes `js_runtimes=['deno', 'node']` to every yt-dlp call (priority deno > node, so the highest-priority available runtime is used), applied centrally via a thin `YoutubeDL` wrapper.

## [0.9.10] — 2026-06-12

### Changed

- **Graceful handling of expected job failures.** Misconfiguration and bad-input errors (e.g. `fetchurls` not finding this weekend's worksheet, a missing/unauthenticated workbook, or a missing optional dependency) are now recorded as a clean, actionable message in the job-run history and logged at WARNING — no stack trace. A new internal `JobError` distinguishes these expected failures from unexpected bugs (which still log a full traceback and keep their exception-type prefix).
- The `fetchurls` "sheet not found" message now reads as guidance ("This weekend's worksheet 'M.D-M.D' was not found…") and lists only the date-format weekend sheets instead of every tab in the workbook.
- The admin Jobs history table now shows a **Detail** column — the failure message for failed runs, or a compact summary (sheet, imported playlists, counts) for successful ones.

## [0.9.8] — 2026-06-12

### Added

- **`fetchurls` job now reads the Channel Z workbook from SharePoint** (Microsoft Graph) and **writes resolved dropsugar URLs back to column F**, restoring the original tool's full round-trip. The service authenticates *silently* from a pre-seeded MSAL token cache and never prompts interactively. A one-time sign-in helper seeds the cache: `python -m kryten_webqueue.jobs.fetchurls_auth` (also installed as `kryten-webqueue-fetchurls-auth`). It prints a device-code URL; sign in once and the ~90-day refresh token is cached for unattended runs. If the cache is missing/expired the job fails with a clear "run fetchurls_auth" message.
  - New config under `fetchurls`: `sharepoint_tenant_id`, `sharepoint_client_id`, `sharepoint_sharing_url`, `token_cache_path`. A local `workbook_path` (or a per-run `workbook_path` param) still works as a fallback/override.
  - New `writeback` job parameter (default on) controls column-F writeback; `dry_run` and local-file mode skip it.
  - Added `msal` as a core dependency.
- **`fetchurls` imports into three fixed, well-known playlists** — `Friday Night`, `Saturday Morning`, `Saturday Night` — instead of date-prefixed names. The playlists are matched by name (any creator), their items replaced in place on every run (idempotent), and their existing immutability preserved; if a playlist is missing it is (re)created as immutable so the reserved status is retained.

### Fixed

- Bulk/`cm:` playlist imports for items not yet in the local catalog (e.g. freshly downloaded by `fetch`/`fetchurls` before a sync) now construct the CyTube manifest URL from the token instead of leaving a bare token that wouldn't play.

## [0.9.7] — 2026-06-11

### Changed

- **Paid-queue chat announcement reworded.** A purchased item now announces as `"<title> added to the queue with Zcoin by <user> and is now <position>."` where `<position>` is `next` for the item immediately after the currently-playing one, or an English ordinal counting the now-playing item as first (e.g. `third`, `forty-second`, `one hundred seventh`). Position is computed relative to the currently-playing item and wraps around the playlist.
- **Admin queueing is no longer announced** in the channel chat (only paid placements are announced).
- Renamed the admin Schedules heading from “Scheduled Fires” to “Scheduled Events”.

## [0.9.6] — 2026-06-11

### Added

- **Richer Bulk Text Import on the admin Playlists editor.** The text import now accepts, one entry per line:
  - **dropsugar.co links** (watch `?m=TOKEN` or manifest `/api/v1/media/cytube/TOKEN.json`) — resolved against the catalog for title/duration, falling back to a constructed manifest URL when the token isn't catalogued yet.
  - **YouTube / youtu.be links** — playlist (`list=`), start-time (`t`/`start`) and all other arguments are stripped, leaving a clean `yt:VIDEOID` item.
  - Legacy `cm:token`, `yt:id`, and bare catalog tokens (unchanged).
  - Trailing free text after a URL (e.g. `URL - My Title`) is used as a title hint.
- **Upload a local text file** into the importer via a "Choose text file…" button (contents are loaded into the textarea for review before parsing).

### Changed

- The text import parser is now **tolerant**: blank lines, whole-line `#` comments, and inline trailing `#` comments are ignored, and links to unknown sites are skipped (reported as `unsupported_site`) instead of failing the import.

## [0.9.5] — 2026-06-11

### Changed

- No code changes — version bump only, to refresh the PyPI release index.

## [0.9.4] — 2026-06-11

### Added

- **App version in the footer.** Every view now shows the running `kryten-webqueue` version (e.g. `v0.9.4`) in the footer, sourced from the installed package metadata.
- **Scheduled-event fallback playlist.** Each schedule can name an optional *mutable* fallback playlist that is appended to the live CyTube queue right after the event's items, so the queue is no longer left empty once a scheduled event is exhausted — no manual intervention required. The fallback items aren't part of the "scheduled event", so they stay available for pay-to-play/search and don't affect when the event lock lifts. Configured via a dropdown (mutable playlists only) in the admin Schedule editor.

### Fixed

- **Playlist rows with a description no longer break row formatting.** The admin table action cell was a `display:flex` `<td>`, which dropped it from the table layout so its bottom border stopped aligning once a name + description grew taller. Action cells are now normal table cells (top-aligned, inline button layout), so buttons and row separators line up regardless of description length.

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
