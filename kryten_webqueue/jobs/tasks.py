"""Async wrappers that drive the vendored cmsutils / yt-pipe tools as jobs.

Each vendored tool exposes a synchronous ``run(params, *, config, progress)``.
These wrappers run that blocking work off the event loop via
``asyncio.to_thread`` and bridge the tool's sync ``progress`` callback back to
the loop so it can update the ``job_runs`` row through the async
``JobContext.progress``.

Jobs whose optional dependencies are missing register normally but fail fast
with a clear message instead of crashing startup.
"""

import asyncio
import functools
import importlib
import json
import logging

from .manager import JobError

logger = logging.getLogger(__name__)


def _require(module_name: str, *, extra: str = "jobs"):
    """Import a job dependency, raising a clear error if it's not installed."""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            f"Dependency '{module_name}' is not installed. "
            f"Install the optional extra: pip install 'kryten-webqueue[{extra}]'"
        ) from exc


def _thread_safe_progress(ctx, loop):
    """Return a sync ``progress(detail)`` that schedules ctx.progress on the loop.

    Fire-and-forget: the worker thread does not block on the DB write.
    """

    def _progress(detail: dict) -> None:
        try:
            asyncio.run_coroutine_threadsafe(ctx.progress(detail), loop)
        except Exception:  # noqa: BLE001 - progress is best-effort
            logger.debug("progress bridge failed", exc_info=True)

    return _progress


async def _run_vendored(module_path: str, params: dict, ctx, *, deps: list[str]):
    """Import a vendored module, verify deps, and run its ``run()`` off-loop.

    The vendored tools raise ``RuntimeError`` for expected, user-facing failures
    (missing/unauthenticated workbook, a sheet that isn't present, a missing
    optional dependency). Surface those as :class:`JobError` so the run history
    shows a clean, actionable message instead of a stack trace.
    """
    try:
        for dep in deps:
            _require(dep)
        module = importlib.import_module(module_path)
        loop = asyncio.get_running_loop()
        progress = _thread_safe_progress(ctx, loop)
        fn = functools.partial(module.run, params, config=ctx.config, progress=progress)
        return await asyncio.to_thread(fn)
    except RuntimeError as exc:
        raise JobError(str(exc)) from exc


# ── Enrich jobs ────────────────────────────────────────────────────────────────


async def enrichtitles_job(params: dict, ctx):
    params.setdefault("steps", "classify,title")
    return await catalog_enrich_job(params, ctx)


async def enrichmeta_job(params: dict, ctx):
    params.setdefault("steps", "classify,meta")
    return await catalog_enrich_job(params, ctx)


async def enrichtv_job(params: dict, ctx):
    params.setdefault("steps", "classify,meta")
    return await catalog_enrich_job(params, ctx)


async def catalog_enrich_job(params: dict, ctx) -> dict:
    """Unified catalog enrichment pipeline.

    params:
      steps     comma-separated list or "all" (default)
      tokens    comma-separated friendly_tokens or "all" (default)
      force     "1"|"true" — bypass cached state
      dry_run   "1"|"true" — preview without writing
      limit     integer — max items per step
      min_score integer — description quality threshold (default 50)
    """
    from ..catalog.enrichment import CatalogEnrichmentPipeline

    pipeline = CatalogEnrichmentPipeline(
        db=ctx.db,
        config=ctx.config,
        cover_art=getattr(ctx, "cover_art", None),
    )
    step_param = params.get("steps", "all")
    steps = None if step_param == "all" else [s.strip() for s in step_param.split(",")]
    token_param = (params.get("tokens") or "").strip()
    tokens = (
        None
        if not token_param or token_param == "all"
        else [t.strip() for t in token_param.replace(",", " ").split() if t.strip()]
    )

    report = await pipeline.run(
        steps=steps,
        tokens=tokens,
        force=str(params.get("force", "")).lower() in ("1", "true"),
        dry_run=str(params.get("dry_run", "")).lower() in ("1", "true"),
        limit=int(params["limit"]) if params.get("limit") else None,
        min_score=int(params.get("min_score", 50)),
        ctx=ctx,
    )
    return report.to_dict()


