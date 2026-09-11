"""Build the weekend MOTD poster grid from the schedule workbook.

Blocking by design (OMDB lookups and poster downloads use ``requests``, mirroring
the ``motd_posters`` job); the async job runs :func:`build_slots` via
``asyncio.to_thread``.

Slot keys are stable positions — ``night{N}-slot{M}`` — so an admin override
pins a grid position for a week regardless of whether the underlying title is
later resolved, renamed, or still missing.
"""

from __future__ import annotations

import datetime
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..integrations.cmsutils.fetchurls import upcoming_weekend_sheet
from ..integrations.cmsutils.motdposters import (
    _NIGHT_LABELS,
    _NIGHT_MAP,
    _SECTION_ORDER,
    _download_image,
    _poster_filename,
    _resolve_omdb,
    extract_movies_by_section,
    load_workbook_bytes,
    resolve_weekend_sheet,
)

logger = logging.getLogger(__name__)

SLOT_KEY_RE = re.compile(r"^night[1-3]-slot([1-9]\d{0,2})$")

# Every night the workbook can describe. The grid itself covers only the nights
# in ``motd.nights`` — Sunday has no schedule yet, so its titles are parsed but
# never placed.
_NIGHTS = (1, 2, 3)


@dataclass
class MOTDSlot:
    """One position in the poster grid."""

    slot_key: str
    night: int
    night_label: str
    date: str
    position: int
    title: str | None = None
    imdb_id: str | None = None
    poster_url: str = ""
    href: str = ""
    source: str = "mystery"  # "omdb" | "override" | "mystery"
    note: str = ""

    @property
    def resolved(self) -> bool:
        return self.source != "mystery"

    def to_dict(self) -> dict:
        return {
            "slot_key": self.slot_key,
            "night": self.night,
            "night_label": self.night_label,
            "date": self.date,
            "position": self.position,
            "title": self.title,
            "imdb_id": self.imdb_id,
            "poster_url": self.poster_url,
            "href": self.href,
            "source": self.source,
            "resolved": self.resolved,
            "note": self.note,
        }


@dataclass
class MOTDWeek:
    """A built weekend: the sheet it came from, its dates, and its slots."""

    week_key: str
    friday: datetime.date
    saturday: datetime.date
    sunday: datetime.date
    slots: list[MOTDSlot] = field(default_factory=list)
    workbook_found: bool = True
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "week_key": self.week_key,
            "friday": self.friday.isoformat(),
            "saturday": self.saturday.isoformat(),
            "sunday": self.sunday.isoformat(),
            "workbook_found": self.workbook_found,
            "warnings": self.warnings,
            "slots": [s.to_dict() for s in self.slots],
        }


def week_context(
    today: datetime.date | None = None, *, week_offset: int = 0
) -> tuple[str, datetime.date]:
    """Return ``(week_key, friday)`` for the current/upcoming weekend.

    ``week_offset=1`` targets the *following* weekend, which is how the grid can
    debut on Sunday (while weekend traffic is still watching) instead of waiting
    for the sheet to roll over on Monday.
    """
    base = (today or datetime.date.today()) + datetime.timedelta(weeks=week_offset)
    week_key, friday, _ = upcoming_weekend_sheet(base)
    return week_key, friday


def _imdb_href(imdb_id: str | None) -> str:
    return f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else ""


def _grid_nights(motd_cfg) -> tuple[int, ...]:
    """Nights the grid reserves space for, in display order."""
    configured = getattr(motd_cfg, "nights", None) or _NIGHTS
    nights = tuple(int(n) for n in configured if int(n) in _NIGHTS)
    return nights or _NIGHTS


def _slot_counts(
    titles_by_night: dict[int, list[str]], nights: tuple[int, ...], slots: int
) -> dict[int, int]:
    """Decide how many positions each night gets.

    The schedule drives the shape — a 5/7 weekend renders 5/7, not an even 6/6 —
    and a night is never truncated, so a scheduled movie can't be hidden. Only
    when the weekend is still thin is the grid padded up to ``slots``, handing
    each extra to whichever night is furthest below its even share.
    """
    counts = {n: len(titles_by_night.get(n, [])) for n in nights}
    share = slots / len(nights)
    while sum(counts.values()) < slots:
        night = min(nights, key=lambda n: (counts[n] - share, nights.index(n)))
        counts[night] += 1
    return counts


def _blank_grid(
    friday: datetime.date,
    saturday: datetime.date,
    sunday: datetime.date,
    counts: dict[int, int],
    nights: tuple[int, ...],
) -> list[MOTDSlot]:
    """Lay out the per-night positions in display order."""
    dates = {1: friday, 2: saturday, 3: sunday}
    grid: list[MOTDSlot] = []
    for night in nights:
        for position in range(1, counts[night] + 1):
            grid.append(
                MOTDSlot(
                    slot_key=f"night{night}-slot{position}",
                    night=night,
                    night_label=_NIGHT_LABELS.get(night, f"Night {night}"),
                    date=dates[night].isoformat(),
                    position=position,
                )
            )
    return grid


def _titles_by_night(movies_by_section: dict[str, list[str]]) -> dict[int, list[str]]:
    """Flatten the workbook's sections into per-night title lists, in air order."""
    by_night: dict[int, list[str]] = {n: [] for n in _NIGHTS}
    for slug in _SECTION_ORDER:
        night = _NIGHT_MAP.get(slug, 1)
        by_night.setdefault(night, []).extend(movies_by_section.get(slug, []))
    return by_night


def mystery_pool(config, cover_art) -> list[str]:
    """Absolute URLs for the branded placeholder art the browse view uses.

    ``list_placeholder_urls`` returns site-relative ``/images/...`` paths; CyTube
    renders the MOTD off-site, so they have to be absolutized.
    """
    if cover_art is None:
        return []
    base = str(getattr(config.motd, "mystery_box_base_url", "") or "").rstrip("/")
    if not base:
        return []
    return [base + url for url in cover_art.list_placeholder_urls()]


