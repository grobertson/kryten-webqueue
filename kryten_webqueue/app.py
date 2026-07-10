import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from . import __version__
from .config import Config
from .catalog.db import Database
from .api_gate.client import ApiGateClient
from .catalog.sync import CatalogSync
from .jobs import JobManager
from .catalog.images import CoverArtResolver
from .queue.shadow import QueueShadow
from .queue.poller import StatePoller
from .queue.completion import CompletionRecorder
from .queue.race_poller import RacePoller
from .queue.presence import PresenceRefundMonitor
from .promos.director import PromoDirector
from .ws.manager import WebSocketManager
from .playlists.scheduler import PlaylistScheduler
from .auth.rate_limit import RateLimiter

from .routes.auth import router as auth_router
from .routes.catalog import router as catalog_router
from .routes.queue import router as queue_router
from .routes.user import router as user_router
from .routes.admin_playlists import router as admin_playlists_router
from .routes.admin_schedules import router as admin_schedules_router
from .routes.admin_queue import router as admin_queue_router
from .routes.admin_jobs import router as admin_jobs_router
from .routes.admin_catalog import router as admin_catalog_router
from .routes.admin_promos import router as admin_promos_router
from .routes.admin_moderation import router as admin_moderation_router
from .routes.feedback import router as feedback_router
from .routes.admin_feedback import router as admin_feedback_router
from .routes.pages import router as pages_router
from .ws.handler import router as ws_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: Config = app.state.config

    # Database
    db = Database(config.db_path)
    await db.connect()
    await db.run_migrations()
    # Any job run still marked 'running' is an orphan from a prior crash/restart
    # (the running flag is in-memory only). Reconcile before registering jobs.
    orphaned = await db.reconcile_orphaned_job_runs()
    if orphaned:
        logger.warning("Reconciled %d orphaned job run(s) to 'interrupted'", orphaned)
    app.state.db = db

    # API Gate client
    api_gate = ApiGateClient(config.api_gate_url, config.api_gate_token)
    app.state.api_gate = api_gate

    # Cover art resolver (created before sync so it can be passed in)
    cover_art = CoverArtResolver(
        image_dir=config.image_dir,
        placeholder_dir=config.placeholder_dir,
        tmdb_api_key=config.tmdb_api_key,
        omdb_api_key=config.omdb_api_key,
    )
    app.state.cover_art = cover_art

    # Catalog sync
    catalog_sync = CatalogSync(
        mediacms_url=config.mediacms_url,
        mediacms_token=config.mediacms_token,
        db=db,
        cover_art=cover_art,
    )
    app.state.catalog_sync = catalog_sync

    # Generic background job runner (records run history to job_runs)
    job_manager = JobManager(db, api_gate=api_gate, config=config)

    async def _catalog_sync_job(params, ctx):
        # catalog_sync registers with no schema; params/ctx are ignored. The
        # adapter keeps the zero-arg sync compatible with the params/ctx job API.
        return await catalog_sync.sync()

    job_manager.register(
        "catalog_sync",
        _catalog_sync_job,
        label="Catalog Sync",
    )

    # Reimplemented cmsutils enrichment jobs (vendored, run off-loop). These
    # register regardless of optional deps; a missing dep fails the run fast
    # with a clear message rather than crashing startup.
    from .jobs.tasks import (
        enrichtitles_job, enrichmeta_job, enrichtv_job,
        fetch_job, fetchurls_job,
        ENRICHTITLES_SCHEMA, ENRICHMETA_SCHEMA, ENRICHTV_SCHEMA,
        FETCH_SCHEMA, FETCHURLS_SCHEMA,
    )
    job_manager.register("enrichtitles", enrichtitles_job,
                         label="Enrich Titles", schema=ENRICHTITLES_SCHEMA)
    job_manager.register("enrichmeta", enrichmeta_job,
                         label="Enrich Movie Metadata", schema=ENRICHMETA_SCHEMA)
    job_manager.register("enrichtv", enrichtv_job,
                         label="Enrich TV Metadata", schema=ENRICHTV_SCHEMA)
    job_manager.register("fetch", fetch_job,
                         label="Fetch (download → MediaCMS)", schema=FETCH_SCHEMA)
    job_manager.register("fetchurls", fetchurls_job,
                         label="Fetch URLs (weekend workbook)", schema=FETCHURLS_SCHEMA)
    app.state.job_manager = job_manager

    # WebSocket manager
    ws_manager = WebSocketManager()
    app.state.ws_manager = ws_manager

    # Public race-view WebSocket manager (anonymous spectators) + poller.
    race_ws_manager = WebSocketManager()
    app.state.race_ws_manager = race_ws_manager
    race_poller = RacePoller(api_gate=api_gate, ws_manager=race_ws_manager)
    await race_poller.start()
    app.state.race_poller = race_poller

    # Queue shadow
    shadow = QueueShadow(db)
    await shadow.load_from_db()
    app.state.shadow = shadow

    # Promo director (poller-driven; also used synchronously by the pay path)
    promo_director = PromoDirector(
        api_gate=api_gate, db=db, shadow=shadow, config=config.promos,
        add_delay_sec=config.playlist_bulk_add_delay_sec,
        add_max_retries=config.playlist_bulk_add_max_retries,
    )
    app.state.promo_director = promo_director

    # Play-completion recorder (poller-driven; records genuine completions that
    # drive hiding recently-played catalog items from regular users).
    completion_recorder = CompletionRecorder(db=db)
    app.state.completion_recorder = completion_recorder

    # State poller
    poller = StatePoller(
        api_gate=api_gate,
        shadow=shadow,
        ws_manager=ws_manager,
        db=db,
        interval=config.state_poll_interval_sec,
        promo_director=promo_director,
        completion_recorder=completion_recorder,
    )
    await poller.start()
    app.state.poller = poller

    # Rate limiter (OTP requests) plus a more lenient limiter shared by the
    # feedback/suggestion endpoints (per-user, namespaced keys).
    app.state.rate_limiter = RateLimiter()
    app.state.feedback_rate_limiter = RateLimiter(max_requests=12, window_seconds=300)

    # Playlist scheduler
    scheduler = PlaylistScheduler(
        db=db, api_gate=api_gate, shadow=shadow, ws_manager=ws_manager,
        add_delay_sec=config.playlist_bulk_add_delay_sec,
        add_max_retries=config.playlist_bulk_add_max_retries,
        promo_director=promo_director,
    )
    await scheduler.start()
    app.state.scheduler = scheduler

    # Presence-based cancel/refund monitor
    presence_monitor = PresenceRefundMonitor(
        api_gate=api_gate, shadow=shadow, db=db, ws_manager=ws_manager,
        config=config.presence_refund,
    )
    await presence_monitor.start()
    app.state.presence_monitor = presence_monitor

    # Background workers
    async def _catalog_sync_loop():
        interval = config.catalog_sync_interval_hours * 3600
        # Sync on the interval, NOT immediately on startup — a restart should
        # not trigger a catalog sync. Admins can run it on demand from the
        # "Sync Catalog" button.
        while True:
            await asyncio.sleep(interval)
            try:
                await catalog_sync.sync()
            except Exception as e:
                logger.exception(f"Catalog sync error: {type(e).__name__}: {e}")

    async def _otp_cleanup_loop():
        while True:
            await asyncio.sleep(3600)  # every hour
            try:
                await db.cleanup_expired_otps()
            except Exception as e:
                logger.error(f"OTP cleanup error: {e}")

    async def _immutability_expiry_loop():
        while True:
            await asyncio.sleep(300)  # every 5 minutes
            try:
                await db._execute("""
                    UPDATE saved_playlists SET is_immutable = 0
                    WHERE id IN (
                        SELECT sp.id FROM saved_playlists sp
                        JOIN playlist_schedules ps ON ps.playlist_id = sp.id
                        WHERE sp.is_immutable = 1
                          AND ps.immutability_expires_at IS NOT NULL
                          AND ps.immutability_expires_at < datetime('now')
                          AND NOT EXISTS (
                              SELECT 1 FROM playlist_schedules ps2
                              WHERE ps2.playlist_id = sp.id
                                AND ps2.is_active = 1
                                AND (ps2.immutability_expires_at IS NULL OR ps2.immutability_expires_at > datetime('now'))
                          )
                    )
                """)
            except Exception as e:
                logger.error(f"Immutability expiry error: {e}")

    bg_tasks = [
        asyncio.create_task(_catalog_sync_loop()),
        asyncio.create_task(_otp_cleanup_loop()),
        asyncio.create_task(_immutability_expiry_loop()),
    ]

    logger.info(f"kryten-webqueue v{__version__} started on {config.host}:{config.port}")

    yield

    # Shutdown — order matters: cancel in-flight work before closing resources.
    for task in bg_tasks:
        task.cancel()
    await asyncio.gather(*bg_tasks, return_exceptions=True)
    await job_manager.stop()  # cancel running jobs while DB/client still open
    await poller.stop()
    await race_poller.stop()
    await scheduler.stop()
    await presence_monitor.stop()
    await catalog_sync.close()
    await cover_art.close()
    await api_gate.close()
    await db.close()
    logger.info("kryten-webqueue shut down")


def create_app(config: Config) -> FastAPI:
    app = FastAPI(
        title="kryten-webqueue",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.config = config

    # Register routes
    app.include_router(pages_router)
    app.include_router(auth_router)
    app.include_router(catalog_router)
    app.include_router(queue_router)
    app.include_router(user_router)
    app.include_router(admin_playlists_router)
    app.include_router(admin_schedules_router)
    app.include_router(admin_queue_router)
    app.include_router(admin_jobs_router)
    app.include_router(admin_catalog_router)
    app.include_router(admin_promos_router)
    app.include_router(admin_moderation_router)
    app.include_router(feedback_router)
    app.include_router(admin_feedback_router)
    app.include_router(ws_router)

    # Health check
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # Static files
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Cover art images (nginx serves this in production, but also mount here
    # so uvicorn handles it directly when nginx isn't in front)
    image_dir = Path(config.image_dir)
    image_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/images", StaticFiles(directory=str(image_dir)), name="images")

    return app
