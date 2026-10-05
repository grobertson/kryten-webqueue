# MOTD Templates, Scheduling, and Media Library: Spec

Status: **Implemented in 0.51.0** (2026-10-05). Section 14 records where the
implementation deliberately differs from the first draft.
Target service: `kryten-webqueue` (baseline 0.50.6).
Background: [CYTUBE_MOTD_AND_ROBOT_CALL_STATE.md](CYTUBE_MOTD_AND_ROBOT_CALL_STATE.md).

## 0. Goals and Non-Goals

Goals:

1. Admins can maintain any number of named **MOTD templates** with full
   revision history, and edit, preview, and save them (to the same or a new name).
2. Any **master** template can be scheduled for an arbitrary window, using
   one-off windows or recurring windows.
3. Masters expose named **zones**. Each zone is filled by a scheduled
   **fragment** template whose schedule is independent of the master's.
4. A shared **media library** lets admins upload images (static or animated) and get
   back a stable public `https://queue.dropsugar.co/...` URL. It serves both the
   MOTD editor and general one-off use.
5. Every mutation is recorded in an audit trail.

Non-goals for v1 (explicitly deferred):

- Per-template CSS and channel CSS pushes (stage 2, see §11).
- Video of any kind (uploads, `<video>` in templates). Removed from scope 2026-10-03.
- Non-admin access. Everything stays behind the existing `rank >= 3` gate.
- Per-user ownership of templates. Any admin can edit anything, and the audit
  trail provides accountability.
- Exposing uploads through api-gate/NATS to other services.
- Any in-chat surfacing (e.g. `!motd`, kryten-llm awareness). No NATS contract is added.
- Migrating `/motd/boxes` slot-override art into the media library. The two stay separate permanently.

## 1. Decisions Recorded (from review Q&A)

| Topic | Decision |
| --- | --- |
| Live hand edits | **Templates are the source of truth.** Every publish replaces the whole MOTD, after backing up the live document. The CyTube MOTD editor is no longer used. This supersedes the 0.50.4 selective-merge policy. |
| Access | CyTube rank >= 3 (`require_admin`) for everything. |
| Template language | Sandboxed Jinja (`ImmutableSandboxedEnvironment`) with a fixed, documented context. |
| Movie grid | **Required** in every master, with exactly the configured slot set. |
| Schedule shapes | One-off window and recurring window (RRULE within a date range). |
| Timezone | Admin input and display in `America/New_York`. Storage in UTC. |
| Overlaps | Highest `priority` wins. Ties go to the narrower occurrence window, then the most recently updated schedule. |
| Zones | Multiple named zones from day one. |
| Transitions | Automatic within about 60 s of a boundary, plus reconcile on startup. |
| Upload types | GIF / PNG / JPEG / WebP (animated included). No video, SVG, or APNG. |
| General uploader | Admin web page in webqueue only. |
| Media library | Human slug with collision suffixing, browsable and searchable, delete blocked while referenced, metadata stripped. |
| CSS column | Not reserved. Stage 2 designs CSS from scratch. |

## 2. Phase 0: Prerequisites (resolved 2026-10-03)

| # | Question | Result |
| --- | --- | --- |
| P1 | `<video>` support | **Dropped.** Video is out of scope. |
| P2 | `<style>` blocks and `style=""` | **Verified working** by the channel admin. |
| P3 | MOTD length limit | CyTube `src/channel/customization.js` `handleSetMotd` runs `data.motd.substring(0, 20000)` **before** sanitizing and **silently truncates**. The unit is JavaScript string length, which is **UTF-16 code units**, so an emoji such as 🟥 counts as 2. Publish therefore measures `len(html.encode("utf-16-le")) // 2` and fails closed above `motd.max_html_chars` (20000), instead of letting CyTube cut the document mid-tag. |
| P4 | Video range requests | **Dropped** with video. |

## 3. Domain Model

All new tables live in the **`catalog`** schema, next to `motd_overrides`. That
avoids cross-domain joins under the partitioned layout. Every new DB method
must be registered in the domain routing map in
[../kryten_webqueue/catalog/db/__init__.py](../kryten_webqueue/catalog/db/__init__.py).
Do not rely on the generic attribute fallback.

### 3.1 Templates and revisions

