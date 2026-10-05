"""Emote rehost job — download externally-hosted emotes and serve from dropsugar.co.

Processes emotes whose image URLs are not already on the configured rehost
domain.  For each, downloads the image politely (ample backoff, Retry-After
respected, permanent 4xx skipped immediately), places it at
``{static_dir}/{bare_name}{ext}`` with www-data group ownership, and pushes
the new URL back to CyTube via api-gate.

Emote names in the channel JSON include the ``#`` prefix (e.g. ``#behold``);
filenames always use the bare name without ``#`` (e.g. ``behold.gif``).
"""

import asyncio
import json
import logging
import os
import random
import shutil
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from itertools import chain
from pathlib import Path
from urllib.parse import urlparse

import requests

from .manager import JobError

# Status codes that will never succeed on retry — skip immediately.
_NON_RETRYABLE = frozenset({400, 401, 403, 404, 410, 451})
# Sentinel returned by _place_emote for a permanent HTTP error (caller removes the emote).
_DEAD = "DEAD"

logger = logging.getLogger(__name__)

REHOST_EMOTES_SCHEMA: list[dict] = []

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

_EMOTE_EXTENSIONS = frozenset({".gif", ".webp"})
_MAX_SOURCE_PAGE_BYTES = 2 * 1024 * 1024


class _InvalidImageResponse(ValueError):
    pass


class _GiphyMediaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.media_urls: list[str] = []
        self.preview_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "img" and "giphy-gif-img" in (attributes.get("class") or "").split():
            source = attributes.get("src")
            if source:
                self.media_urls.append(source)
        elif tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            if key in {"og:image", "og:image:secure_url", "twitter:image"}:
                self.preview_url = self.preview_url or attributes.get("content")


def normalize_emote_name(value: str) -> str:
    """Return the canonical bare emote name used for both file and URL.

    Channel emotes are named ``#foo_bar-baz`` while static assets must be
    addressable from a stable filename.  The hash is never stored in a
    filename, and underscores/dashes are deliberately removed.
    """
    bare = value.lstrip("#").replace("_", "").replace("-", "").lower()
    if not bare or "/" in bare or "\\" in bare:
        raise ValueError(f"Invalid emote name: {value!r}")
    return bare


def unique_emote_filename(
    source: Path, destination: Path, used_names: set[str]
) -> Path:
    """Choose a collision-free destination using a numeric suffix.

    The initial migration imports reactions first, so their canonical names
    remain unchanged.  A distinct emote with the same canonical name becomes
    ``name2``, then ``name3``, and so on.  Identical bytes are deduplicated by
    the migration caller before this function is used.
    """
    bare = normalize_emote_name(source.stem)
    suffix = source.suffix.lower()
    candidate = bare
    index = 2
    while candidate.casefold() in used_names:
        candidate = f"{bare}{index}"
        index += 1
    used_names.add(candidate.casefold())
    return destination / f"{candidate}{suffix}"


def build_emote_manifest(static_dir: Path, base_url: str) -> list[dict[str, str]]:
    """Build a restorable API-gate emote export from GIF/WebP files on disk."""
    emotes: dict[str, dict[str, str]] = {}
    for path in sorted(static_dir.iterdir() if static_dir.exists() else []):
        if not path.is_file() or path.suffix.lower() not in _EMOTE_EXTENSIONS:
            continue
        bare = normalize_emote_name(path.stem)
        name = f"#{bare}"
        if name in emotes:
            raise ValueError(
                f"Emote filename collision for {name}: {emotes[name]['image']} and {path.name}"
            )
        emotes[name] = {
            "name": name,
            "image": f"{base_url.rstrip('/')}/{bare}{path.suffix.lower()}",
        }
    return list(emotes.values())


