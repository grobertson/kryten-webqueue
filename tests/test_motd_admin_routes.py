"""Admin MOTD override route tests.

Covers the HTTP surface the admin panel actually calls: saving an override by
URL (``PUT``), uploading replacement art (``POST .../art``), and clearing one
(``DELETE``), including the failure mode where ``motd.poster_dir`` is not
writable — which previously surfaced as an opaque 500 and made every art
upload look like a generic "save failed".
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kryten_webqueue.auth.session import create_session_token
from kryten_webqueue.catalog.db import Database
from kryten_webqueue.config import Config, DatabaseConfig, MOTDConfig
from kryten_webqueue.motd import builder
from kryten_webqueue.routes.admin_motd import router as admin_motd_router

SECRET = "x" * 32
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
async def db(tmp_path):
    database = Database(
        DatabaseConfig(
            backend="sqlite",
            layout="monolith",
            data_dir=str(tmp_path),
            db_path=str(tmp_path / "motd-route.db"),
        )
    )
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


def _client(db, tmp_path, **motd_kw) -> TestClient:
    motd = MOTDConfig(
        poster_dir=str(tmp_path / "boxes"),
        poster_base_url="https://cdn.example/boxes",
        output_dir=str(tmp_path / "out"),
        **motd_kw,
    )
    payload = {
        "secret_key": SECRET,
        "api_gate_token": "t",
        "mediacms_token": "t",
        "database": db.db_config.model_dump(),
        "motd": motd.model_dump(),
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(payload), encoding="utf-8")

    app = FastAPI()
    app.state.db = db
    app.state.config = Config.from_file(cfg_path)
    app.state.cover_art = None

    class _JobManager:
        async def run(self, *a, **kw):
            return {"started": False}

    app.state.job_manager = _JobManager()
    app.include_router(admin_motd_router)
    token = create_session_token("admin", 3, SECRET)
    return TestClient(app, cookies={"session": token})


@pytest.fixture(autouse=True)
def _stub_grid_network(monkeypatch):
    """Keep the grid endpoints off the network (workbook + OMDB)."""
    monkeypatch.setattr(builder, "load_workbook_bytes", lambda config, **kw: b"xlsx")
    monkeypatch.setattr(builder, "resolve_weekend_sheet", lambda b, s: None)
    monkeypatch.setattr(
        builder,
        "extract_movies_by_section",
        lambda b, s: {"friday": ["Alpha (1976)"], "saturday-night": []},
    )
    monkeypatch.setattr(
        builder,
        "_resolve_omdb",
        lambda t, *, api_key: {
            "imdb_id": "tt0000001",
            "poster_url": "https://omdb/img.jpg",
        },
    )
    monkeypatch.setattr(builder, "_download_image", lambda url: b"jpegbytes")


# --- saving an override ---


async def test_save_override_by_url_persists(db, tmp_path):
    client = _client(db, tmp_path)
    r = client.put(
        "/admin/motd/overrides/night1-slot1",
        params={"week": "current"},
        json={
            "title": "Hand Picked",
            "href": "https://example.com/notes",
            "poster_url": "https://cdn.example/boxes/custom.png",
        },
    )
    assert r.status_code == 200

    week_key, _ = builder.week_context()
    rows = await db.list_motd_overrides(week_key)
    assert len(rows) == 1
    assert rows[0]["slot_key"] == "night1-slot1"
    assert rows[0]["title"] == "Hand Picked"
    assert rows[0]["poster_url"] == "https://cdn.example/boxes/custom.png"
    assert rows[0]["created_by"] == "admin"


async def test_saved_override_shows_up_in_the_grid(db, tmp_path):
    client = _client(db, tmp_path)
    client.put(
        "/admin/motd/overrides/night1-slot1",
        params={"week": "current"},
        json={
            "title": "Hand Picked",
            "poster_url": "https://cdn.example/boxes/custom.png",
        },
    )
    grid = client.get("/admin/motd/grid", params={"week": "current"})
    assert grid.status_code == 200
    pinned = [s for s in grid.json()["slots"] if s["source"] == "override"]
    assert [s["slot_key"] for s in pinned] == ["night1-slot1"]
    assert pinned[0]["title"] == "Hand Picked"


async def test_overrides_are_scoped_to_their_week(db, tmp_path):
    client = _client(db, tmp_path)
    body = {"title": "Current", "poster_url": "https://cdn.example/boxes/c.png"}
    assert (
        client.put(
            "/admin/motd/overrides/night1-slot1", params={"week": "current"}, json=body
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/admin/motd/overrides/night1-slot1", params={"week": "next"}, json=body
        ).status_code
        == 200
    )
    current_key, _ = builder.week_context()
    next_key, _ = builder.week_context(week_offset=1)
    assert current_key != next_key
    assert len(await db.list_motd_overrides(current_key)) == 1
    assert len(await db.list_motd_overrides(next_key)) == 1


async def test_clear_override_removes_it(db, tmp_path):
    client = _client(db, tmp_path)
    client.put(
        "/admin/motd/overrides/night1-slot1",
        params={"week": "current"},
        json={"title": "Hand Picked"},
    )
    r = client.delete("/admin/motd/overrides/night1-slot1", params={"week": "current"})
    assert r.status_code == 200
    assert r.json()["removed"] == 1
    week_key, _ = builder.week_context()
    assert await db.list_motd_overrides(week_key) == []


# --- uploading replacement art ---


async def test_upload_art_saves_file_and_override(db, tmp_path):
    client = _client(db, tmp_path)
    r = client.post(
        "/admin/motd/overrides/night1-slot1/art",
        data={"title": "Artful", "href": "https://example.com/n"},
        files={"file": ("poster.png", PNG, "image/png")},
    )
    assert r.status_code == 200, r.text
    poster_url = r.json()["poster_url"]
    assert poster_url.startswith("https://cdn.example/boxes/custom-")
    assert poster_url.endswith(".png")

    written = list((tmp_path / "boxes").glob("custom-*.png"))
    assert len(written) == 1
    assert written[0].read_bytes() == PNG

    week_key, _ = builder.week_context()
    rows = await db.list_motd_overrides(week_key)
    assert rows[0]["title"] == "Artful"
    assert rows[0]["poster_url"] == poster_url
    assert rows[0]["href"] == "https://example.com/n"


async def test_upload_art_rejects_a_non_image(db, tmp_path):
    client = _client(db, tmp_path)
    r = client.post(
        "/admin/motd/overrides/night1-slot1/art",
        files={"file": ("evil.svg", b"<svg/>", "image/svg+xml")},
    )
    assert r.status_code == 400
    assert "Unsupported image type" in r.json()["detail"]


async def test_upload_art_rejects_an_empty_file(db, tmp_path):
    client = _client(db, tmp_path)
    r = client.post(
        "/admin/motd/overrides/night1-slot1/art",
        files={"file": ("poster.png", b"", "image/png")},
    )
    assert r.status_code == 400


def test_upload_art_explains_an_unwritable_poster_dir(db, tmp_path, monkeypatch):
    """Regression: an unwritable poster_dir must not look like a generic 500.

    This is the production failure — the container's config still pointed at
    /home/mediacms.io/... so mkdir raised PermissionError and the admin saw an
    opaque 500 with no hint that the config was the problem.
    """
    client = _client(db, tmp_path)

    def _deny(self, *a, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("pathlib.Path.mkdir", _deny)
    r = client.post(
        "/admin/motd/overrides/night1-slot1/art",
        files={"file": ("poster.png", PNG, "image/png")},
    )
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "motd.poster_dir" in detail
    assert "not writable" in detail
    # Names the remedy rather than just restating the errno.
    assert "/var/lib/kryten-webqueue" in detail


# --- validation ---


async def test_save_rejects_a_non_http_poster_url(db, tmp_path):
    client = _client(db, tmp_path)
    r = client.put(
        "/admin/motd/overrides/night1-slot1",
        params={"week": "current"},
        json={"poster_url": "javascript:alert(1)"},
    )
    assert r.status_code == 400
    assert "http(s) URL" in r.json()["detail"]


async def test_save_rejects_an_invalid_slot_key(db, tmp_path):
    client = _client(db, tmp_path)
    r = client.put(
        "/admin/motd/overrides/night9-slot1",
        params={"week": "current"},
        json={"title": "Nope"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid slot key"


async def test_save_rejects_an_invalid_week(db, tmp_path):
    client = _client(db, tmp_path)
    r = client.put(
        "/admin/motd/overrides/night1-slot1",
        params={"week": "someday"},
        json={"title": "Nope"},
    )
    assert r.status_code == 400


async def test_save_requires_an_admin_session(db, tmp_path):
    client = _client(db, tmp_path)
    client.cookies.clear()
    r = client.put(
        "/admin/motd/overrides/night1-slot1",
        params={"week": "current"},
        json={"title": "Nope"},
    )
    assert r.status_code == 401


async def test_save_rejects_a_non_admin_session(db, tmp_path):
    app_client = _client(db, tmp_path)
    app_client.cookies.clear()
    app_client.cookies.set("session", create_session_token("viewer", 1, SECRET))
    r = app_client.put(
        "/admin/motd/overrides/night1-slot1",
        params={"week": "current"},
        json={"title": "Nope"},
    )
    assert r.status_code == 403
