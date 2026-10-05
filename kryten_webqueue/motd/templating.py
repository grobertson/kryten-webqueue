"""Sandboxed rendering of admin-authored MOTD templates.

Masters and fragments are Jinja source stored in the database. They render in
an ``ImmutableSandboxedEnvironment`` with no loader (so ``include``/``extends``/
``import`` cannot reach the filesystem) and a fixed, documented context.
Composition happens only through ``zone("name")``: a master names its zones
statically, and the scheduler decides which fragment fills each one.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from jinja2 import StrictUndefined, TemplateSyntaxError, nodes
from jinja2.sandbox import ImmutableSandboxedEnvironment
from markupsafe import Markup

from .render import MOTDMergeError, _safe_url, collect_motd_slots, render_movie_grid

MAX_BODY_CHARS = 64 * 1024
TEMPLATE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
ZONE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MEDIA_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
KINDS = ("master", "fragment")

_FORBIDDEN_NODES = (nodes.Include, nodes.Extends, nodes.Import, nodes.FromImport)
_MASTER_ONLY = ("zone", "zone_active", "movie_grid")
_ZONE_STYLE = "margin:1.25em auto 0;"


class MOTDTemplateError(ValueError):
    """A template cannot be saved, rendered, or published as-is."""

    def __init__(self, message: str, line: int | None = None):
        super().__init__(message if line is None else f"line {line}: {message}")
        self.line = line


def js_length(text: str) -> int:
    """Length as JavaScript counts it (UTF-16 code units), which is what CyTube cuts on."""
    return len(text.encode("utf-16-le")) // 2


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass
class Analysis:
    zones: list[str] = field(default_factory=list)
    media_slugs: set[str] = field(default_factory=set)


def _environment() -> ImmutableSandboxedEnvironment:
    env = ImmutableSandboxedEnvironment(
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["safe_url"] = _safe_url
    return env


def _literal_arg(call: nodes.Call, func: str) -> str:
    if (
        len(call.args) != 1
        or call.kwargs
        or call.dyn_args
        or call.dyn_kwargs
        or not isinstance(call.args[0], nodes.Const)
        or not isinstance(call.args[0].value, str)
    ):
        raise MOTDTemplateError(
            f'{func}() takes exactly one quoted name, e.g. {func}("promo")', call.lineno
        )
    return call.args[0].value


def analyze(body: str, kind: str) -> Analysis:
    """Parse and statically check a template body; return its zones and media."""
    if kind not in KINDS:
        raise MOTDTemplateError(f"kind must be one of {', '.join(KINDS)}")
    if len(body) > MAX_BODY_CHARS:
        raise MOTDTemplateError(f"Template exceeds {MAX_BODY_CHARS} characters")
    try:
        ast = _environment().parse(body)
    except TemplateSyntaxError as exc:
        raise MOTDTemplateError(exc.message or "Syntax error", exc.lineno) from exc
    for node in ast.find_all(_FORBIDDEN_NODES):
        raise MOTDTemplateError(
            "include/extends/import are not available; use zone() for composition",
            node.lineno,
        )

    result = Analysis()
    for call in ast.find_all(nodes.Call):
        if not isinstance(call.node, nodes.Name):
            continue
        func = call.node.name
        if func in _MASTER_ONLY and kind == "fragment":
            raise MOTDTemplateError(
                f"{func}() is only available in master templates", call.lineno
            )
        if func in ("zone", "zone_active"):
            zone = _literal_arg(call, func)
            if not ZONE_NAME_RE.match(zone):
                raise MOTDTemplateError(
                    f"Invalid zone name {zone!r} (lowercase letters, digits, - and _)",
                    call.lineno,
                )
            if func == "zone":
                if zone in result.zones:
                    raise MOTDTemplateError(
                        f"Zone {zone!r} appears more than once", call.lineno
                    )
                result.zones.append(zone)
        elif func == "media_url":
            slug = _literal_arg(call, func)
            if not MEDIA_SLUG_RE.match(slug):
                raise MOTDTemplateError(f"Invalid media slug {slug!r}", call.lineno)
            result.media_slugs.add(slug)
    return result


def _sandbox_data(context: dict) -> dict:
    """Plain-data copy of the trusted render context for user templates."""
    nights = [
        {
            "night": night["night"],
            "label": night["label"],
            "slots": [slot.to_dict() for slot in night["slots"]],
        }
        for night in context["nights"]
    ]
    return {
        "banner_url": context["banner_url"],
        "headline": context["headline"],
        "nights": nights,
        "slots": [slot for night in nights for slot in night["slots"]],
        "links": [dict(link) for link in context["links"]],
        "next_event": dict(context["next_event"]) if context["next_event"] else None,
        "show_next_event": context["show_next_event"],
        "week_key": context["week_key"],
        "generated_at": context["generated_at"],
    }


def _render(body: str, data: dict) -> str:
    env = _environment()
    try:
        return env.from_string(body).render(**data)
    except MOTDTemplateError:
        raise
    except TemplateSyntaxError as exc:
        raise MOTDTemplateError(exc.message or "Syntax error", exc.lineno) from exc
    except (
        Exception
    ) as exc:  # noqa: BLE001 - any template fault is an admin-facing error
        raise MOTDTemplateError(f"{type(exc).__name__}: {exc}") from exc


def render_template(
    master_body: str,
    fragments: dict[str, str | None],
    context: dict,
    *,
    media_urls: dict[str, str],
    timezone: str = "America/New_York",
    now: datetime.datetime | None = None,
) -> str:
    """Render a master with its zone fragments.

    ``context`` is the trusted dict from ``render.motd_context``. ``fragments``
    maps zone name to fragment body (None = zone empty). ``media_urls`` must
    already contain every slug the bodies reference.
    """
    local_now = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(
        ZoneInfo(timezone)
    )

    def media_url(slug: str) -> str:
        if slug not in media_urls:
            raise MOTDTemplateError(f"Unknown or deleted media asset {slug!r}")
        return media_urls[slug]

    common = {**_sandbox_data(context), "now": local_now, "media_url": media_url}
    grid = Markup(render_movie_grid(context))

    def zone(name: str) -> Markup:
        body = fragments.get(name)
        if not body:
            return Markup("")
        inner = _render(body, common)
        return (
            Markup(
                '<div class="kryten-motd-zone" data-kryten-motd-zone="{}" style="{}">'
            ).format(name, _ZONE_STYLE)
            + Markup(inner)
            + Markup("</div>")
        )

    def zone_active(name: str) -> bool:
        return bool(fragments.get(name))

    return _render(
        master_body,
        {
            **common,
            "movie_grid": lambda: grid,
            "zone": zone,
            "zone_active": zone_active,
        },
    )


def render_fragment(
    body: str,
    context: dict,
    *,
    media_urls: dict[str, str],
    timezone: str = "America/New_York",
    now: datetime.datetime | None = None,
) -> str:
    local_now = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(
        ZoneInfo(timezone)
    )

    def media_url(slug: str) -> str:
        if slug not in media_urls:
            raise MOTDTemplateError(f"Unknown or deleted media asset {slug!r}")
        return media_urls[slug]

    return _render(
        body, {**_sandbox_data(context), "now": local_now, "media_url": media_url}
    )


def validate_master_output(html: str, expected_slots: set[str], max_chars: int) -> None:
    """Enforce the required movie grid and CyTube's hard length limit."""
    try:
        found = collect_motd_slots(html)
    except MOTDMergeError as exc:
        raise MOTDTemplateError(f"Movie grid markup is invalid: {exc}") from exc
    missing = sorted(expected_slots - found)
    extra = sorted(found - expected_slots)
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if extra:
            parts.append(f"unexpected {', '.join(extra)}")
        raise MOTDTemplateError(
            "Master templates must contain the full movie grid "
            f"(use {{{{ movie_grid() }}}}): {'; '.join(parts)}"
        )
    check_length(html, max_chars)