CATALOG_ENRICH_SCHEMA = [
    {
        "name": "steps",
        "label": "Pipeline steps",
        "type": "enum",
        "default": "all",
        "required": False,
        "options": [
            {
                "value": "all",
                "label": "Full pipeline — sync → classify → identify → title → meta → art → tags",
            },
            {
                "value": "classify,identify,meta,art,tags",
                "label": "Enrichment only — classify, identify, fetch metadata, art, and tags (no sync)",
            },
            {
                "value": "classify,identify",
                "label": "Identify only — resolve TMDB id + IMDb tt# for each item",
            },
            {"value": "art", "label": "Art only — (re-)fetch poster art"},
            {
                "value": "classify,art",
                "label": "Classify + art — re-detect content type then fetch art",
            },
            {
                "value": "classify,meta",
                "label": "Classify + metadata — descriptions and credits only",
            },
            {
                "value": "tags",
                "label": "Tags only — push genres/MPAA/hosted-show tags to CMS",
            },
            {
                "value": "title",
                "label": "Title cleanup only — normalise and push corrected titles",
            },
            {"value": "sync", "label": "Sync only — pull latest items from MediaCMS"},
            {
                "value": "classify",
                "label": "Classify only — detect hosted shows, TV episodes, etc.",
            },
        ],
        "help": "Choose which parts of the pipeline to run. 'Full pipeline' is the normal nightly run.",
    },
    {
        "name": "tokens",
        "label": "Specific items (optional)",
        "type": "string",
        "default": None,
        "required": False,
        "placeholder": "Leave blank for entire catalog",
        "help": (
            "Paste one or more MediaCMS media IDs (the short alphanumeric codes in item URLs, "
            "e.g. AbCd1234) separated by commas or spaces. Leave blank to process the entire catalog."
        ),
    },
    {
        "name": "force",
        "label": "Force re-run",
        "type": "bool",
        "default": False,
        "required": False,
        "help": "Re-process items even if they were enriched before. Useful after fixing a bug or adding a new hosted-show pattern.",
    },
    {
        "name": "dry_run",
        "label": "Dry run (preview only)",
        "type": "bool",
        "default": False,
        "required": False,
        "help": "Walk through the pipeline and log what would change, but write nothing to the database or CMS.",
    },
    {
        "name": "limit",
        "label": "Item limit per step",
        "type": "int",
        "default": None,
        "required": False,
        "placeholder": "blank = no limit",
        "help": "Stop after processing this many items per step. Handy for a quick test run before committing to the full catalog.",
    },
    {
        "name": "min_score",
        "label": "Description quality threshold",
        "type": "int",
        "default": 50,
        "required": False,
        "help": (
            "Skip the metadata step for items whose description already scores at or above this value "
            "(0–130 scale; a fully-enriched description scores ~100). Lower this or use Force re-run "
            "to refresh already-enriched items."
        ),
    },
]


# ── TMDB local index refresh ───────────────────────────────────────────────────