```sql
CREATE TABLE catalog.motd_templates (
    id                  BIGSERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,       -- slug: ^[a-z0-9][a-z0-9_-]{1,63}$
    kind                TEXT NOT NULL CHECK (kind IN ('master','fragment')),
    display_name        TEXT NOT NULL,
    description         TEXT,
    current_revision_id BIGINT,                     -- FK set after first revision
    is_default          BOOLEAN NOT NULL DEFAULT FALSE,  -- masters only
    archived_at         TIMESTAMPTZ,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX motd_templates_one_default
    ON catalog.motd_templates ((TRUE)) WHERE is_default AND kind = 'master';

CREATE TABLE catalog.motd_template_revisions (
    id           BIGSERIAL PRIMARY KEY,
    template_id  BIGINT NOT NULL REFERENCES catalog.motd_templates(id),
    revision_no  INTEGER NOT NULL,
    body         TEXT NOT NULL,                     -- Jinja source, <= 64 KiB
    body_sha256  TEXT NOT NULL,
    zones        TEXT[] NOT NULL DEFAULT '{}',      -- masters: zone names found at save
    note         TEXT,
    created_by   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (template_id, revision_no)
);
```

Rules:

- Revisions are immutable. "Save" always appends a revision and moves
  `current_revision_id`. "Restore revision N" appends a copy of N, so history
  stays linear.
- "Save as" creates a new template whose first revision is the edited body.
  It can be saved as master or fragment.
- `kind` is fixed after creation.
- Archiving is a soft delete. It is refused while an active, non-expired
  schedule references the template, or while the template is the default
  master.
- There must always be exactly one default master. This is the fallback when no
  master schedule is active.
- `zones` is extracted at save time from static `zone("…")` calls (see §4.3).
  It is used for timeline warnings. It is not used at render time.

### 3.2 Schedules

```sql
CREATE TABLE catalog.motd_schedules (
    id                BIGSERIAL PRIMARY KEY,
    template_id       BIGINT NOT NULL REFERENCES catalog.motd_templates(id),
    zone              TEXT,             -- NULL for masters; required for fragments
    label             TEXT NOT NULL,
    starts_at         TIMESTAMPTZ NOT NULL,   -- one-off: window start; recurring: series start
    ends_at           TIMESTAMPTZ,            -- one-off: window end (required); recurring: series end (NULL = open)
    rrule             TEXT,                   -- NULL = one-off; else RFC 5545 RRULE (no DTSTART)
    duration_minutes  INTEGER,                -- recurring only: occurrence length
    tz                TEXT NOT NULL DEFAULT 'America/New_York',
    priority          INTEGER NOT NULL DEFAULT 0,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_by        TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (rrule IS NULL OR duration_minutes BETWEEN 1 AND 10080),
    CHECK (rrule IS NOT NULL OR ends_at IS NOT NULL)
);
```

- A schedule points at a **template**, not at a revision. Whatever revision is
  current at render time is what displays. Saving a template that is live
  therefore republishes on the next reconcile tick, and the editor must warn
  about this (§6).
- Recurring occurrences are expanded in `tz` local time with `dateutil.rrule`,
  then converted to UTC. This keeps "every Thursday 18:00–23:00" anchored to
  wall-clock time across DST. The DTSTART is the local equivalent of
  `starts_at`, and expansion is bounded by `ends_at`. The same `rrulestr`
  helper style as [../kryten_webqueue/playlists/scheduler.py](../kryten_webqueue/playlists/scheduler.py)
  is used.
- Validation rejects the following: a master schedule with a zone, a fragment
  schedule without a zone, a schedule pointing at an archived template, an
  RRULE containing DTSTART/UNTIL/COUNT conflicting with the series bounds, a
  zero-length window, and an RRULE that yields no occurrence inside the series.

### 3.3 Publications (history, reconcile state, backups)

```sql
CREATE TABLE catalog.motd_publications (
    id                    BIGSERIAL PRIMARY KEY,
    published_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    trigger               TEXT NOT NULL,   -- 'schedule' | 'manual' | 'weekly' | 'startup'
    composition_key       TEXT NOT NULL,   -- see §5.3
    master_revision_id    BIGINT NOT NULL,
    fragment_revisions    JSONB NOT NULL,  -- {"promo": 123, "top-banner": null}
    week_key              TEXT NOT NULL,
    html_sha256           TEXT NOT NULL,
    html_chars            INTEGER NOT NULL,
    backup_path           TEXT,            -- live MOTD captured before replace
    job_run_id            BIGINT,
    published_by          TEXT NOT NULL
);
```

