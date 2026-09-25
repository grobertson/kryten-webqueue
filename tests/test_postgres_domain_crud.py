"""Live-Postgres CRUD verification for the asyncpg domain port (catalog/queue/jobs/users).

Runs against a disposable ``webqueue_test`` database on chandra-1 (schema applied from
``sql/001_initial_schema.sql``, privileges granted to the existing ``kryten`` role) — never
touches the production ``webqueue`` database. Skips gracefully if chandra-1 is unreachable,
mirroring ``test_postgres_live.py``.
"""

import os
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.config import DatabaseConfig, PostgresConfig

TEST_PG_HOST = os.getenv("KRYTEN_TEST_PG_HOST", "chandra-1.local")
TEST_PG_PASSWORD_ENV = "KRYTEN_TEST_PG_PASSWORD"
os.environ.setdefault(TEST_PG_PASSWORD_ENV, "kryten_secret_password")

_TRUNCATE_SQL = """
    TRUNCATE
        catalog.catalog, catalog.categories, catalog.tags, catalog.sync_log,
        catalog.people, catalog.studios, catalog.item_enrichment_state,
        catalog.item_edit_log, catalog.motd_overrides,
        queue.queue_shadow, queue.spend_requests, queue.queue_history,
        queue.saved_playlists, queue.playlist_schedules, queue.active_schedule,
        queue.play_completions, queue.catalog_blackouts,
        jobs.job_runs, jobs.job_schedules, jobs.fetch_queue,
        users.otps, users.device_link_codes, users.device_api_keys,
        users.user_watchlist, users.feedback, users.title_suggestions
    CASCADE
"""


def _make_config() -> DatabaseConfig:
    return DatabaseConfig(
        backend="postgres",
        postgres=PostgresConfig(
            host=TEST_PG_HOST,
            port=5432,
            user="kryten",
            dbname="webqueue_test",
            password_env=TEST_PG_PASSWORD_ENV,
        ),
    )


@pytest.fixture
async def pg_db():
    db = Database(_make_config())
    try:
        await db.connect()
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(
            f"chandra-1 webqueue_test not reachable from this environment: {exc}"
        )
        return
    await db.catalog._execute(_TRUNCATE_SQL)
    yield db
    await db.close()


# --- users domain ---------------------------------------------------------


async def test_users_watchlist_and_otp(pg_db):
    assert await pg_db.users.watchlist_add("alice", "tok_1") is True
    assert (
        await pg_db.users.watchlist_add("alice", "tok_1") is False
    )  # ON CONFLICT DO NOTHING
    assert await pg_db.users.watchlist_add("alice", "tok_2") is True
    assert await pg_db.users.watchlist_count("alice") == 2
    assert set(await pg_db.users.watchlist_tokens("alice")) == {"tok_1", "tok_2"}
    assert await pg_db.users.watchlist_remove("alice", "tok_1") is True
    assert await pg_db.users.watchlist_remove("alice", "tok_1") is False
    assert await pg_db.users.watchlist_count("alice") == 1

    expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    await pg_db.users.store_otp("alice", "123456", expires)
    assert await pg_db.users.verify_otp("alice", "000000") is False
    assert await pg_db.users.verify_otp("alice", "123456") is True
    assert await pg_db.users.verify_otp("alice", "123456") is False  # already used

    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    await pg_db.users.store_otp("bob", "999999", expired)
    await pg_db.users.cleanup_expired_otps()
    assert await pg_db.users.verify_otp("bob", "999999") is False


async def test_users_link_codes_and_device_keys(pg_db):
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    await pg_db.users.create_link_code("ABCDE", "alice", "Living Room TV", future)
    assert await pg_db.users.link_code_exists("ABCDE") is True
    row = await pg_db.users.get_valid_link_code("ABCDE")
    assert row is not None and row["username"] == "alice"
    await pg_db.users.delete_link_code("ABCDE")
    assert await pg_db.users.link_code_exists("ABCDE") is False

    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    await pg_db.users.create_link_code("STALE", "bob", "Old TV", past)
    purged = await pg_db.users.purge_expired_link_codes()
    assert purged == 1

    key_id = await pg_db.users.create_device_key("alice", "Roku", "abcd", "hash123")
    assert isinstance(key_id, int)
    fetched = await pg_db.users.get_device_key_by_hash("hash123")
    assert fetched is not None and fetched["username"] == "alice"
    await pg_db.users.touch_device_key(key_id)
    assert len(await pg_db.users.list_device_keys("alice")) == 1
    assert "alice" in await pg_db.users.device_key_usernames()
    assert await pg_db.users.delete_device_key(key_id, "alice") is True
    assert await pg_db.users.delete_device_key(key_id, "alice") is False

    key_id2 = await pg_db.users.create_device_key("alice", "Roku2", "efgh", "hash456")
    assert await pg_db.users.revoke_user_device_keys("alice") == 1
    assert await pg_db.users.get_device_key_by_hash("hash456") is None
    assert key_id2  # sanity


