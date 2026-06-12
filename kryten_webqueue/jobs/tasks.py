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
    return await _run_vendored(
        "kryten_webqueue.integrations.cmsutils.enrichtitles", params, ctx,
        deps=["requests"],
    )


async def enrichmeta_job(params: dict, ctx):
    return await _run_vendored(
        "kryten_webqueue.integrations.cmsutils.enrichmeta", params, ctx,
        deps=["requests"],
    )


async def enrichtv_job(params: dict, ctx):
    return await _run_vendored(
        "kryten_webqueue.integrations.cmsutils.enrichtv", params, ctx,
        deps=["requests"],
    )


# ── Fetch job (yt-pipe downloader) ─────────────────────────────────────────────

def _manifest_url_for(config, token: str) -> str:
    """Build the CyTube manifest URL for a freshly uploaded item (mirrors sync)."""
    base = config.mediacms_url.rstrip("/")
    return f"{base}/api/v1/media/cytube/{token}.json?format=json"


async def fetch_job(params: dict, ctx):
    """Download a URL to MediaCMS and optionally append results to a playlist."""
    result = await _run_vendored(
        "kryten_webqueue.integrations.ytpipe.downloader", params, ctx,
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
            result.setdefault("errors", []).append("nothing uploaded to add to playlist")
        for token in tokens:
            await ctx.db.append_playlist_item(playlist_id, {
                "media_type": "cm",
                "media_id": _manifest_url_for(ctx.config, token),
                "title": None,
                "duration_sec": None,
            })
        result["added_to_playlist"] = playlist_id
    return result


# ── fetchurls job ──────────────────────────────────────────────────────────────

async def _import_section_as_playlist(ctx, name: str, lines: list[str], triggered_by: str) -> dict | None:
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
            name=name, description=None, is_immutable=True, created_by=triggered_by,
        )
    await ctx.db.replace_playlist_items(playlist_id, items)
    return {"id": playlist_id, "name": name, "count": len(items)}


async def fetchurls_job(params: dict, ctx):
    """Resolve the upcoming-weekend workbook, then import each section playlist.

    Sections map to the fixed playlists by their human label:
      friday → "Friday Night", saturday-morning → "Saturday Morning",
      saturday-night → "Saturday Night".
    """
    result = await _run_vendored(
        "kryten_webqueue.integrations.cmsutils.fetchurls", params, ctx,
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
    result["imported_playlists"] = imported
    result.pop("section_lines", None)  # keep the persisted detail compact
    result.pop("section_labels", None)
    return result


# ── Schemas (rendered by the admin Run modal, validated by JobManager) ─────────

ENRICHTITLES_SCHEMA = [
    {"name": "dry_run", "type": "bool", "default": False, "label": "Dry run (report only)"},
    {"name": "limit", "type": "int", "default": None, "label": "Limit"},
    {"name": "days", "type": "int", "default": None, "label": "Only last N days"},
]

ENRICHMETA_SCHEMA = [
    {"name": "dry_run", "type": "bool", "default": False, "label": "Dry run (report only)"},
    {"name": "limit", "type": "int", "default": None, "label": "Limit"},
    {"name": "days", "type": "int", "default": None, "label": "Only last N days"},
    {"name": "tubi_upgrade", "type": "bool", "default": False, "label": "Re-enrich Tubi items"},
    {"name": "min_score", "type": "int", "default": 50, "label": "Min score threshold"},
    {"name": "min_duration", "type": "int", "default": 3600, "label": "Min duration (sec)"},
    {"name": "delay", "type": "float", "default": 0.25, "label": "API delay (sec)"},
]

ENRICHTV_SCHEMA = [
    {"name": "dry_run", "type": "bool", "default": False, "label": "Dry run (report only)"},
    {"name": "limit", "type": "int", "default": None, "label": "Limit"},
    {"name": "days", "type": "int", "default": None, "label": "Only last N days"},
    {"name": "min_score", "type": "int", "default": 50, "label": "Min score threshold"},
    {"name": "min_duration", "type": "int", "default": 600, "label": "Min duration (sec)"},
    {"name": "max_duration", "type": "int", "default": 3599, "label": "Max duration (sec)"},
    {"name": "delay", "type": "float", "default": 0.25, "label": "API delay (sec)"},
]

FETCH_SCHEMA = [
    {"name": "url", "type": "string", "required": True, "label": "Source URL"},
    {"name": "quality", "type": "enum", "default": "medium",
     "options": ["best", "good", "medium"], "label": "Quality"},
    {"name": "max_videos", "type": "int", "default": 50, "label": "Max videos (playlists)"},
    {"name": "add_to_playlist", "type": "playlist", "default": None, "label": "Add to playlist"},
]

FETCHURLS_SCHEMA = [
    {"name": "section", "type": "enum", "default": "all",
     "options": ["all", "friday", "saturday-night", "saturday-morning"], "label": "Section"},
    {"name": "dry_run", "type": "bool", "default": False, "label": "Dry run (resolve only)"},
    {"name": "validate", "type": "bool", "default": True, "label": "Validate existing URLs"},
    {"name": "writeback", "type": "bool", "default": True, "label": "Write resolved URLs back to SharePoint (col F)"},
    {"name": "workbook_path", "type": "string", "default": None, "label": "Local workbook path (override SharePoint)"},
]