### 3.4 Media library

```sql
CREATE TABLE catalog.media_assets (
    id             BIGSERIAL PRIMARY KEY,
    slug           TEXT NOT NULL UNIQUE,     -- ^[a-z0-9][a-z0-9-]{0,79}$
    filename       TEXT NOT NULL UNIQUE,     -- slug + detected extension
    kind           TEXT NOT NULL CHECK (kind IN ('image')),
    mime           TEXT NOT NULL,
    bytes          BIGINT NOT NULL,
    width          INTEGER,
    height         INTEGER,
    animated       BOOLEAN NOT NULL DEFAULT FALSE,
    frame_count    INTEGER,
    sha256         TEXT NOT NULL,
    original_name  TEXT,
    description    TEXT,
    uploaded_by    TEXT NOT NULL,
    uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    deleted_at     TIMESTAMPTZ,
    deleted_by     TEXT
);
CREATE INDEX media_assets_search ON catalog.media_assets
    USING gin ((slug || ' ' || coalesce(original_name,'') || ' ' || coalesce(description,'')) gin_trgm_ops);
```

### 3.5 Audit trail

This is a generic table that other admin features can reuse later.

```sql
CREATE TABLE catalog.admin_audit_log (
    id           BIGSERIAL PRIMARY KEY,
    at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,   -- e.g. motd.template.save, motd.schedule.delete, media.upload
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    summary      TEXT,
    before       JSONB,
    after        JSONB
);
CREATE INDEX admin_audit_log_entity ON catalog.admin_audit_log (entity_type, entity_id, at DESC);
```

Template bodies are **not** copied into `before` / `after`. Revision IDs are
recorded instead, because revisions already hold the bodies.

### 3.6 Schema delivery

- PostgreSQL: new file `catalog/db/sql/002_motd_templates_media.sql`. It uses
  `IF NOT EXISTS` everywhere and inserts `schema_version (2, …)`. It is applied
  out-of-band, the same way as 001, because
  [../kryten_webqueue/catalog/db/_pg_base_domain.py](../kryten_webqueue/catalog/db/_pg_base_domain.py)
  `migrate()` is a no-op. The deploy runbook must include this step.
- SQLite parity: migration **v31** in
  [../kryten_webqueue/catalog/db/_connection.py](../kryten_webqueue/catalog/db/_connection.py)
  provides the equivalent tables, using JSON as TEXT and arrays as JSON TEXT,
  so the test suite and legacy layout keep working.
- Seeding happens on first startup after migration, when there are no
  templates. Two templates are created:
  - Default master `channel-z-weekend`, converted from the current file
    template ([../kryten_webqueue/templates/motd/channel_z.html](../kryten_webqueue/templates/motd/channel_z.html)),
    with the grid replaced by `{{ movie_grid() }}` and a `{{ zone("promo") }}`
    added after the links.
  - Empty fragment `promo-none` is **not** created. An empty zone is the
    absence of a fragment.
- The DDL shown above is illustrative. The shipped DDL stores JSON (`zones`,
  `fragment_revisions`, audit `before`/`after`) as TEXT on both backends, and
  sets timestamps from the application rather than column defaults, so one code
  path serves SQLite and PostgreSQL. Media search uses `LIKE`, without a trigram
  index.

## 4. Rendering

### 4.1 Environment

- `jinja2.sandbox.ImmutableSandboxedEnvironment` with `autoescape=True`,
  `StrictUndefined`, and **no loader**. `{% include %}`, `{% extends %}`, and
  `{% import %}` all fail, because the only composition mechanism is `zone()`.
- Globals and filters are whitelisted (§4.2). Nothing from `os`, the config
  object, the DB, or the request is reachable.
- Limits:
  - Body: at most 64 KiB.
  - Rendered output: at most `motd.max_html_chars` UTF-16 code units (P3).
  - The sandbox's `MAX_RANGE` stays at the default.
  - Rendering runs in `asyncio.to_thread`. Pathological templates are an
    accepted admin-only risk, because a thread cannot be killed. This is
    documented rather than mitigated.