async def tmdb_index_refresh_job(params: dict, ctx) -> dict:
    """Rebuild the local TMDB index from the daily ID-export dumps.

    params:
      source    "local" (default) — build from unpacked dumps on disk
      dump_dir  directory holding the dumps; must resolve under the configured
                ``tmdb_index_source_dir`` (defaults to it when blank)
      kinds     "movies,tv" (default) | "movies" | "tv" | "all"
    """
    import os.path

    from ..catalog.tmdb_index import build_index

    source = params.get("source", "local")
    if source != "local":
        raise JobError(f"Unsupported source {source!r}; only 'local' is available")

    allowed_root = (ctx.config.tmdb_index_source_dir or "").strip()
    dump_dir = (params.get("dump_dir") or allowed_root).strip()
    if not dump_dir:
        raise JobError(
            "No dump directory configured. Set 'tmdb_index_source_dir' in config "
            "or pass a dump_dir under it."
        )
    # Confine dump_dir to the configured root to prevent arbitrary path access.
    if allowed_root:
        root = os.path.realpath(allowed_root)
        target = os.path.realpath(dump_dir)
        if target != root and not target.startswith(root + os.sep):
            raise JobError(f"dump_dir must be within {allowed_root!r}")
        dump_dir = target

    kinds = params.get("kinds", "movies,tv")
    index_path = ctx.config.tmdb_index_path

    loop = asyncio.get_running_loop()
    progress = _thread_safe_progress(ctx, loop)
    try:
        stats = await asyncio.to_thread(
            build_index, dump_dir, index_path, kinds, progress=progress
        )
    except ValueError as exc:
        raise JobError(str(exc)) from exc

    return {
        "source_date": stats.source_date,
        "built_at": stats.built_at,
        "elapsed_sec": round(stats.elapsed_sec, 1),
        "counts": stats.counts,
        "index_path": index_path,
    }


async def tmdb_coverage_report_job(params: dict, ctx) -> dict:
    """Summarise identity-resolution coverage across the catalog (no network)."""
    from ..catalog.tmdb_index.coverage import build_coverage_report

    report = await build_coverage_report(ctx.db)
    return report.to_dict()


TMDB_COVERAGE_REPORT_SCHEMA: list[dict] = []


TMDB_INDEX_REFRESH_SCHEMA = [
    {
        "name": "source",
        "label": "Source",
        "type": "enum",
        "default": "local",
        "required": False,
        "options": [
            {
                "value": "local",
                "label": "Local dumps — build from unpacked files on disk",
            },
        ],
        "help": "Where to read the TMDB ID-export dumps from.",
    },
    {
        "name": "dump_dir",
        "label": "Dump directory (optional)",
        "type": "string",
        "default": None,
        "required": False,
        "placeholder": "blank = configured tmdb_index_source_dir",
        "help": (
            "Directory holding the unpacked TMDB dump files. Must be within the "
            "configured tmdb_index_source_dir. Leave blank to use it directly."
        ),
    },
    {
        "name": "kinds",
        "label": "Kinds to index",
        "type": "enum",
        "default": "movies,tv",
        "required": False,
        "options": [
            {"value": "movies,tv", "label": "Movies + TV (recommended)"},
            {"value": "movies", "label": "Movies only"},
            {"value": "tv", "label": "TV only"},
            {
                "value": "all",
                "label": "Everything (movies, tv, people, keywords, companies, networks)",
            },
        ],
        "help": "Which dump types to load into the index.",
    },
]


# ── Fetch job (yt-pipe downloader) ─────────────────────────────────────────────


def _manifest_url_for(config, token: str) -> str:
    """Build the CyTube manifest URL for a freshly uploaded item (mirrors sync)."""
    base = config.mediacms_url.rstrip("/")
    return f"{base}/api/v1/media/cytube/{token}.json?format=json"


async def _run_single_fetch(params: dict, ctx) -> dict:
    """Download one URL and optionally add results to a playlist. Shared by fetch_job and drain."""
    result = await _run_vendored(
        "kryten_webqueue.integrations.ytpipe.downloader",
        params,
        ctx,
        deps=["yt_dlp"],
    )

    add_to = params.get("add_to_playlist")
    result["added_to_playlist"] = None
    if add_to:
        try:
            playlist_id = int(add_to)
        except (TypeError, ValueError):
            result.setdefault("errors", []).append(f"invalid playlist id: {add_to!r}")
            return result
        playlist = await ctx.db.get_saved_playlist(playlist_id)
        if not playlist:
            result.setdefault("errors", []).append(f"playlist {playlist_id} not found")
            return result
        tokens = result.get("tokens") or []
        if not tokens:
            result.setdefault("errors", []).append(
                "nothing uploaded to add to playlist"
            )
        for token in tokens:
            await ctx.db.append_playlist_item(
                playlist_id,
                {
                    "media_type": "cm",
                    "media_id": _manifest_url_for(ctx.config, token),
                    "title": None,
                    "duration_sec": None,
                },
            )
        result["added_to_playlist"] = playlist_id
    return result


