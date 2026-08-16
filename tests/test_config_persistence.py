"""Config file persistence (O2 — inline promo config editing).

The promo admin panel edits ``Config.promos`` and persists it back to the source
file via :meth:`Config.save`. These tests cover the round-trip and the
no-source-path guard.
"""

import json

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from kryten_webqueue.config import Config
from kryten_webqueue.routes.admin_promos import update_promo_config


def _write_minimal_config(path):
    path.write_text(
        json.dumps(
            {
                "secret_key": "x" * 16,
                "api_gate_token": "tok",
                "mediacms_token": "tok",
            }
        ),
        encoding="utf-8",
    )


def test_save_round_trips_promo_edits(tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_minimal_config(cfg_path)

    config = Config.from_file(cfg_path)
    # Edit the promo config the way the admin PUT endpoint does.
    new_promos = config.promos.model_copy(deep=True)
    new_promos.enabled = False
    new_promos.general.every_n_items = 9
    new_promos.types["event"].weight = 7
    config.promos = new_promos
    config.save()

    reloaded = Config.from_file(cfg_path)
    assert reloaded.promos.enabled is False
    assert reloaded.promos.general.every_n_items == 9
    assert reloaded.promos.types["event"].weight == 7
    # Untouched secrets survive the rewrite.
    assert reloaded.api_gate_token == "tok"


def test_save_without_source_path_raises():
    config = Config(secret_key="x" * 16, api_gate_token="t", mediacms_token="t")
    with pytest.raises(RuntimeError):
        config.save()


def test_from_file_records_source_path(tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_minimal_config(cfg_path)
    config = Config.from_file(cfg_path)
    assert config._source_path == cfg_path


# --- Admin promo config PUT (persist + live-apply + rollback) ---------------


class _RecordingDirector:
    def __init__(self):
        self.applied = None

    def update_config(self, cfg):
        self.applied = cfg


class _FakeRequest:
    def __init__(self, body, config, director=None):
        self._body = body
        self.app = SimpleNamespace(
            state=SimpleNamespace(config=config, promo_director=director)
        )

    async def json(self):
        return self._body


async def test_update_promo_config_persists_and_applies(tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_minimal_config(cfg_path)
    config = Config.from_file(cfg_path)
    director = _RecordingDirector()

    body = config.promos.model_dump()
    body["enabled"] = False
    req = _FakeRequest(body, config, director)

    result = await update_promo_config(req, user={"username": "admin"})

    assert result["config"]["enabled"] is False
    assert config.promos.enabled is False
    assert director.applied is not None and director.applied.enabled is False
    # Persisted to disk.
    assert Config.from_file(cfg_path).promos.enabled is False


async def test_update_promo_config_rolls_back_on_persist_failure(tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_minimal_config(cfg_path)
    config = Config.from_file(cfg_path)
    assert config.promos.enabled is True

    # Point the source at a non-existent directory so the atomic write fails with
    # OSError, mirroring the read-only /etc sandbox that produced the production
    # 500. The live director must not be touched and the in-memory config must
    # roll back, so the panel never diverges from what promos are actually doing.
    config._source_path = tmp_path / "missing" / "config.json"
    director = _RecordingDirector()

    body = config.promos.model_dump()
    body["enabled"] = False
    req = _FakeRequest(body, config, director)

    with pytest.raises(HTTPException) as exc_info:
        await update_promo_config(req, user={"username": "admin"})

    assert exc_info.value.status_code == 500
    assert config.promos.enabled is True
    assert director.applied is None
