from pathlib import Path

import pytest

from kryten_webqueue.jobs.rehost_emotes import (
    build_emote_manifest,
    normalize_emote_name,
    unique_emote_filename,
)


def test_normalize_emote_name_removes_hash_underscores_and_dashes():
    assert normalize_emote_name("##my_favorite-gif") == "myfavoritegif"


def test_unique_emote_filename_suffixes_collisions(tmp_path: Path):
    used = {"beer"}
    assert unique_emote_filename(Path("#beer.gif"), tmp_path, used).name == "beer2.gif"
    assert unique_emote_filename(Path("beer.webp"), tmp_path, used).name == "beer3.webp"


def test_build_emote_manifest_uses_canonical_names_and_urls(tmp_path: Path):
    (tmp_path / "#my_favorite-gif.GIF").write_bytes(b"gif")
    (tmp_path / "ignored.png").write_bytes(b"png")

    assert build_emote_manifest(tmp_path, "https://queue.example/emotes/images") == [
        {
            "name": "#myfavoritegif",
            "image": "https://queue.example/emotes/images/myfavoritegif.gif",
        }
    ]


def test_build_emote_manifest_rejects_canonical_collisions(tmp_path: Path):
    (tmp_path / "#foo-bar.gif").write_bytes(b"one")
    (tmp_path / "foo_bar.webp").write_bytes(b"two")

    with pytest.raises(ValueError, match="collision"):
        build_emote_manifest(tmp_path, "https://queue.example/emotes/images")