async def fetch_job(params: dict, ctx):
    """Download a URL to MediaCMS and optionally append results to a playlist."""
    return await _run_single_fetch(params, ctx)


# ── fetchurls job ──────────────────────────────────────────────────────────────


async def _import_section_as_playlist(
    ctx, name: str, lines: list[str], triggered_by: str
) -> dict | None:
    """Import resolved ``cm:`` lines into a fixed, well-known saved playlist.

    The three fetchurls playlists ("Friday Night", "Saturday Morning",
    "Saturday Night") pre-exist and are immutable. We match by **name only**
    (any creator), replace their items in place (idempotent re-runs), and
    preserve their existing immutability. If a playlist is missing it is created
    as immutable so a recreated playlist keeps the reserved status.
    """
    from ..playlists.importer import import_playlist_text

    if not lines:
        return None
    parsed = await import_playlist_text(
        ctx.db, "\n".join(lines), mediacms_url=ctx.config.mediacms_url
    )
    items = parsed.get("items") or []
    if not items:
        return None

    existing = await ctx.db.get_playlist_by_name_any(name)
    if existing:
        playlist_id = existing["id"]
    else:
        playlist_id = await ctx.db.create_saved_playlist(
            name=name,
            description=None,
            is_immutable=True,
            created_by=triggered_by,
        )
    await ctx.db.replace_playlist_items(playlist_id, items)
    return {"id": playlist_id, "name": name, "count": len(items)}


async def fetchurls_job(params: dict, ctx):
    """Resolve the upcoming-weekend workbook, then import each section playlist.

    Sections map to the fixed playlists by their human label:
      friday → "Friday Night", saturday-morning → "Saturday Morning",
      saturday-night → "Saturday Night", sunday-morning → "Sunday Morning",
      sunday-daytime → "Sunday Daytime".
    """
    result = await _run_vendored(
        "kryten_webqueue.integrations.cmsutils.fetchurls",
        params,
        ctx,
        deps=["openpyxl", "yaml", "requests"],
    )

    if result.get("dry_run"):
        return result

    triggered_by = ctx.triggered_by or "system"
    labels = result.get("section_labels") or {}
    imported = []
    for slug, lines in (result.get("section_lines") or {}).items():
        name = labels.get(slug) or slug
        info = await _import_section_as_playlist(ctx, name, lines, triggered_by)
        if info:
            imported.append(info["name"])
            logger.info(
                "fetchurls: imported %d item(s) into '%s'", info["count"], info["name"]
            )
    result["imported_playlists"] = imported

    # Per-section resolved/failed summary for the process log.
    sheet = result.get("sheet", "?")
    for label, counts in (result.get("section_summary") or {}).items():
        logger.info(
            "fetchurls[%s] section '%s': resolved %d / failed %d",
            sheet,
            label,
            counts.get("resolved", 0),
            counts.get("failed", 0),
        )

    # Surface each failing URL (with its Excel row) at WARNING, and keep a
    # compact copy in the job result so the admin "Detail" column shows it.
    failure_details = result.get("failure_details") or []
    if failure_details:
        logger.warning(
            "fetchurls[%s]: %d URL(s) failed to resolve", sheet, len(failure_details)
        )
        for f in failure_details:
            logger.warning(
                "fetchurls[%s]   [%s row %s] %s — %s",
                sheet,
                f.get("section", "?"),
                f.get("row", "?"),
                f.get("url", ""),
                f.get("note", ""),
            )
        # Keep at most 25 in the persisted detail to stay compact.
        result["failures_detail"] = [
            {
                "section": f.get("section"),
                "row": f.get("row"),
                "url": f.get("url"),
                "note": f.get("note"),
            }
            for f in failure_details[:25]
        ]

    # Trim the bulky intermediates from the persisted detail.
    result.pop("section_lines", None)
    result.pop("section_labels", None)
    result.pop("failure_details", None)

    pm = result.get("played_movies") or {}
    if pm.get("found") or pm.get("added") or pm.get("failed"):
        logger.info(
            "fetchurls[%s] played movies: found %d, added %d, skipped %d, failed %d",
            result.get("sheet", "?"),
            pm.get("found", 0),
            pm.get("added", 0),
            pm.get("skipped", 0),
            pm.get("failed", 0),
        )

    return result