def check_length(html: str, max_chars: int) -> None:
    length = js_length(html)
    if length > max_chars:
        raise MOTDTemplateError(
            f"Rendered MOTD is {length} characters; CyTube silently truncates "
            f"above {max_chars}"
        )


_LINT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"<!--"), "HTML comments are stripped by CyTube"),
    (re.compile(r"<script\b", re.I), "<script> is stripped by CyTube"),
    (
        re.compile(r"<\w[^>]*\son[a-z]+\s*=", re.I),
        "Inline event handlers (on...=) are stripped by CyTube",
    ),
    (re.compile(r"javascript:", re.I), "javascript: URLs are stripped by CyTube"),
    (
        re.compile(r"<(video|audio|iframe|object|embed)\b", re.I),
        "Embedded media/frames are not supported in the MOTD",
    ),
    (
        re.compile(r"""(?:src|href)\s*=\s*["']http://""", re.I),
        "Plain http:// art or links cause mixed-content warnings; use https://",
    ),
)


def lint(html: str, max_chars: int) -> list[str]:
    warnings = [message for pattern, message in _LINT_RULES if pattern.search(html)]
    length = js_length(html)
    if length > max_chars * 0.9:
        warnings.append(f"Rendered MOTD uses {length} of {max_chars} characters")
    return warnings