def write_emote_manifest(
    static_dir: Path, base_url: str, manifest_path: Path
) -> list[dict[str, str]]:
    """Atomically refresh and return the restorable emote export."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = build_emote_manifest(static_dir, base_url)
    _write_json(manifest_path, manifest)
    return manifest


def _detect_ext(prefix: bytes, content_type: str | None) -> str:
    """Identify supported image bytes; never infer image data from a URL."""
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return ".webp"
    received = content_type or "unknown content type"
    raise _InvalidImageResponse(f"Response was not a supported image ({received})")


def _is_html_response(prefix: bytes, content_type: str | None) -> bool:
    try:
        _detect_ext(prefix, content_type)
        return False
    except _InvalidImageResponse:
        pass
    normalized = prefix.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return bool(
        content_type
        and "html" in content_type.lower()
        or normalized.startswith((b"<!doctype html", b"<html", b"<?xml"))
    )


def _resolve_giphy_media_url(page_url: str, page_html: bytes) -> str | None:
    page_host = (urlparse(page_url).hostname or "").lower()
    if page_host != "giphy.com" and not page_host.endswith(".giphy.com"):
        return None
    parser = _GiphyMediaParser()
    parser.feed(page_html.decode("utf-8", errors="replace"))
    page_id = urlparse(page_url).path.rstrip("/").rsplit("/", 1)[-1].rsplit("-", 1)[-1]
    candidates = [*parser.media_urls]
    if parser.preview_url:
        candidates.append(parser.preview_url)
    candidate = next(
        (
            candidate
            for candidate in candidates
            if page_id in urlparse(candidate).path.strip("/").split("/")
        ),
        None,
    )
    if candidate is None and not page_id:
        candidate = parser.media_urls[0] if parser.media_urls else parser.preview_url
    if not candidate:
        return None
    parsed = urlparse(candidate)
    media_host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or (
        media_host != "giphy.com" and not media_host.endswith(".giphy.com")
    ):
        return None
    return candidate


def _make_session() -> requests.Session:
    # No automatic urllib3 retries; _place_emote owns all backoff logic.
    return requests.Session()


def _set_permissions(path: Path) -> None:
    """Set mode 0644 and group to www-data (best-effort; logs on failure)."""
    path.chmod(0o644)
    try:
        import grp  # type: ignore[import]  # Unix only

        gid = grp.getgrnam("www-data").gr_gid  # type: ignore[attr-defined]
        os.chown(path, -1, gid)  # type: ignore[attr-defined]
    except (ImportError, KeyError, PermissionError) as exc:
        logger.warning("Could not set www-data group on %s: %s", path, exc)


def _place_emote(
    url: str, bare_name: str, static_dir: Path, max_retries: int
) -> str | None:
    """Download url → static_dir/{bare_name}{ext} with correct permissions.

    Returns the file extension (e.g. ``.gif``) on success, ``None`` if all
    strategies are exhausted.  Blocking — must be called via asyncio.to_thread.
    """
    base = static_dir / bare_name
    tmp = base.parent / f"{base.name}.tmp"
    session = _make_session()
    page_url = url
    download_url = url
    resolved_page = False

    try:
        for attempt in range(max_retries):
            resp = None
            try:
                headers: dict[str, str] = {
                    "User-Agent": random.choice(_USER_AGENTS),
                    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://giphy.com/",
                    "DNT": "1",
                }
                if tmp.exists() and not resolved_page:
                    headers["Range"] = f"bytes={tmp.stat().st_size}-"

                # A Giphy share URL serves HTML; resolve its primary animated
                # media URL, then request it with the share page as Referer.
                for _ in range(2):
                    resp = session.get(
                        download_url, headers=headers, timeout=(10, 30), stream=True
                    )

                    if resp.status_code in _NON_RETRYABLE:
                        logger.info(
                            "Permanent %d for %s — skipping",
                            resp.status_code,
                            download_url,
                        )
                        return _DEAD

                    if resp.status_code == 429:
                        wait = float(
                            resp.headers.get("Retry-After", 60)
                        ) + random.uniform(0, 10)
                        logger.info(
                            "Rate-limited on %s; waiting %.0fs", download_url, wait
                        )
                        time.sleep(wait)
                        resp.close()
                        resp = None
                        break

                    resp.raise_for_status()
                    chunks = iter(resp.iter_content(chunk_size=8192))
                    first_chunk = next(chunks, b"")
                    content_type = resp.headers.get("content-type")

                    if not _is_html_response(first_chunk, content_type):
                        break
                    if resolved_page:
                        raise _InvalidImageResponse(
                            f"Resolved media URL still returned HTML: {download_url}"
                        )

                    page = bytearray()
                    for chunk in chain((first_chunk,), chunks):
                        if chunk:
                            page.extend(chunk)
                            if len(page) > _MAX_SOURCE_PAGE_BYTES:
                                raise _InvalidImageResponse(
                                    "Source HTML exceeded the 2 MiB resolver limit"
                                )

                    media_url = _resolve_giphy_media_url(page_url, bytes(page))
                    if not media_url:
                        raise _InvalidImageResponse(
                            f"HTML source did not contain a supported Giphy media URL: {page_url}"
                        )
                    logger.info(
                        "Resolved Giphy page %s to media URL %s", page_url, media_url
                    )
                    resp.close()
                    resp = None
                    download_url = media_url
                    resolved_page = True
                    headers["Referer"] = page_url
                    headers.pop("Range", None)
                    tmp.unlink(missing_ok=True)
                    continue
                else:
                    continue

                if resp is None or _is_html_response(first_chunk, content_type):
                    continue

                mode = "ab" if "Range" in headers and resp.status_code == 206 else "wb"
                prefix = bytearray(tmp.read_bytes()[:12] if mode == "ab" else b"")
                with open(tmp, mode) as fh:
                    for chunk in chain((first_chunk,), chunks):
                        if chunk:
                            if len(prefix) < 12:
                                prefix.extend(chunk[: 12 - len(prefix)])
                            fh.write(chunk)

                ext = _detect_ext(bytes(prefix), content_type)
                final = base.with_suffix(ext)
                shutil.move(str(tmp), str(final))
                _set_permissions(final)
                return ext

            except _InvalidImageResponse as exc:
                logger.warning("Rejecting non-image response for %s: %s", page_url, exc)
                return None

            except requests.exceptions.RequestException as exc:
                logger.info(
                    "Attempt %d/%d for %s: %s",
                    attempt + 1,
                    max_retries,
                    download_url,
                    exc,
                )
                tmp.unlink(missing_ok=True)
            except Exception as exc:
                logger.info(
                    "Unexpected error attempt %d/%d for %s: %s",
                    attempt + 1,
                    max_retries,
                    download_url,
                    exc,
                )
                tmp.unlink(missing_ok=True)
            finally:
                if resp is not None:
                    resp.close()

            # Ample backoff: 15s, 30s, 60s, 120s, 120s … with jitter
            time.sleep(min(120, 15 * (2**attempt)) + random.uniform(0, 10))

        return None

    finally:
        session.close()
        tmp.unlink(missing_ok=True)


def _attach_file_handler(log_path: Path) -> logging.FileHandler:
    """Add a per-run file handler to this module's logger."""
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    logger.addHandler(handler)
    return handler


