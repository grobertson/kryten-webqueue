"""Tests for Sortie 4: ETL Split Script and Concurrency Stress Testing."""

import asyncio
import json
import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.config import DatabaseConfig
from scripts.split_databases import split_database


@pytest.fixture
async def populated_monolith_db(tmp_path):
    """Create a populated legacy monolithic SQLite database with records across all domains."""
    db_file = tmp_path / "legacy_webqueue.db"
    db = Database(str(db_file))
    await db.connect()
    await db.run_migrations()

    # Populate Catalog
    await db.insert_catalog(
        {
            "friendly_token": "film_alien",
            "title": "Alien",
            "description": "In space no one can hear you scream",
            "duration_sec": 7020,
            "manifest_url": "https://dropsugar.com/cytube/film_alien.json",
            "thumbnail_url": "https://dropsugar.com/thumbs/alien.jpg",
            "synced_at": "2026-01-01T00:00:00Z",
        }
    )
    await db.insert_catalog(
        {
            "friendly_token": "film_predator",
            "title": "Predator",
            "description": "If it bleeds we can kill it",
            "duration_sec": 6420,
            "manifest_url": "https://dropsugar.com/cytube/film_predator.json",
            "thumbnail_url": "https://dropsugar.com/thumbs/predator.jpg",
            "synced_at": "2026-01-01T00:00:00Z",
        }
    )

    # Populate Queue
    await db.upsert_shadow_item(
        {
            "uid": 101,
            "position": 1,
            "title": "Alien",
            "friendly_token": "film_alien",
            "media_type": "cm",
            "media_id": "https://dropsugar.com/cytube/film_alien.json",
            "duration_sec": 7020,
            "is_pay": False,
        }
    )
    pl_id = await db.create_saved_playlist(
        name="Sci-Fi Classics",
        description="Great films",
        is_immutable=False,
        created_by="curator",
    )
    await db.append_playlist_items(
        pl_id,
        [{"media_type": "cm", "media_id": "film_alien", "title": "Alien"}],
    )
    await db.record_play_completion(friendly_token="film_alien")

    # Populate Jobs
    run_id = await db.start_job_run("catalog_sync", triggered_by="cron")
    await db.add_job_run_logs(
        run_id,
        [
            ("2026-09-23T10:00:00Z", "INFO", "sync", "Sync started"),
            ("2026-09-23T10:00:05Z", "INFO", "sync", "Processed 2 items"),
        ],
    )
    await db.finish_job_run(run_id, "success", detail=json.dumps({"processed": 2}))

    # Populate Users
    await db.store_otp("groberts", "123456", "2099-01-01T00:00:00Z")
    await db.watchlist_add("groberts", "film_predator")
    await db.add_feedback(username="groberts", body="Love the new layout!")

    await db.close()
    return db_file


async def test_split_databases_etl(populated_monolith_db, tmp_path):
    """Verify split_databases migrates data with 100% parity into 4 distinct databases."""
    data_dir = tmp_path / "partitioned_out"

    # Execute split
    success = await split_database(populated_monolith_db, data_dir)
    assert success is True

    # Verify report
    report_file = data_dir / "split_report.json"
    assert report_file.is_file()
    with open(report_file, encoding="utf-8") as f:
        report = json.load(f)

    assert report["status"] == "success"
    assert report["errors"] == 0
    assert report["tables"]["catalog"]["match"] is True
    assert report["tables"]["catalog"]["source_rows"] == 2
    assert report["tables"]["queue_shadow"]["match"] is True
    assert report["tables"]["job_run_logs"]["match"] is True
    assert report["tables"]["otps"]["match"] is True

    # Connect to partitioned target
    cfg = DatabaseConfig(layout="partitioned", data_dir=str(data_dir))
    part_db = Database(cfg)
    await part_db.connect()

    try:
        # Check catalog and FTS search
        alien = await part_db.catalog.get_item("film_alien", is_partitioned=True)
        assert alien is not None
        assert alien["title"] == "Alien"

        search_results = await part_db.search("Predator")
        assert len(search_results) == 1
        assert search_results[0]["friendly_token"] == "film_predator"

        # Check queue
        shadow = await part_db.get_shadow_items()
        assert len(shadow) == 1
        assert shadow[0]["uid"] == 101

        # Check jobs
        runs = await part_db.get_job_runs("catalog_sync")
        assert len(runs) == 1
        logs = await part_db.get_job_run_logs(runs[0]["id"])
        assert len(logs) == 2

        # Check users
        otp_valid = await part_db.verify_otp("groberts", "123456")
        assert otp_valid is True

        watchlist = await part_db.watchlist_get("groberts")
        assert len(watchlist) == 1
        assert watchlist[0]["friendly_token"] == "film_predator"
    finally:
        await part_db.close()


