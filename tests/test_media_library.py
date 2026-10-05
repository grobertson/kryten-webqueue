"""Media library: upload sanitizing, stable URLs, reference-guarded delete, serving."""

import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from kryten_webqueue.app import create_app
from kryten_webqueue.auth.session import create_session_token
from kryten_webqueue.catalog.db import Database
from kryten_webqueue.config import Config, DatabaseConfig
from kryten_webqueue.motd import media
from kryten_webqueue.routes.admin_media import router as media_router

SECRET = "z" * 32
LIMITS = {"max_pixels": 40_000_000, "max_frames": 1000}


def _gif(colors=((255, 0, 0), (0, 255, 0), (0, 0, 255)), durations=(100, 250, 400)):
    frames = [Image.new("RGB", (40, 30), c) for c in colors]
    out = io.BytesIO()
    frames[0].save(
        out,
        "GIF",
        save_all=True,
        append_images=frames[1:],
        duration=list(durations),
        loop=0,
        comment=b"secret-comment",
    )
    return out.getvalue()


def _webp():
    frames = [Image.new("RGB", (40, 30), c) for c in ((255, 0, 0), (0, 255, 0))]
    out = io.BytesIO()
    frames[0].save(
        out,
        "WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=[120, 300],
        loop=3,
    )
    return out.getvalue()


def _jpeg_with_exif():
    img = Image.new("RGB", (64, 32), (10, 20, 30))
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"
    exif[0x0112] = 6
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif.tobytes(), comment=b"jpeg-comment")
    return out.getvalue()


def _png():
    out = io.BytesIO()
    Image.new("RGBA", (8, 8), (1, 2, 3, 4)).save(out, "PNG")
    return out.getvalue()


def _durations(data):
    img = Image.open(io.BytesIO(data))
    result = []
    for i in range(getattr(img, "n_frames", 1)):
        img.seek(i)
        img.load()
        result.append(img.info.get("duration"))
    return result, img.info.get("loop")


# --- processing ----------------------------------------------------------------


def test_animated_gif_keeps_frames_timing_and_loop_but_drops_comments():
    processed = media.process_image(_gif(), **LIMITS)
    assert processed.animated and processed.frame_count == 3
    assert _durations(processed.data) == ([100, 250, 400], 0)
    assert b"secret-comment" not in processed.data


def test_animated_webp_keeps_frames_timing_and_loop():
    processed = media.process_image(_webp(), **LIMITS)
    assert processed.frame_count == 2
    assert _durations(processed.data) == ([120, 300], 3)


def test_jpeg_exif_and_comment_are_stripped_after_applying_orientation():
    processed = media.process_image(_jpeg_with_exif(), **LIMITS)
    assert b"SecretCam" not in processed.data
    assert b"jpeg-comment" not in processed.data
    assert "exif" not in Image.open(io.BytesIO(processed.data)).info
    assert Image.open(io.BytesIO(processed.data)).size == (32, 64)


@pytest.mark.parametrize(
    "data,match",
    [
        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "Unsupported"),
        (b"\x00\x00\x00\x18ftypmp42", "Unsupported"),
        (b"\x89PNG\r\n\x1a\nnot really", "not a readable image"),
    ],
)
def test_non_images_and_corrupt_files_are_rejected(data, match):
    with pytest.raises(media.MediaError, match=match):
        media.process_image(data, **LIMITS)


def test_pixel_and_frame_limits():
    with pytest.raises(media.MediaError, match="pixels"):
        media.process_image(_png(), max_pixels=10, max_frames=10)
    with pytest.raises(media.MediaError, match="frames"):
        media.process_image(_gif(), max_pixels=10_000, max_frames=2)


def test_slugify():
    assert media.slugify("Halloween Banner (Final).GIF") == "halloween-banner-final"
    assert media.slugify("../../etc/passwd") == "passwd"
    assert media.slugify("!!!") == "image"
    assert media.slugify(None) == "image"


# --- routes --------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    database = Database(
        DatabaseConfig(
            backend="sqlite",
            layout="monolith",
            data_dir=str(tmp_path),
            db_path=str(tmp_path / "media.db"),
        )
    )
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


