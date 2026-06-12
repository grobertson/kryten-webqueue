import asyncio
import logging
import re

from .bulk_add import add_item_throttled

logger = logging.getLogger(__name__)


# --- URL parsing helpers (module-level, pure) ---

_YT_ID = r"[A-Za-z0-9_-]{11}"


def extract_youtube_id(url: str) -> str | None:
    """Return the 11-char YouTube video id from any YouTube/youtu.be URL.

    Strips playlists (``list=``), start times (``t=``/``start=``) and every
    other query/path argument. Returns None when no video id is present (e.g. a
    bare ``/playlist?list=...`` link, which we do not expand)."""
    # youtu.be/<id>
    m = re.search(rf"youtu\.be/({_YT_ID})", url)
    if m:
        return m.group(1)
    # youtube.com/watch?v=<id>
    m = re.search(rf"[?&]v=({_YT_ID})", url)
    if m:
        return m.group(1)
    # youtube.com/shorts/<id>, /embed/<id>, /v/<id>, /live/<id>
    m = re.search(rf"/(?:shorts|embed|v|live)/({_YT_ID})", url)
    if m:
        return m.group(1)
    return None


def extract_dropsugar_token(url: str) -> str | None:
    """Return the MediaCMS friendly_token from a dropsugar manifest/view URL."""
    m = re.search(r"/media/cytube/([^./?&]+)\.json", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]m=([^&]+)", url)
    if m:
        return m.group(1)
    return None


def _is_youtube(host: str) -> bool:
    host = host.lower()
    return "youtube.com" in host or "youtu.be" in host


def _is_dropsugar(host: str) -> bool:
    return "dropsugar." in host.lower()


def _manifest_url_for_token(token: str, mediacms_url: str | None) -> str | None:
    if not mediacms_url:
        return None
    return f"{mediacms_url.rstrip('/')}/api/v1/media/cytube/{token}.json?format=json"


class PlaylistImporter:
    """Imports items from a saved playlist into the live CyTube queue."""

    def __init__(self, *, api_gate, db, shadow, add_delay_sec: float = 0.0, add_max_retries: int = 0):
        self._api_gate = api_gate
        self._db = db
        self._shadow = shadow
        self._add_delay_sec = add_delay_sec
        self._add_max_retries = add_max_retries

    async def import_playlist(self, playlist_id: int) -> dict:
        """Import all items from a saved playlist into the live queue."""
        items = await self._db.get_saved_playlist_items(playlist_id)
        if not items:
            return {"success": False, "error": "Playlist is empty"}

        added = 0
        errors = 0
        for index, item in enumerate(items):
            # Throttle consecutive adds so CyTube can validate each item before
            # the next arrives (avoids transient queueFail/422 under load).
            if index and self._add_delay_sec:
                await asyncio.sleep(self._add_delay_sec)
            try:
                result = await add_item_throttled(
                    self._api_gate,
                    media_type=item["media_type"],
                    media_id=item["media_id"],
                    position="end",
                    max_retries=self._add_max_retries,
                    retry_delay_sec=self._add_delay_sec or 0.5,
                )
                if result.get("success"):
                    added += 1
                else:
                    errors += 1
            except Exception as e:
                logger.warning(f"Failed to add {item['media_id']}: {e}")
                errors += 1

        return {"success": True, "added": added, "errors": errors}