# ── Schemas (rendered by the admin Run modal, validated by JobManager) ─────────

ENRICHTITLES_SCHEMA = [
    {
        "name": "dry_run",
        "type": "bool",
        "default": False,
        "label": "Dry run (report only)",
    },
    {"name": "limit", "type": "int", "default": None, "label": "Limit"},
    {"name": "days", "type": "int", "default": None, "label": "Only last N days"},
]

ENRICHMETA_SCHEMA = [
    {
        "name": "dry_run",
        "type": "bool",
        "default": False,
        "label": "Dry run (report only)",
    },
    {"name": "limit", "type": "int", "default": None, "label": "Limit"},
    {"name": "days", "type": "int", "default": None, "label": "Only last N days"},
    {
        "name": "tubi_upgrade",
        "type": "bool",
        "default": False,
        "label": "Re-enrich Tubi items",
    },
    {"name": "min_score", "type": "int", "default": 50, "label": "Min score threshold"},
    {
        "name": "min_duration",
        "type": "int",
        "default": 3600,
        "label": "Min duration (sec)",
    },
    {"name": "delay", "type": "float", "default": 0.25, "label": "API delay (sec)"},
]

ENRICHTV_SCHEMA = [
    {
        "name": "dry_run",
        "type": "bool",
        "default": False,
        "label": "Dry run (report only)",
    },
    {"name": "limit", "type": "int", "default": None, "label": "Limit"},
    {"name": "days", "type": "int", "default": None, "label": "Only last N days"},
    {"name": "min_score", "type": "int", "default": 50, "label": "Min score threshold"},
    {
        "name": "min_duration",
        "type": "int",
        "default": 600,
        "label": "Min duration (sec)",
    },
    {
        "name": "max_duration",
        "type": "int",
        "default": 3599,
        "label": "Max duration (sec)",
    },
    {"name": "delay", "type": "float", "default": 0.25, "label": "API delay (sec)"},
]

FETCH_SCHEMA = [
    {"name": "url", "type": "string", "required": True, "label": "Source URL"},
    {
        "name": "quality",
        "type": "enum",
        "default": "medium",
        "options": ["best", "good", "medium"],
        "label": "Quality",
    },
    {
        "name": "max_videos",
        "type": "int",
        "default": 50,
        "label": "Max videos (playlists)",
    },
    {
        "name": "add_to_playlist",
        "type": "playlist",
        "default": None,
        "label": "Add to playlist",
    },
]

FETCHURLS_SCHEMA = [
    {
        "name": "section",
        "type": "enum",
        "default": "all",
        "options": [
            "all",
            "friday",
            "saturday-night",
            "saturday-morning",
            "sunday-morning",
            "sunday-daytime",
        ],
        "label": "Section",
    },
    {
        "name": "dry_run",
        "type": "bool",
        "default": False,
        "label": "Dry run (resolve only)",
    },
    {
        "name": "validate",
        "type": "bool",
        "default": True,
        "label": "Validate existing URLs",
    },
    {
        "name": "writeback",
        "type": "bool",
        "default": True,
        "label": "Write resolved URLs back to SharePoint (col F)",
    },
    {
        "name": "workbook_path",
        "type": "string",
        "default": None,
        "label": "Local workbook path (override SharePoint)",
    },
]

MOTD_POSTERS_SCHEMA = [
    {
        "name": "dry_run",
        "type": "bool",
        "default": False,
        "label": "Dry run (resolve titles only, no downloads)",
    },
    {
        "name": "workbook_path",
        "type": "string",
        "default": None,
        "label": "Local workbook path (override SharePoint)",
    },
]