- The existing trusted environment in
  [../kryten_webqueue/motd/render.py](../kryten_webqueue/motd/render.py)
  remains only for internal snippets. User bodies never render in it.

### 4.2 Template context (documented contract)

| Name | Type | Masters | Fragments |
| --- | --- | --- | --- |
| `week_key`, `headline`, `dates`, `showtime`, `banner_url` | str | yes | yes |
| `nights`, `slots` | existing MOTDSlot views (read-only) | yes | yes |
| `links` | list of `{label,url}` | yes | yes |
| `next_event`, `show_next_event` | existing | yes | yes |
| `now` | aware datetime in `America/New_York` | yes | yes |
| `generated_at` | ISO str | yes | yes |
| `movie_grid()` | Markup: the standard marked grid | yes | **no** |
| `zone(name)` | Markup: the zone wrapper plus the active fragment, or `""` | yes | **no** |
| `zone_active(name)` | bool | yes | **no** |
| `media_url(slug)` | absolute URL for a live media asset. An unknown slug is a render error. | yes | yes |
| filter `safe_url` | existing http(s)-only filter | yes | yes |

Fragments receive the same data. Their output is wrapped in `Markup` when
inserted, so the fragment's own autoescaping is the safety boundary.

### 4.3 Zones

- `zone("promo")` renders
  `<div class="kryten-motd-zone" data-kryten-motd-zone="promo">…</div>` only
  when a fragment is active. Otherwise it renders `""`, so no blank gap is left
  behind.
- The wrapper's default spacing mirrors `.weekend-title`'s vertical rhythm.
  That is the "room to breathe below the last text" requirement. A fragment can
  override it with its own inline `<style>`.
- A zone name may appear at most once per master. A duplicate is a save-time
  error.
- Zone names are parsed statically at save time from `zone("literal")` calls
  with a Jinja AST walk and stored in `revisions.zones`. A non-literal argument
  is a save error, because the timeline cannot reason about it.
- Masters can branch with
  `{% if zone_active("promo") %}…{% endif %}`. This covers the request to
  "enable via schedule and use the schedule in the parent jinja".

### 4.4 Required grid validation

This is applied on every master save, every preview, and every publish.

1. Render the master with the real context for the target week.
2. Run the existing slot-marker collector from `update_motd_slots`, extracted
   as a public `collect_motd_slots(html)`.
3. Require that the slot set equals the configured slot set exactly, with each
   anchor/image pair valid. On failure, raise `MOTDTemplateError` with the
   specific missing or duplicate slots.

Because every published document still carries the 0.50.4 slot markers,
rolling back to 0.50.6 remains safe. The old merge-based job would find the
markers and would not whole-replace.

### 4.5 Lint (warnings, not errors)

- HTML comments: CyTube strips them.
- `<script>`, `on*=` attributes, and `javascript:` URLs: these are expected to
  be stripped by CyTube.
- `<video>`, `<audio>`, `<iframe>`, `<object>`, `<embed>`: out of scope and likely stripped.
- `http://` art URLs, which cause mixed content.
- URLs pointing at a deleted or unknown media asset.
- Output larger than 90 % of `max_html_chars`.

## 5. Scheduling and Publishing

### 5.1 Resolver

`resolve_composition(at_utc) -> Composition(master_rev, {zone: fragment_rev | None})`
is a pure function over schedule rows plus a template lookup, and it is unit
tested without a DB.

- **Master scope:** take all active master schedules whose occurrence contains
  `at`. Pick by `(priority DESC, occurrence_length ASC, updated_at DESC, id DESC)`.
  If none match, use the default master.
- **Zone scope:** for each zone name in the chosen master's revision, apply the
  same ordering among fragment schedules for that zone.
- Window semantics are half-open `[start, end)`.

### 5.2 Timeline

`timeline(from, to)` computes every boundary (window start or end) in range,
then calls `resolve_composition` at each boundary to produce contiguous
segments. Warnings include:

- A fragment scheduled into a zone the active master lacks. This is hidden
  content.
