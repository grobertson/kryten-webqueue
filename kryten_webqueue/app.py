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
    job_manager = JobManager(db)
    job_manager.register(
        "catalog_sync",
        catalog_sync.sync,
        label="Catalog Sync",
    )
    app.state.job_manager = job_manager

    # WebSocket manager
    ws_manager = WebSocketManager()
    app.state.ws_manager = ws_manager

    # Queue shadow
    shadow = QueueShadow(db)
    await shadow.load_from_db()
    app.state.shadow = shadow

    # State poller
    poller = StatePoller(
        api_gate=api_gate,
        shadow=shadow,
        ws_manager=ws_manager,
        db=db,
        interval=config.state_poll_interval_sec,
    )
    await poller.start()
    app.state.poller = poller

    # Rate limiter
    app.state.rate_limiter = RateLimiter()

    # Playlist scheduler
    scheduler = PlaylistScheduler(db=db, api_gate=api_gate, shadow=shadow, ws_manager=ws_manager)
    await scheduler.start()
    app.state.scheduler = scheduler

    # Background workers
    async def _catalog_sync_loop():
        interval = config.catalog_sync_interval_hours * 3600
        while True:
            try:
                await catalog_sync.sync()
            except Exception as e:
                logger.exception(f"Catalog sync error: {type(e).__name__}: {e}")
            await asyncio.sleep(interval)

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

    # Shutdown
    for task in bg_tasks:
        task.cancel()
    await poller.stop()
    await scheduler.stop()
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
