# kryten-webqueue — Jobs & Browse Enhancements Spec

**Version:** 1.1
**Date:** 2026-06-08
**Status:** Design — open questions resolved — ready for phased implementation
**Author direction:** self-authored implementation plan (GitHub Copilot)

---

## 0. Decisions captured (from clarification)

| # | Question | Decision |
|---|----------|----------|
| 1 | How to run fetch/fetchurls/enrich* (Windows scripts vs Linux service) | **Reimplement the tools' logic inside webqueue** by vendoring the existing Python modules into an internal `integrations/` package and driving them in-process (see §A2). |
| 2 | Tool identity | Confirmed: `d:\devel\cmsutils\{fetchurls,enrichtitles,enrichmeta,enrichtv}.py` + `d:\devel\yt-pipe` downloader (`youtube_to_mediacms.py`, invoked today via `fetch.ps1`). The original request's "cmstools / enhance*" names map to these. |
| 3 | "Jobs never run at the same time" | **Per-job lock only** (already enforced in memory). The real defect the user is seeing is the **job-run history list** showing phantom `running` rows — fix that (see §A1.2). |
| 4 | Hide Item tag + write target | Write tag **`kryten-hidden`** to MediaCMS via the API token (MediaCMS is the source of truth). Hide **immediately in the local catalog**; the next sync confirms it. |
| 5 | Browse sort options + scope | `Default (quality)`, `Title A–Z`, `Title Z–A`, `Newest first`, `Oldest first`. **`Newest first` is available to everyone** (not admin-only). |
| 6 | "Most recent playlist" | **Most recently *created* saved playlist by the current admin** (`saved_playlists` where `created_by = user ORDER BY created_at DESC LIMIT 1`). |
| 7 | fetchurls weekend | **Always the upcoming weekend**: compute the next Friday and target the sheet named `M.D-M.D` (e.g. `3.6-3.7`), overriding the tool's current/just-past auto-select. |

### 0.1 Resolved open questions (was §I)

| OQ | Question | **Resolution** |
|----|----------|----------------|
| OQ-1 | fetchurls SharePoint auth in a headless service | **v1 = local file only.** The job reads the workbook from a configured/uploaded `.xlsx` path (`sharepoint.workbook_path` or an admin upload), reusing the tool's existing `--file` code path. **No Microsoft Graph / MSAL device-code in v1.** A future phase MAY add Graph with the device code surfaced in the admin UI; spec'd but not built now. Column-F writeback is **disabled** in file-only mode unless the file is writable in place. |
| OQ-2 | Vendor vs packaged dependency | **Vendor-and-adapt** into `kryten_webqueue/integrations/` (accept drift from `d:\devel\cmsutils`). Record the upstream commit/date in each vendored file's header. A future option to repackage `cmsutils` as an installable dependency is noted but not pursued now. |
| OQ-3 | "Upcoming weekend" when run on a Friday | **Use today's weekend** (the imminent Fri/Sat). `friday = today + ((4 - weekday) mod 7)` yields today when run on Friday; only Sat/Sun roll forward to next Friday. |
| OQ-4 | Random branded art stability | **Per server-render.** The browse route picks `random.choice(placeholders)` per affected tile when building the page; the src is stable for that page load (no client reshuffle, no layout thrash). Hover still reveals the real thumbnail. |
| OQ-5 | Include `unhide`? | **Yes.** Ship `POST /admin/catalog/{token}/unhide` (removes `kryten-hidden` in MediaCMS + locally) so a mis-hide is reversible from the admin "show hidden" view. |
| OQ-6 | MediaCMS tag-write endpoint | **Reuse the enrich tools' MediaCMS edit path.** Tags are written via the media-edit call to `POST /api/v1/media/{friendly_token}` with the `tags` field and the API token — the same mechanism `enrichmeta`/`enrichtv` use. Extract into `integrations/cmsutils/_common.py:MediaCMSClient.set_tags(token, tags)` and **read-modify-write** (fetch current tags, add/remove `kryten-hidden`, submit) to preserve existing tags. **Verify exact field/verb against the live instance during B6** with a round-trip integration test on a disposable item before wiring the UI. |

---

## 1. Scope

Two feature areas plus a jobs-framework fix:

