# AGENTS.md — Working on MediaCMS (dropsugar.co)

Guidance for agents modifying or integrating with the **MediaCMS** instance that
backs the Kryten catalog. MediaCMS is a third-party Django app; we run our own
instance and have applied **local source patches** to it. This file records how
it is deployed, what we changed, and the sharp edges — because the MediaCMS
source is **not under version control** and these facts are otherwise lost.

## Deployment
- **Bare install** on `grindhouse.local` at `/home/mediacms.io/mediacms` (v7.2.0).
  Not Docker. No git. Files owned by `www-data` — edit with `sudo`.
- Served over uWSGI. systemd units: `mediacms` (uwsgi) + `celery_beat`,
  `celery_long`, `celery_short`.
- **Reload = stop then start.** `systemctl reload mediacms` is NOT wired
  (no `ExecReload`, no `touch-reload` in `deploy/local_install/uwsgi.ini`). Apply
  code changes with:
  ```
  sudo systemctl stop mediacms; sudo systemctl start mediacms
  ```
- Public host `www.dropsugar.co` resolves to a public IP but the source you edit
  is the local tree above.
- **Always back up before patching** (source only; skip static/media uploads):
  ```
  tar -czf /home/kryten/mediacms_source_backup_$(date +%Y%m%d-%H%M%S).tar.gz \
    --exclude=static --exclude=media_files --exclude=media --exclude=node_modules \
    --exclude=__pycache__ -C /home/mediacms.io mediacms
  ```
  Per-file backups from our patches: `*.bak-tagpatch`, `*.bak-facets`.

## Auth model (important)
- Our API token user is `admin`, a **Django superuser**.
- `is_mediacms_editor(user)` (in `files/methods.py`) returns True for superusers,
  managers, and editors.
- `GET /api/v1/whoami` does **NOT** expose `is_superuser`/`is_manager`/`is_editor`.
  Do not try to detect privilege level from `whoami`. If a consumer needs to know
  it can manage all media, use an explicit config flag (kryten-webqueue does:
  `mediacms_manage_all_media`).

## Local patches we applied (reapply after any MediaCMS upgrade)
Recorded as idempotent, self-checking scripts in `kryten-webqueue/scripts/`:

1. **`patch_mediacms_tags.py`** → `files/views/media.py`, `MediaBulkUserActions.post`
   - Stock behavior scopes the queryset to `Media.objects.filter(user=request.user, …)`
     with **no manager/superuser bypass**, so `add_tags`/`change_owner` on media owned
     by another user return `400 {"detail":"No matching media found"}`.
   - Patch: editors/superusers operate on **all** media; `add_tags` now
     `get_or_create`s missing tags (normalized) instead of only attaching pre-existing ones.

2. **`patch_mediacms_bulk_facets.py`** → adds `GET /api/v1/media_facets`
   - New read-only, editor-only, paginated endpoint returning
     `{friendly_token, tags, categories}` (`page`, `page_size`≤2000, `has_next`).
   - Lets integrations pull every item's facets in ~20 calls instead of one
     `GET /api/v1/media/{token}` per item (10k items: ~35s vs ~30+ min).
   - Touches `files/views/media.py` (view), `files/views/__init__.py` (re-export),
     `files/urls.py` (route). `views` is `files.views`; new views must be re-exported
     in `files/views/__init__.py` to be reachable from `urls.py`.

## API sharp edges
- **`PUT /api/v1/media/{token}` ignores a `tags` field.** It only accepts
  `title`/`description`/`media_file`. To change tags you MUST use
  `POST /api/v1/media/user/bulk_actions` with `action: add_tags` /`remove_tags`
  and `tag_titles: [...]`.
- **`Tag.save()` normalizes titles** via `get_alphanumeric_only()` → strips all
  non-alphanumerics and lowercases (`"mpaa-r"` → `"mpaar"`). Keep tag slugs
  alphanumeric so local and CMS match and `get_or_create` doesn't collide.
- `bulk_actions` is single-purpose per call; `media_ids` are `friendly_token`s.
- `PUT` on media reassigns ownership — the title/description push paths restore
  the original owner via a follow-up `change_owner` bulk action.

## Interaction with kryten-webqueue (source of truth)
- **CMS is the source of truth for tags.** kryten's catalog sync mirrors CMS via
  `set_catalog_tags` (REPLACE). Locally-derived genre/MPAA tags only persist if
  they were pushed to CMS first — so the `tags` enrichment step must run.
- The hourly scheduled jobs (`enrichtitles/meta/tv`) do **not** include the `tags`
  step. Run `steps=tags force=1` manually or add a scheduled `tags`/`all` run,
  otherwise a sync can wipe locally-derived tags that never reached CMS.
- kryten talks to CMS from `kryten_webqueue/catalog/` (`sync.py`, `mediacms.py`,
  and `enrichment/steps/{title,meta,art,tags}.py`).

## Verifying a change (curl as the admin token)
```
U=$(python3 -c 'import json;print(json.load(open("/etc/kryten-webqueue/config.json"))["mediacms_url"])')
T=$(python3 -c 'import json;print(json.load(open("/etc/kryten-webqueue/config.json"))["mediacms_token"])')
# add tags to a foreign-owned item (expect HTTP 200 after the tags patch)
curl -s -X POST "$U/api/v1/media/user/bulk_actions" -H "Authorization: Token $T" \
  -H 'Content-Type: application/json' \
  -d '{"action":"add_tags","media_ids":["<token>"],"tag_titles":["horror"]}'
# bulk facets (after the facets patch)
curl -s "$U/api/v1/media_facets?page=1&page_size=5" -H "Authorization: Token $T"
```

## Do / Don't
- **Do** back up before editing; validate with `python3 -c "import ast; ast.parse(open(path).read())"`.
- **Do** stop/start (not reload/restart) to apply changes.
- **Don't** assume a REST endpoint is owner-agnostic — most `user/*` endpoints are
  owner-scoped in stock MediaCMS.
- **Don't** rely on these patches surviving an upgrade; re-run the scripts in
  `kryten-webqueue/scripts/` and confirm with the curl checks above.