async def import_playlist_text(db, text: str, *, mediacms_url: str | None = None) -> dict:
    """Parse the plain-text playlist import format into resolved items.

    Tolerant by design — never raises; unrecognised lines are reported in
    ``errors`` so the import can proceed with whatever resolved.

    Supported per line (one entry per line):
      - Blank lines are skipped.
      - ``#`` starts a comment: a whole-line comment, or an inline trailing
        comment (everything from the first ``#`` is ignored).
      - **dropsugar.co / dropsugar.com URLs** — watch (``/view?m=TOKEN``) or
        manifest (``/api/v1/media/cytube/TOKEN.json``) links. Resolved against
        the catalog (for title/duration); falls back to a constructed manifest
        URL when the token isn't catalogued yet.
      - **YouTube / youtu.be URLs** — playlist, start-time (``t``) and all other
        arguments are stripped, leaving a clean ``yt:VIDEOID`` item.
      - Other ``http(s)`` URLs (unknown sites) are skipped (reported as
        ``unsupported_site``).
      - Legacy tokens: ``cm:token``, ``yt:id``, or a bare catalog token.
      - Trailing free text after a URL (e.g. ``URL - Some Title``) is used as a
        title hint for items not found in the catalog.

    Returns: ``{"items": [...], "errors": [...]}``.
    """
    items = []
    errors = []
    url_re = re.compile(r"https?://\S+")

    for line_num, raw in enumerate(text.splitlines(), 1):
        # Strip inline comments: everything from the first '#'.
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        url_match = url_re.search(line)
        if url_match:
            url = url_match.group(0).rstrip(".,;")
            # Free text after the URL is a title hint (e.g. " - My Title").
            trailing = line[url_match.end():].strip().lstrip("-–").strip()
            title_hint = trailing or None
            host = re.sub(r"^https?://", "", url).split("/", 1)[0]

            if _is_youtube(host):
                vid = extract_youtube_id(url)
                if vid:
                    items.append({
                        "media_type": "yt",
                        "media_id": vid,
                        "title": title_hint,
                        "duration_sec": None,
                    })
                else:
                    errors.append({"line": line_num, "token": url, "reason": "youtube_no_video_id"})
                continue

            if _is_dropsugar(host):
                token = extract_dropsugar_token(url)
                if not token:
                    errors.append({"line": line_num, "token": url, "reason": "no_token_in_url"})
                    continue
                catalog_item = await db.get_item_admin(token)
                if catalog_item:
                    items.append({
                        "media_type": "cm",
                        "media_id": catalog_item["manifest_url"],
                        "title": catalog_item.get("title") or title_hint,
                        "duration_sec": catalog_item.get("duration_sec"),
                    })
                else:
                    manifest = _manifest_url_for_token(token, mediacms_url)
                    if manifest:
                        items.append({
                            "media_type": "cm",
                            "media_id": manifest,
                            "title": title_hint,
                            "duration_sec": None,
                        })
                    else:
                        errors.append({"line": line_num, "token": token, "reason": "not_in_catalog"})
                continue

            # Any other site — skip tolerantly.
            errors.append({"line": line_num, "token": url, "reason": "unsupported_site"})
            continue

        # --- legacy token forms (no URL on the line) ---
        if line.startswith("cm:"):
            media_id = line[3:].strip()
            catalog_item = await db.get_item_admin(media_id)
            if catalog_item:
                resolved_media = catalog_item["manifest_url"]
            else:
                # Not yet in the local catalog (e.g. freshly downloaded by the
                # fetch/fetchurls job before a sync) — construct the CyTube
                # manifest URL from the token so it still plays.
                resolved_media = _manifest_url_for_token(media_id, mediacms_url) or media_id
            items.append({
                "media_type": "cm",
                "media_id": resolved_media,
                "title": catalog_item["title"] if catalog_item else None,
                "duration_sec": catalog_item["duration_sec"] if catalog_item else None,
            })
        elif line.startswith("yt:"):
            items.append({
                "media_type": "yt",
                "media_id": line[3:].strip(),
                "title": None,
                "duration_sec": None,
            })
        else:
            # Bare token — resolve from catalog.
            catalog_item = await db.get_item_admin(line)
            if catalog_item:
                items.append({
                    "media_type": "cm",
                    "media_id": catalog_item["manifest_url"],
                    "title": catalog_item["title"],
                    "duration_sec": catalog_item["duration_sec"],
                })
            else:
                errors.append({"line": line_num, "token": line, "reason": "not_in_catalog"})

    return {"items": items, "errors": errors}