async def motd_posters_job(params: dict, ctx):
    return await _run_vendored(
        "kryten_webqueue.integrations.cmsutils.motdposters",
        params,
        ctx,
        deps=["openpyxl", "requests"],
    )


# ── Fetch queue jobs ───────────────────────────────────────────────────────────


FETCH_QUEUE_ADD_SCHEMA = [
    {
        "name": "urls",
        "type": "textarea",
        "required": True,
        "label": "URL(s)",
        "placeholder": "One URL per line",
        "help": "Paste one or more URLs to download. Each line becomes a separate queue item.",
    },
    {
        "name": "quality",
        "type": "enum",
        "default": "medium",
        "options": ["best", "good", "medium"],
        "label": "Quality",
    },
    {
        "name": "max_videos",
        "type": "int",
        "default": 50,
        "label": "Max videos (playlists)",
    },
    {
        "name": "add_to_playlist",
        "type": "playlist",
        "default": None,
        "label": "Add results to playlist",
    },
]

FETCH_QUEUE_DRAIN_SCHEMA: list[dict] = []


async def fetch_queue_add_job(params: dict, ctx) -> dict:
    """Add one or more URLs to the fetch queue and auto-start the drain if idle."""
    raw = (params.get("urls") or "").strip()
    urls = [u.strip() for u in raw.splitlines() if u.strip()]
    if not urls:
        from .manager import JobError

        raise JobError("At least one URL is required")

    quality = params.get("quality") or "medium"
    max_videos = int(params.get("max_videos") or 50)
    add_to_raw = params.get("add_to_playlist")
    add_to_playlist = int(add_to_raw) if add_to_raw else None

    queued = []
    for url in urls:
        item_id = await ctx.db.enqueue_fetch(
            url=url,
            quality=quality,
            max_videos=max_videos,
            add_to_playlist=add_to_playlist,
            added_by=ctx.triggered_by,
        )
        queued.append({"id": item_id, "url": url})
        logger.info(
            "fetch_queue: enqueued %s (id=%d, by=%s)", url, item_id, ctx.triggered_by
        )

    if ctx.job_manager and not ctx.job_manager.is_running("fetch_queue_drain"):
        try:
            await ctx.job_manager.run(
                "fetch_queue_drain", triggered_by=ctx.triggered_by
            )
            logger.info(
                "fetch_queue: auto-started drain (triggered by %s)", ctx.triggered_by
            )
        except Exception:  # noqa: BLE001
            logger.debug("fetch_queue: could not auto-start drain", exc_info=True)

    return {"queued": len(queued), "items": queued}


async def fetch_queue_drain_job(params: dict, ctx) -> dict:
    """Process all pending fetch-queue items one at a time."""
    processed = 0
    failed = 0

    while True:
        item = await ctx.db.claim_next_fetch_item()
        if not item:
            break

        await ctx.progress(
            {
                "status": "downloading",
                "url": item["url"],
                "processed": processed,
                "failed": failed,
            }
        )
        logger.info(
            "fetch_queue_drain: processing id=%d url=%s", item["id"], item["url"]
        )

        item_params = {
            "url": item["url"],
            "quality": item["quality"],
            "max_videos": item["max_videos"],
            "add_to_playlist": item["add_to_playlist"],
        }
        try:
            result = await _run_single_fetch(item_params, ctx)
            await ctx.db.finish_fetch_item(
                item["id"], status="done", result_json=json.dumps(result)
            )
            processed += 1
            logger.info("fetch_queue_drain: id=%d done", item["id"])
        except asyncio.CancelledError:
            await ctx.db.finish_fetch_item(
                item["id"], status="failed", error="cancelled"
            )
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fetch_queue_drain: id=%d (%s) failed: %s", item["id"], item["url"], exc
            )
            await ctx.db.finish_fetch_item(item["id"], status="failed", error=str(exc))
            failed += 1

    logger.info("fetch_queue_drain: done — processed=%d failed=%d", processed, failed)
    return {"processed": processed, "failed": failed}