async def test_users_feedback_and_title_suggestions(pg_db):
    fid = await pg_db.users.add_feedback(username="alice", body="Great service!")
    assert isinstance(fid, int)
    assert await pg_db.users.count_feedback() == 1
    assert await pg_db.users.set_feedback_status(fid, "reviewed") is True
    rows = await pg_db.users.list_feedback(status="reviewed")
    assert len(rows) == 1
    assert await pg_db.users.delete_feedback(fid) is True

    sid = await pg_db.users.add_title_suggestion(username="alice", query="The Matrix")
    assert isinstance(sid, int)
    assert await pg_db.users.count_title_suggestions() == 1
    assert await pg_db.users.set_title_suggestion_status(sid, "resolved") is True
    assert len(await pg_db.users.list_title_suggestions(status="resolved")) == 1
    assert await pg_db.users.delete_title_suggestion(sid) is True


# --- jobs domain -----------------------------------------------------------


async def test_jobs_run_lifecycle_and_logs(pg_db):
    run_id = await pg_db.jobs.start_job_run("test_job", triggered_by="pytest")
    assert isinstance(run_id, int)
    await pg_db.jobs.update_job_run_detail(run_id, "50% complete")
    await pg_db.jobs.add_job_run_logs(
        run_id,
        [
            (datetime.now(timezone.utc).isoformat(), "INFO", "test", "line 1"),
            (datetime.now(timezone.utc).isoformat(), "INFO", "test", "line 2"),
        ],
    )
    logs = await pg_db.jobs.get_job_run_logs(run_id)
    assert [row["message"] for row in logs] == ["line 1", "line 2"]

    await pg_db.jobs.finish_job_run(run_id, "success", detail="done")
    row = await pg_db.jobs.get_job_run(run_id)
    assert row["status"] == "success"
    assert row["detail"] == "done"

    run_id2 = await pg_db.jobs.start_job_run("test_job")
    reconciled = await pg_db.jobs.reconcile_orphaned_job_runs()
    assert reconciled == 1
    row2 = await pg_db.jobs.get_job_run(run_id2)
    assert row2["status"] == "interrupted"

    runs = await pg_db.jobs.get_job_runs("test_job", limit=10)
    assert len(runs) == 2


async def test_jobs_schedules_and_fetch_queue(pg_db):
    await pg_db.jobs.upsert_job_schedule("rehost_emotes", "0 * * * *", label="Hourly")
    sched = await pg_db.jobs.get_job_schedule("rehost_emotes")
    assert sched["cron_expression"] == "0 * * * *"
    await pg_db.jobs.upsert_job_schedule("rehost_emotes", "0 0 * * *", label="Daily")
    sched = await pg_db.jobs.get_job_schedule("rehost_emotes")
    assert sched["cron_expression"] == "0 0 * * *"
    assert len(await pg_db.jobs.get_job_schedules()) == 1
    await pg_db.jobs.delete_job_schedule("rehost_emotes")
    assert await pg_db.jobs.get_job_schedule("rehost_emotes") is None

    fq_id = await pg_db.jobs.enqueue_fetch(url="https://example.com/a")
    await pg_db.jobs.enqueue_fetch(url="https://example.com/b")
    assert await pg_db.jobs.count_fetch_queue_pending() == 2

    claimed = await pg_db.jobs.claim_next_fetch_item()
    assert claimed["id"] == fq_id
    assert claimed["status"] == "running"
    assert await pg_db.jobs.count_fetch_queue_pending() == 1

    await pg_db.jobs.finish_fetch_item(fq_id, status="done", result_json='{"ok":true}')
    row = (await pg_db.jobs.get_fetch_queue(status="done"))[0]
    assert row["result_json"] == '{"ok":true}'

    await pg_db.jobs.requeue_fetch_item(fq_id)
    assert (await pg_db.jobs.get_fetch_queue())[
        [r["id"] for r in await pg_db.jobs.get_fetch_queue()].index(fq_id)
    ]["status"] == "pending"

    attempts = await pg_db.jobs.requeue_fetch_item_for_retry(fq_id, error="timeout")
    assert attempts == 1

    reset = await pg_db.jobs.reset_running_fetch_items()
    assert reset == 0  # nothing currently running

    assert await pg_db.jobs.delete_fetch_queue_item(fq_id) is True


