"""Tests for title normalisation applied at catalog sync time."""


from kryten_webqueue.catalog.images import (
    _normalize_leading_year,
    _strip_extension,
    _clean_title,
)


# --- _strip_extension ---


def test_strip_mp4():
    assert _strip_extension("Godzilla vs. Megalon.mp4") == "Godzilla vs. Megalon"


def test_strip_mkv():
    assert _strip_extension("Some Movie.mkv") == "Some Movie"


def test_strip_case_insensitive():
    assert _strip_extension("Title.MKV") == "Title"
    assert _strip_extension("Title.MP4") == "Title"


def test_strip_no_extension_unchanged():
    assert _strip_extension("Clean Title (1989)") == "Clean Title (1989)"


def test_strip_extension_not_in_middle():
    # Extension mid-title (e.g. after leading year) is NOT stripped.
    assert _strip_extension("(1973) Godzilla.mp4") == "(1973) Godzilla"


def test_strip_extension_with_leading_year():
    # Full pipeline order: strip ext THEN normalize year.
    raw = "(1973) Godzilla vs. Megalon.mp4"
    assert _strip_extension(raw) == "(1973) Godzilla vs. Megalon"
    assert (
        _normalize_leading_year(_strip_extension(raw)) == "Godzilla vs. Megalon (1973)"
    )


# --- _normalize_leading_year ---


def test_normalize_leading_year_parens():
    assert (
        _normalize_leading_year("(1989) Godzilla vs. Biollante")
        == "Godzilla vs. Biollante (1989)"
    )


def test_normalize_leading_year_brackets():
    assert (
        _normalize_leading_year("[1989] Godzilla vs. Biollante")
        == "Godzilla vs. Biollante (1989)"
    )


def test_normalize_trailing_year_unchanged():
    assert (
        _normalize_leading_year("Godzilla vs. Biollante (1989)")
        == "Godzilla vs. Biollante (1989)"
    )


def test_normalize_no_year_unchanged():
    assert _normalize_leading_year("Godzilla vs. Biollante") == "Godzilla vs. Biollante"


# --- _clean_title strips extensions too ---


def test_clean_title_strips_extension():
    cleaned, year = _clean_title("Godzilla vs. Megalon.mp4 (1973)")
    assert ".mp4" not in cleaned
    assert year == "1973"


def test_clean_title_leading_year_with_ext():
    # After the full pipeline the cleaned title should be extension-free.
    raw = "(1973) Godzilla vs. Megalon.mp4"
    normalized = _normalize_leading_year(_strip_extension(raw))
    cleaned, year = _clean_title(normalized)
    assert ".mp4" not in cleaned
    assert year == "1973"
    assert "Godzilla" in cleaned
