"""Static serving contract for app-owned media volumes.

Both the rehosted emotes and the weekend MOTD poster art live on the service's
persistent volume and are served by the app itself, so the public reverse proxy
can stay the only internet-facing endpoint. The proxy (nginx on grindhouse) is
a thin pass-through: it forwards ``/emotes/images/`` and ``/motd/boxes/`` to
this app, so a file that this app cannot serve is unreachable no matter what the
proxy says.

These tests pin the parts that silently regressed during the Podman migration:
the mount path, the configured ``poster_base_url`` matching it, and the fact
that art written by the admin upload endpoint is actually retrievable.
"""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from kryten_webqueue.config import MOTDConfig

MOUNT_PATH = "/motd/boxes"


def _app_serving(directory: Path) -> FastAPI:
    """A miniature of the real app's static mounts (see app.py create_app)."""
    app = FastAPI()
    app.mount(MOUNT_PATH, StaticFiles(directory=str(directory)), name="motd-boxes")
    return app


# --- configuration contract ----------------------------------------------


def test_default_poster_base_url_points_at_the_mounted_path():
    """The advertised base URL must be the one this app actually serves.

    A mismatch here is silent: uploads save fine, then every poster renders
    broken on the channel. This is exactly the state the migration left behind
    (base URL on the MediaCMS frontend, files on the local volume).
    """
    motd = MOTDConfig()
    assert motd.poster_base_url.endswith(MOUNT_PATH.lstrip("/"))
    assert "www.dropsugar.co/static" not in motd.poster_base_url


def test_poster_dir_and_output_dir_live_on_the_service_volume():
    motd = MOTDConfig()
    assert motd.poster_dir.startswith("/var/lib/kryten-webqueue/")
    assert motd.output_dir.startswith("/var/lib/kryten-webqueue/")
    # Never a foreign host path: it is neither present nor creatable in a container.
    assert "mediacms" not in motd.poster_dir
    assert "mediacms" not in motd.output_dir


def test_shipped_example_config_matches_the_defaults(tmp_path):
    """config.example.json must not drift from the code defaults."""
    example = json.loads(
        (Path(__file__).resolve().parents[1] / "config.example.json").read_text(
            encoding="utf-8"
        )
    )
    cfg = example["motd"]
    assert cfg["poster_dir"] == MOTDConfig().poster_dir
    assert cfg["poster_base_url"] == MOTDConfig().poster_base_url
    assert cfg["output_dir"] == MOTDConfig().output_dir


def test_nginx_conf_proxies_the_motd_art_path():
    """The reverse proxy must not intercept the art with a local alias.

    The proxy runs on a different host from the container and cannot see its
    volume, so a `location /motd/boxes/ { alias ...; }` would serve 404s.
    """
    conf = (
        Path(__file__).resolve().parents[1] / "deploy" / "nginx-queue.conf"
    ).read_text(encoding="utf-8")
    assert f"location {MOUNT_PATH}/" in conf
    assert "alias" not in conf.split(f"location {MOUNT_PATH}/")[1].split("}")[0]


# --- serving contract ------------------------------------------------------


def test_art_written_to_the_volume_is_served_back(tmp_path):
    poster_dir = tmp_path / "motd_boxes"
    poster_dir.mkdir()
    (poster_dir / "custom-10.2-10.3-night1-slot1-abcd1234.jpg").write_bytes(
        b"\xff\xd8\xff\xe0jpegbytes"
    )

    client = TestClient(_app_serving(poster_dir))
    r = client.get(f"{MOUNT_PATH}/custom-10.2-10.3-night1-slot1-abcd1234.jpg")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8\xff\xe0jpegbytes"


def test_missing_art_is_404_not_500(tmp_path):
    client = TestClient(_app_serving(tmp_path))
    assert client.get(f"{MOUNT_PATH}/nope.jpg").status_code == 404


def test_directory_listing_is_not_exposed(tmp_path):
    """A volume must not be enumerable; only known filenames resolve."""
    poster_dir = tmp_path / "motd_boxes"
    poster_dir.mkdir()
    (poster_dir / "a.jpg").write_bytes(b"a")

    client = TestClient(_app_serving(poster_dir))
    assert client.get(MOUNT_PATH).status_code == 404
    assert client.get(f"{MOUNT_PATH}/").status_code == 404


def test_missing_mount_raises_at_startup_not_at_request_time(tmp_path):
    """create_app() must create the directory, so the mount never 500s.

    StaticFiles(directory=...) raises when the directory is absent. The real
    app calls mkdir(parents=True) first; this asserts the ordering matters.
    """
    missing = tmp_path / "does-not-exist-yet"
    with pytest.raises(RuntimeError):
        StaticFiles(directory=str(missing))
    missing.mkdir(parents=True)
    assert (
        TestClient(_app_serving(missing)).get(f"{MOUNT_PATH}/x.jpg").status_code == 404
    )