async def test_jobs_prune_job_run_logs(pg_db):
    run_id = await pg_db.jobs.start_job_run("prune_target")
    old_time = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    recent_time = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    await pg_db.jobs.add_job_run_logs(
        run_id,
        [
            (old_time, "INFO", "test", "old 1"),
            (old_time, "INFO", "test", "old 2"),
            (recent_time, "INFO", "test", "recent 1"),
        ],
    )
    deleted = await pg_db.jobs.prune_job_run_logs(retention_days=30)
    assert deleted == 2
    remaining = await pg_db.jobs.get_job_run_logs(run_id)
    assert [r["message"] for r in remaining] == ["recent 1"]


# --- queue domain -----------------------------------------------------------


async def test_queue_shadow_and_spend(pg_db):
    await pg_db.queue.upsert_shadow_item(
        {
            "uid": 1,
            "position": 0,
            "title": "Movie A",
            "media_type": "cm",
            "media_id": "tok_a",
            "duration_sec": 120,
            "is_pay": True,
            "paid_by": "alice",
            "tier": "premium",
            "z_cost": 50,
        }
    )
    items = await pg_db.queue.get_shadow_items()
    assert len(items) == 1 and items[0]["title"] == "Movie A"
    assert items[0]["is_pay"] is True

    # Update via ON CONFLICT
    await pg_db.queue.upsert_shadow_item(
        {
            "uid": 1,
            "position": 0,
            "title": "Movie A (renamed)",
            "media_type": "cm",
            "media_id": "tok_a",
            "duration_sec": 120,
            "is_pay": True,
        }
    )
    items = await pg_db.queue.get_shadow_items()
    assert len(items) == 1 and items[0]["title"] == "Movie A (renamed)"

    assert await pg_db.queue.get_last_pay_uid() == 1
    assert len(await pg_db.queue.get_pay_items()) == 1
    assert await pg_db.queue.get_shadow_position_after(1) == 1

    await pg_db.queue.update_shadow_position(1, 5)
    est = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    await pg_db.queue.update_shadow_estimated_start(1, est)

    await pg_db.queue.remove_shadow_items({1})
    assert await pg_db.queue.get_shadow_items() == []

    await pg_db.queue.save_spend_request(
        "req_1",
        username="alice",
        uid=1,
        friendly_token="tok_a",
        tier="premium",
        z_cost=50,
    )
    await pg_db.queue.save_spend_request(
        "req_1", username="alice", uid=1
    )  # ON CONFLICT DO NOTHING, no error
    assert await pg_db.queue.get_request_id_for_uid(1) == "req_1"
    await pg_db.queue.mark_spend_refunded("req_1")
    assert await pg_db.queue.get_request_id_for_uid(1) is None  # refunded, excluded

    await pg_db.queue.add_queue_history(
        username="alice",
        friendly_token="tok_a",
        title="Movie A",
        tier="premium",
        z_cost=50,
    )
    history = await pg_db.queue.get_user_queue_history("alice")
    assert len(history) == 1


