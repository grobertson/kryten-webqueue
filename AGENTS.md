# Kryten-WebQueue — Project Guidelines

Kryten-WebQueue is the **web/HTTP layer** of the Kryten ecosystem: a Netflix/Tubi-style
catalog browser and pay-to-play queue manager for a CyTube channel. It is a FastAPI +
uvicorn app that serves the public site, enriches a local catalog mirrored from **MediaCMS**,
and drives playback by issuing commands through **kryten-api-gate**. Unlike the NATS-only
microservices, this is a user-facing HTTP application with its own SQLite database.

## Architecture
- **FastAPI app** (`kryten_webqueue/app.py`) — routers under `routes/`, Jinja2 `templates/`,
  `static/` assets, WebSockets under `ws/` (queue + race views). Auth is a JWT **session
  cookie** (`auth/session.py`); admin routes require rank ≥ 3 (`require_admin`).
- **Local catalog in SQLite** via `aiosqlite` (`catalog/db/`). Schema + **forward-only
  migrations** live in `catalog/db/_connection.py` (bump the version and add a migration;
  never rewrite an applied one). DB path comes from config (`db_path`).
- **Catalog sync** (`catalog/sync.py`) mirrors items + facets from MediaCMS. It bulk-pulls
  tags/categories via `GET /api/v1/media_facets` (falls back to per-item detail on stock
  CMS). `set_catalog_tags` is a **REPLACE** — **CMS is the source of truth for tags**.
- **Enrichment pipeline** (`catalog/enrichment/`): `sync → classify → title → meta → art →
  tags → categories`. TMDB/OMDB lookups in `providers.py`; all title normalization in
  `normalise.py` (`normalize_movie_title`). Classification is **cached** in enrichment state;
  classify-logic changes only reach existing items on a `force` re-run.
- **Jobs** (`jobs/`): `JobManager` runs single-flight background jobs with **schema-validated
  params** (`catalog_enrich`, `catalog_sync`, …) and APScheduler cron (`job_scheduler.py`).
  The `steps` param is a **validated enum** — only fixed combos are accepted (e.g.
  `all`, `classify,meta,art,tags`, `classify,meta`, `art`, `tags`, `title`, `sync`,
  `classify`); arbitrary combos return HTTP 400.
- **External surfaces**: MediaCMS over HTTP (catalog source, art, tag/title writes) and
  kryten-api-gate (playback commands, via `api_gate_url`/`api_gate_token`). **Working on the
  MediaCMS instance and our local patches: see [scripts/AGENTS.md](scripts/AGENTS.md).**

## Build and Test
Run from the repo root (uv-managed):
- Install deps: `uv sync`
- Format: `uv run black .`
- Lint (autofix): `uv run ruff check --fix .`
- Types: `uv run mypy kryten_webqueue`
- Tests: `uv run pytest` (add `--cov=kryten_webqueue --cov-report=term-missing` for coverage)

Run all four before committing. Do not bypass checks (`--no-verify`).

## Conventions
- Python 3.12+, flat `kryten_webqueue/` package, 100% `async`/`await`, Pydantic v2 config,
  FastAPI + uvicorn. black/ruff use their **default** line length (no override configured).
  pytest `asyncio_mode = "auto"`.
- Config is JSON with auto-discovery: `--config` flag → `/etc/kryten-webqueue/config.json` →
  `./config.json`. Keep `config.example.json` in sync; never hardcode secrets, keys, or
  MediaCMS/api-gate URLs. Notable keys: `secret_key` (JWT), `port` (default 2010),
  `api_gate_url`/`api_gate_token`, `mediacms_url`/`mediacms_token`,
  `mediacms_manage_all_media`, `tmdb_api_key`/`omdb_api_key`, `db_path`.
- **High-stakes surfaces**: DB migrations, enrichment classify/title logic (affects the whole
  catalog; needs a `force` re-run to re-apply), MediaCMS write paths, and catalog sync
  (REPLACE semantics). Flag and version changes to these.
- Version lives only in `pyproject.toml [project] version`. Update `CHANGELOG.md`
  (Keep-a-Changelog + SemVer, ISO dates) for every versioned change.
- Commit prefixes: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `ci:`.
  Branches: `feature/…`, `fix/…`.

## Deployment
Deployed via **pipx** on `grindhouse.local` as the `kryten-webqueue` systemd service
(uvicorn on `port`, behind nginx; `deploy/` ships the unit + nginx config). Release flow:
1. Bump `version` in `pyproject.toml`, update `CHANGELOG.md`.
2. Commit, `git tag vX.Y.Z`, `git push && git push origin vX.Y.Z` (also published to PyPI).
3. Install the tag and cycle the service:
   ```
   ssh kryten@grindhouse.local "sudo systemctl stop kryten-webqueue; \
     pipx runpip kryten-webqueue install 'git+https://github.com/grobertson/kryten-webqueue.git@vX.Y.Z'; \
     sudo systemctl start kryten-webqueue; systemctl is-active kryten-webqueue"
   ```
Trigger enrichment/sync jobs via `POST /admin/jobs/catalog_enrich/run` (admin session
required; jobs are single-flight — restart the service to clear a stuck run).
