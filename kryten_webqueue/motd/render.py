"""Render the MOTD HTML snippet from a built weekend.

Uses a dedicated Jinja environment rooted at ``templates/motd/`` with autoescape
on — every value in the context (curator titles, override URLs) is untrusted and
is pasted straight into the channel's Message of the Day.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .builder import MOTDWeek

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "motd"

_SAFE_SCHEMES = {"http", "https"}


def _safe_url(url: str | None) -> str:
    """Allow only http(s) URLs; anything else (javascript:, data:) becomes '#'."""
    if not url:
        return "#"
    parsed = urlparse(url)
    if parsed.scheme.lower() in _SAFE_SCHEMES and parsed.netloc:
        return url
    return "#"


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["safe_url"] = _safe_url
    return env


def _headline(config, week: MOTDWeek) -> str:
    showtime = getattr(config.motd, "showtime", "") or ""
    days = [week.friday, week.saturday]
    parts = [f"{d.month}/{d.day} @ {showtime}".strip() for d in days]
    return str(getattr(config.motd, "headline", "{dates}")).replace(
        "{dates}", " & ".join(parts)
    )


def render_motd(config, week: MOTDWeek, *, next_event: dict | None = None) -> str:
    """Render the poster-grid MOTD snippet for ``week``."""
    motd_cfg = config.motd
    nights: list[dict] = []
    for slot in week.slots:
        if not nights or nights[-1]["night"] != slot.night:
            nights.append({"night": slot.night, "label": slot.night_label, "slots": []})
        nights[-1]["slots"].append(slot)

    template = _environment().get_template(
        getattr(motd_cfg, "template", "channel_z.html")
    )
    return template.render(
        banner_url=getattr(motd_cfg, "banner_url", ""),
        headline=_headline(config, week),
        nights=nights,
        slots=week.slots,
        links=[
            {"label": link.label, "url": link.url}
            for link in getattr(motd_cfg, "links", [])
        ],
        next_event=next_event,
        week_key=week.week_key,
        generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
    )