async def test_queue_playlists_and_schedules(pg_db):
    pid = await pg_db.queue.create_saved_playlist(
        name="Friday Night", description=None, is_immutable=False, created_by="admin"
    )
    assert isinstance(pid, int)
    await pg_db.queue.replace_playlist_items(
        pid,
        [
            {"media_type": "cm", "media_id": "a", "title": "A", "duration_sec": 10},
            {"media_type": "cm", "media_id": "b", "title": "B", "duration_sec": 20},
        ],
    )
    assert len(await pg_db.queue.get_saved_playlist_items(pid)) == 2
    count = await pg_db.queue.append_playlist_item(
        pid, {"media_type": "cm", "media_id": "c", "title": "C", "duration_sec": 30}
    )
    assert count == 3
    added = await pg_db.queue.append_playlist_items(
        pid,
        [
            {"media_type": "cm", "media_id": "c", "title": "C dup"},  # skipped, dup
            {"media_type": "cm", "media_id": "d", "title": "D"},
        ],
    )
    assert added == 1

    rotated = await pg_db.queue.rotate_playlist_item_to_bottom("a", media_type="cm")
    assert rotated == 1
    items = await pg_db.queue.get_saved_playlist_items(pid)
    assert items[-1]["media_id"] == "a"

    assert (await pg_db.queue.get_playlist_by_name("Friday Night", "admin"))[
        "id"
    ] == pid
    assert (await pg_db.queue.get_playlist_by_name_any("Friday Night"))["id"] == pid
    assert (await pg_db.queue.get_most_recent_playlist("admin"))["id"] == pid

    await pg_db.queue.update_saved_playlist(
        pid, name="Friday Night", description="updated", is_immutable=False
    )
    assert (await pg_db.queue.get_saved_playlist(pid))["description"] == "updated"

    # Promo pool
    promo_pid = await pg_db.queue.create_saved_playlist(
        name="Promo Pool",
        description=None,
        is_immutable=True,
        created_by="admin",
        promo_type="event",
    )
    await pg_db.queue.replace_playlist_items(
        promo_pid, [{"media_type": "cm", "media_id": "p1", "title": "Promo"}]
    )
    assert len(await pg_db.queue.get_promo_pools()) == 1
    assert len(await pg_db.queue.get_promo_pool_items("event")) == 1

    # Schedules — interval arithmetic is the highest-risk part of this port.
    near_fire = datetime.now(timezone.utc) + timedelta(minutes=1)
    far_fire = datetime.now(timezone.utc) + timedelta(hours=6)
    near_id = await pg_db.queue.create_schedule(
        playlist_id=pid,
        label="Near event",
        fire_at=near_fire,
        pre_fire_lock_minutes=15,
        created_by="admin",
    )
    far_id = await pg_db.queue.create_schedule(
        playlist_id=pid,
        label="Far event",
        fire_at=far_fire,
        pre_fire_lock_minutes=15,
        created_by="admin",
    )
    assert await pg_db.queue.is_pre_fire_lock_active() is True
    active_lock = await pg_db.queue.get_active_pre_fire_lock()
    assert active_lock["id"] == near_id

    next_sched = await pg_db.queue.get_next_schedule()
    assert next_sched["id"] == near_id  # nearest future schedule

    disabled = await pg_db.queue.disable_active_pre_fire_locks()
    assert disabled == 1
    assert await pg_db.queue.is_pre_fire_lock_active() is False

    await pg_db.queue.update_schedule(far_id, label="Far event (renamed)")
    assert (await pg_db.queue.get_schedule(far_id))["label"] == "Far event (renamed)"
    # The admin form submits ISO strings; PostgreSQL must normalize them before
    # binding to its timestamptz column.
    updated_fire = (datetime.now(timezone.utc) + timedelta(hours=7)).isoformat()
    await pg_db.queue.update_schedule(far_id, fire_at=updated_fire, is_active=True)
    assert (await pg_db.queue.get_schedule(far_id))["fire_at"] == datetime.fromisoformat(
        updated_fire
    )
    await pg_db.queue.mark_schedule_fired(
        near_id, datetime.now(timezone.utc).isoformat()
    )
    assert (await pg_db.queue.get_schedule(near_id))["fired_at"] is not None
    await pg_db.queue.delete_schedule(near_id)
    assert await pg_db.queue.get_schedule(near_id) is None

    # Active schedule singleton upsert
    await pg_db.queue.set_active_schedule(
        schedule_id=far_id,
        playlist_id=pid,
        is_immutable=True,
        started_at=datetime.now(timezone.utc).isoformat(),
        estimated_end_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    active = await pg_db.queue.get_active_schedule()
    assert active["schedule_id"] == far_id
    assert await pg_db.queue.is_event_lock_active() is True
    await pg_db.queue.disable_active_lock()
    assert await pg_db.queue.is_event_lock_active() is False
    # Re-upsert (ON CONFLICT DO UPDATE on the singleton row)
    await pg_db.queue.set_active_schedule(
        schedule_id=far_id,
        playlist_id=pid,
        is_immutable=False,
        started_at=datetime.now(timezone.utc).isoformat(),
        estimated_end_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    assert (await pg_db.queue.get_active_schedule())["is_immutable"] is False
    await pg_db.queue.clear_active_schedule()
    assert await pg_db.queue.get_active_schedule() is None


async def test_queue_blackouts_and_play_state(pg_db):
    expires1 = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    expires2 = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    await pg_db.queue.upsert_blackout("tok_x", reason="weekend", expires_at=expires1)
    assert await pg_db.queue.is_blackout("tok_x") is True
    assert await pg_db.queue.count_active_blackouts() == 1
    # Re-upsert with a LATER expiry must keep the max (GREATEST), not the newer literal value
    await pg_db.queue.upsert_blackout("tok_x", reason="extended", expires_at=expires2)
    rows = await pg_db.queue.list_active_blackouts()
    assert rows[0]["reason"] == "extended"

    # Re-upsert with an EARLIER expiry must NOT shrink the window (GREATEST semantics)
    earlier = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    await pg_db.queue.upsert_blackout(
        "tok_x", reason="should-not-shrink", expires_at=earlier
    )
    tokens = await pg_db.queue.get_active_blackout_tokens()
    assert "tok_x" in tokens

    await pg_db.queue.prune_expired_blackouts()
    assert await pg_db.queue.is_blackout("tok_x") is True  # not expired yet

    # Play completion tracking
    await pg_db.queue.record_play_completion(friendly_token="tok_y", media_type="cm")
    played = await pg_db.queue.get_played_at_for_tokens(["tok_y"])
    assert "tok_y" in played
    hidden = await pg_db.queue.get_active_hidden_media_ids(window_days=7)
    assert "tok_y" in hidden
    removed = await pg_db.queue.unrecord_play_completion("cm", "tok_y")
    assert removed == 1
    hidden_after = await pg_db.queue.get_active_hidden_media_ids(window_days=7)
    assert "tok_y" not in hidden_after

    await pg_db.queue.record_play_completion(friendly_token="tok_z", media_type="cm")
    await pg_db.queue.clear_play_state("tok_z", media_type="cm")
    assert await pg_db.queue.get_played_at_for_tokens(["tok_z"]) == {}


# --- catalog domain ---------------------------------------------------------


async def test_catalog_insert_update_and_lookup(pg_db):
    await pg_db.catalog.insert_catalog(
        {
            "friendly_token": "tok_matrix",
            "title": "The Matrix",
            "description": "A hacker discovers reality is a simulation.",
            "duration_sec": 8160,
            "manifest_url": "https://example.com/tok_matrix.json",
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    item = await pg_db.catalog.get_item("tok_matrix")
    assert item["title"] == "The Matrix"
    admin_item = await pg_db.catalog.get_item_admin("tok_matrix")
    assert admin_item["friendly_token"] == "tok_matrix"

    resolved = await pg_db.catalog.resolve_media("https://example.com/tok_matrix.json")
    assert resolved["friendly_token"] == "tok_matrix"

    await pg_db.catalog.update_catalog(
        "tok_matrix", {"description": "Neo takes the red pill."}
    )
    updated = await pg_db.catalog.get_item("tok_matrix")
    assert updated["description"] == "Neo takes the red pill."

    await pg_db.catalog.update_cover_art("tok_matrix", "/covers/matrix.jpg", "tmdb")
    await pg_db.catalog.set_imdb_tt("tok_matrix", "tt0133093")
    assert (await pg_db.catalog.get_item_by_imdb_tt("tt0133093"))[
        "friendly_token"
    ] == "tok_matrix"

    found = await pg_db.catalog.find_catalog_by_title("The Matrix")
    assert found is not None and found["friendly_token"] == "tok_matrix"

    brief = await pg_db.catalog.get_catalog_brief(["tok_matrix"], [])
    assert "tok_matrix" in brief

    old_sync = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    await pg_db.catalog.insert_catalog(
        {
            "friendly_token": "tok_stale",
            "title": "Stale Item",
            "manifest_url": "https://example.com/tok_stale.json",
            "synced_at": old_sync,
        }
    )
    # sync_started_at must be captured BEFORE re-touching survivors: items
    # synced_at older than this cutoff are stale; items touched during/after
    # the sync (like tok_matrix below) get a newer synced_at and survive.
    sync_started_at = datetime.now(timezone.utc).isoformat()
    await pg_db.catalog.update_catalog(
        "tok_matrix", {"synced_at": datetime.now(timezone.utc).isoformat()}
    )
    deleted = await pg_db.catalog.delete_stale_catalog_items(sync_started_at)
    assert deleted == 1
    assert await pg_db.catalog.get_item("tok_stale") is None
    assert await pg_db.catalog.get_item("tok_matrix") is not None

    assert await pg_db.catalog.delete_catalog_item("tok_matrix") is True
    assert await pg_db.catalog.delete_catalog_item("tok_matrix") is False


async def test_catalog_browse_search_and_facets(pg_db):
    for i, title in enumerate(["Terminator 2", "Terminator 3", "The Matrix Reloaded"]):
        await pg_db.catalog.insert_catalog(
            {
                "friendly_token": f"tok_{i}",
                "title": title,
                "duration_sec": 100 + i,
                "manifest_url": f"https://example.com/tok_{i}.json",
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    cat_id = await pg_db.catalog.upsert_category("Action")
    tag_id = await pg_db.catalog.upsert_tag("sci-fi")
    await pg_db.catalog.set_catalog_categories("tok_0", [cat_id])
    await pg_db.catalog.set_catalog_tags("tok_0", [tag_id])
    await pg_db.catalog.add_catalog_tag("tok_1", "sci-fi")
    await pg_db.catalog.remove_catalog_tag("tok_1", "sci-fi")

    browsed = await pg_db.catalog.browse(per_page=10)
    assert len(browsed) == 3

    count = await pg_db.catalog.browse_count()
    assert count == 3

    filtered = await pg_db.catalog.browse(category="action", per_page=10)
    assert {r["friendly_token"] for r in filtered} == {"tok_0"}

    results = await pg_db.catalog.search("Terminator", per_page=10)
    assert {r["friendly_token"] for r in results} == {"tok_0", "tok_1"}
    assert await pg_db.catalog.search_count("Terminator") == 2

    # Trigram fallback should still find a close typo match.
    fuzzy = await pg_db.catalog.search("Terminater", per_page=10)
    assert len(fuzzy) >= 1

    categories = await pg_db.catalog.get_categories()
    assert any(c["slug"] == "action" for c in categories)

    facets = await pg_db.catalog.get_item_facets("tok_0")
    assert facets["categories"][0]["slug"] == "action"
    assert facets["tags"] == ["sci-fi"]

    tokens = await pg_db.catalog.get_items_by_tokens(["tok_0", "tok_1"])
    assert len(tokens) == 2

    resolved = await pg_db.catalog.resolve_friendly_tokens(
        ["tok_0", "https://example.com/tok_1.json"]
    )
    assert resolved == {"tok_0", "tok_1"}


async def test_catalog_people_studios_enrichment_and_motd(pg_db):
    await pg_db.catalog.insert_catalog(
        {
            "friendly_token": "tok_people",
            "title": "Ensemble Film",
            "manifest_url": "https://example.com/tok_people.json",
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    await pg_db.catalog.set_catalog_people(
        "tok_people",
        [
            {"name": "Keanu Reeves", "role": "cast", "position": 0},
            {"name": "Lana Wachowski", "role": "director", "position": 0},
        ],
    )
    await pg_db.catalog.set_catalog_studios("tok_people", ["Warner Bros."])
    people = await pg_db.catalog.get_item_people("tok_people")
    assert people["cast"] == ["Keanu Reeves"]
    assert people["director"] == ["Lana Wachowski"]
    assert await pg_db.catalog.get_item_studios("tok_people") == ["Warner Bros."]

    await pg_db.catalog.ensure_enrichment_state("tok_people")
    await pg_db.catalog.save_enrichment_state(
        "tok_people", content_type="movie", tmdb_id="603"
    )
    state = await pg_db.catalog.get_enrichment_state("tok_people")
    assert state["content_type"] == "movie"
    await pg_db.catalog.update_enrichment_state(
        "tok_people", {"lookup_title": "The Matrix"}
    )
    candidates = await pg_db.catalog.get_catalog_for_enrichment(step="art")
    assert any(c["friendly_token"] == "tok_people" for c in candidates)
    coverage = await pg_db.catalog.get_identify_coverage()
    assert any(c["friendly_token"] == "tok_people" for c in coverage)

    await pg_db.catalog.upsert_motd_override("2026-W39", "hero", title="Custom Hero")
    overrides = await pg_db.catalog.list_motd_overrides("2026-W39")
    assert overrides[0]["title"] == "Custom Hero"
    assert await pg_db.catalog.delete_motd_override("2026-W39", "hero") == 1

    log_id = await pg_db.catalog.start_sync_log()
    await pg_db.catalog.finish_sync_log(
        log_id, {"seen": 10, "new": 2, "updated": 1, "errors": 0}, "success"
    )
    logs = await pg_db.catalog.get_sync_logs(limit=5)
    assert logs[0]["status"] == "success"

    await pg_db.catalog.log_item_edit("tok_people", "admin", "title", "Old", "New")
    history = await pg_db.catalog.get_item_edit_history("tok_people")
    assert history[0]["field_name"] == "title"
