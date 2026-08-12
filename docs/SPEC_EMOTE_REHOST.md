# kryten-webqueue — Emote Rehost Job Spec

**Version:** 1.1  
**Date:** 2026-08-07  
**Service:** kryten-webqueue v0.33.3 → v0.34.0  
**Status:** Implemented — OQ-6 (`source` value) awaiting confirmation  
**Scope:** kryten-webqueue only (kryten-api-gate requires no changes; existing emote routes are sufficient)

---

## 0. Open Questions — Resolution Status

| # | Question | Resolution |
|---|----------|------------|
| OQ-1 | Same host? | **Resolved: yes.** Direct file I/O. |
| OQ-2 | File ownership | **Resolved: `kryten:www-data`.** Group set via `os.chown(-1, www_data_gid)`; passwordless sudo available if full `www-data:www-data` is needed later. |
| OQ-3 | Emote name format | **Resolved: names include `#`** (confirmed from channel JSON). Filename strips `#` → `behold.gif`. API call URL-encodes `#` → `%23behold`. |
| OQ-4 | Rehosted URL pattern | **Resolved:** `https://www.dropsugar.co/static/emotes/{bare_name}{ext}` |
| OQ-5 | Periodic interval | **Resolved: configurable** via `emote_rehost.check_interval_hours` (default 24). |
| OQ-6 | `source` value | **Pending confirmation.** Using `"url"` (direct hosted URL). See OQ-6 note at end of this doc. |

---

## 1. Scope

One new job — `rehost_emotes` — added to `kryten-webqueue`. It:

1. Fetches the current channel emote list via `GET /api/v1/emotes/` (api-gate).
2. Saves a timestamped backup JSON to `/home/kryten/emote_backups/` (configurable).
3. Identifies emotes **not already** hosted on the configured rehost domain (`www.dropsugar.co`).
4. For each such emote, aggressively downloads the image file (multiple strategies, exponential back-off, resume support).
5. Moves the downloaded file to the configured static directory (`/home/mediacms.io/mediacms/static/emotes/`) with the bare emote name as filename (no `#`).
6. Sets file permissions and group ownership (`kryten:www-data`, mode 0644).
7. Updates the emote's `image` URL in the local list to the new rehosted URL.
8. Pushes each updated emote back to CyTube via `PUT /api/v1/emotes/{name}` (api-gate).
9. Saves a second backup with the updated JSON.
10. Reports a result summary (counts of attempted / succeeded / failed).

The job is:
- **On-demand**: runnable from the admin Jobs panel at `https://queue.dropsugar.co/admin/jobs`.
- **Periodic**: runs automatically on a configurable interval (default 24 h) via a background loop.

No changes are required to **kryten-api-gate** — `GET /emotes/`, `PUT /emotes/{name}`, and `POST /emotes/import` already exist and provide the full surface needed.

---

## 2. Files Changed

| File | Change |
|------|--------|
| `kryten_webqueue/config.py` | Add `EmoteRehostConfig` model + `emote_rehost: EmoteRehostConfig` field on `Config` |
| `kryten_webqueue/api_gate/client.py` | Add `get_emotes()` and `update_emote()` methods |
| `kryten_webqueue/jobs/rehost_emotes.py` | **New file.** Job logic + aggressive downloader (adapted from `aggressive_emote_retry.py`) |
| `kryten_webqueue/app.py` | Register job + add periodic background loop |
| `config.example.json` | Add `emote_rehost` section |

---

## 3. Config Changes

### 3.1 New model — `EmoteRehostConfig`

Add to `kryten_webqueue/config.py`:

```python
class EmoteRehostConfig(BaseModel):
    """Settings for the emote rehost job.

    Downloads emotes hosted outside ``rehost_domain`` and moves them into
    the local static directory, then updates CyTube via api-gate.
    """

    enabled: bool = True
    # Domain considered "already rehosted". Emotes whose image URL contains
    # this string are skipped.
    rehost_domain: str = "dropsugar.co"
    # Local directory where rehosted image files are placed.
    static_dir: str = "/home/mediacms.io/mediacms/static/emotes"
    # Public base URL served from static_dir.
    # Final URL: {base_url}/{bare_name}.{ext}
    base_url: str = "https://www.dropsugar.co/static/emotes"
    # Directory for backup JSON files (created if absent).
    backup_dir: str = "/home/kryten/emote_backups"
    # Periodic check interval. Set to 0 to disable the background loop.
    check_interval_hours: float = 24.0
    # Per-file download: max retry attempts.
    download_max_retries: int = 5
    # Seconds to wait between emote downloads (be nice to source hosts).
    inter_emote_delay_sec: float = 2.0
```