async def test_concurrency_batch_jobs_vs_state_poller(tmp_path):
    """Stress test: Concurrent batch job log writes in jobs.db + metadata updates in catalog.db

    never block high-frequency StatePoller writes in queue.db.
    """
    data_dir = tmp_path / "concurrency_test_1"
    cfg = DatabaseConfig(layout="partitioned", data_dir=str(data_dir))
    db = Database(cfg)
    await db.connect()
    await db.run_migrations()

    stop_event = asyncio.Event()
    poller_iterations = 0
    batch_records_written = 0
    lock_errors: list[Exception] = []

    async def run_state_poller_worker():
        nonlocal poller_iterations
        while not stop_event.is_set():
            try:
                # Simulated StatePoller updating queue_shadow every 10ms
                await db.queue.upsert_shadow_item(
                    {
                        "uid": 1000 + (poller_iterations % 10),
                        "position": 1,
                        "title": f"Polled Title {poller_iterations}",
                        "friendly_token": f"tok_{poller_iterations}",
                        "media_type": "cm",
                        "media_id": f"id_{poller_iterations}",
                        "duration_sec": 300,
                        "is_pay": False,
                    }
                )
                poller_iterations += 1
                await asyncio.sleep(0.01)
            except Exception as e:
                lock_errors.append(e)

    async def run_heavy_jobs_and_catalog_worker():
        nonlocal batch_records_written
        run_id = await db.jobs.start_job_run("enrichment_stress")
        for batch_num in range(30):
            if stop_event.is_set():
                break
            try:
                # Burst logging in jobs.db
                log_lines = [
                    (
                        "2026-09-23T12:00:00Z",
                        "INFO",
                        "enrich",
                        f"Log line {batch_num * 100 + i}",
                    )
                    for i in range(50)
                ]
                await db.jobs.add_job_run_logs(run_id, log_lines)

                # Batch metadata updates in catalog.db
                await db.catalog.insert_catalog(
                    {
                        "friendly_token": f"stress_tok_{batch_num}",
                        "title": f"Stress Film {batch_num}",
                        "manifest_url": f"https://dropsugar.com/{batch_num}.json",
                        "synced_at": "2026-01-01T00:00:00Z",
                    }
                )
                batch_records_written += 50
                await asyncio.sleep(0.01)
            except Exception as e:
                lock_errors.append(e)

    # Run concurrently for a burst
    poller_task = asyncio.create_task(run_state_poller_worker())
    job_task = asyncio.create_task(run_heavy_jobs_and_catalog_worker())

    await asyncio.sleep(0.6)
    stop_event.set()
    await asyncio.gather(poller_task, job_task)
    await db.close()

    assert len(lock_errors) == 0, f"Encountered unexpected lock errors: {lock_errors}"
    assert (
        poller_iterations > 10
    ), f"Expected poller to run at least 10 times, ran {poller_iterations}"
    assert (
        batch_records_written > 200
    ), f"Expected at least 200 log records, got {batch_records_written}"


async def test_concurrency_device_keys_vs_catalog_browse(tmp_path):
    """Stress test: Concurrent device API key authentication in users.db

    never blocks browse queries in catalog.db.
    """
    data_dir = tmp_path / "concurrency_test_2"
    cfg = DatabaseConfig(layout="partitioned", data_dir=str(data_dir))
    db = Database(cfg)
    await db.connect()
    await db.run_migrations()

    # Pre-populate catalog items
    for i in range(20):
        await db.catalog.insert_catalog(
            {
                "friendly_token": f"item_{i}",
                "title": f"Catalog Title {i}",
                "manifest_url": f"https://dropsugar.com/{i}.json",
                "synced_at": "2026-01-01T00:00:00Z",
            }
        )

    # Issue device API key in users.db
    key_id = await db.users.create_device_key(
        "testuser", "Living Room TV", "wq_live", "hash_1234567890"
    )

    lock_errors: list[Exception] = []
    auth_success_count = 0
    browse_success_count = 0

    async def device_auth_worker():
        nonlocal auth_success_count
        for _ in range(25):
            try:
                await db.users.touch_device_key(key_id)
                auth_success_count += 1
                await asyncio.sleep(0.005)
            except Exception as e:
                lock_errors.append(e)

    async def catalog_browse_worker():
        nonlocal browse_success_count
        for _ in range(25):
            try:
                items = await db.browse(page=1, per_page=10)
                if items:
                    browse_success_count += 1
                await asyncio.sleep(0.005)
            except Exception as e:
                lock_errors.append(e)

    await asyncio.gather(device_auth_worker(), catalog_browse_worker())
    await db.close()

    assert len(lock_errors) == 0, f"Encountered lock errors: {lock_errors}"
    assert auth_success_count == 25
    assert browse_success_count == 25