- **A. Admin → Jobs**: framework fix (history reconciliation), and five new jobs that reimplement `fetch`, `fetchurls`, `enrichtitles`, `enrichmeta`, `enrichtv` with reasonable defaults and a small amount of new wiring (fetch → playlist, fetchurls → imported saved playlists).
- **B. Browse / Results**: sort control, random branded art with hover-to-real-thumbnail, vertically stacked tile buttons, admin "Add to playlist" / "Add to most-recent playlist" / "Hide Item".
- **C. Queue page**: hide the order number, remove the drag handle and all drag-reorder affordance.

Out of scope: changing the economy/pay flow, the scheduler, or the public catalog filtering rules beyond the new hide tag.

---

## A. Admin → Jobs

### A1. Job framework changes

#### A1.1 Per-job concurrency (confirm existing)
`JobManager.run()` already rejects a second start of the same job while one is in `self._running`. **No change required** to the guard itself. Document it: a job that is running returns `{"started": false, "reason": "already_running"}` and the UI disables its Run button (already implemented in `admin/index.html`).

#### A1.2 Fix the job-run history list (the actual bug)
**Problem:** `start_job_run()` inserts a row with `status='running'`; `_running` is in-memory only. If the service restarts (or the worker is killed) mid-run, the row is **never updated** and shows `running` forever in the history table. Long-running jobs (fetch/enrich) make this common.

**Fix (required):**
1. **Startup reconciliation.** On app startup, before registering jobs, run:
   ```sql
   UPDATE job_runs SET status='interrupted',
       ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP)
   WHERE status='running';
   ```
   Add a `Database.reconcile_orphaned_job_runs()` method; call it in the `lifespan` startup (after `run_migrations`, before background workers).
2. **Status vocabulary.** Add `interrupted` to the known statuses; style it in CSS (`.job-status-interrupted { color: var(--warning); }`).
3. **History query.** `get_job_runs` ordering by `id DESC` is fine; ensure the admin dashboard groups/labels by `job_name` and shows `triggered_by`. (Optional polish: a per-job "last run" summary above the raw history.)
4. **Heartbeat (optional, phase 2).** For very long jobs, periodically `UPDATE job_runs SET detail=? WHERE id=?` with progress (e.g. `{"processed": N, "total": M}`) so the UI can show live progress; the dashboard already polls.

**Acceptance:** after a hard restart during a job, the history shows that run as `interrupted`, never a perpetual `running`; a fresh run of the same job is allowed.

#### A1.3 Parameterized jobs
The current `JobManager.register(name, func)` takes a zero-arg coroutine. New jobs need **parameters** (URL, quality, playlist id, limits, dry-run, etc.). Extend the framework:

- `register(name, func, *, label, schema=None)` where `schema` is a small declarative list of fields (name, type, default, label, required, options) used to render a parameter form in the admin UI and to validate input.
- `run(name, *, triggered_by, params: dict | None = None)` passes validated `params` to the job function: `func(params, ctx)` where `ctx` exposes `db`, `api_gate`, `config`, and an async `progress(detail: dict)` callback.
- Persist the submitted `params` into the `job_runs.detail` (or a new `params` column) so history shows what was run.
- Back-compat: existing `catalog_sync` registers with no schema and a `func(params, ctx)` that ignores params.

**Admin UI:** the Run button opens a small modal generated from `schema` (reusing the shared admin modal/`field` CSS from v0.8.0). Jobs with no schema run immediately as today.

**New endpoint:** `GET /admin/jobs/{name}/schema` (or include schema in `GET /admin/jobs`) so the UI can render the form. `POST /admin/jobs/{name}/run` accepts a JSON `params` body.

### A2. Reimplementation strategy ("logic inside webqueue")

The enrich tools are large (`enrichtv.py` ≈ 1700 lines) and battle-tested. **Do not rewrite from scratch.** Instead:

1. **Vendor** the source modules into `kryten_webqueue/integrations/`:
   ```
   kryten_webqueue/integrations/
     __init__.py
     cmsutils/            # vendored from d:\devel\cmsutils
       enrichtitles.py
       enrichmeta.py
       enrichtv.py
       fetchurls.py
       _common.py         # shared MediaCMS client / scoring helpers if extracted
     ytpipe/              # vendored from d:\devel\yt-pipe
       downloader.py      # from youtube_to_mediacms.py
   ```
