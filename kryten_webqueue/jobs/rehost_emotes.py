"""Emote rehost job — download externally-hosted emotes and serve from dropsugar.co.

Processes emotes whose image URLs are not already on the configured rehost
domain.  For each, aggressively downloads the image, places it at
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
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .manager import JobError

logger = logging.getLogger(__name__)

REHOST_EMOTES_SCHEMA: list[dict] = []

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


def _detect_ext(url: str, content_type: str | None) -> str:
    """Determine file extension from Content-Type or URL path; default .gif."""
    if content_type:
        ct = content_type.lower()
        if "gif" in ct:
            return ".gif"
        if "png" in ct:
            return ".png"
        if "jpeg" in ct or "jpg" in ct:
            return ".jpg"
        if "webp" in ct:
            return ".webp"
    path = unquote(urlparse(url).path).lower()
    for ext in (".gif", ".png", ".jpg", ".jpeg", ".webp"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".gif"


def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


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

    try:
        # Primary strategy: resumable, rotating User-Agent, progressive timeout
        for attempt in range(max_retries):
            try:
                headers: dict[str, str] = {
                    "User-Agent": random.choice(_USER_AGENTS),
                    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                    "DNT": "1",
                }
                if tmp.exists():
                    headers["Range"] = f"bytes={tmp.stat().st_size}-"

                timeout = min(60 + attempt * 30, 180)
                resp = session.get(url, headers=headers, timeout=timeout, stream=True)
                resp.raise_for_status()

                ext = _detect_ext(url, resp.headers.get("content-type"))
                mode = "ab" if "Range" in headers and resp.status_code == 206 else "wb"
                with open(tmp, mode) as fh:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            fh.write(chunk)

                final = base.with_suffix(ext)
                shutil.move(str(tmp), str(final))
                _set_permissions(final)
                return ext

            except requests.exceptions.RequestException as exc:
                logger.debug(
                    "Attempt %d/%d for %s: %s", attempt + 1, max_retries, url, exc
                )
                time.sleep(2**attempt + random.uniform(0, 1))
            except Exception as exc:
                logger.debug(
                    "Unexpected error attempt %d/%d for %s: %s",
                    attempt + 1,
                    max_retries,
                    url,
                    exc,
                )
                tmp.unlink(missing_ok=True)
                time.sleep(2**attempt + random.uniform(0, 1))

        # Fallback: minimal headers, non-streaming
        for attempt in range(2):
            try:
                resp = requests.get(
                    url,
                    headers={"User-Agent": "curl/7.68.0", "Accept": "*/*"},
                    timeout=120,
                )
                resp.raise_for_status()
                ext = _detect_ext(url, resp.headers.get("content-type"))
                final = base.with_suffix(ext)
                final.write_bytes(resp.content)
                _set_permissions(final)
                return ext
            except Exception as exc:
                logger.debug("Fallback attempt %d for %s: %s", attempt + 1, url, exc)
                time.sleep(2)

        return None

    finally:
        session.close()
        tmp.unlink(missing_ok=True)


def _write_json(path: Path, data: list[dict]) -> None:
    """Atomically write data to path as indented JSON."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


async def rehost_emotes_job(params: dict, ctx) -> dict:
    """Rehost externally-hosted channel emotes to dropsugar.co."""
    cfg = ctx.config.emote_rehost
    api = ctx.api_gate

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
        }

    # Back up current state before making any changes
    backup_dir = Path(cfg.backup_dir)
    await asyncio.to_thread(backup_dir.mkdir, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    await asyncio.to_thread(
        _write_json, backup_dir / f"emotes-{stamp}-before.json", emotes
    )

    to_rehost = [e for e in emotes if cfg.rehost_domain not in e.get("image", "")]
    already = len(emotes) - len(to_rehost)
    await ctx.progress(
        {"step": "fetched", "total": len(emotes), "to_rehost": len(to_rehost)}
    )

    if not to_rehost:
        result: dict = {
            "total_emotes": len(emotes),
            "already_rehosted": already,
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "pushed": 0,
            "failed_emotes": [],
        }
        await ctx.progress({"step": "complete", **result})
        return result

    static_dir = Path(cfg.static_dir)
    await asyncio.to_thread(static_dir.mkdir, parents=True, exist_ok=True)

    succeeded: list[str] = []
    failed: list[str] = []
    # mutable working copy keyed by name for building the after-backup
    updated = {e["name"]: dict(e) for e in emotes}

    for i, emote in enumerate(to_rehost, 1):
        name = emote["name"]  # e.g. "#behold" (includes #)
        url = emote["image"]
        bare = name.lstrip("#")  # "behold" → used as filename root

        ext = await asyncio.to_thread(
            _place_emote, url, bare, static_dir, cfg.download_max_retries
        )

        if ext is None:
            logger.warning(
                "All download strategies failed for emote %s (%s)", name, url
            )
            failed.append(name)
            await ctx.progress(
                {"step": "failed", "emote": name, "done": i, "total": len(to_rehost)}
            )
            await asyncio.sleep(cfg.inter_emote_delay_sec)
            continue

        new_url = f"{cfg.base_url.rstrip('/')}/{bare}{ext}"
        updated[name]["image"] = new_url

        try:
            await api.update_emote(name, new_url)
            succeeded.append(name)
            await ctx.progress(
                {"step": "pushed", "emote": name, "done": i, "total": len(to_rehost)}
            )
        except Exception as exc:
            logger.error("Failed to push emote %s to api-gate: %s", name, exc)
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
        _write_json, backup_dir / f"emotes-{stamp}-after.json", list(updated.values())
    )

    result = {
        "total_emotes": len(emotes),
        "already_rehosted": already,
        "attempted": len(to_rehost),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "pushed": len(succeeded),
        "failed_emotes": failed,
    }
    await ctx.progress({"step": "complete", **result})
    return result
