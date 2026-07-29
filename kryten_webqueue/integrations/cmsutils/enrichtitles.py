#!/usr/bin/env python3
# VENDORED from d:\Devel\cmsutils\enrichtitles.py on 2026-06-09.
# Adapted for in-process use by kryten-webqueue jobs: a headless
# run(params, *, config, progress) entry point is appended at the bottom; the
# original CLI main()/argparse path is retained but unused by the service.
# Keep adapters thin so re-vendoring from upstream stays mechanical.
"""
enrichtitles - Clean up movie titles in a MediaCMS instance.

Finds movies with legacy naming patterns like "The.Movie.Title[1982].mp4"
and renames them to a clean format: "The Movie Title (1982)".

Handles:
  - Dots as word separators  →  spaces
  - [year] brackets          →  (year) parentheses
  - File extensions stripped  (.mp4, .mkv, .avi, etc.)
  - Scene/codec/resolution tags stripped (720p, x264, BluRay, etc.)
  - Various edge cases (extra dots, missing dots, "Aka" aliases, etc.)

Modes:
  --dry-run  (default)  Show what would change without touching the CMS
  --commit              Actually push the title updates via the API
  --limit N             Only process the first N matches

Examples
--------
  python enrichtitles.py --token TOKEN                          # dry-run, show all
  python enrichtitles.py --token TOKEN --limit 10               # dry-run, first 10
  python enrichtitles.py --token TOKEN --commit --limit 5       # update first 5
  python enrichtitles.py --token TOKEN --commit                 # update ALL
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
import time
from dataclasses import dataclass

import requests

# ── Configuration ──────────────────────────────────────────────────────────────
API_BASE = "https://www.dropsugar.co/api/v1"
DEFAULT_TIMEOUT = 30
REQUEST_DELAY = 0.15  # polite delay between API calls


# ── Title cleaning ─────────────────────────────────────────────────────────────

# File extensions to strip
_EXT_RE = re.compile(r"\.\w{2,5}$")

# The core pattern: title contains [YYYY] (four-digit year in brackets)
_YEAR_BRACKET_RE = re.compile(r"\[(\d{4})\]")

# Video extensions that should always be stripped when present on a title
_VIDEO_EXT_RE = re.compile(
    r"\.(mp4|mkv|avi|mov|wmv|flv|m4v|mpg|mpeg|ts|webm|divx|xvid)$",
    re.IGNORECASE,
)

# Scene / codec / resolution / source tags to strip (case-insensitive).
# Order matters — longer tokens first to avoid partial matches.
_SCENE_TAGS = [
    # Resolution
    r"2160p",
    r"1080p",
    r"1080i",
    r"720p",
    r"480p",
    r"360p",
    r"4K",
    r"UHD",
    # Codecs
    r"x\.?264",
    r"x\.?265",
    r"h\.?264",
    r"h\.?265",
    r"HEVC",
    r"AVC",
    r"XviD",
    r"DivX",
    r"VP9",
    r"AV1",
    r"MPEG-?[24]",
    # Sources
    r"Blu-?Ray",
    r"BRRip",
    r"BDRip",
    r"BD-?Remux",
    r"REMUX",
    r"DVDRip",
    r"DVD-?R",
    r"DVD-?Scr",
    r"DVDScr",
    r"WEB-?DL",
    r"WEBDL",
    r"WEBRip",
    r"WEB-?Rip",
    r"WEB",
    r"HDRip",
    r"HDTV",
    r"PDTV",
    r"SDTV",
    r"DSR",
    r"VHS-?Rip",
    r"VHSRip",
    r"LaserDisc",
    r"LD-?Rip",
    r"CAM-?Rip",
    r"CAMRip",
    r"HDCAM",
    r"HDTS",
    r"TS-?Rip",
    r"TSRip",
    r"TC-?Rip",
    r"TCRip",
    r"SCR",
    r"SCREENER",
    r"R5",
    r"PPVRip",
    # Audio
    r"AAC\d?\.?\d?",
    r"AC-?3",
    r"DTS-?HD(?:\.?MA)?",
    r"DTS",
    r"TrueHD",
    r"Atmos",
    r"FLAC",
    r"MP3",
    r"OGG",
    r"DD\+?\d?\.?\d?",
    r"EAC-?3",
    r"5\.1",
    r"7\.1",
    r"2\.0",
    # Scene tags
    r"REPACK",
    r"PROPER",
    r"EXTENDED",
    r"UNRATED",
    r"UNCUT",
    r"DIRECTORS?\.?CUT",
    r"DC",
    r"REMASTERED",
    r"RESTORED",
    r"RETAIL",
    r"INTERNAL",
    r"LIMITED",
    r"THEATRICAL",
    r"CRITERION",
    r"IMAX",
    r"3D",
    r"HDR\d*",
    # Container hints that might appear mid-title
    r"MKV",
    r"AVI",
    r"MP4",
    # Misc
    r"HB",  # seen in actual data: "Love.And.A.45.[1994]HB.mp4"
    r"MULTI",
    r"DUAL",
    r"DUBBED",
    r"SUBBED",
    r"SUB(?:S)?",
]

# Build one big alternation pattern (word-boundary aware, case-insensitive)
_SCENE_RE = re.compile(
    r"[\.\-\s_]*\b(?:" + "|".join(_SCENE_TAGS) + r")\b[\.\-\s_]*",
    re.IGNORECASE,
)

# "Aka" alias pattern: ".Aka.Something" → keep but clean
_AKA_RE = re.compile(r"\.Aka\.", re.IGNORECASE)

# ── TV episode title helpers ───────────────────────────────────────────────────

_FULLWIDTH_PIPE = "\uff5c"  # ｜  used in YouTube rips as field separator
_YOUTUBE_ID_RE = re.compile(r"\[[a-zA-Z0-9_-]{11}\]")
_TV_SEASON_RE = re.compile(r"Season\s*(\d{1,2})", re.IGNORECASE)
_TV_EPISODE_RE = re.compile(r"(?:Episode|Ep\.?)\s*#?\s*(\d{1,3})", re.IGNORECASE)
_TV_SXEX_RE = re.compile(r"\b[Ss](\d{1,2})[EeXx](\d{1,3})\b")
_TV_NXNN_RE = re.compile(r"\b(\d{1,2})[Xx](\d{2,3})\b")
_FULL_EPISODE_RE = re.compile(r"\(?FULL\s+EPISODE\)?", re.IGNORECASE)
# Leading playlist/sequence number: "01 Show Name - S01E01"
# Must NOT match NxNN-at-start like "4x01 Some Show"
_LEADING_SEQ_RE = re.compile(r"^\d+\s+")
_LEADING_NXNN_RE = re.compile(r"^\d+[xX]\d+")
# Bare split season+episode: "S04 Ep 20", "S04 Episode 20"
_TV_S_EP_RE = re.compile(r"\bS(\d{1,2})\s+[Ee]p(?:isode)?\.?\s*#?\s*(\d{1,3})\b")


def _try_clean_tv_title(raw: str) -> str | None:
    """Attempt to normalise a TV episode title to 'Show Name S01E08' form.

    Handles:
      - YouTube fullwidth-pipe format:
          "Show ｜ Season 1 ｜ Episode 8 [ytid].webm" → "Show S01E08"
      - Leading playlist/sequence number + SxxExx:
          "01 The Show - S01E01"  → "The Show S01E01"
          (skips NxNN-at-start like "4x01 Some Show")
      - Split season + episode tokens (S## Ep ##):
          "095 Sabrina The Teenage Witch S04 Ep 20 - She's Baaaack …"
          → "Sabrina The Teenage Witch S04E20"
      - SxxExx / NxNN titles that carry a video file extension:
          "Show.Name.S01E08.720p.mkv" → "Show Name S01E08"

    Returns the cleaned title, or None if this doesn't look like a TV episode.
    """

    # ── Full-width pipe (YouTube rip) ─────────────────────────────────────
    if _FULLWIDTH_PIPE in raw:
        title = _VIDEO_EXT_RE.sub("", raw).strip()
        parts = title.split(_FULLWIDTH_PIPE)
        show_part = parts[0].strip()
        season = episode = None
        for part in parts[1:]:
            sm = _TV_SEASON_RE.search(part)
            if sm:
                season = int(sm.group(1))
            em = _TV_EPISODE_RE.search(part)
            if em:
                episode = int(em.group(1))
        if season is not None and episode is not None:
            show_part = _YOUTUBE_ID_RE.sub("", show_part)
            show_part = _FULL_EPISODE_RE.sub("", show_part)
            show_part = re.sub(r"\s+", " ", show_part).strip().strip("-\u2013\u2014 ")
            if show_part:
                return f"{show_part} S{season:02d}E{episode:02d}"

    # ── Leading sequence number + SxxExx ────────────────────────────────────
    # e.g. "01 The Show - S01E01"  →  "The Show S01E01"
    # Skips NxNN-at-start like "4x01 Some Show - Episode"
    if _LEADING_SEQ_RE.match(raw) and not _LEADING_NXNN_RE.match(raw):
        m = _TV_SXEX_RE.search(raw)
        if m:
            seq_end = _LEADING_SEQ_RE.match(raw).end()
            show_part = raw[seq_end : m.start()]
            show_part = re.sub(r"[\s\-\u2013\u2014]+$", "", show_part)
            show_part = re.sub(r"\s+", " ", show_part).strip()
            season = int(m.group(1))
            episode = int(m.group(2))
            if show_part:
                return f"{show_part} S{season:02d}E{episode:02d}"

    # ── Split S## Ep ## (season and episode as separate tokens) ────────────────
    # e.g. "095 Sabrina The Teenage Witch S04 Ep 20 - She's Baaaack …"
    # Handles both leading-sequence and plain titles.
    m = _TV_S_EP_RE.search(raw)
    if m:
        show_part = raw[: m.start()]
        ls = _LEADING_SEQ_RE.match(show_part)
        if ls:
            show_part = show_part[ls.end() :]
        show_part = re.sub(r"[\s\-\u2013\u2014]+$", "", show_part)
        show_part = re.sub(r"\s+", " ", show_part).strip()
        season = int(m.group(1))
        episode = int(m.group(2))
        if show_part:
            return f"{show_part} S{season:02d}E{episode:02d}"

    # ── SxxExx / NxNN with a video extension ──────────────────────────────
    if _VIDEO_EXT_RE.search(raw):
        stripped = _VIDEO_EXT_RE.sub("", raw).strip()
        season = episode = None
        show_part = None

        m = _TV_SXEX_RE.search(stripped)
        if m:
            show_part = stripped[: m.start()]
            season = int(m.group(1))
            episode = int(m.group(2))
        else:
            m = _TV_NXNN_RE.search(stripped)
            if m:
                show_part = stripped[: m.start()]
                season = int(m.group(1))
                episode = int(m.group(2))

        if show_part is not None and season is not None and episode is not None:
            if show_part.strip() and re.search(r"[a-zA-Z]", show_part):
                show_part = show_part.replace(".", " ").replace("_", " ")
                show_part = _SCENE_RE.sub(" ", show_part)
                show_part = _FULL_EPISODE_RE.sub("", show_part)
                show_part = (
                    re.sub(r"\s+", " ", show_part).strip().strip("-\u2013\u2014 ")
                )
                if show_part:
                    return f"{show_part} S{season:02d}E{episode:02d}"

    return None


def clean_title(raw: str) -> str | None:
    """
    Clean a raw movie or TV episode title.
    Returns the cleaned title, or None if nothing needs fixing.

    Triggers on:
      - Full-width pipe ｜ separator (YouTube TV rip)
          "Show ｜ Season 1 ｜ Episode 8 [ytid].webm" → "Show S01E08"
      - SxxExx / NxNN pattern with a video extension
          "Show.Name.S01E08.720p.mkv" → "Show Name S01E08"
      - [YYYY] bracket year  (e.g. "The.Movie.[1982].mp4")
      - Any bare video extension  (e.g. "Weirdsville (2007).mp4")
    """

    # ── TV episode titles take priority ───────────────────────────────────
    tv = _try_clean_tv_title(raw)
    if tv is not None and tv != raw:
        return tv

    # ── Movie cleaning ────────────────────────────────────────────────────
    has_bracket_year = bool(_YEAR_BRACKET_RE.search(raw))
    has_video_ext = bool(_VIDEO_EXT_RE.search(raw))
    if not has_bracket_year and not has_video_ext:
        return None

    title = raw

    # 1. Strip file extension
    title = _EXT_RE.sub("", title)

    # 2. Convert [YYYY] → (YYYY) in place (no placeholder needed)
    for year in _YEAR_BRACKET_RE.findall(title):
        title = title.replace(f"[{year}]", f"({year})", 1)

    # 3. Strip scene/codec/resolution tags
    title = _SCENE_RE.sub(" ", title)

    # 4. Replace dots and underscores with spaces
    title = title.replace(".", " ").replace("_", " ")

    # 5. Strip stray orphan brackets left from source typos like [1989]]
    title = title.replace("[", "").replace("]", "")

    # 6. Clean up punctuation immediately before (YYYY)
    title = re.sub(r"[,\s]+\((\d{4})\)", r" (\1)", title)

    # 7. Clean up spacing: collapse multiple spaces, strip edges
    title = re.sub(r"\s+", " ", title).strip()

    # 8. Remove trailing/leading hyphens or dashes left from stripping
    title = title.strip("- ")

    # 9. If the year is at the end, ensure one space before it
    title = re.sub(r"\s*\((\d{4})\)\s*$", r" (\1)", title)

    # If nothing changed, skip
    if title == raw:
        return None

    return title


# ── Data model ─────────────────────────────────────────────────────────────────


@dataclass
class TitleChange:
    friendly_token: str
    old_title: str
    new_title: str
    url: str
    cms_user: str = (
        ""  # original uploader — captured before any PUT so we can restore it
    )


# ── API helpers ────────────────────────────────────────────────────────────────


def fetch_all_media(session: requests.Session) -> list[dict]:
    """Paginate through /manage_media to get every media item."""
    all_items: list[dict] = []
    page = 1

    # First request to get total count
    resp = session.get(
        f"{API_BASE}/manage_media",
        params={"page": 1},
        timeout=DEFAULT_TIMEOUT,
    )

    if resp.status_code == 403:
        print(
            "⚠  /manage_media returned 403. Falling back to /media (may be capped).",
            file=sys.stderr,
        )
        return _fetch_media_fallback(session)

    resp.raise_for_status()
    data = resp.json()
    total = data.get("count", 0)
    all_items.extend(data.get("results", []))
    print(f"    Total media in CMS: {total}")

    while data.get("next"):
        page += 1
        time.sleep(REQUEST_DELAY)
        resp = session.get(
            f"{API_BASE}/manage_media",
            params={"page": page},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        all_items.extend(data.get("results", []))
        # Progress indicator
        pct = min(100, int(len(all_items) / total * 100)) if total else 0
        print(f"\r    Fetched {len(all_items)}/{total} ({pct}%) …", end="", flush=True)

    print(f"\r    Fetched {len(all_items)}/{total} (100%)    ")
    return all_items


def _fetch_media_fallback(session: requests.Session) -> list[dict]:
    """Fallback: paginate through /media (may be capped at ~1000)."""
    all_items: list[dict] = []
    page = 1

    while True:
        resp = session.get(
            f"{API_BASE}/media",
            params={"page": page},
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code != 200:
            break
        data = resp.json()
        batch = data.get("results", [])
        if not batch:
            break
        all_items.extend(batch)
        total = data.get("count", "?")
        print(f"\r    Fetched {len(all_items)}/{total} …", end="", flush=True)
        if not data.get("next"):
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    print(f"\r    Fetched {len(all_items)} items (via /media fallback)    ")
    return all_items


def update_title(
    session: requests.Session, friendly_token: str, new_title: str
) -> bool:
    """PUT a new title to /media/{token}. Returns True on success."""
    resp = session.put(
        f"{API_BASE}/media/{friendly_token}",
        data={"title": new_title},
        timeout=DEFAULT_TIMEOUT,
    )
    return resp.status_code in (200, 201)


def _restore_owner(
    session: requests.Session,
    friendly_token: str,
    cms_user: str,
) -> None:
    """Restore original ownership after a PUT.

    MediaCMS bug: PUT /api/v1/media/{token} always calls
    ``serializer.save(user=request.user)``, silently overwriting the stored
    owner with whoever holds the API token (typically admin).  Calling this
    immediately after each commit restores the correct uploader.
    """
    if not cms_user:
        return
    try:
        session.post(
            f"{API_BASE}/media/user/bulk_actions",
            json={
                "action": "change_owner",
                "media_ids": [friendly_token],
                "owner": cms_user,
            },
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception:
        pass  # ownership restoration is best-effort; don't fail the whole run


# ── CLI ────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="enrichtitles",
        description="Clean up movie titles in DropSugar MediaCMS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
modes:
  Default is dry-run (no changes made). Use --commit to apply.

examples:
  %(prog)s --token TOKEN                       # dry-run, show all matches
  %(prog)s --token TOKEN --limit 10            # dry-run, first 10
  %(prog)s --token TOKEN --commit --limit 5    # update first 5
  %(prog)s --token TOKEN --commit              # update ALL matches
        """,
    )
    p.add_argument(
        "--token",
        required=True,
        metavar="TOKEN",
        help="MediaCMS API token for authentication.",
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help="Actually apply title changes. Without this flag, runs in dry-run mode.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Only process the first N matching titles.",
    )
    p.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="Only consider items uploaded in the last N days.",
    )
    p.add_argument(
        "--api-url",
        default=API_BASE,
        metavar="URL",
        help=f"Override the API base URL (default: {API_BASE}).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows console UTF-8 fix
    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
        import io

        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )

    parser = build_parser()
    args = parser.parse_args(argv)

    global API_BASE  # noqa: PLW0603
    API_BASE = args.api_url.rstrip("/")

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"\n{'='*60}")
    print(f"  enrichtitles  —  Mode: {mode}")
    if args.days:
        print(f"  Window: last {args.days} day(s)")
    if args.limit:
        print(f"  Limit: {args.limit} title(s)")
    print(f"{'='*60}\n")

    session = requests.Session()
    session.headers["Authorization"] = f"Token {args.token}"

    # ── Fetch all media ────────────────────────────────────────────────────
    print("📡  Fetching media catalog …")
    all_media = fetch_all_media(session)

    # ── Filter by upload date ─────────────────────────────────────────────
    if args.days:
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=args.days)
        ).strftime("%Y-%m-%d")
        before = len(all_media)
        all_media = [i for i in all_media if (i.get("add_date") or "")[:10] >= cutoff]
        print(
            f"  Filtered to {len(all_media)}/{before} item(s) "
            f"uploaded in the last {args.days} day(s).\n"
        )

    # ── Find candidates ────────────────────────────────────────────────────
    print("🔍  Scanning for titles to clean …\n")
    changes: list[TitleChange] = []

    for item in all_media:
        old = item.get("title", "")
        new = clean_title(old)
        if new is None:
            continue

        changes.append(
            TitleChange(
                friendly_token=item.get("friendly_token", ""),
                old_title=old,
                new_title=new,
                url=item.get("url", ""),
                cms_user=item.get("user", ""),
            )
        )

        if args.limit and len(changes) >= args.limit:
            break

    if not changes:
        print("✅  No titles need cleaning. Everything looks good!")
        return 0

    # ── Display changes ────────────────────────────────────────────────────
    max_old = max(len(c.old_title) for c in changes)
    col_w = min(max_old + 2, 65)

    print(f"  {'CURRENT TITLE':<{col_w}} → NEW TITLE")
    print(f"  {'─' * col_w}   {'─' * 40}")
    for c in changes:
        old_display = (
            c.old_title if len(c.old_title) <= col_w else c.old_title[: col_w - 1] + "…"
        )
        print(f"  {old_display:<{col_w}} → {c.new_title}")

    print(f"\n  Found {len(changes)} title(s) to update.\n")

    # ── Apply changes ──────────────────────────────────────────────────────
    if not args.commit:
        print("  ℹ  Dry-run mode — no changes made. Use --commit to apply.\n")
        return 0

    print("  Applying changes …\n")
    success = 0
    failed = 0

    for i, c in enumerate(changes, 1):
        ok = update_title(session, c.friendly_token, c.new_title)
        status = "✅" if ok else "❌"
        print(f"  [{i}/{len(changes)}] {status}  {c.old_title} → {c.new_title}")
        if ok:
            success += 1
            # MediaCMS bug: PUT /media/{token} overwrites the owner with
            # request.user (the admin token).  Restore the original uploader.
            if c.cms_user:
                _restore_owner(session, c.friendly_token, c.cms_user)
        else:
            failed += 1
        time.sleep(REQUEST_DELAY)

    print(f"\n  Done: {success} updated, {failed} failed.\n")
    return 0 if failed == 0 else 1


