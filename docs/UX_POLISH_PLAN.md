# kryten-webqueue — UX Polish & Convenience Plan

Status: **proposed (awaiting approval)**. No application code changed yet.

This plan covers seven UX/convenience items. Each can ship independently; grouped
into phases so each phase is independently verifiable and releasable.

Scope note: display-only relabeling and front-end live-refresh are deliberately
kept separate from data-model/API changes so nothing downstream (api-gate,
economy, DB schema) needs to move in lock-step.

---

## Item 1 — Active event shows as "active" long after it ended

**Root cause.** The `active_schedule` row (single row, `id=1`) is only removed by
the admin "Clear Active Schedule" button (`clear_active_schedule()` →
`DELETE FROM active_schedule`). The in-progress *lock* auto-lifts
(`disable_active_lock()` sets `lock_disabled=1` when the last scheduled item
begins), but the **row itself persists**, so the admin banner keeps showing the
event. `estimated_end_at` is computed once at fire time and never enforced.

Relevant code:
- `catalog/db.py` — `set_active_schedule()`, `get_active_schedule()`,
  `clear_active_schedule()`, `disable_active_lock()`, `is_event_lock_active()`,
  and `active_schedule` schema (`last_item_uid`, `lock_disabled`, `estimated_end_at`).
- `queue/shadow.py` — `_maybe_lift_event_lock()` already detects when the last
  scheduled item is playing (event effectively over).
- `templates/admin/schedules.html` — `loadActive()` renders the banner once.

**Fix (event-driven primary + safety net).**
1. Backend: when the scheduled event is truly over, clear the active row, don't
   just lift the lock. Extend the existing `_maybe_lift_event_lock()` logic in
   `shadow.apply_poll_result()`: once the last scheduled item (`last_item_uid`)
   has played out (i.e. now-playing has advanced *past* it, or the row is gone
   from the queue), call a new `db.expire_active_schedule_if_done()` that clears
   the row. Keeps the existing "lift lock when last item *starts*" behavior, and
   adds "clear active when last item *ends*".
2. Backend safety net: in the same path, if `estimated_end_at` is more than a
   small grace (e.g. 5 min) in the past, clear the active row. Guards against a
   missed boundary (manual queue edits, restart during an event).
3. Frontend: `loadActive()` treats a past `estimated_end_at` as "ended" (hide
   banner) even before the backend clears it, and refreshes (see Item 2).

**Verification.** Fire a short immutable schedule; confirm the banner clears
shortly after the last item finishes (not on reload). Unit test the new db
helper: row present → played past last item → row cleared. Test the
`estimated_end_at` grace path.

---

## Item 2 — Admin page doesn't live-update (queue, now-playing, jobs)

**Root cause.** `templates/admin/index.html` calls `loadAdminData()` /
`loadJobs()` **once** on load. Unlike `templates/queue/index.html`, it never
opens the `/ws` WebSocket, so queue size / now-playing / job status only refresh
on a manual reload. The poller already broadcasts `{"type":"queue_state"}` every
~3s (`queue/poller.py`).

