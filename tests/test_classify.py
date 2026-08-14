"""Tests for classify.py — hosted show detection, content-type determination."""

import pytest

from kryten_webqueue.catalog.enrichment.classify import (
    classify_item,
    HOSTED_SHOW_REGISTRY,
)


def _cls(title: str, duration: int = 7200, *, cover_art_source: str | None = None):
    return classify_item(title, title, duration, cover_art_source=cover_art_source)


# --- Hosted movies ---


def test_svengoolie_detected():
    c = _cls("Phantom of the Mall Svengoolie")
    assert c.content_type == "hosted_movie"
    assert c.hosted.show_name == "Svengoolie"
    assert c.hosted.cms_tag == "svengoolie"
    assert "Phantom" in c.lookup_title
    assert "Svengoolie" not in c.lookup_title


def test_monstervision_detected():
    c = _cls("Monstervision 01 Friday the 13th Part 1")
    assert c.content_type == "hosted_movie"
    assert "MonsterVision" in c.hosted.show_name
    assert c.hosted.cms_tag == "monstervision"
    assert "Friday" in c.lookup_title


def test_last_drive_in_detected():
    c = _cls("The Last Drive In - Piranha (1978)-008")
    assert c.content_type == "hosted_movie"
    assert "Last Drive-In" in c.hosted.show_name
    assert c.hosted.cms_tag == "lastdrivein"
    assert "Piranha" in c.lookup_title
    assert c.lookup_year == "1978"


def test_jbbtldi_alias():
    c = _cls("JBBTLDI (2021) S3-Wk 1 Film 2 THE HOUSE BY THE CEMETERY (1981)")
    assert c.content_type == "hosted_movie"
    assert c.hosted.cms_tag == "lastdrivein"


def test_rifftrax_detected():
    c = _cls("Rifftrax Presents Krull 1983")
    assert c.content_type == "riffed_movie"
    assert c.hosted.show_name == "RiffTrax"
    assert c.hosted.cms_tag == "rifftrax"
    assert "Krull" in c.lookup_title


def test_mst3k_detected():
    c = _cls("MST3K 910: The Final Sacrifice (FULL MOVIE)")
    assert c.content_type == "riffed_movie"
    assert "Mystery Science Theater" in c.hosted.show_name
    assert c.hosted.cms_tag == "mst3k"


# --- TV episodes ---


def test_tv_episode_sxe():
    c = _cls("The X-Files - S01E01 - Pilot", 2746)
    assert c.content_type == "tv_episode"
    assert c.tv_season == 1
    assert c.tv_episode == 1


def test_tv_episode_season_word():
    c = _cls("Game of Thrones Season 3 Episode 9", 3400)
    assert c.content_type == "tv_episode"


# --- Movies ---


def test_standard_movie():
    c = _cls("Aliens (1986)")
    assert c.content_type == "movie"
    assert c.lookup_title == "Aliens"
    assert c.lookup_year == "1986"


def test_leading_year_movie():
    c = _cls("(1989) Godzilla vs. Biollante")
    assert c.content_type == "movie"
    assert c.lookup_year == "1989"
    assert "Godzilla" in c.lookup_title


def test_extension_stripped_before_classify():
    c = _cls("(1973) Godzilla vs. Megalon.mp4")
    assert ".mp4" not in c.lookup_title
    assert c.lookup_year == "1973"


# --- Short / unknown ---


def test_short_item_unknown():
    c = _cls("Some Clip", 300)
    assert c.content_type == "unknown"


# --- has_real_art ---


def test_has_real_art_tmdb():
    c = _cls("Aliens (1986)", cover_art_source="tmdb")
    assert c.has_real_art is True


def test_has_real_art_thumbnail():
    c = _cls("Aliens (1986)", cover_art_source="thumbnail")
    assert c.has_real_art is False


# --- HOSTED_SHOW_REGISTRY completeness ---


def test_all_registry_entries_have_cms_tag():
    for entry in HOSTED_SHOW_REGISTRY:
        assert entry.cms_tag, f"{entry.show_name} missing cms_tag"