# ── Headless entry point for the webqueue job runner ───────────────────────────


def run(params: dict, *, config, progress=None) -> dict:
    """Run title cleanup headlessly (no argparse, no interactive prompts).

    ``params`` keys: ``dry_run`` (bool), ``limit`` (int|None), ``days`` (int|None).
    ``config`` is the webqueue Config (uses ``mediacms_url``/``mediacms_token``).
    ``progress`` is an optional ``callable(dict)`` for coarse milestone updates.
    Returns a counts dict suitable for ``job_runs.detail``.
    """
    global API_BASE  # noqa: PLW0603
    API_BASE = f"{config.mediacms_url.rstrip('/')}/api/v1"

    dry_run = bool(params.get("dry_run", False))
    limit = params.get("limit")
    days = params.get("days")

    def _emit(detail):
        if progress:
            progress(detail)

    session = requests.Session()
    session.headers["Authorization"] = f"Token {config.mediacms_token}"

    _emit({"phase": "fetching"})
    all_media = fetch_all_media(session)

    if days:
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        ).strftime("%Y-%m-%d")
        all_media = [i for i in all_media if (i.get("add_date") or "")[:10] >= cutoff]

    changes: list[TitleChange] = []
    for item in all_media:
        old = item.get("title", "")
        new = clean_title(old)
        if new is None:
            continue
        changes.append(
            TitleChange(
                friendly_token=item.get("friendly_token", ""),
                old_title=old,
                new_title=new,
                url=item.get("url", ""),
                cms_user=item.get("user", ""),
            )
        )
        if limit and len(changes) >= limit:
            break

    _emit({"phase": "scanned", "scanned": len(all_media), "matched": len(changes)})

    committed = 0
    failed = 0
    if not dry_run:
        for i, c in enumerate(changes, 1):
            ok = update_title(session, c.friendly_token, c.new_title)
            if ok:
                committed += 1
                if c.cms_user:
                    _restore_owner(session, c.friendly_token, c.cms_user)
            else:
                failed += 1
            if i % 10 == 0:
                _emit({"phase": "committing", "processed": i, "total": len(changes)})
            time.sleep(REQUEST_DELAY)

    return {
        "scanned": len(all_media),
        "matched": len(changes),
        "committed": committed,
        "skipped": (len(changes) - committed - failed) if not dry_run else len(changes),
        "failed": failed,
        "dry_run": dry_run,
    }


if __name__ == "__main__":
    raise SystemExit(main())