**Fix.**
1. Subscribe the admin page to `/ws` (reuse the queue page's connect pattern).
   On `queue_state`, update the "Queue Status" block (items count + now-playing).
   On `schedule_fired`, refresh the active-schedule banner + queue status.
2. Jobs: lightweight `setInterval` (e.g. every 5s) re-fetch of `/admin/jobs` and
   `/admin/jobs/runs?limit=10` while the tab is visible (pause via
   `document.visibilityState` to avoid background churn). Jobs are DB-polled, not
   broadcast, so polling is the pragmatic choice; interval is cheap.
3. Active schedule banner (shared with Item 1): re-render on the same interval
   and on `schedule_fired`.

Relevant code:
- `templates/admin/index.html` — `loadAdminData()`, `loadJobs()`, init at bottom.
- `templates/queue/index.html` — `connectWebSocket()` reference implementation.
- `templates/admin/schedules.html` — `loadActive()` (extract/reuse).

**Verification.** Open admin page; queue another item from CyTube → count and
now-playing update within a few seconds without reload. Run a job → its status
flips to running then completed live.

---

## Item 3 — Richer logging, especially fetchurls failures

**Current state.** `jobs/manager.py::_execute()` already logs unexpected failures
with a full traceback (`logger.exception`) and records `{type}: {msg}` to the
`job_runs.detail`; `JobError` logs a clean WARNING. The integration
(`integrations/cmsutils/fetchurls.py`) emits progress phases but uses `print()`
for per-URL/per-section detail, so that detail never reaches the app logger or
the job record.

Gaps to close:
- fetchurls per-section results (resolved/failed counts + the failing URLs &
  Excel row numbers) are not summarized into the logger or `job_runs.detail`.
- SharePoint download failures wrap into a `RuntimeError` that loses the HTTP
  status / response snippet.
- `JobContext.progress()` swallows DB errors at debug only (acceptable, but note).
- General: the global log format (added in `logging_config.py`) can include
  `filename:lineno` to make every line more actionable.

**Fix.**
1. `jobs/tasks.py::fetchurls_job` (and `_import_section_as_playlist`): after the
   run, log an INFO summary per section (`name: resolved X / failed Y`) and a
   WARNING listing each failed URL with its row number; fold a compact
   `failures` array into the returned `result` so it lands in `job_runs.detail`
   and shows in the admin "Detail" column.
2. Enrich the SharePoint download error to include HTTP status + a short response
   excerpt before raising.
3. `logging_config.py`: extend the default formatter to
   `%(asctime)s %(levelname)-8s %(name)s %(filename)s:%(lineno)d: %(message)s`
   (app loggers only; keep uvicorn/access formats lean).
4. Convert the most useful `print()` lines in the fetchurls integration to
   `logger.info/warning` (guarded so standalone CLI use still prints).

**Verification.** Run fetchurls with a deliberately bad URL; confirm the admin
job "Detail" shows the failing URL + row, and the process log has an INFO
section summary + WARNING per failure with `file:line`.

---

## Item 4 — Search string and category/tag filters don't combine

**Root cause.** Two separate code paths. `db.browse()` ANDs category+tag via
subqueries; `db.search()` (FTS5 `MATCH`) accepts **no** category/tag. The
frontend `applyFacets()` drops the facet dropdowns entirely when a query is
active (`if (CURRENT_QUERY) {...} else { set category/tag }`), and
`/catalog/search` neither accepts facets nor returns facet lists.

**Fix (recommended: make them AND together).**
1. `db.search()`: add optional `category` / `tag` params and append the same
   `AND friendly_token IN (… categories …)` / `AND … IN (… tags …)` subqueries
   `browse()` already uses (intersection with the FTS match).
2. `routes/catalog.py::search`: accept `category` & `tag`, pass through, and also
   return `categories`/`tags` facet lists (like `/browse`) plus `active_category`
   / `active_tag` so the template can keep selections.
3. `templates/catalog/browse.html::applyFacets()`: when a query is present,
   include `category`/`tag` in the search URL instead of discarding them; keep
   the dropdowns populated and selected on the results page.

Fallback option (if you'd rather not expand search): visually disable the facet
dropdowns on a search results page with a tooltip ("Clear search to filter by
category/tag"). Cheaper, but less capable. **Recommendation: do the real fix.**

**Verification.** Search "matrix" + pick a category → results are the
intersection. Remove the query → browse facets still AND as before. Add a small
test for `db.search(category=…, tag=…)`.

---

## Item 5 — Rename Mutable/Immutable → Preemptable/Non-preemptable (display only)

**Root cause.** Pure UX wording. The data field `is_immutable` (DB column, API
body, JS variable) must stay; only visible labels change.

**Fix (display-only).** In `templates/admin/playlists.html`:
- Badges: "Immutable" → "Non-preemptable"; "Mutable" → "Preemptable" (L~96).
- Create-modal checkbox label (L~128) and editor metadata text (L~164/170).
- Confirm dialog copy in `toggleImmutable()` if it references the words.
- Keep the button verbs ("Reserve"/"Release") as-is (decision 2 default).
In `templates/admin/schedules.html`: the active-banner "Immutable" badge (L~40)
→ "Non-preemptable".

Do **not** change: `is_immutable` column, `set_active_schedule(is_immutable=…)`,
API request/response keys, JS variable names, or config keys.

**Verification.** Grep templates for user-visible "mutable"/"immutable" → none
remain (data attributes/keys excluded). Page renders new labels; toggle still
posts `is_immutable`.

---

## Item 6 — Zcoin dashboard: tabbed container + wider account column

**Current.** `templates/user/dashboard.html` is a 3-column grid
(`balance-card | history-card | transactions-card`), with vanity controls
crammed into the left balance card.

**Fix.** Two-region layout:
- **Left (widened) account card:** balance, rank/level, progress, perks. Remove
  the vanity block from here.
- **Right (wide) tabbed container** with three tabs, lazy-loaded on first show:
  1. **Queue History** (existing `loadQueue()` + pager)
  2. **Recent Transactions** (existing `loadTransactions()` + credit/debit toggle)
  3. **Vanity Items** (moved here: greeting + chat-color editors, with room to
     grow to other econ-surfaced properties later)

Implementation:
- Restructure `dashboard.html`: account card + `.tabs` (buttons) + `.tab-panel`s.
- Reuse existing JS (`loadAccount`, `loadQueue`, `loadTransactions`, vanity
  dialogs); add a tiny tab controller that lazy-loads each panel once.
- `static/css/main.css`: change `.dashboard-grid` to a 2-column layout
  (e.g. `minmax(280px, 360px) 1fr`), collapse to 1 column under ~900px; add
  `.tabs`, `.tab-btn.active`, `.tab-panel[hidden]` styles (reuse `--accent`,
  `--border`, etc.). Mirror the existing `.tx-toggle` styling for consistency.

**Verification.** Dashboard shows account card + tabs; switching tabs loads each
once; vanity edit/purchase still works from its tab; responsive collapse at
narrow widths. Existing economy endpoints unchanged.

---

## Item 7 — Light / dark mode

**Foundation.** `static/css/main.css` already drives the entire UI from CSS
variables on `:root` (`--bg-*`, `--text-*`, `--accent`, `--border`, …). Adding a
theme is mainly a palette swap + a toggle; no per-component CSS rewrite needed.

**Fix.**
1. Define a light palette under `:root[data-theme="light"]` (and keep the current
   dark values as the default `:root`). Tune `--bg-*`, `--text-*`, `--border`,
   `--shadow`; keep `--accent` family. Add an explicit
   `:root[data-theme="dark"]` block equal to the defaults so the toggle is
   symmetric.
2. Default behavior: respect `prefers-color-scheme` when the user hasn't chosen;
   persist an explicit choice in `localStorage` (`wq_theme`).
3. No-FOUC: a tiny inline script in `base.html <head>` sets
   `document.documentElement.dataset.theme` from `localStorage`/media query
   **before** CSS paints.
4. Toggle control in the navbar (`base.html`), wired in `static/js/main.js`:
   flips `data-theme`, saves to `localStorage`, updates the icon/label.
   Default: icon-only (🌙/☀️) with `aria-label` (decision 3 default).
5. Audit a few hard-coded colors (e.g. badge `rgba(...)` backgrounds, toast,
   modal overlay) for acceptable contrast in light mode; promote any offenders
   to variables.

**Verification.** Toggle flips instantly with no flash on reload; choice
persists; fresh visitor matches OS preference; spot-check catalog, queue, admin,
dashboard (incl. new tabs) and modals/toasts in both themes for contrast.

---

## Suggested phasing (each independently releasable)

- **Phase A (quick wins / low risk):** Item 5 (relabel), Item 7 (theme).
- **Phase B (admin live + lifecycle):** Item 1 (active-event expiry) + Item 2
  (admin live-update) — they share the active-schedule banner refresh.
- **Phase C (catalog):** Item 4 (search × facets).
- **Phase D (dashboard):** Item 6 (tabs) — self-contained.
- **Phase E (observability):** Item 3 (logging) — can land anytime; complements
  the earlier promo observability work.

## Open questions / decisions (defaults chosen)
1. Item 4: real combine **(recommended, default)** vs. disable-with-tooltip.
2. Item 5: relabel **nouns only** (default), leave the "Reserve/Release" verbs.
3. Item 7: navbar toggle **icon-only** (🌙/☀️) with `aria-label` (default).
4. Versioning: **one minor (e.g. 0.16.0) per phase** as completed (default),
   vs. batch all into one.
