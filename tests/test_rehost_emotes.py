from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kryten_webqueue.config import EmoteRehostConfig
from kryten_webqueue.jobs import rehost_emotes
from kryten_webqueue.jobs.rehost_emotes import (
    build_emote_manifest,
    normalize_emote_name,
    rehost_emotes_job,
    unique_emote_filename,
)


class _FakeApiGate:
    def __init__(self, emotes: list[dict]):
        self.emotes = emotes
        self.updates: list[tuple[str, str]] = []
        self.replaced: list[list[dict]] = []

    async def get_emotes(self) -> list[dict]:
        return [dict(e) for e in self.emotes]

    async def update_emote(self, name: str, image: str) -> dict:
        self.updates.append((name, image))
        for emote in self.emotes:
            if emote["name"] == name:
                emote["image"] = image
        return {}

    async def replace_emotes(self, emotes: list[dict]) -> dict:
        self.replaced.append(emotes)
        self.emotes = [dict(emote) for emote in emotes]
        return {}


def _ctx(tmp_path: Path, api: _FakeApiGate, **overrides) -> SimpleNamespace:
    cfg = EmoteRehostConfig(
        static_dir=str(tmp_path / "images"),
        base_url="https://queue.dropsugar.co/emotes/images",
        manifest_path=str(tmp_path / "emotes.json"),
        backup_dir=str(tmp_path / "backups"),
        inter_emote_delay_sec=0,
        **overrides,
    )

    async def progress(_: dict) -> None:
        return None

    return SimpleNamespace(
        config=SimpleNamespace(emote_rehost=cfg), api_gate=api, progress=progress
    )


@pytest.mark.parametrize("sync_disk_manifest", [False, True])
async def test_job_never_replaces_live_emote_list_from_disk(
    tmp_path: Path, monkeypatch, sync_disk_manifest: bool
):
    """Regression: a disk-derived replace wiped emotes added since the last run."""
    images = tmp_path / "images"
    images.mkdir()
    (images / "old.gif").write_bytes(b"gif")
    api = _FakeApiGate(
        [
            {
                "name": "#old",
                "image": "https://queue.dropsugar.co/emotes/images/old.gif",
            },
            {"name": "#brand_new", "image": "https://media.giphy.com/x/new.gif"},
        ]
    )

    def fake_place(url, bare, static_dir, max_retries):
        (static_dir / f"{bare}.gif").write_bytes(b"gif")
        return ".gif"

    monkeypatch.setattr(rehost_emotes, "_place_emote", fake_place)
    result = await rehost_emotes_job(
        {}, _ctx(tmp_path, api, sync_disk_manifest=sync_disk_manifest)
    )

    assert api.replaced == []
    assert api.updates == [
        ("#brand_new", "https://queue.dropsugar.co/emotes/images/brandnew.gif")
    ]
    assert result["succeeded"] == 1
    assert result["manifest_count"] == 2


@pytest.mark.parametrize("sync_disk_manifest", [False, True])
async def test_leathertowel_download_uses_hashtag_filename_and_survives_next_run(
    tmp_path: Path, monkeypatch, sync_disk_manifest: bool
):
    source_url = "https://i.ibb.co/b5TV82xR/leathertowelcardgif.gif"
    destination_url = "https://queue.dropsugar.co/emotes/images/leathertowel.gif"
    images = tmp_path / "images"
    images.mkdir()
    (images / "old.gif").write_bytes(b"old image")
    api = _FakeApiGate(
        [
            {
                "name": "#old",
                "image": "https://queue.dropsugar.co/emotes/images/old.gif",
            },
            {"name": "#leathertowel", "image": source_url},
        ]
    )
    image_bytes = b"GIF89a downloaded image"
    response = Mock(status_code=200, headers={"content-type": "image/gif"})
    response.iter_content.return_value = [image_bytes]
    session = Mock()
    session.get.return_value = response
    monkeypatch.setattr(rehost_emotes, "_make_session", lambda: session)
    monkeypatch.setattr(rehost_emotes, "_set_permissions", Mock())
    ctx = _ctx(tmp_path, api, sync_disk_manifest=sync_disk_manifest)

    first = await rehost_emotes_job({}, ctx)
    second = await rehost_emotes_job({}, ctx)

    assert session.get.call_args.args == (source_url,)
    session.get.assert_called_once()
    assert (images / "leathertowel.gif").read_bytes() == image_bytes
    assert not (images / "leathertowelcardgif.gif").exists()
    assert api.updates == [("#leathertowel", destination_url)]
    assert api.replaced == []
    assert api.emotes[-1] == {"name": "#leathertowel", "image": destination_url}
    assert first["succeeded"] == 1
    assert second["attempted"] == 0
    assert second["total_emotes"] == 2


async def test_job_does_not_push_url_for_permanently_dead_source(
    tmp_path: Path, monkeypatch
):
    api = _FakeApiGate(
        [{"name": "#gone", "image": "https://media.giphy.com/x/gone.gif"}]
    )
    monkeypatch.setattr(rehost_emotes, "_place_emote", lambda *a: rehost_emotes._DEAD)

    result = await rehost_emotes_job({}, _ctx(tmp_path, api))

    assert api.updates == []
    assert result["failed_emotes"] == ["#gone"]


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