### 3.2 Field on `Config`

```python
emote_rehost: EmoteRehostConfig = EmoteRehostConfig()
```

### 3.3 `config.example.json` addition

```json
"emote_rehost": {
  "enabled": true,
  "rehost_domain": "dropsugar.co",
  "static_dir": "/home/mediacms.io/mediacms/static/emotes",
  "base_url": "https://www.dropsugar.co/static/emotes",
  "backup_dir": "/home/kryten/emote_backups",
  "check_interval_hours": 24,
  "download_max_retries": 5,
  "inter_emote_delay_sec": 2.0
}
```

---

## 4. ApiGateClient Changes

Add to `kryten_webqueue/api_gate/client.py`:

```python
# --- Emotes ---

async def get_emotes(self) -> list[dict]:
    """Fetch current channel emote list. Each dict has name, image, source."""
    result = await self.get("/emotes/")
    return result.get("emotes", [])

async def update_emote(self, name: str, image: str, source: str = "url") -> dict:
    """Update a single emote's image URL.  name must NOT include the '#' prefix."""
    return await self.put(f"/emotes/{name}", json={"image": image, "source": source})
```

---

## 5. Job Implementation — `kryten_webqueue/jobs/rehost_emotes.py`

### 5.1 Module layout

```
kryten_webqueue/jobs/rehost_emotes.py
```

Top-level exports:
- `REHOST_EMOTES_SCHEMA: list[dict]` — empty (no user parameters)
- `rehost_emotes_job(params: dict, ctx: JobContext) -> dict` — the job entry point

### 5.2 Download strategy (adapted from `aggressive_emote_retry.py`)

The downloader uses `requests` (already a dep) with a `requests.Session` carrying:
- `Retry(total=5, backoff_factor=2, status_forcelist=[429,500,502,503,504])`
- Rotating `User-Agent` pool (5 desktop browsers)
- `Accept: image/*` headers
- Progressive timeout (60 s + 30 s per attempt, max 180 s)
- `.tmp` resume file for partial downloads

On failure, a second strategy is tried: minimal-header `requests.get` (curl-style UA), then bare `requests.get` with no headers.

**All blocking I/O (requests, file ops) is run off the event loop via `asyncio.to_thread`.**

### 5.3 File naming

Given emote `name` (e.g. `behold`, without `#`) and detected extension (from Content-Type or URL path, defaulting to `.gif`):

```
filename = f"{name}{ext}"          # e.g.  behold.gif
dest     = static_dir / filename   # /home/mediacms.io/mediacms/static/emotes/behold.gif
```

Collision handling: if `dest` already exists and the source URL matches `rehost_domain`, skip (already done). If the URL changed, overwrite.

### 5.4 Permissions

After `shutil.move(tmp, dest)`:

```python
import os, grp
os.chmod(dest, 0o644)
# Set group to www-data (kryten is a member; this does not require root)
www_data_gid = grp.getgrnam("www-data").gr_gid
os.chown(dest, -1, www_data_gid)   # -1 = keep existing user-owner (kryten)
```

> **Note OQ-2**: User-owner will be `kryten`, not `www-data`. If strict `www-data:www-data` ownership is required, a `sudoers` entry scoped to `chown -R www-data:www-data /home/mediacms.io/mediacms/static/emotes` is the cleanest solution without changing how the service runs.

### 5.5 Backup format

Files are written to `backup_dir` with UTC timestamp names:

```
{backup_dir}/emotes-{YYYYMMDD-HHMMSS}-before.json
{backup_dir}/emotes-{YYYYMMDD-HHMMSS}-after.json
```

Content: the raw list as returned/modified, JSON-serialised with `indent=2`.

### 5.6 Progress reporting

