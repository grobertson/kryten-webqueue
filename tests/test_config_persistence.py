"""Config file persistence (O2 — inline promo config editing).

The promo admin panel edits ``Config.promos`` and persists it back to the source
file via :meth:`Config.save`. These tests cover the round-trip and the
no-source-path guard.
"""

import json

import pytest

from kryten_webqueue.config import Config


def _write_minimal_config(path):
    path.write_text(json.dumps({
        "secret_key": "x" * 16,
        "api_gate_token": "tok",
        "mediacms_token": "tok",
    }), encoding="utf-8")


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