- A schedule that is fully shadowed by a higher-priority schedule.
- A schedule that references a template whose current revision fails
  validation.

### 5.3 Reconcile loop (automatic transitions)

- A new `MOTDAutomation` runs alongside `JobScheduler` as an asyncio task. It
  ticks once at startup and then every `motd.reconcile_interval_seconds`
  (default 60).
- Each tick:
  1. If `motd.automation_enabled` is false, stop. This is the kill switch, and
     it can be toggled in the admin UI (audited).
  2. Resolve the composition for now.
  3. Compute the composition key: `sha256(master_rev_id, sorted zone→rev ids)`.
     The weekend is **not** part of the key (see §14).
  4. If the key equals the latest `motd_publications.composition_key`, do
     nothing.
  5. Otherwise, run the existing `motd_publish` job through `JobManager` with
     `triggered_by="motd_automation"` and `params={"publish": true, "week": …}`,
     where `week` keeps whichever weekend is live (`next` if the latest
     publication was an early next-weekend debut). JobManager's single-run
     guard serializes this with manual and weekly runs. A composition whose
     publish fails is retried at most every 10 minutes.
- Schedule, template, and default changes do not publish directly. They only
  change what the next tick resolves. This gives one write path and makes
  startup and missed ticks self-healing.

### 5.4 `motd_publish` job changes

The job parameters are unchanged. The publication's `trigger` is derived from
the run's `triggered_by`: `motd_automation` becomes `schedule`, the cron
`scheduler` becomes `weekly`, and anything else becomes `manual`. The flow
becomes:

1. Build the slots as today, and drain the progress futures (keep the 0.50.6
   fix).
2. Resolve the composition, render master plus zones in the sandbox, and
   validate (§4.4) and check size limits. Any failure becomes a `JobError` and
   nothing is sent.
3. **Back up the live MOTD.** Call `get_motd()`, then write
   `motd/backups/motd-<UTC>-<sha8>.html`. If the read fails, **fail closed**:
   raise a `JobError` and do not publish. The next reconcile tick retries.
4. Call `set_motd(html)`, write the output artifact, and insert a
   `motd_publications` row. The final result includes `published`,
   `composition`, `html_chars`, and `backup_path`.
5. Optional postcondition: within 10 s, read back the MOTD and compare its
   normalized form (comments removed) with what was sent. Record
   `readback_match` in the result. A mismatch is logged, not fatal, because
   CyTube sanitizer normalization is not fully characterized.

The selective-merge branch (`update_motd_slots` against live) is **removed from
publish**. The parser stays for validation and legacy import.
`GET /admin/motd/render` changes to return the composed render. Its 409 merge
error disappears and is replaced by 422 for template validation errors.

Retention: a daily `motd_retention_prune` job, run through the existing job
infrastructure, deletes the following once they are older than
`motd.retention_days` (default **30**):

- `admin_audit_log` rows
- `motd_publications` rows, except the newest one, which reconcile needs as its
  baseline
- backup files under `motd/backups/`, kept in step with their publication rows

Template revisions, schedules, and media assets are **not** time-pruned.

## 6. Admin UI (server-rendered Jinja + vanilla JS, matching existing pages)

The existing page [../kryten_webqueue/templates/admin/motd.html](../kryten_webqueue/templates/admin/motd.html)
gains tabs: **Weekend grid** (existing) · **Templates** · **Schedule** ·
**History**. **Media** is a separate admin tab, because it is general purpose.

- **Templates:**
  - The list is filtered by master/fragment and archived state.
  - The editor is a split pane, with a monospace `<textarea>` on the left and a
    preview on the right. CodeMirror is a later enhancement and is not needed in
    v1.
  - Preview posts the unsaved body to `/admin/motd/templates/preview`, debounced
    at about 600 ms.
  - Preview options:
    - week = current/next
    - as-of datetime (ET), which resolves the zones as they would be at that
      time
    - for fragments, a host master to preview inside
    - an ad-hoc override of the zone contents
  - Rendering happens in `<iframe sandbox srcdoc=…>` with no scripts allowed.
  - Validation errors and lint are shown inline with line numbers.
  - Buttons: Save (requires a note), Save as…, Revisions (with diff), Restore,
    Archive, Set default (masters).
  - A banner warns when the template is currently live: "saving publishes
    within ~1 min".
  - "Insert media" opens the library picker and inserts
    `{{ media_url("slug") }}`.