The job calls `ctx.progress({...})` at key points:
- After fetching emotes: `{"step": "fetched", "total": N, "to_rehost": M}`
- After each download: `{"step": "downloaded", "emote": name, "done": i, "total": M}`
- After each api-gate push: `{"step": "pushed", "emote": name}`
- On completion: `{"step": "complete", "rehosted": X, "failed": Y, "skipped": Z}`

### 5.7 Error handling

- `JobError` is raised (not generic `Exception`) when api-gate returns a non-2xx for the emote fetch — a misconfigured token or unreachable gate is a user-facing problem.
- Individual download failures are **logged and counted** but do not abort the job — the job continues to the next emote and reports failures in its result summary.
- Individual api-gate push failures are treated the same way.
- File system errors (permission denied on `static_dir`) are raised as `JobError` with a clear message.

### 5.8 Result dict (returned to job runner, stored in `job_runs.detail`)

```json
{
  "total_emotes": 120,
  "already_rehosted": 95,
  "attempted": 25,
  "succeeded": 23,
  "failed": 2,
  "pushed": 23,
  "failed_emotes": ["brokenemote1", "brokenemote2"]
}
```

---

## 6. Job Schema (admin UI)

No user-configurable parameters needed at run time. The schema is an empty list. The Run modal will simply confirm and launch.

```python
REHOST_EMOTES_SCHEMA: list[dict] = []
```

If OQ-1 is resolved as "different host / SSH" in future, a `dry_run` bool param could be useful. Not included in v1.

---

## 7. app.py Changes

### 7.1 Job registration (in the lifespan block, after existing job registrations)

```python
from .jobs.rehost_emotes import rehost_emotes_job, REHOST_EMOTES_SCHEMA

job_manager.register(
    "rehost_emotes",
    rehost_emotes_job,
    label="Rehost Emotes (download & re-upload to dropsugar.co)",
    schema=REHOST_EMOTES_SCHEMA,
)
```

### 7.2 Periodic background loop

Add alongside the `_catalog_sync_loop` inside the lifespan block:

```python
async def _emote_rehost_loop():
    if not config.emote_rehost.enabled:
        return
    interval = config.emote_rehost.check_interval_hours * 3600
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        try:
            await job_manager.run("rehost_emotes", triggered_by="scheduler")
        except Exception as e:
            logger.exception("Emote rehost periodic run failed: %s", e)
```

Add `asyncio.create_task(_emote_rehost_loop())` to the `bg_tasks` list.

---

## 8. Pseudo-code walkthrough (step by step)

```
rehost_emotes_job(params, ctx):
    cfg = ctx.config.emote_rehost
    api = ctx.api_gate

    # 1. Fetch emote list
    try:
        emotes = await api.get_emotes()           # GET /emotes/
    except httpx.HTTPStatusError as e:
        raise JobError(f"Failed to fetch emotes: {e}")

    # 2. Save before-backup
    backup_dir = Path(cfg.backup_dir)
    await asyncio.to_thread(backup_dir.mkdir, parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    before_path = backup_dir / f"emotes-{stamp}-before.json"
    await asyncio.to_thread(_write_json, before_path, emotes)

    # 3. Partition
    to_rehost = [e for e in emotes if cfg.rehost_domain not in e["image"]]
    already   = len(emotes) - len(to_rehost)
    await ctx.progress({"step": "fetched", "total": len(emotes), "to_rehost": len(to_rehost)})

    # 4. Download each
    static_dir = Path(cfg.static_dir)
    await asyncio.to_thread(static_dir.mkdir, parents=True, exist_ok=True)

    results = {"succeeded": [], "failed": []}
    updated_emotes = list(emotes)   # working copy

    for i, emote in enumerate(to_rehost, 1):
        name = emote["name"]          # bare name (no #), per OQ-3
        url  = emote["image"]

        ext, file_bytes = await asyncio.to_thread(
            _aggressive_download, url, cfg.download_max_retries
        )
        if file_bytes is None:
            logger.warning("Failed to download emote %s from %s", name, url)
            results["failed"].append(name)
            await ctx.progress({"step": "failed", "emote": name, "done": i, "total": len(to_rehost)})
            continue

        dest = static_dir / f"{name}{ext}"
        await asyncio.to_thread(_write_and_chown, file_bytes, dest)

        new_url = f"{cfg.base_url.rstrip('/')}/{name}{ext}"

        # Update working copy
        for entry in updated_emotes:
            if entry["name"] == name:
                entry["image"] = new_url
                entry["source"] = "url"
                break

        # Push to CyTube via api-gate
        try:
            await api.update_emote(name, new_url, source="url")
            results["succeeded"].append(name)
            await ctx.progress({"step": "pushed", "emote": name, "done": i, "total": len(to_rehost)})
        except Exception as e:
            logger.error("Failed to push emote %s to api-gate: %s", name, e)
            results["failed"].append(name)

        await asyncio.sleep(cfg.inter_emote_delay_sec)

    # 9. Save after-backup
    after_path = backup_dir / f"emotes-{stamp}-after.json"
    await asyncio.to_thread(_write_json, after_path, updated_emotes)

    await ctx.progress({"step": "complete", ...})
    return {
        "total_emotes": len(emotes),
        "already_rehosted": already,
        "attempted": len(to_rehost),
        "succeeded": len(results["succeeded"]),
        "failed": len(results["failed"]),
        "pushed": len(results["succeeded"]),
        "failed_emotes": results["failed"],
    }
```

