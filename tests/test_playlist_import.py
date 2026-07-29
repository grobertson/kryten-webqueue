"""Bulk text import parser (admin playlists) — v0.9.6.

Covers dropsugar.co URL resolution, YouTube/youtu.be id extraction with arg
stripping, comment handling (whole-line + inline), and tolerant skipping of
unknown sites.
"""

import pytest

from kryten_webqueue.catalog.db import Database
from kryten_webqueue.playlists.importer import (
    import_playlist_text,
    extract_youtube_id,
    extract_dropsugar_token,
)

MEDIACMS = "https://www.dropsugar.com"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    await database.run_migrations()
    yield database
    await database.close()


async def _add_item(db, token, title="Title", *, duration=600):
    await db.insert_catalog(
        {
            "friendly_token": token,
            "title": title,
            "description": "",
            "duration_sec": duration,
            "manifest_url": f"{MEDIACMS}/api/v1/media/cytube/{token}.json?format=json",
            "thumbnail_url": "",
            "synced_at": "2026-01-01T00:00:00+00:00",
        }
    )


# --- pure URL helpers ---


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&t=10", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?list=PL123&v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ?start=5", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/playlist?list=PL123", None),
    ],
)
def test_extract_youtube_id(url, expected):
    assert extract_youtube_id(url) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.dropsugar.co/view?m=kBl82FCgy", "kBl82FCgy"),
        ("https://www.dropsugar.co/view?m=kBl82FCgy&foo=bar", "kBl82FCgy"),
        (
            "https://www.dropsugar.com/api/v1/media/cytube/abc123.json?format=json",
            "abc123",
        ),
        ("https://www.dropsugar.co/", None),
    ],
)
def test_extract_dropsugar_token(url, expected):
    assert extract_dropsugar_token(url) == expected


# --- full parse ---


async def test_youtube_links_stripped_to_yt_items(db):
    text = (
        "https://youtu.be/dQw4w9WgXcQ?t=42\n"
        "https://www.youtube.com/watch?v=abcdefghijk&list=PL9&t=3\n"
    )
    out = await import_playlist_text(db, text, mediacms_url=MEDIACMS)
    assert [i["media_id"] for i in out["items"]] == ["dQw4w9WgXcQ", "abcdefghijk"]
    assert all(i["media_type"] == "yt" for i in out["items"])
    assert not out["errors"]


async def test_dropsugar_url_resolves_catalog(db):
    await _add_item(db, "kBl82FCgy", "Hollis Live")
    out = await import_playlist_text(
        db,
        "https://www.dropsugar.co/view?m=kBl82FCgy - Hollis Live on Channel-Z",
        mediacms_url=MEDIACMS,
    )
    assert len(out["items"]) == 1
    it = out["items"][0]
    assert it["media_type"] == "cm"
    assert (
        it["media_id"] == f"{MEDIACMS}/api/v1/media/cytube/kBl82FCgy.json?format=json"
    )
    assert it["title"] == "Hollis Live"
    assert not out["errors"]


async def test_dropsugar_url_not_in_catalog_constructs_manifest(db):
    out = await import_playlist_text(
        db,
        "https://www.dropsugar.co/view?m=newtoken123 - Some Title",
        mediacms_url=MEDIACMS,
    )
    assert len(out["items"]) == 1
    it = out["items"][0]
    assert it["media_type"] == "cm"
    assert (
        it["media_id"] == f"{MEDIACMS}/api/v1/media/cytube/newtoken123.json?format=json"
    )
    assert it["title"] == "Some Title"  # trailing text used as title hint


async def test_comments_and_blanks_and_unknown_sites(db):
    await _add_item(db, "good1")
    text = (
        "# a whole-line comment\n"
        "\n"
        "https://www.dropsugar.co/view?m=good1  # inline comment stripped\n"
        "https://example.com/whatever\n"
        "   \n"
        "https://vimeo.com/12345 # unknown site\n"
    )
    out = await import_playlist_text(db, text, mediacms_url=MEDIACMS)
    # Only the dropsugar line resolves; the two unknown sites are skipped.
    assert len(out["items"]) == 1
    assert out["items"][0]["media_id"].endswith("good1.json?format=json")
    reasons = {e["reason"] for e in out["errors"]}
    assert reasons == {"unsupported_site"}
    assert len(out["errors"]) == 2


async def test_legacy_tokens_still_work(db):
    await _add_item(db, "bare1", "Bare One")
    text = "yt:dQw4w9WgXcQ\ncm:cmtoken\nbare1\nunknownbare\n"
    out = await import_playlist_text(db, text, mediacms_url=MEDIACMS)
    yt = [i for i in out["items"] if i["media_type"] == "yt"]
    cm = [i for i in out["items"] if i["media_type"] == "cm"]
    assert yt[0]["media_id"] == "dQw4w9WgXcQ"
    # bare1 resolves to its manifest URL; cm:cmtoken (not in catalog) now falls
    # back to a constructed manifest URL so not-yet-synced items still play.
    assert any(i["media_id"].endswith("bare1.json?format=json") for i in cm)
    assert any(
        i["media_id"] == f"{MEDIACMS}/api/v1/media/cytube/cmtoken.json?format=json"
        for i in cm
    )
    # unknownbare is not in catalog -> error
    assert any(e["token"] == "unknownbare" for e in out["errors"])
