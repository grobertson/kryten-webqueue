"""Render the MOTD HTML snippet from a built weekend.

Uses a dedicated Jinja environment rooted at ``templates/motd/`` with autoescape
on — every value in the context (curator titles, override URLs) is untrusted and
is pasted straight into the channel's Message of the Day.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .builder import MOTDWeek

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "motd"

_SAFE_SCHEMES = {"http", "https"}
_SLOT_ATTRIBUTE = "data-kryten-motd-slot"
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class MOTDMergeError(ValueError):
    pass


@dataclass
class _Tag:
    name: str
    start: int
    end: int
    attrs: list[tuple[str, str | None]]
    anchor: int | None
    grid: int | None
    self_closing: bool


class _MOTDParser(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.tags: list[_Tag] = []
        self.stack: list[int] = []
        self.offsets = [0] + [
            index + 1 for index, character in enumerate(html) if character == "\n"
        ]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        line, column = self.getpos()
        start = self.offsets[line - 1] + column
        anchor = next(
            (index for index in reversed(self.stack) if self.tags[index].name == "a"),
            None,
        )
        grid = next(
            (
                index
                for index in reversed(self.stack)
                if "poster-grid"
                in (dict(self.tags[index].attrs).get("class") or "").split()
            ),
            None,
        )
        index = len(self.tags)
        self.tags.append(
            _Tag(
                tag,
                start,
                start + len(raw),
                attrs,
                anchor,
                grid,
                raw.rstrip().endswith("/>"),
            )
        )
        if tag not in _VOID_TAGS and not raw.rstrip().endswith("/>"):
            self.stack.append(index)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        for position in range(len(self.stack) - 1, -1, -1):
            if self.tags[self.stack[position]].name == tag:
                del self.stack[position:]
                return


def _slot_tags(
    parser: _MOTDParser, markers: dict[int, str]
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for index, slot in markers.items():
        tag = parser.tags[index]
        if not slot or tag.name not in ("a", "img"):
            raise MOTDMergeError("Slot markers must identify movie anchors and images")
        names = [name for name, _ in tag.attrs]
        if len(names) != len(set(names)):
            raise MOTDMergeError(f"Duplicate attributes on MOTD slot {slot}")
        pair = result.setdefault(slot, {})
        if tag.name in pair:
            raise MOTDMergeError(f"Duplicate {tag.name} marker for MOTD slot {slot}")
        pair[tag.name] = index
    for slot, pair in result.items():
        if set(pair) != {"a", "img"} or parser.tags[pair["img"]].anchor != pair["a"]:
            raise MOTDMergeError(
                f"MOTD slot {slot} needs one anchor containing its marked image"
            )
    return result


def _markers(parser: _MOTDParser) -> dict[int, str]:
    return {
        index: value or ""
        for index, tag in enumerate(parser.tags)
        for name, value in tag.attrs
        if name == _SLOT_ATTRIBUTE
    }


def _legacy_markers(parser: _MOTDParser, slots: list[str]) -> dict[int, str]:
    grids = [
        index
        for index, tag in enumerate(parser.tags)
        if "poster-grid" in (dict(tag.attrs).get("class") or "").split()
    ]
    if len(grids) != 1:
        raise MOTDMergeError(
            "Live MOTD needs slot markers or exactly one legacy poster-grid"
        )
    anchors = [
        index
        for index, tag in enumerate(parser.tags)
        if tag.name == "a" and tag.grid == grids[0]
    ]
    images = [
        index
        for index, tag in enumerate(parser.tags)
        if tag.name == "img" and tag.grid == grids[0]
    ]
    if len(anchors) != len(slots) or len(images) != len(slots):
        raise MOTDMergeError(
            "Live poster-grid does not match the configured slot count; refusing to replace the MOTD"
        )
    markers: dict[int, str] = {}
    for slot, anchor in zip(slots, anchors):
        children = [index for index in images if parser.tags[index].anchor == anchor]
        if len(children) != 1:
            raise MOTDMergeError("Each legacy movie box must contain exactly one image")
        markers[anchor] = slot
        markers[children[0]] = slot
    return markers


def _render_tag(tag: _Tag, updates: dict[str, str | None]) -> str:
    attrs = []
    seen = set()
    for name, value in tag.attrs:
        seen.add(name)
        value = updates.get(name, value)
        if value is None and name in updates:
            continue
        attrs.append(name if value is None else f'{name}="{escape(value, quote=True)}"')
    for name, value in updates.items():
        if name not in seen and value is not None:
            attrs.append(f'{name}="{escape(value, quote=True)}"')
    ending = " />" if tag.self_closing else ">"
    return f"<{tag.name} {' '.join(attrs)}{ending}"


def update_motd_slots(current_html: str, generated_html: str) -> str:
    generated = _MOTDParser(generated_html)
    generated_slots = _slot_tags(generated, _markers(generated))
    if not generated_slots:
        raise MOTDMergeError("Generated MOTD has no managed movie slots")
    if not current_html.strip():
        return generated_html
    current = _MOTDParser(current_html)
    markers = _markers(current)
    if not markers:
        markers = _legacy_markers(current, list(generated_slots))
    current_slots = _slot_tags(current, markers)
    if set(current_slots) != set(generated_slots):
        raise MOTDMergeError(
            "Live MOTD slot markers do not match the configured layout; refusing to replace the MOTD"
        )

    replacements = []
    for slot, pair in current_slots.items():
        for name, fields in (("a", ("href", "title")), ("img", ("src", "alt"))):
            tag = current.tags[pair[name]]
            generated_attrs = dict(generated.tags[generated_slots[slot][name]].attrs)
            updates = {
                _SLOT_ATTRIBUTE: slot,
                **{field: generated_attrs.get(field) for field in fields},
            }
            url_field = "href" if name == "a" else "src"
            updates[url_field] = _safe_url(updates[url_field])
            replacements.append((tag.start, tag.end, _render_tag(tag, updates)))
    for start, end, replacement in sorted(replacements, reverse=True):
        current_html = current_html[:start] + replacement + current_html[end:]
    return current_html


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
        show_next_event=bool(getattr(motd_cfg, "show_next_event", False)),
        week_key=week.week_key,
        generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
    )
