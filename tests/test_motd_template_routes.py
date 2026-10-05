"""HTTP surface for MOTD templates, schedules, timeline, automation, and audit."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kryten_webqueue.auth.session import create_session_token
from kryten_webqueue.catalog.db import Database
from kryten_webqueue.config import Config, DatabaseConfig, MediaConfig, MOTDConfig
from kryten_webqueue.motd import composer
from kryten_webqueue.routes.admin_motd import router as admin_motd_router
from kryten_webqueue.routes.admin_motd_templates import (
    audit_router,
    router as templates_router,
)

SECRET = "y" * 32
GRID_MASTER = '<h1>{{ headline }}</h1>{{ movie_grid() }}{{ zone("promo") }}'


@pytest.fixture
async def db(tmp_path):
    database = Database(
        DatabaseConfig(
            backend="sqlite",
            layout="monolith",
            data_dir=str(tmp_path),
            db_path=str(tmp_path / "routes.db"),
        )
    )
    await database.connect()
    await database.run_migrations()
    await composer.ensure_seeded(database)
    yield database
    await database.close()


def _app(db, tmp_path) -> FastAPI:
    payload = {
        "secret_key": SECRET,
        "api_gate_token": "t",
        "mediacms_token": "t",
        "database": db.db_config.model_dump(),
        "motd": MOTDConfig(
            poster_dir=str(tmp_path / "boxes"),
            output_dir=str(tmp_path / "out"),
            mystery_box_url="https://cdn.example/m.jpg",
            slots=4,
        ).model_dump(),
        "media": MediaConfig(
            dir=str(tmp_path / "media"), base_url="https://q.example/media"
        ).model_dump(),
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(payload), encoding="utf-8")
    app = FastAPI()
    app.state.db = db
    app.state.config = Config.from_file(cfg_path)
    app.state.cover_art = None
    app.include_router(admin_motd_router)
    app.include_router(templates_router)
    app.include_router(audit_router)
    return app


def _client(db, tmp_path, *, user="alice", rank=3) -> TestClient:
    return TestClient(
        _app(db, tmp_path),
        cookies={"session": create_session_token(user, rank, SECRET)},
    )


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/admin/motd/templates"),
        ("post", "/admin/motd/templates"),
        ("put", "/admin/motd/templates/x"),
        ("post", "/admin/motd/templates/preview"),
        ("get", "/admin/motd/schedules"),
        ("post", "/admin/motd/schedules"),
        ("delete", "/admin/motd/schedules/1"),
        ("get", "/admin/motd/timeline"),
        ("put", "/admin/motd/automation"),
        ("get", "/admin/audit"),
    ],
)
async def test_every_route_requires_an_admin(db, tmp_path, method, path):
    anonymous = TestClient(_app(db, tmp_path))
    assert getattr(anonymous, method)(path).status_code == 401
    member = _client(db, tmp_path, rank=2)
    assert getattr(member, method)(path).status_code == 403


async def test_create_save_conflict_and_restore(db, tmp_path):
    client = _client(db, tmp_path)
    r = client.post(
        "/admin/motd/templates",
        json={
            "name": "october",
            "kind": "master",
            "display_name": "October",
            "body": GRID_MASTER,
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["zones"] == ["promo"] and created["revision_no"] == 1

    r = client.put(
        "/admin/motd/templates/october",
        json={
            "body": GRID_MASTER + "<p>v2</p>",
            "note": "add footer",
            "expected_revision_id": created["current_revision_id"],
        },
    )
    assert r.status_code == 200 and r.json()["revision_no"] == 2

    stale = client.put(
        "/admin/motd/templates/october",
        json={
            "body": "x",
            "note": "late",
            "expected_revision_id": created["current_revision_id"],
        },
    )
    assert stale.status_code == 409

    restored = client.post("/admin/motd/templates/october/revisions/1/restore")
    assert restored.status_code == 200 and restored.json()["revision_no"] == 3
    assert restored.json()["body"] == GRID_MASTER

    revisions = client.get("/admin/motd/templates/october/revisions").json()[
        "revisions"
    ]
    assert [r["revision_no"] for r in revisions] == [3, 2, 1]
    audit = client.get("/admin/audit", params={"entity_type": "motd_template"}).json()
    actions = [e["action"] for e in audit["entries"] if e["actor"] == "alice"]
    assert actions == [
        "motd.template.restore",
        "motd.template.save",
        "motd.template.create",
    ]


@pytest.mark.parametrize(
    "body,kind,status,needle",
    [
        ("<p>no grid</p>", "master", 422, "movie grid"),
        ('{% include "x" %}{{ movie_grid() }}', "master", 422, "include"),
        ("{{ zone('a') }}", "fragment", 422, "only available in master"),
        (
            '{{ movie_grid() }}{{ media_url("missing") }}',
            "master",
            422,
            "Unknown or deleted",
        ),
    ],
)
async def test_invalid_templates_are_not_saved(
    db, tmp_path, body, kind, status, needle
):
    client = _client(db, tmp_path)
    r = client.post(
        "/admin/motd/templates",
        json={"name": "bad", "kind": kind, "display_name": "Bad", "body": body},
    )
    assert r.status_code == status
    assert needle in r.json()["detail"]["message"]
    assert await db.get_motd_template("bad") is None


async def test_template_names_are_validated_and_unique(db, tmp_path):
    client = _client(db, tmp_path)
    body = {"kind": "fragment", "display_name": "P", "body": "<b>hi</b>"}
    assert (
        client.post(
            "/admin/motd/templates", json={**body, "name": "Bad Name"}
        ).status_code
        == 422
    )
    assert (
        client.post("/admin/motd/templates", json={**body, "name": "promo"}).status_code
        == 201
    )
    assert (
        client.post("/admin/motd/templates", json={**body, "name": "promo"}).status_code
        == 409
    )


async def test_preview_reports_errors_inline_and_renders_fragments_in_a_host(
    db, tmp_path
):
    client = _client(db, tmp_path)
    bad = client.post(
        "/admin/motd/templates/preview", json={"body": "{% if %}", "kind": "master"}
    )
    assert (
        bad.status_code == 200 and bad.json()["ok"] is False and bad.json()["line"] == 1
    )

    fragment = client.post(
        "/admin/motd/templates/preview",
        json={"body": "<b>Contest!</b>", "kind": "fragment"},
    ).json()
    assert fragment["ok"] is True
    assert 'data-kryten-motd-zone="promo"' in fragment["html"]
    assert "<b>Contest!</b>" in fragment["html"]
    assert fragment["chars"] <= fragment["max_chars"]


async def test_default_and_archive_guards(db, tmp_path):
    client = _client(db, tmp_path)
    seed = composer.SEED_TEMPLATE_NAME
    assert client.post(f"/admin/motd/templates/{seed}/archive").status_code == 409
    client.post(
        "/admin/motd/templates",
        json={
            "name": "promo",
            "kind": "fragment",
            "display_name": "P",
            "body": "<b>x</b>",
        },
    )
    assert (
        client.patch(
            "/admin/motd/templates/promo", json={"is_default": True}
        ).status_code
        == 422
    )
    r = client.post(
        "/admin/motd/schedules",
        json={
            "template": "promo",
            "zone": "promo",
            "label": "Always",
            "starts_at": "2026-01-01T00:00",
            "ends_at": "2099-01-01T00:00",
        },
    )
    assert r.status_code == 201, r.text
    assert client.post("/admin/motd/templates/promo/archive").status_code == 409
    client.delete(f"/admin/motd/schedules/{r.json()['id']}")
    assert client.post("/admin/motd/templates/promo/archive").status_code == 200


async def test_schedule_validation_and_local_time_handling(db, tmp_path):
    client = _client(db, tmp_path)
    seed = composer.SEED_TEMPLATE_NAME
    zoned = client.post(
        "/admin/motd/schedules",
        json={
            "template": seed,
            "zone": "promo",
            "label": "x",
            "starts_at": "2026-10-01T00:00",
            "ends_at": "2026-10-02T00:00",
        },
    )
    assert zoned.status_code == 422
    no_end = client.post(
        "/admin/motd/schedules",
        json={"template": seed, "label": "x", "starts_at": "2026-10-01T00:00"},
    )
    assert no_end.status_code == 422
    ok = client.post(
        "/admin/motd/schedules",
        json={
            "template": seed,
            "label": "Oct",
            "starts_at": "2026-10-01T00:00",
            "ends_at": "2026-11-01T00:00",
        },
    )
    assert ok.status_code == 201
    rows = client.get("/admin/motd/schedules").json()["schedules"]
    # Naive input is wall-clock America/New_York (EDT, UTC-4 on Oct 1).
    assert rows[0]["starts_at"].startswith("2026-10-01T04:00:00")
    assert rows[0]["starts_at_local"].startswith("2026-10-01T00:00:00-04:00")


async def test_timeline_flags_fragments_hidden_by_the_master(db, tmp_path):
    client = _client(db, tmp_path)
    client.post(
        "/admin/motd/templates",
        json={
            "name": "plain",
            "kind": "master",
            "display_name": "Plain",
            "body": "{{ movie_grid() }}",
        },
    )
    client.post(
        "/admin/motd/templates",
        json={
            "name": "promo",
            "kind": "fragment",
            "display_name": "P",
            "body": "<b>x</b>",
        },
    )
    for body in (
        {
            "template": "plain",
            "label": "Plain week",
            "starts_at": "2026-10-05T00:00",
            "ends_at": "2026-10-12T00:00",
        },
        {
            "template": "promo",
            "zone": "promo",
            "label": "Promo",
            "starts_at": "2026-10-01T00:00",
            "ends_at": "2026-10-20T00:00",
        },
    ):
        assert client.post("/admin/motd/schedules", json=body).status_code == 201
    r = client.get(
        "/admin/motd/timeline",
        params={"start": "2026-10-01T00:00", "end": "2026-10-21T00:00"},
    )
    data = r.json()
    assert [s["master"] for s in data["segments"]] == [
        composer.SEED_TEMPLATE_NAME,
        "plain",
        composer.SEED_TEMPLATE_NAME,
        composer.SEED_TEMPLATE_NAME,
    ]
    assert data["segments"][1]["zones"]["promo"]["hidden"] is True
    assert any("not in master 'plain'" in w for w in data["warnings"])
    too_long = client.get(
        "/admin/motd/timeline",
        params={"start": "2026-01-01T00:00", "end": "2026-06-01T00:00"},
    )
    assert too_long.status_code == 422


async def test_automation_toggle_persists_and_is_audited(db, tmp_path):
    client = _client(db, tmp_path)
    assert client.get("/admin/motd/automation").json()["enabled"] is False
    assert (
        client.put("/admin/motd/automation", json={"enabled": True}).status_code == 200
    )
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["motd"]["automation_enabled"] is True
    entries = client.get(
        "/admin/audit", params={"entity_type": "motd_automation"}
    ).json()
    assert entries["entries"][0]["actor"] == "alice"
    assert entries["entries"][0]["after"] == {"enabled": True}
