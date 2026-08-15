"""Step: art — poster resolution using classification.lookup_title (key fix for hosted movies)."""

from __future__ import annotations

import hashlib
import io
import logging
from datetime import datetime, UTC
from pathlib import Path

import httpx
from PIL import Image

from ..classify import ItemClassification
from ..providers import TMDBProvider, OMDBProvider
from ..report import StepResult

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_WIDTHS = [200, 400, 800]


class ArtStep:
    def __init__(self, *, db, config):
        self._db = db
        self._image_dir = Path(config.image_dir)
        self._tmdb = TMDBProvider(config.tmdb_api_key)
        self._omdb = OMDBProvider(config.omdb_api_key)

    async def close(self) -> None:
        await self._tmdb.close()
        await self._omdb.close()

    async def run(
        self,
        *,
        classifications: list[ItemClassification],
        dry_run: bool = False,
        force: bool = False,
        ctx=None,
    ) -> StepResult:
        result = StepResult()
        now = datetime.now(UTC).isoformat()

        for cls in classifications:
            result.processed += 1
            try:
                # In normal mode skip items that already have real art
                if not force and cls.has_real_art:
                    result.skipped += 1
                    continue

                # Fetch the enrichment row to get cached poster_url
                state = await self._db.get_enrichment_state(cls.friendly_token)
                poster_url = self._cached_poster_url(state)

                # If not cached, query providers using lookup_title (NOT raw title)
                if not poster_url:
                    poster_url = await self._resolve_poster(cls)

                if not poster_url:
                    # Fall back to MediaCMS thumbnail
                    row = await self._db.get_item_admin(cls.friendly_token)
                    thumb = (row or {}).get("thumbnail_url")
                    if thumb:
                        poster_url = thumb
                        source = "thumbnail"
                    else:
                        result.skipped += 1
                        continue
                else:
                    source = "tmdb"  # set correctly after save

                if dry_run:
                    result.changed += 1
                    continue

                # File-size check in force mode
                existing_path = self._art_path(cls.friendly_token)
                if force and existing_path.exists() and source != "thumbnail":
                    if not await self._needs_update(poster_url, existing_path):
                        result.skipped += 1
                        await self._db.save_enrichment_state(
                            cls.friendly_token, last_art_at=now
                        )
                        continue

                # Download and save
                src = await self._download_and_save(
                    cls.friendly_token, poster_url, source
                )
                if src:
                    result.changed += 1
                    logger.info("[art] %s: saved %s poster", cls.friendly_token, src)
                else:
                    result.skipped += 1

                await self._db.save_enrichment_state(
                    cls.friendly_token, last_art_at=now
                )

            except Exception as exc:
                logger.warning("[art] %s error: %s", cls.friendly_token, exc)
                result.record_error(f"{cls.friendly_token}: {exc}")

        return result

    def _cached_poster_url(self, state: dict | None) -> str | None:
        if not state:
            return None
        meta = self._db.parse_meta_json(state)
        return (meta or {}).get("poster_url")

    async def _resolve_poster(self, cls: ItemClassification) -> str | None:
        """Query TMDB then OMDB using lookup_title (the key fix for hosted movies).

        For TV episodes, search for the show poster (not episode-specific art).
        """
        if not cls.lookup_title:
            return None

        # TV episodes: get the show poster
        if cls.content_type == "tv_episode":
            meta = await self._tmdb.search_tv_show(cls.lookup_title)
            if meta.poster_url:
                return meta.poster_url
            meta = await self._omdb.search_tv_show(cls.lookup_title)
            return meta.poster_url

        # Movies and other content: search as movie
        meta = await self._tmdb.search_movie(cls.lookup_title, cls.lookup_year)
        if meta.poster_url:
            return meta.poster_url
        meta = await self._omdb.search_movie(cls.lookup_title, cls.lookup_year)
        return meta.poster_url

    def _art_path(self, token: str) -> Path:
        h = hashlib.md5(token.encode()).hexdigest()[:8]
        return self._image_dir / h / "400.webp"

    async def _needs_update(self, url: str, existing: Path) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.head(url)
                remote = int(resp.headers.get("content-length", 0))
                if remote <= 0:
                    return True
                return existing.stat().st_size != remote
        except Exception:
            return True

    async def _download_and_save(self, token: str, url: str, source: str) -> str | None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.content

        tok_hash = hashlib.md5(token.encode()).hexdigest()[:8]
        base_dir = self._image_dir / tok_hash
        base_dir.mkdir(parents=True, exist_ok=True)

        img = Image.open(io.BytesIO(data))
        if img.mode != "RGB":
            img = img.convert("RGB")

        for width in _WIDTHS:
            resized = (
                img.resize((width, int(img.height * width / img.width)), Image.LANCZOS)
                if img.width > width
                else img.copy()
            )
            (base_dir / f"{width}.webp").write_bytes(self._encode_webp(resized))

        relative = tok_hash
        await self._db.update_cover_art(token, relative, source)
        return source

    @staticmethod
    def _encode_webp(img: Image.Image) -> bytes:
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=80)
        return buf.getvalue()
