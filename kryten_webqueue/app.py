import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from . import __version__
from .config import Config
from .catalog.db import Database
from .api_gate.client import ApiGateClient
from .catalog.sync import CatalogSync
from .jobs import JobManager
from .jobs.job_scheduler import JobScheduler
from .catalog.images import CoverArtResolver
from .queue.shadow import QueueShadow
from .queue.poller import StatePoller
from .queue.completion import CompletionRecorder
from .queue.race_poller import RacePoller
from .queue.presence import PresenceRefundMonitor
from .promos.director import PromoDirector
from .ws.manager import WebSocketManager
from .playlists.scheduler import PlaylistScheduler
from .auth.rate_limit import QuotaLimiter, RateLimiter

from .routes.auth import router as auth_router
from .routes.catalog import router as catalog_router
from .routes.queue import router as queue_router
from .routes.user import router as user_router
from .routes.admin_playlists import router as admin_playlists_router
from .routes.admin_schedules import router as admin_schedules_router
from .routes.admin_queue import router as admin_queue_router
from .routes.admin_jobs import router as admin_jobs_router
from .routes.admin_job_schedules import router as admin_job_schedules_router
from .routes.admin_catalog import router as admin_catalog_router
from .routes.admin_promos import router as admin_promos_router
from .routes.admin_moderation import router as admin_moderation_router
from .routes.feedback import router as feedback_router
from .routes.admin_feedback import router as admin_feedback_router
from .routes.devices import router as devices_router
from .routes.public_api import router as public_api_router
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
    # Self-heal: purge any recently-played hide state recorded for promos/bumpers
    # (they are excluded from the public catalog and must never be hidden by the
    # recently-played rules). Cheap and idempotent; also cleans rows written by
    # older builds that classified promos only by pool membership.
    purged = await db.purge_promo_hide_state()
    if purged["completions"]:
        logger.info(
            "Purged promo recently-played state: %d completion(s)",
            purged["completions"],
        )
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
    job_manager = JobManager(db, api_gate=api_gate, config=config, cover_art=cover_art)

    # Reimplemented cmsutils enrichment jobs (vendored, run off-loop). These
    # register regardless of optional deps; a missing dep fails the run fast
    # with a clear message rather than crashing startup.
    from .jobs.tasks import (
        catalog_enrich_job,
        catalog_blackout_job,
        device_key_ban_reconcile_job,
        fetch_job,
        fetch_queue_add_job,
        fetch_queue_drain_job,
        fetchurls_job,
        motd_posters_job,
        tmdb_index_refresh_job,
        tmdb_coverage_report_job,
        CATALOG_ENRICH_SCHEMA,
        CATALOG_BLACKOUT_SCHEMA,
        DEVICE_KEY_BAN_RECONCILE_SCHEMA,
        FETCH_SCHEMA,
        FETCH_QUEUE_ADD_SCHEMA,
        FETCH_QUEUE_DRAIN_SCHEMA,
        FETCHURLS_SCHEMA,
        MOTD_POSTERS_SCHEMA,
        TMDB_INDEX_REFRESH_SCHEMA,
        TMDB_COVERAGE_REPORT_SCHEMA,
    )

    job_manager.register(
        "catalog_enrich",
        catalog_enrich_job,
        label="Catalog Enrichment Pipeline",
        schema=CATALOG_ENRICH_SCHEMA,
    )
    job_manager.register(
        "fetch", fetch_job, label="Fetch (download → MediaCMS)", schema=FETCH_SCHEMA
    )
    job_manager.register(
        "fetch_queue_add",
        fetch_queue_add_job,
        label="Fetch Queue — Add URL(s)",
        schema=FETCH_QUEUE_ADD_SCHEMA,
    )
    job_manager.register(
        "fetch_queue_drain",
        fetch_queue_drain_job,
        label="Fetch Queue — Process",
        schema=FETCH_QUEUE_DRAIN_SCHEMA,
    )
    job_manager.register(
        "fetchurls",
        fetchurls_job,
        label="Fetch URLs (weekend workbook)",
        schema=FETCHURLS_SCHEMA,
    )
    job_manager.register(
        "catalog_blackout",
        catalog_blackout_job,
        label="Catalog Blackout (hide upcoming-weekend items)",
        schema=CATALOG_BLACKOUT_SCHEMA,
    )
    job_manager.register(
        "device_key_ban_reconcile",
        device_key_ban_reconcile_job,
        label="Device Key Ban Reconcile (revoke banned users' API keys)",
        schema=DEVICE_KEY_BAN_RECONCILE_SCHEMA,
    )
    job_manager.register(
        "motd_posters",
        motd_posters_job,
        label="MOTD Posters (weekend poster grid)",
        schema=MOTD_POSTERS_SCHEMA,
    )
    job_manager.register(
        "tmdb_index_refresh",
        tmdb_index_refresh_job,
        label="TMDB Index Refresh (rebuild local index from dumps)",
        schema=TMDB_INDEX_REFRESH_SCHEMA,
    )
    job_manager.register(
        "tmdb_coverage_report",
        tmdb_coverage_report_job,
        label="TMDB Coverage Report (identity resolution summary)",
        schema=TMDB_COVERAGE_REPORT_SCHEMA,
    )
    from .jobs.rehost_emotes import rehost_emotes_job, REHOST_EMOTES_SCHEMA

    job_manager.register(
        "rehost_emotes",
        rehost_emotes_job,
        label="Rehost Emotes (download & serve from dropsugar.co)",
        schema=REHOST_EMOTES_SCHEMA,
    )
    app.state.job_manager = job_manager

    # Recover interrupted fetch-queue downloads. Any item left 'running' was cut
    # off by a crash/restart (the running flag isn't durable); flip it back to
    # 'pending' so the drain re-attempts it, then auto-start the drain if
    # anything is pending so downloads resume without an admin re-triggering it.
    requeued = await db.reset_running_fetch_items()
    if requeued:
        logger.warning(
            "Re-queued %d interrupted fetch-queue item(s) after restart", requeued
        )
    if await db.count_fetch_queue_pending() > 0 and not job_manager.is_running(
        "fetch_queue_drain"
    ):
        try:
            await job_manager.run("fetch_queue_drain", triggered_by="startup-recovery")
            logger.info("Auto-started fetch_queue_drain to resume pending downloads")
        except Exception:  # noqa: BLE001 - best-effort; admin can re-trigger
            logger.debug(
                "Could not auto-start fetch_queue_drain at startup", exc_info=True
            )

    # Cron-based job scheduler (persists schedules to job_schedules table)
    job_scheduler = JobScheduler(db, job_manager)
    # Seed a default schedule for the security-sensitive ban-reconcile job so it
    # runs periodically out-of-the-box. Only seeded when absent, so an admin's
    # cron/active changes are preserved (disable it by deactivating, not
    # deleting — a deleted row is re-seeded on next startup).
    if await db.get_job_schedule("device_key_ban_reconcile") is None:
        await db.upsert_job_schedule(
            "device_key_ban_reconcile",
            "*/15 * * * *",
            label="Device Key Ban Reconcile (revoke banned users' API keys)",
            created_by="system",
        )
    await job_scheduler.start()
    app.state.job_scheduler = job_scheduler

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
        api_gate=api_gate,
        db=db,
        shadow=shadow,
        config=config.promos,
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
    # Device linking: throttle pad generation (per user) and redemption (per IP).
    app.state.device_link_rate_limiter = RateLimiter(
        max_requests=10, window_seconds=600
    )
    app.state.device_exchange_rate_limiter = RateLimiter(
        max_requests=10, window_seconds=600
    )
    # Hard per-user submission quota for feedback and suggestions: 2/day, 6/week
    # (namespaced keys, checked in addition to the short-burst limiter above).
    app.state.feedback_quota_limiter = QuotaLimiter(
        [("day", 2, 86_400), ("week", 6, 604_800)]
    )

    # Playlist scheduler
    scheduler = PlaylistScheduler(
        db=db,
        api_gate=api_gate,
        shadow=shadow,
        ws_manager=ws_manager,
        add_delay_sec=config.playlist_bulk_add_delay_sec,
        add_max_retries=config.playlist_bulk_add_max_retries,
        promo_director=promo_director,
    )
    await scheduler.start()
    app.state.scheduler = scheduler

    # Presence-based cancel/refund monitor
    presence_monitor = PresenceRefundMonitor(
        api_gate=api_gate,
        shadow=shadow,
        db=db,
        ws_manager=ws_manager,
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
                await db._execute(
                    """
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
                """
                )
            except Exception as e:
                logger.error(f"Immutability expiry error: {e}")

    async def _emote_rehost_loop():
        interval_hours = config.emote_rehost.check_interval_hours
        if not config.emote_rehost.enabled or interval_hours <= 0:
            return
        interval = interval_hours * 3600
        # Mirror catalog_sync pattern: don't run immediately on startup.
        while True:
            await asyncio.sleep(interval)
            try:
                await job_manager.run("rehost_emotes", triggered_by="scheduler")
            except Exception as e:
                logger.exception("Emote rehost periodic run error: %s", e)

    bg_tasks = [
        asyncio.create_task(_catalog_sync_loop()),
        asyncio.create_task(_otp_cleanup_loop()),
        asyncio.create_task(_immutability_expiry_loop()),
        asyncio.create_task(_emote_rehost_loop()),
    ]

    logger.info(
        f"kryten-webqueue v{__version__} started on {config.host}:{config.port}"
    )

    yield

    # Shutdown — order matters: cancel in-flight work before closing resources.
    for task in bg_tasks:
        task.cancel()
    await asyncio.gather(*bg_tasks, return_exceptions=True)
    await job_manager.stop()  # cancel running jobs while DB/client still open
    await job_scheduler.stop()
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
    app.include_router(admin_job_schedules_router)
    app.include_router(admin_catalog_router)
    app.include_router(admin_promos_router)
    app.include_router(admin_moderation_router)
    app.include_router(feedback_router)
    app.include_router(admin_feedback_router)
    app.include_router(devices_router)
    app.include_router(public_api_router)
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