- **Schedule:**
  - Lists master and fragment schedules.
  - The create/edit form accepts a one-off window, or a recurring window built
    with weekday checkboxes plus start/end time-of-day plus a series date range.
    The form generates the RRULE, and a raw RRULE field is available under
    "advanced".
  - Shows a 30-day **timeline** with warnings, and a "now" indicator.
- **History:**
  - Lists publications, with the composition, actor/trigger, size, and links to
    the job run and backup.
  - "Republish now" is available.
  - The automation on/off toggle is here.
- **Media:**
  - Upload by drag-drop or file picker, with a slug that is pre-filled from the
    filename and editable, plus an optional description.
  - The library grid supports search, shows thumbnails/size/dimensions/uploader,
    and has a **Copy URL** button for both the absolute URL and the
    `media_url()` snippet.
  - Delete shows the templates that block it.

## 7. Media Library

- Storage:
  - Public files go in `media.dir` (default `/var/lib/kryten-webqueue/media/public`).
  - Deleted files are moved to `media.trash_dir`
    (`/var/lib/kryten-webqueue/media/trash`), which is outside the mount, so
    they stop serving immediately.
  - Both are on the existing persistent volume.
- Serving: `app.mount("/media", StaticFiles(media.dir))`. The public URL is
  `media.base_url` (default `https://queue.dropsugar.co/media`) plus
  `/<filename>`. The grindhouse proxy is a pass-through and needs no change, but
  this must be verified as in 0.50.2. Responses add
  `X-Content-Type-Options: nosniff` and long cache headers.
- URLs are stable. Filenames are never reused. If the same slug is uploaded
  again after a delete, it gets a new suffix.
- Upload pipeline (all checks happen server-side, and the client MIME type is
  ignored):
  1. Stream to a temp file and enforce `media.max_image_bytes` (default 10 MiB)
     while reading.
  2. Sniff the type from magic bytes. Accept only GIF/PNG/JPEG/WebP.
  3. **Images:**
     - Open with Pillow, with `MAX_IMAGE_PIXELS` bounded, and `verify()`.
     - Re-encode to strip EXIF/XMP/comments while keeping all frames, per-frame
       durations, loop count, disposal, and transparency.
     - If re-encoding grows the file past the limit, or loses frames, reject it
       with a clear message.
     - Record dimensions, frame count, and animated.
  4. Slug: lowercase it, keep only `[a-z0-9-]`, and append `-2`, `-3`, … on
     collision. The extension comes from the detected type.
  5. Write atomically with `os.replace`, insert the row, and audit it.
- Delete is blocked when the current revision of any non-archived template
  contains the asset's filename or its `media_url("slug")`. The block response
  lists those templates. Older revisions do not block deletion, but restoring
  one lints for dead media.
- Uses outside templates, such as URLs pasted into chat, cannot be tracked. The
  UI states this in the delete confirmation.
- Existing `/motd/boxes` per-slot override art is unchanged. It is a separate
  per-week concern and stays separate from the library permanently.

## 8. HTTP Routes (all `Depends(require_admin)`, all JSON unless noted)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/admin/motd/templates?kind=&archived=` | List |
| POST | `/admin/motd/templates` | Create `{name, kind, display_name, body, note}` |
| GET | `/admin/motd/templates/{name}` | Template plus current body |
| PUT | `/admin/motd/templates/{name}` | Save a new revision `{body, note, expected_revision_id}`. Returns 409 if stale, which prevents silent overwrite between two admins. |
| PATCH | `/admin/motd/templates/{name}` | display_name / description / is_default |
| POST | `/admin/motd/templates/{name}/archive` | Soft delete (with guards) |
| GET | `/admin/motd/templates/{name}/revisions` | List |
| GET | `/admin/motd/templates/{name}/revisions/{n}` | Body |
| POST | `/admin/motd/templates/{name}/revisions/{n}/restore` | Append a copy |
| POST | `/admin/motd/templates/preview` | `{body?, name?, kind, week, as_of?, host_master?, zone_overrides?}` → `{html, chars, errors, warnings}` |
| GET/POST | `/admin/motd/schedules` | List / create |
| PUT/DELETE | `/admin/motd/schedules/{id}` | Edit / delete (hard delete; audited with the full before-row) |
| GET | `/admin/motd/timeline?from=&to=` | Segments plus warnings (max 92 days) |
| GET | `/admin/motd/publications?limit=` | History |
| GET/PUT | `/admin/motd/automation` | `{enabled}` kill switch (persisted, audited) |
| POST | `/admin/motd/publish` | Existing; now publishes the resolved composition |
| GET | `/admin/motd/render` | Existing; now the composed render (no live merge) |
| GET | `/admin/media/assets?q=&limit=&offset=` | Library |
| POST | `/admin/media/assets` (multipart `file`, `slug?`, `description?`) | Upload → `{slug, url, snippet, …}` |
| PATCH | `/admin/media/assets/{slug}` | description |
| DELETE | `/admin/media/assets/{slug}` | Soft delete, or 409 with blocking templates |
| GET | `/admin/audit?entity_type=&entity_id=` | Audit viewer (read-only) |