def _write_json(path: Path, data: list[dict]) -> None:
    """Atomically write data to path as indented JSON."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


async def _refresh_manifest(
    static_dir: Path, base_url: str, manifest_path: Path
) -> int:
    """Rewrite the on-disk export; never fatal because it is not load-bearing."""
    try:
        manifest = await asyncio.to_thread(
            write_emote_manifest, static_dir, base_url, manifest_path
        )
    except (ValueError, OSError) as exc:
        logger.warning("Could not refresh emote manifest %s: %s", manifest_path, exc)
        return 0
    return len(manifest)


async def rehost_emotes_job(params: dict, ctx) -> dict:
    """Rehost externally-hosted channel emotes to dropsugar.co.

    The live CyTube emote list is the source of truth: emotes are only ever
    updated one at a time, never replaced wholesale from files on disk.
    """
    cfg = ctx.config.emote_rehost
    api = ctx.api_gate
    static_dir = Path(cfg.static_dir)
    manifest_path = Path(cfg.manifest_path)
    await asyncio.to_thread(static_dir.mkdir, parents=True, exist_ok=True)

    if cfg.sync_disk_manifest:
        logger.warning(
            "emote_rehost.sync_disk_manifest is deprecated and ignored: replacing the "
            "channel emote list from disk deletes emotes not yet rehosted"
        )

    try:
        emotes = await api.get_emotes()
    except Exception as exc:
        raise JobError(f"Failed to fetch emotes from api-gate: {exc}") from exc

    if not emotes:
        return {
            "total_emotes": 0,
            "already_rehosted": 0,
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "pushed": 0,
            "failed_emotes": [],
            "manifest_path": str(manifest_path),
            "manifest_count": await _refresh_manifest(
                static_dir, cfg.base_url, manifest_path
            ),
        }

    backup_dir = Path(cfg.backup_dir)
    await asyncio.to_thread(backup_dir.mkdir, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_path = backup_dir / f"rehost-emotes-{stamp}.log"
    file_handler = _attach_file_handler(log_path)
    try:
        await asyncio.to_thread(
            _write_json, backup_dir / f"emotes-{stamp}-before.json", emotes
        )

        to_rehost = [e for e in emotes if cfg.rehost_domain not in e.get("image", "")]
        already = len(emotes) - len(to_rehost)
        await ctx.progress(
            {
                "step": "fetched",
                "total": len(emotes),
                "to_rehost": len(to_rehost),
                "log": str(log_path),
            }
        )

        if not to_rehost:
            logger.info(
                "Nothing to do: all %d emotes already on %s",
                len(emotes),
                cfg.rehost_domain,
            )
            result: dict = {
                "total_emotes": len(emotes),
                "already_rehosted": already,
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
                "pushed": 0,
                "failed_emotes": [],
                "log": str(log_path),
                "manifest_path": str(manifest_path),
                "manifest_count": await _refresh_manifest(
                    static_dir, cfg.base_url, manifest_path
                ),
            }
            await ctx.progress({"step": "complete", **result})
            return result

        logger.info(
            "Starting rehost: %d to download, %d already on %s",
            len(to_rehost),
            already,
            cfg.rehost_domain,
        )

        succeeded: list[str] = []
        failed: list[str] = []
        removed: list[str] = []
        updated = {e["name"]: dict(e) for e in emotes}

        for i, emote in enumerate(to_rehost, 1):
            name = emote["name"]
            url = emote["image"]
            bare = normalize_emote_name(name)

            logger.info("[%d/%d] downloading %s from %s", i, len(to_rehost), name, url)
            t0 = time.perf_counter()
            ext = await asyncio.to_thread(
                _place_emote, url, bare, static_dir, cfg.download_max_retries
            )
            elapsed = time.perf_counter() - t0

            if ext is None or ext == _DEAD:
                logger.warning(
                    "[%d/%d] FAILED%s %s (%.1fs) — %s",
                    i,
                    len(to_rehost),
                    " (source permanently unavailable)" if ext == _DEAD else "",
                    name,
                    elapsed,
                    url,
                )
                failed.append(name)
                await ctx.progress(
                    {
                        "step": "failed",
                        "emote": name,
                        "done": i,
                        "total": len(to_rehost),
                    }
                )
                await asyncio.sleep(cfg.inter_emote_delay_sec)
                continue

            new_url = f"{cfg.base_url.rstrip('/')}/{bare}{ext}"
            updated[name]["image"] = new_url

            try:
                await api.update_emote(name, new_url)
                succeeded.append(name)
                logger.info(
                    "[%d/%d] OK  %s → %s (%.1fs)",
                    i,
                    len(to_rehost),
                    name,
                    new_url,
                    elapsed,
                )
                await ctx.progress(
                    {
                        "step": "pushed",
                        "emote": name,
                        "done": i,
                        "total": len(to_rehost),
                    }
                )
            except Exception as exc:
                logger.error("[%d/%d] push_failed %s: %s", i, len(to_rehost), name, exc)
                failed.append(name)
                await ctx.progress(
                    {
                        "step": "push_failed",
                        "emote": name,
                        "done": i,
                        "total": len(to_rehost),
                        "error": str(exc),
                    }
                )

            await asyncio.sleep(cfg.inter_emote_delay_sec)

        await asyncio.to_thread(
            _write_json,
            backup_dir / f"emotes-{stamp}-after.json",
            list(updated.values()),
        )
        manifest_count = await _refresh_manifest(
            static_dir, cfg.base_url, manifest_path
        )

        logger.info(
            "Done: %d/%d succeeded, %d removed (dead), %d failed",
            len(succeeded),
            len(to_rehost),
            len(removed),
            len(failed),
        )
        if removed:
            logger.info("Removed dead emotes (%d): %s", len(removed), removed)
        if failed:
            logger.warning("Failed emotes: %s", failed)

        result = {
            "total_emotes": len(emotes),
            "already_rehosted": already,
            "attempted": len(to_rehost),
            "succeeded": len(succeeded),
            "removed": len(removed),
            "failed": len(failed),
            "pushed": len(succeeded),
            "failed_emotes": failed,
            "removed_emotes": removed,
            "log": str(log_path),
            "manifest_path": str(manifest_path),
            "manifest_count": manifest_count,
        }
        await ctx.progress({"step": "complete", **result})
        return result
    finally:
        logger.removeHandler(file_handler)
        file_handler.close()