def _config(db, tmp_path, **media_kw):
    payload = {
        "secret_key": SECRET,
        "api_gate_token": "t",
        "mediacms_token": "t",
        "database": db.db_config.model_dump(),
        "image_dir": str(tmp_path / "images"),
        "emote_rehost": {"static_dir": str(tmp_path / "emotes")},
        "motd": {
            "poster_dir": str(tmp_path / "boxes"),
            "output_dir": str(tmp_path / "out"),
        },
        "media": {
            "dir": str(tmp_path / "media" / "public"),
            "trash_dir": str(tmp_path / "media" / "trash"),
            "base_url": "https://q.example/media",
            **media_kw,
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return Config.from_file(path)


def _client(db, tmp_path, **media_kw):
    from fastapi import FastAPI

    app = FastAPI()
    app.state.db = db
    app.state.config = _config(db, tmp_path, **media_kw)
    app.include_router(media_router)
    return TestClient(app, cookies={"session": create_session_token("bob", 3, SECRET)})


async def test_upload_returns_a_stable_url_and_sniffs_the_real_type(db, tmp_path):
    client = _client(db, tmp_path)
    r = client.post(
        "/admin/media/assets",
        data={"slug": "Pumpkin Patch", "description": "Halloween"},
        # Lies about its type: the bytes are a GIF.
        files={"file": ("pumpkin.png", _gif(), "image/png")},
    )
    assert r.status_code == 201, r.text
    asset = r.json()
    assert asset["slug"] == "pumpkin-patch"
    assert asset["url"] == "https://q.example/media/pumpkin-patch.gif"
    assert asset["snippet"] == '{{ media_url("pumpkin-patch") }}'
    assert asset["mime"] == "image/gif" and asset["animated"] is True
    stored = tmp_path / "media" / "public" / "pumpkin-patch.gif"
    assert b"secret-comment" not in stored.read_bytes()

    again = client.post(
        "/admin/media/assets",
        data={"slug": "pumpkin patch"},
        files={"file": ("x.jpg", _jpeg_with_exif(), "image/jpeg")},
    ).json()
    assert again["slug"] == "pumpkin-patch-2" and again["url"].endswith(
        "pumpkin-patch-2.jpg"
    )

    listing = client.get("/admin/media/assets", params={"q": "halloween"}).json()[
        "assets"
    ]
    assert [a["slug"] for a in listing] == ["pumpkin-patch"]
    audit = await db.list_admin_audit("media_asset", "pumpkin-patch")
    assert audit[0]["actor"] == "bob" and audit[0]["action"] == "media.upload"


async def test_upload_rejections(db, tmp_path):
    client = _client(db, tmp_path, max_image_bytes=1024)
    big = client.post(
        "/admin/media/assets",
        files={"file": ("a.gif", b"GIF89a" + b"0" * 2000, "image/gif")},
    )
    assert big.status_code == 413
    svg = client.post(
        "/admin/media/assets",
        files={"file": ("a.svg", b"<svg></svg>", "image/svg+xml")},
    )
    assert svg.status_code == 422
    empty = client.post(
        "/admin/media/assets", files={"file": ("a.gif", b"", "image/gif")}
    )
    assert empty.status_code == 400
    assert (
        not list((tmp_path / "media" / "public").glob("*"))
        if (tmp_path / "media" / "public").exists()
        else True
    )


async def test_delete_is_blocked_by_templates_and_never_recycles_the_url(db, tmp_path):
    client = _client(db, tmp_path)
    client.post(
        "/admin/media/assets",
        data={"slug": "banner"},
        files={"file": ("b.png", _png(), "image/png")},
    )
    body = '{{ movie_grid() }}<img src="{{ media_url("banner") }}">'
    await db.create_motd_template(
        name="uses-banner",
        kind="master",
        display_name="x",
        description=None,
        body=body,
        body_sha256="h",
        zones=[],
        note=None,
        created_by="t",
    )
    blocked = client.delete("/admin/media/assets/banner")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["templates"] == ["uses-banner"]

    template = await db.get_motd_template("uses-banner")
    await db.archive_motd_template(template["id"])
    assert client.delete("/admin/media/assets/banner").status_code == 200
    assert not (tmp_path / "media" / "public" / "banner.png").exists()
    assert list((tmp_path / "media" / "trash").glob("banner.png.*"))
    assert client.get("/admin/media/assets").json()["assets"] == []

    reuse = client.post(
        "/admin/media/assets",
        data={"slug": "banner"},
        files={"file": ("b.png", _png(), "image/png")},
    ).json()
    assert reuse["slug"] == "banner-2"


async def test_member_cannot_upload(db, tmp_path):
    client = _client(db, tmp_path)
    client.cookies.set("session", create_session_token("eve", 2, SECRET))
    r = client.post(
        "/admin/media/assets", files={"file": ("b.png", _png(), "image/png")}
    )
    assert r.status_code == 403


async def test_app_serves_media_with_nosniff_and_hides_trash(db, tmp_path):
    config = _config(db, tmp_path)
    app = create_app(config)
    public = tmp_path / "media" / "public"
    (public / "logo.png").write_bytes(_png())
    client = TestClient(app)
    r = client.get("/media/logo.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/media/").status_code == 404
    assert client.get("/media/../config.json").status_code == 404


async def test_admin_pages_render(db, tmp_path):
    client = TestClient(
        create_app(_config(db, tmp_path)),
        cookies={"session": create_session_token("bob", 3, SECRET)},
    )
    motd = client.get("/admin/motd")
    assert motd.status_code == 200
    assert "admin-motd-templates.js" in motd.text and 'id="tpl-body"' in motd.text
    assert "{{ movie_grid() }}" in motd.text
    page = client.get("/admin/media")
    assert page.status_code == 200 and "admin-media.js" in page.text