Every write route takes the actor from the session, never from the request
body.

## 9. Configuration

New and changed keys, with all defaults safe. `config.example.json` must be
updated.

```json
"motd": {
  "timezone": "America/New_York",
  "automation_enabled": false,
  "reconcile_interval_seconds": 60,
  "max_html_chars": 20000,
  "retention_days": 30,
  "template": "channel_z.html"
},
"media": {
  "dir": "/var/lib/kryten-webqueue/media/public",
  "trash_dir": "/var/lib/kryten-webqueue/media/trash",
  "base_url": "https://queue.dropsugar.co/media",
  "max_image_bytes": 10485760
}
```

- `automation_enabled` ships **false**. An admin can change it from the MOTD
  History tab after the seeded default master has been previewed and published
  manually once. The change applies immediately and is saved back to the
  service config file with `Config.save()`, so it persists across restarts; it
  is not a deployment-only setting. It is config-backed rather than stored in
  the database.
- `max_html_chars` is CyTube's hard 20000 UTF-16 code-unit cut (P3). Lowering
  it is allowed; raising it is not useful.
- `motd.template` is deprecated. It is used only to seed the default master
  when no templates exist.
- There are no NATS, KV, or api-gate contract changes. All CyTube writes still
  go through `ApiGateClient.set_motd` → `KrytenClient.set_motd` → Robot.

## 10. Implementation Phases

Each phase can be released and rolled back independently. Every phase runs
black/ruff/mypy on the touched slice, the full `uv run pytest`, a CHANGELOG
entry, and a version bump through the existing release workflow.

| Phase | Scope | Live verification |
| --- | --- | --- |
| **0** | Done: see §2 | — |
| **1** | Media library: schema, upload pipeline, `/media` mount, admin tab, audit table. Covers #5 and #6. | Upload GIF/PNG/JPEG/WebP; the public URL returns 200 with the correct type; EXIF is gone; deleting stops serving |
| **2** | Templates + revisions + sandbox + zones + preview editor + seeding + required-grid validation. **Manual publish only**, which renders the default master with no schedules. | Publish the seeded master; the readback contains 24 slot markers; the backup was written |
| **3** | Schedules + resolver + timeline + preview "as of". **No automatic publishing.** | The timeline matches a hand-computed October scenario, including a DST boundary |
| **4** | Reconcile loop, kill switch, publications history, 30-day retention prune, removal of the publish merge branch | Enable automation; create a 5-minute fragment window; observe the start and end transitions within 60 s, with the backups and publication rows |
| 5 (stage 2) | Per-template CSS (§11) | — |

Seeding (phase 2) intentionally discards any hand edits currently in the live
MOTD. Before phase 2 is deployed, capture the live MOTD backup and diff it
against the seeded render, so an admin can port any live-only content into the
default master before the first publish.

## 11. Stage 2 Notes (CSS), Not Designed Here

- CyTube channel CSS is one global document, limited to 20 KB by the kryten-py
  `set_channel_css` docstring. api-gate and webqueue have no CSS write path
  yet.
- Pushing per-template CSS raises a document question: does webqueue replace
  the whole channel CSS, or only a delimited section of it? This is about which
  part of the document the automation controls, not about who can edit it. CSS
  comments may also be normalized by CyTube, the same way HTML comments are.
  This must be answered before stage 2 starts.