---

## 9. kryten-api-gate — No Changes Required

The existing emote surface is complete:

| Route | Used for |
|-------|----------|
| `GET /api/v1/emotes/` | Fetch full emote list |
| `PUT /api/v1/emotes/{name}` | Update single emote URL (per-emote push) |
| `POST /api/v1/emotes/import` | Not used by this job (individual updates preferred for granular progress + error isolation) |

---

## 10. Testing

New tests in `tests/test_rehost_emotes.py`:

| Test | What it covers |
|------|----------------|
| `test_skips_already_rehosted` | Emotes already on dropsugar.co → `already_rehosted == total` |
| `test_downloads_and_pushes` | Mock download succeeds + mock api.update_emote called for each external emote |
| `test_download_failure_continues` | One download fails → job continues; failure appears in `failed_emotes` |
| `test_push_failure_continues` | Download succeeds but api-gate push fails → logged, counted, not fatal |
| `test_backup_files_written` | Both before/after JSON files are created in backup_dir |
| `test_filenames_from_name` | Filename on disk is bare name (no `#`) + detected extension |
| `test_group_ownership_set` | `os.chown` called with correct gid |
| `test_periodic_loop_calls_job_manager` | Periodic loop triggers `job_manager.run("rehost_emotes")` after interval |

---

## 11. Version / Changelog

Bump `pyproject.toml` to `0.34.0` (new feature).

`CHANGELOG.md` entry:

```
## [0.34.0] - 2026-08-07

### Added
- `rehost_emotes` background job: fetches channel emote list, downloads any images
  not yet hosted on dropsugar.co, places them in mediacms static/emotes/, updates
  each emote URL via api-gate, and saves before/after JSON backups. Runnable on
  demand from Admin → Jobs or automatically on the configured interval
  (default: 24 h). New config section `emote_rehost` (see config.example.json).
```

---

## 12. Blast Radius & Cross-Service Impact

| Concern | Assessment |
|---------|------------|
| kryten-api-gate contract | No changes. Uses existing `GET /emotes/` + `PUT /emotes/{name}` — unchanged routes. |
| CyTube emote list | Each successful rehost issues a `updateEmote` socket command through kryten-robot (via api-gate → NATS). This is a live channel operation; CyTube will reflect new URLs immediately. Low risk: we're only updating URLs for emotes we've just successfully downloaded. |
| mediacms static files | New files land in `/home/mediacms.io/mediacms/static/emotes/`. mediacms/nginx serves the directory; no config change needed if it already serves `static/emotes/`. Confirm with OQ-4. |
| Other services | No other services read the emote list at job time. kryten-robot re-emits emote-update events that kryten-llm observes, but those are informational. |
| Concurrency | Job manager's per-job lock ensures only one `rehost_emotes` run at a time. Periodic loop uses `job_manager.run()` (which returns `already_running` instead of double-starting). |