2. **Refactor each module's entry point** from `main(argv)` (argparse + console-UTF8 + `print` + interactive prompts) into a **callable** `run(params: dict, *, config, progress) -> dict`:
   - Remove `argparse`, `sys.stdin` prompts, and `-i/--interactive` paths (service is headless — interactive enrich modes are **disabled**).
   - Replace `print(...)` with the `progress()` callback + `logging`.
   - Accept config (MediaCMS URL/token, TMDb/OMDb keys) from webqueue `Config`, not from `config.yaml`/CLI.
   - Return a result dict (counts, errors) for `job_runs.detail`.
3. **Run blocking work off the event loop.** These modules use synchronous `requests`, `openpyxl`, `yt_dlp`, `msal`. Each job function is an `async def` that does `await asyncio.to_thread(module.run, params, config=..., progress=thread_safe_progress)`. The `progress` callback must hop back to the loop (`asyncio.run_coroutine_threadsafe` or a queue) to write `job_runs`.
4. **Config reuse.** MediaCMS token and TMDb/OMDb keys already exist in webqueue `Config` (`mediacms_token`, `tmdb_api_key`, `omdb_api_key`). The enrich tools accept exactly these. fetchurls needs **new** SharePoint config (§A4).
5. **System dependencies.** `fetch` needs `yt-dlp` + `ffmpeg` on the host; document in deploy notes. `fetchurls` needs `openpyxl` + `msal`. Add Python deps to `pyproject.toml` as an **optional extra** (`jobs`) so a minimal deployment without these tools still installs:
   ```toml
   [project.optional-dependencies]
   jobs = ["yt-dlp>=2024.1", "openpyxl>=3.1", "msal>=1.28", "requests>=2.31"]
   ```
   Jobs whose deps are missing register but fail fast with a clear "dependency not installed" message (don't crash startup).

### A3. Job: `fetch` (yt-pipe downloader)

Reimplements `youtube_to_mediacms.py` (today wrapped by `fetch.ps1`). Downloads a yt-dlp-supported URL and uploads it to MediaCMS.

**Parameters (schema):**

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `url` | string (required) | — | Source URL (YouTube/Tubi/etc.); apply the existing Mix/Radio playlist cleanup. |
| `quality` | enum | `medium` | `best` \| `good` \| `medium` (same mapping as `fetch.ps1`). |
| `max_videos` | int | `50` | For playlist URLs. |
| `add_to_playlist` | playlist picker | none | **New.** If set, append the resulting MediaCMS item(s) to this saved playlist after upload. |

**Config:** `mediacms_url`, `mediacms_token` (already present). Cookies: optional `fetch_cookies_path` config for age/region-gated sources (mirrors `cookies.txt`).

**Add-to-playlist behavior (new):**
- After a successful upload, the downloader returns the new `friendly_token`(s).
- For each, append `{media_type: 'cm', media_id: <manifest_url or token>, title, duration_sec}` to the chosen `saved_playlist` via the existing `replace_playlist_items` (read → append → write) or a new `append_playlist_item` helper.
- If `add_to_playlist` is set but upload yields no token, record a non-fatal warning in the run detail.

**Result detail:** `{"downloaded": N, "uploaded": N, "tokens": [...], "added_to_playlist": <id|null>, "errors": [...]}`.

### A4. Job: `fetchurls`

Reimplements `fetchurls.py`: read the Channel Z Excel workbook from SharePoint, resolve each source URL (validate dropsugar.co with HEAD; download YouTube/Tubi via the `fetch` downloader), and produce per-section playlists.

**Always-upcoming-weekend rule (new, overrides tool):**
- Compute the **next Friday** from today: `friday = today + ((4 - today.weekday()) % 7)`. When run **on a Friday this yields today** (the imminent weekend, per OQ-3); Sat/Sun roll forward to the next Friday. Saturday = friday + 1.
- Sheet name = `f"{friday.month}.{friday.day}-{saturday.month}.{saturday.day}"` (e.g. `3.6-3.7`), matching `_SHEET_DATE_RE`. Pass this explicitly as the target sheet rather than calling `_auto_select_sheet` (which selects current/just-past).
- If the computed sheet is absent from the workbook, fail the run with a clear message listing available sheet names.

**Parameters (schema):**

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `section` | enum | `all` | `all` \| `friday` \| `saturday-night` \| `saturday-morning`. |
| `dry_run` | bool | `false` | Resolve/preview only; no downloads, no writeback, no import. |
| `writeback` | bool | `true` | Write resolved URLs back to column F. |
| `validate` | bool | `true` | HEAD-check existing dropsugar.co URLs. |

**Import resulting playlists (new):**
- The tool produces `playlists/{sheet}-friday.txt`, `{sheet}-saturday-night.txt`, `{sheet}-saturday-morning.txt`, and `{sheet}-failures.txt`.
- After a successful (non-dry-run) run, **import each non-failures file as a saved playlist** named exactly like the file stem: `{sheet}-friday`, `{sheet}-saturday-night`, `{sheet}-saturday-morning`. Reuse `import_playlist_text()` to resolve lines, then `create_saved_playlist(name=stem, created_by=triggered_by)` + `replace_playlist_items()`.
- If a playlist of that name already exists, **replace its items** (idempotent re-runs) rather than creating a duplicate. (Match by exact name + `created_by`.)
- The `{sheet}-failures` file is **not** imported; surface its count in the run detail.

**Workbook source (OQ-1 resolved — local file only in v1):** `fetchurls` reads the Channel Z workbook from a **configured/uploaded `.xlsx`**, reusing the tool's existing `--file` path. **No SharePoint/Graph/MSAL in v1.** New config:
  ```jsonc
  "fetchurls": {
    "workbook_path": ""   // absolute path to a synced/exported Channel Z Playlist .xlsx
  }
  ```
  Optionally the admin Run modal accepts a one-off file upload that overrides `workbook_path` for that run. Column-F writeback is **disabled** in file-only mode unless the configured file is writable in place (toggle `writeback`). A future phase may add Graph auth (device code surfaced in the admin UI) — out of scope here.

**Result detail:** `{"sheet": "3.6-3.7", "resolved": N, "downloaded": N, "failures": N, "imported_playlists": ["3.6-3.7-friday", ...]}`.

### A5. Jobs: `enrichtitles`, `enrichmeta`, `enrichtv`

Reimplement the three enrich tools. All three already accept the same core config webqueue has (`--token`, `--tmdb-key`, `--omdb-key`, `--api-url`) and default to **dry-run** unless `--commit`. For one-click admin jobs we default to **commit on** (the point is to apply enrichment) with a `dry_run` toggle for safety. **Interactive mode is disabled.**

Common parameters:

| Field | Type | Default | Applies to |
|-------|------|---------|-----------|
| `dry_run` | bool | `false` | all (true = report/scan only, no writes) |
| `limit` | int | none | all |
| `days` | int | none | all (only items uploaded in last N days) |

Tool-specific defaults (mirror the CLIs):

- **enrichtitles** — params: `dry_run`, `limit`, `days`. Cleans/normalizes titles. Default commit.
- **enrichmeta** — params: `dry_run`, `limit`, `days`, `tubi_upgrade` (bool, default false), `min_score` (default = tool's `MIN_SCORE_THRESHOLD`), `min_duration` (default `MIN_DURATION`), `delay` (default `REQUEST_DELAY` 0.25s). Uses TMDb/OMDb keys from config.
- **enrichtv** — params: `dry_run`, `limit`, `days`, `min_score` (default 50), `min_duration` (default 600), `max_duration` (default 3599), `delay` (default 0.25s). Uses TMDb/OMDb keys from config.

**MediaCMS writes:** these tools push enriched metadata to MediaCMS via the API token — this is the existing, working write path we also rely on for the Hide tag (§B6). When vendoring, **extract the MediaCMS edit call into `integrations/cmsutils/_common.py`** so Hide and enrich share one client.

**Result detail:** per tool, `{"scanned": N, "matched": N, "committed": N, "skipped": N, "errors": [...]}`.

**Phasing note:** enrich jobs are the lowest-risk reimplementations (pure HTTP, no auth dance, no large downloads). Do these **first** to prove the vendor+thread pattern, then `fetch`, then `fetchurls` last.

---

## B. Browse / Results

Applies to both `catalog/browse.html` (browse + search results share this template) and the JSON `/catalog/browse` + `/catalog/search` routes.

### B1. Sort control
- Add a **Sort by** `<select>` beside the existing Category/Tag facets: `Default` (current quality-weighted order), `Title A–Z`, `Title Z–A`, `Newest first`, `Oldest first`. All options available to everyone (per decision #5).
- **DB:** add a `sort` parameter to `db.browse(...)` and `db.search(...)` mapping to an `ORDER BY`:
  - `default` → existing quality-weighted clause.
  - `title_asc` / `title_desc` → `c.title ASC|DESC`.
  - `newest` / `oldest` → `c.added_at DESC|ASC` (with `synced_at` tiebreaker).
- **Data gap (must fix):** `catalog.added_at` is currently **not populated** on insert (it has no default and `insert_catalog` omits it), so `Newest first` would be unreliable. During sync, **populate `added_at` from MediaCMS `add_date`** (the media list includes `add_date`). Backfill existing rows in a migration: `UPDATE catalog SET added_at = synced_at WHERE added_at IS NULL` as a stopgap, then let sync overwrite with the true `add_date`.
- **Routes/UI:** carry `sort` as a query param through pagination and the facet form (extend `applyFacets()`); persist the user's choice in `localStorage` for convenience.

### B2. Random branded art with hover-to-thumbnail
Goal: stop showing "shitty" MediaCMS thumbnails as the primary tile art; show a random branded placeholder instead, but reveal the real thumbnail on hover (sometimes useful).

- **When it applies:** a tile whose best art is *not* a real poster — i.e. `cover_art_source` is `null`/`thumbnail` (no TMDB/OMDB match). Tiles with a real `cover_art_path` from `tmdb`/`omdb` are unchanged.
- **Front art:** a **random** image from the branded placeholder folder (`config.placeholder_dir`, served under `/images/placeholders/`), chosen **per server-render** (OQ-4): the browse route picks `random.choice(...)` per affected tile when building the page, so each tile has a stable src for that response (no client reshuffle, no layout thrash).
- **Hover:** on pointer-over, swap/overlay the real `thumbnail_url` (the "shitty" art) so it can still be inspected; restore the placeholder on pointer-out. Implement as two stacked `<img>`s with a CSS hover crossfade (no JS needed), guarded so it only renders when a `thumbnail_url` exists.
- **Expose the placeholder list:** add a tiny endpoint or template-context helper that lists files in `placeholder_dir` (filename only). The browse route picks `random.choice(...)` per affected tile. Cache the directory listing in memory (refresh on an interval) to avoid disk scans per request.
- **Fallback:** if `placeholder_dir` is empty, keep the current letter placeholder.

### B3. Vertical tile button stack
- The tile `.card-actions` (currently a horizontal row of `Queue` / `Queue as Admin`) should **stack vertically**, full-width buttons. CSS only: `.card-actions { flex-direction: column; align-items: stretch; gap: 0.4rem; }` plus `.card-actions .btn { width: 100%; }`. Verify it reads well at the grid's min tile width (180px).

### B4. Admin "Add to playlist" (per tile)
- Add an admin-only button to each tile: **Add to playlist** (visible when `user.rank >= 3`).
- Opens the shared admin modal with a playlist `<select>` (populated from `GET /admin/playlists/`) and an "Add" action.
- **Endpoint:** `POST /admin/playlists/{id}/append` `{friendly_token}` — resolves the catalog item (`get_item_admin`) and appends `{media_type:'cm', media_id: manifest_url, title, duration_sec}` to the playlist (read → append → `replace_playlist_items`, or a dedicated `append_playlist_item`). Returns the new item count.
- Toast on success/failure (existing pattern).

### B5. Admin "Add to most-recent playlist" (no modal)
- Add a second admin-only tile button: **+ Recent** (or "Add to <name>"), which appends to the admin's most-recently-**created** playlist **without a modal**.
- **Endpoint:** `POST /admin/playlists/recent/append` `{friendly_token}`:
  - Resolve most-recent: `SELECT * FROM saved_playlists WHERE created_by=? ORDER BY created_at DESC LIMIT 1`.
  - If none exists, return a 409 with a message the UI shows as a toast ("Create a playlist first").
  - Otherwise append as in B4 and return `{playlist_id, name, count}` so the toast can say `Added to "<name>"`.
- Optional polish: label the button with the resolved playlist name if cheaply available (e.g. fetched once on page load and cached client-side).

### B6. Admin "Hide Item" → MediaCMS `kryten-hidden` tag
- Add an admin-only tile button **Hide** with a confirm step ("Hide this item from the catalog? It will be tagged `kryten-hidden` in MediaCMS.").
- **Source of truth = MediaCMS.** On confirm:
  1. **Write** the `kryten-hidden` tag to the item in MediaCMS via the API token, using the same edit mechanism the enrich tools use (`integrations/cmsutils/_common.py` MediaCMS client). Verify the exact endpoint/payload for tag editing against MediaCMS (the enrich tools already PATCH/POST media edits — reuse that). 
  2. **Immediately hide locally** so the admin sees it disappear without waiting for sync: either (a) add `kryten-hidden` to the local `catalog_tags` join for that token, or (b) maintain a local `hidden` flag column. Prefer (a) so the existing hidden-tag filter (v0.7.5) applies uniformly. Ensure `kryten-hidden` is in the hidden-tags exclusion set.
  3. The next catalog sync re-reads tags from MediaCMS and the hide persists (and propagates to any other consumer).
- **Endpoint:** `POST /admin/catalog/{friendly_token}/hide` (admin). Returns success; the tile is removed from the grid client-side on success.
- **Unhide (recommended, low cost):** since admins can already reveal hidden items (`?show_hidden=1` from v0.7.5), add the inverse `POST /admin/catalog/{friendly_token}/unhide` that removes the `kryten-hidden` tag in MediaCMS + locally, so a mis-hide is reversible from the admin "show hidden" view. (See OQ-5.)
- **Config:** ensure `kryten-hidden` is registered in `HIDDEN_TAG_NAMES` (or a dedicated constant) so browse/search/facets exclude it for non-admins.

---

## C. Queue page (`queue/index.html`)

### C1. Hide the order number
- Remove the `qi-pos` index number from each queue item in `renderQueue()` (and its CSS), or hide via CSS (`.qi-pos { display: none; }`). Prefer removing the element from the template render to keep the DOM clean.

### C2. Remove the drag handle / no drag-drop
- Remove the `qi-drag` (☰) handle element from each queue item render. There is **no** drag-reorder implemented on this page and none should be added — drop the handle, its title tooltip, and any related CSS. (Admin reordering lives in the playlist editor, not the live queue.)

---

## D. Data model & migrations

New migration(s):

1. **Job-run reconciliation** is runtime, not schema; but add `params` column to `job_runs` (nullable TEXT/JSON) to record submitted parameters:
   ```sql
   ALTER TABLE job_runs ADD COLUMN params TEXT;
   ```
2. **catalog.added_at backfill** (stopgap before sync repopulates from `add_date`):
   ```sql
   UPDATE catalog SET added_at = synced_at WHERE added_at IS NULL;
   ```
3. No new tables required for B4/B5 (reuse `saved_playlists`/`saved_playlist_items`). B6 reuses `catalog_tags`.

Sync change (not a migration): set `added_at` from MediaCMS `add_date` in `_process_item`/`insert_catalog`/`update_catalog`.

---

## E. Config additions

```jsonc
{
  // existing: mediacms_url, mediacms_token, tmdb_api_key, omdb_api_key, image_dir, placeholder_dir ...

  "fetch_cookies_path": "",                 // optional yt-dlp cookies for gated sources
  "fetchurls": {                             // fetchurls (v1 = local file only, OQ-1)
    "workbook_path": ""                      // absolute path to a synced Channel Z Playlist .xlsx
  }
}
```

All new config is optional; jobs whose config/deps are absent register but fail fast with a clear message. (SharePoint/Graph config is intentionally omitted in v1.)

---

## F. API surface additions

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/admin/jobs` | (extend) include each job's `schema` + last-run summary |
| `POST` | `/admin/jobs/{name}/run` | (extend) accept `{params}` body |
| `POST` | `/admin/playlists/{id}/append` | Append one catalog item to a playlist (B4) |
| `POST` | `/admin/playlists/recent/append` | Append to the admin's most-recent playlist (B5) |
| `POST` | `/admin/catalog/{friendly_token}/hide` | Tag `kryten-hidden` in MediaCMS + hide locally (B6) |
| `POST` | `/admin/catalog/{friendly_token}/unhide` | Remove `kryten-hidden` (B6, recommended) |
| `GET` | `/catalog/browse`, `/catalog/search` | (extend) accept `sort` param (B1) |

All `/admin/*` routes use the existing `require_admin` dependency.

---

## G. Dependencies

- New optional extra `jobs`: `yt-dlp`, `openpyxl`, `requests`. (`msal` is **not** needed in v1 — SharePoint/Graph is deferred per OQ-1.)
- System: `ffmpeg` on the host for `fetch`. Document in `deploy/` notes.
- No new deps for Browse/Queue work.

---

## H. Phasing / sequencing

1. **Phase 1 — Quick wins (no external tools):**
   - A1.2 job-history reconciliation (`interrupted` status).
   - C1/C2 queue page cleanup.
   - B3 vertical buttons, B1 sort (incl. `added_at` backfill + sync populate), B2 random art + hover.
   - B4/B5/B6 admin tile actions (B6 needs the MediaCMS write client — see Phase 3 note).
2. **Phase 2 — Job framework:** A1.3 parameterized jobs + schema-driven Run modal.
3. **Phase 3 — Reimplemented jobs (in dependency order of risk):**
   - `enrichtitles`, `enrichmeta`, `enrichtv` (proves vendor+thread pattern; yields the shared MediaCMS write client used by B6).
   - `fetch` (+ add-to-playlist); needs yt-dlp/ffmpeg.
   - `fetchurls` (+ playlist import, upcoming-weekend, **local-file workbook per OQ-1**) — **last**, highest risk.

> If B6 must ship in Phase 1 before the enrich vendor work, write a minimal standalone MediaCMS tag-write helper now and fold it into `_common.py` later.

---

## I. Residual risks

All open questions are resolved in §0.1. Remaining implementation risks:

- **MediaCMS tag write (B6 / OQ-6):** the exact edit verb/field must be confirmed against the live instance. Mitigation: a round-trip integration test on a disposable item before wiring the Hide UI; reuse the enrich tools' proven edit path.
- **Vendoring drift (OQ-2):** vendored enrich/fetch logic (~3k lines) will diverge from `d:\devel\cmsutils` over time. Mitigation: header-stamp the upstream commit/date; keep adapters thin so re-vendoring is mechanical.
- **Long-job UX:** fetch/fetchurls can run for many minutes. The in-memory per-job lock + history `interrupted` reconciliation cover correctness; the optional heartbeat (A1.2.4) gives admins live progress.
- **`fetch`/`fetchurls` host deps:** require `yt-dlp` + `ffmpeg` (and a readable workbook for fetchurls). Mitigation: jobs register but fail fast with a clear "dependency/config missing" message instead of crashing startup.
- **`added_at` accuracy (B1):** the stopgap backfill sets `added_at = synced_at`; true `add_date` only becomes correct after the next full sync. Mitigation: trigger a sync after deploy, or document that "Newest first" sharpens once sync runs.

---

## J. Validation plan

- **Jobs framework:** unit-test reconciliation (insert a `running` row → startup → becomes `interrupted`); schema validation rejects bad params; a parameterized job receives params and records them.
- **enrich jobs:** run against a disposable MediaCMS item in `dry_run` and assert "scanned/matched" counts without writes; one `commit` run asserts the metadata changed and `job_runs.detail` is populated.
- **fetch:** dry-ish test with a short known clip; assert a `friendly_token` returns and (with `add_to_playlist`) the item appears in the playlist.
- **fetchurls:** unit-test the upcoming-weekend sheet-name computation across weekdays (incl. Friday → today per OQ-3); integration test against a fixture `.xlsx` via the configured file path; assert imported saved playlists are named `{sheet}-{section}` and re-runs replace rather than duplicate.
- **Browse:** SQLite fixture tests for each `sort` ordering incl. `added_at` newest/oldest; verify hidden-tag exclusion still applies; render checks for vertical buttons and the hover art markup.
- **Hide:** assert the MediaCMS write is attempted, the local `catalog_tags` gains `kryten-hidden`, and the item drops from a non-admin browse query.
- **Queue:** assert rendered items contain neither `qi-pos` nor `qi-drag`.