## 12. Test Plan

Unit tests:

- Resolver:
  - priority and tie-breaks
  - half-open boundaries
  - no master → default
  - zone with no fragment → `""`
  - recurring weekly window across the DST change (Nov 1, 2026)
  - series bounds
  - shadowed schedules
- RRULE validation rejects DTSTART/COUNT conflicts and empty series.
- Sandbox:
  - `include`, `extends`, and `import` fail
  - attribute access to `__class__` / `mro` is blocked
  - the `config` object is unreachable
  - fragments cannot call `zone()` or `movie_grid()`
  - dynamic `zone(var)` is a save error
  - duplicate zone is a save error
  - output size limit
- Required-grid validation: missing, duplicate, and extra slots; a grid without
  `movie_grid()` but with correct manual markers passes.
- Revisions: append-only, restore appends, stale `expected_revision_id` → 409,
  only one default master, archive guards.
- Media:
  - magic-byte sniffing beats a spoofed MIME type
  - oversize is rejected while streaming
  - decompression bomb is rejected
  - an animated GIF/WebP keeps its frame count, durations, and loop after
    re-encode
  - EXIF is removed from a JPEG fixture
  - slug sanitizing and collision suffixes
  - a filename is never reused after delete
  - delete is blocked by a referencing template
  - trash is not served
- Publish job:
  - a live read failure → no `set_motd`
  - validation failure → no `set_motd`
  - publication row and backup are written
  - the final result contains `published`
  - progress drain is preserved, as in the 0.50.6 regression
- Retention prune: rows and backups past 30 days are removed; the newest
  publication survives regardless of age; revisions, schedules, and media are
  untouched.

Integration tests (real DB layer, SQLite plus PG where available):

- Native `timestamptz` and `bool` types are handled throughout the new methods.
  This is the lesson from 0.50.5: do not mock schedule lookups to return
  `None`.
- Reconcile: an unchanged composition → no job; a changed composition → one
  job; concurrent ticks → one run.
- A real app boot with `uv run` reaches readiness with automation enabled
  against an empty schema (seeding path).

Routes: every new route returns 401/403 for rank < 3, and the actor recorded in
the audit row comes from the session.

## 13. Open Items

None.

Resolved 2026-10-03:

- Phase 0: video dropped; `<style>` verified; MOTD limit is a silent 20000
  UTF-16 code-unit truncation.
- `/motd/boxes` art stays separate from the media library permanently.
- Audit log, publications, and MOTD backups are retained for 30 days.
- No in-chat surfacing is planned.

## 14. Implementation Notes (deviations from the first draft)

- **Composition key excludes the weekend.** If the key included `week_key`, a
  Sunday "next weekend" debut would make every tick republish the current
  weekend within a minute and undo the debut. The publication row still
  records `week_key`; automation republishes only when templates or schedules
  change, and keeps whichever weekend is live.
- **Automation toggle is admin-editable and persistent.** The History-tab
  control applies it immediately and saves via `Config.save()`; if persistence
  fails, the in-memory value rolls back. Its source of truth is the config
  file, not a DB settings table.
- **Media JSON routes** are under `/admin/media/assets` so that `/admin/media`
  can be the HTML page, matching `/admin/motd` (page) vs `/admin/motd/grid`.
- **Single release.** All phases shipped together as 0.51.0, with automation off
  by default. That keeps the Phase 2 "manual publish only" posture until an
  admin opts in.
- **Save-time validation renders a placeholder grid** (`builder.sample_week`),
  with no workbook or OMDB calls, so saving never waits on the network. The
  preview can opt into the real grid.
- **Stale-editor check runs before validation.** A save from an outdated editor
  returns 409 even if the body is also invalid.
- **Media uploads claim the slug in the DB before writing the file**, so the
  UNIQUE constraint, not the filesystem, decides between racing uploads.
- **Animated re-encode check** accepts a lower frame count only when the total
  duration is unchanged, because Pillow merges identical consecutive frames.
- **The retention prune** also sweeps backup files older than the window that
  no row references (for example, backups from publishes that failed later),
  but never the newest publication's backup.