def _apply_mystery(slot: MOTDSlot, motd_cfg, pool: list[str]) -> None:
    slot.source = "mystery"
    # Vary the art across the grid the way browse varies it across tiles.
    slot.poster_url = (
        random.choice(pool)
        if pool
        else (getattr(motd_cfg, "mystery_box_url", "") or "")
    )
    slot.href = getattr(motd_cfg, "mystery_box_href", "") or ""


def build_slots(
    config,
    *,
    overrides: dict[str, dict] | None = None,
    workbook_path: str = "",
    refresh_art: bool = False,
    dry_run: bool = False,
    today: datetime.date | None = None,
    week_offset: int = 0,
    mystery_urls: list[str] | None = None,
    emit=None,
) -> MOTDWeek:
    """Resolve the upcoming weekend into a complete grid of poster slots.

    ``overrides`` maps ``slot_key`` → ``{title, poster_url, href}``; any non-empty
    field wins over the auto-resolved value and marks the slot as an override, so
    a curator can pin art or a non-IMDb link for a title the lookup can't reach.
    """

    def _say(detail: dict) -> None:
        if emit:
            emit(detail)

    overrides = overrides or {}
    pool = mystery_urls or []
    motd_cfg = getattr(config, "motd", None)
    slot_count = int(getattr(motd_cfg, "slots", 12) or 12)
    poster_dir = Path(
        getattr(motd_cfg, "poster_dir", "/home/mediacms.io/mediacms/static/motd_boxes")
    ).expanduser()
    poster_base_url = str(getattr(motd_cfg, "poster_base_url", "") or "").rstrip("/")
    omdb_key = getattr(config, "omdb_api_key", "") or ""

    base_day = (today or datetime.date.today()) + datetime.timedelta(weeks=week_offset)
    week_key, friday, saturday = upcoming_weekend_sheet(base_day)
    sunday = friday + datetime.timedelta(days=2)
    week = MOTDWeek(week_key=week_key, friday=friday, saturday=saturday, sunday=sunday)

    # The workbook is best-effort: a missing sheet yields an all-mystery grid
    # rather than a failed run, so the MOTD still publishes on schedule.
    titles_by_night: dict[int, list[str]] = {n: [] for n in _NIGHTS}
    try:
        _say({"phase": "workbook"})
        wb_bytes = load_workbook_bytes(config, local_override=workbook_path, emit=emit)
        resolve_weekend_sheet(wb_bytes, week_key)
        _say({"phase": "parsing", "sheet": week_key})
        titles_by_night = _titles_by_night(
            extract_movies_by_section(wb_bytes, week_key)
        )
    except RuntimeError as exc:
        week.workbook_found = False
        week.warnings.append(f"workbook unavailable: {exc}")
        logger.warning("motd: workbook unavailable for %s: %s", week_key, exc)

    nights = _grid_nights(motd_cfg)
    week.slots = _blank_grid(
        friday,
        saturday,
        sunday,
        _slot_counts(titles_by_night, nights, slot_count),
        nights,
    )

    if not dry_run:
        poster_dir.mkdir(parents=True, exist_ok=True)

    total = len(week.slots)
    for index, slot in enumerate(week.slots, 1):
        night_titles = titles_by_night.get(slot.night, [])
        raw_title = (
            night_titles[slot.position - 1]
            if slot.position <= len(night_titles)
            else None
        )
        override = overrides.get(slot.slot_key) or {}
        slot.title = (override.get("title") or raw_title) or None

        _say({"phase": "slots", "slot": slot.slot_key, "index": index, "total": total})

        # An override poster short-circuits lookup entirely — that's its purpose.
        if override.get("poster_url"):
            slot.poster_url = override["poster_url"]
            slot.href = override.get("href") or ""
            slot.source = "override"
            if not slot.href:
                _apply_href_fallback(slot, motd_cfg)
            continue

        if not slot.title:
            _apply_mystery(slot, motd_cfg, pool)
            slot.note = "no title on the schedule yet"
            continue

        info = _resolve_omdb(slot.title, api_key=omdb_key) if omdb_key else None
        if not info:
            _apply_mystery(slot, motd_cfg, pool)
            slot.note = "title could not be verified"
            logger.info("motd: no OMDB match for %r (%s)", slot.title, slot.slot_key)
            if override.get("href"):
                slot.href = override["href"]
                slot.source = "override"
            continue

        slot.imdb_id = info["imdb_id"]
        filename = _poster_filename(
            datetime.date.fromisoformat(slot.date), slot.position, slot.night
        )
        dest = poster_dir / filename
        if dry_run:
            slot.poster_url = f"{poster_base_url}/{filename}"
            slot.source = "omdb"
        elif dest.exists() and not refresh_art:
            slot.poster_url = f"{poster_base_url}/{filename}"
            slot.source = "omdb"
        else:
            art = _download_image(info["poster_url"])
            if art:
                dest.write_bytes(art)
                slot.poster_url = f"{poster_base_url}/{filename}"
                slot.source = "omdb"
            else:
                _apply_mystery(slot, motd_cfg, pool)
                slot.note = "poster download failed"

        slot.href = override.get("href") or _imdb_href(slot.imdb_id)
        if override.get("href"):
            slot.source = "override"
        if not slot.href:
            _apply_href_fallback(slot, motd_cfg)

    return week


def _apply_href_fallback(slot: MOTDSlot, motd_cfg) -> None:
    slot.href = (
        _imdb_href(slot.imdb_id) or getattr(motd_cfg, "mystery_box_href", "") or "#"
    )
