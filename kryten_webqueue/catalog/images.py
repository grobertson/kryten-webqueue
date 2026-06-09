import asyncio
import hashlib
import io
import logging
import re
from pathlib import Path

import httpx
from PIL import Image

logger = logging.getLogger(__name__)


def _clean_title(title: str) -> tuple[str, str | None]:
    """Return (cleaned_title, year_or_None) stripping common noise."""
    # Extract 4-digit year in parens or at end: "Title (2019)" or "Title 2019"
    year = None
    m = re.search(r"\b((?:19|20)\d{2})\b", title)
    if m:
        year = m.group(1)
    # Remove year, episode tags, resolution tags, etc.
    cleaned = re.sub(r"\s*[\(\[]?(?:19|20)\d{2}[\)\]]?", "", title)
    cleaned = re.sub(r"\s*[Ss]\d{1,2}[Ee]\d{1,2}.*", "", cleaned)
    cleaned = re.sub(r"\s*\b(?:720p|1080p|2160p|4K|HDR|BluRay|BDRip|WEB[-.]?DL|HDTV)\b.*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" .-")
    return cleaned or title, year


class CoverArtResolver:
    """Downloads and caches cover art for catalog items."""

    WIDTHS = [200, 400, 800]

    def __init__(self, *, image_dir: str, placeholder_dir: str,
                 tmdb_api_key: str = "", omdb_api_key: str = ""):
        self._image_dir = Path(image_dir)
        self._placeholder_dir = Path(placeholder_dir)
        self._tmdb_key = tmdb_api_key
        self._omdb_key = omdb_api_key
        self._client = httpx.AsyncClient(timeout=15.0)
        self._image_dir.mkdir(parents=True, exist_ok=True)
        self._placeholder_dir.mkdir(parents=True, exist_ok=True)
        # Cached list of branded placeholder image URLs (served under /images).
        self._placeholder_urls: list[str] = []
        self._placeholder_cache_at: float = 0.0

    async def close(self):
        await self._client.aclose()

    def list_placeholder_urls(self, *, ttl: float = 300.0) -> list[str]:
        """Return web URLs for branded placeholder images, cached in memory.

        The directory is rescanned at most once per ``ttl`` seconds to avoid a
        disk scan on every browse request. URLs are resolved relative to the
        ``/images`` static mount when the placeholder dir lives under the image
        dir (the default layout); otherwise the bare filename is used.
        """
        import time

        now = time.monotonic()
        if self._placeholder_urls and (now - self._placeholder_cache_at) < ttl:
            return self._placeholder_urls

        exts = {".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"}
        try:
            files = sorted(
                p.name for p in self._placeholder_dir.iterdir()
                if p.is_file() and p.suffix.lower() in exts
            )
        except OSError:
            files = []

        try:
            rel = self._placeholder_dir.resolve().relative_to(self._image_dir.resolve())
            prefix = "/images/" + rel.as_posix().strip("/") + "/"
        except ValueError:
            prefix = "/images/placeholders/"

        self._placeholder_urls = [prefix + f for f in files]
        self._placeholder_cache_at = now
        return self._placeholder_urls


    async def resolve(self, friendly_token: str, title: str, db) -> str | None:
        """Try to fetch cover art; return relative path or None."""
        # Check if already cached
        existing = await db.get_item_admin(friendly_token)
        if existing and existing.get("cover_art_path"):
            return existing["cover_art_path"]

        if not self._tmdb_key and not self._omdb_key:
            logger.warning("No TMDB or OMDB API keys configured — cover art lookup skipped")
            return None

        image_url = None
        source = None

        if self._tmdb_key:
            image_url = await self._search_tmdb(title)
            if image_url:
                source = "tmdb"
                logger.debug(f"TMDB found art for {friendly_token!r}: {title!r}")
            else:
                logger.debug(f"TMDB found no art for {friendly_token!r}: {title!r}")

        if not image_url and self._omdb_key:
            image_url = await self._search_omdb(title)
            if image_url:
                source = "omdb"
                logger.debug(f"OMDB found art for {friendly_token!r}: {title!r}")
            else:
                logger.debug(f"OMDB found no art for {friendly_token!r}: {title!r}")

        # Last resort: use the MediaCMS thumbnail already in the DB
        if not image_url and existing and existing.get("thumbnail_url"):
            image_url = existing["thumbnail_url"]
            source = "thumbnail"
            logger.debug(f"Falling back to thumbnail for {friendly_token!r}")

        if not image_url:
            return None

        try:
            resp = await self._client.get(image_url)
            if resp.status_code != 200:
                logger.warning(f"Cover art download failed for {friendly_token!r}: HTTP {resp.status_code} {image_url}")
                return None
            path = await self._save_responsive(friendly_token, resp.content, source, db)
            logger.info(f"Cover art saved for {friendly_token!r} ({source}): {path}")
            return path
        except Exception as e:
            logger.warning(f"Failed to download cover art for {friendly_token}: {e}")
            return None

    async def _search_tmdb(self, title: str) -> str | None:
        """Search TMDB for a poster: tries movie+TV in parallel, retries with cleaned title."""
        result = await self._tmdb_search_both(title)
        if result:
            return result
        # Retry with cleaned title if it differs
        cleaned, year = _clean_title(title)
        if cleaned != title:
            result = await self._tmdb_search_both(cleaned, year=year)
        return result

    async def _tmdb_search_both(self, title: str, year: str | None = None) -> str | None:
        """Search TMDB movie and TV endpoints in parallel, pick best poster by popularity."""
        movie_task = asyncio.create_task(self._tmdb_search_type("movie", title, year))
        tv_task = asyncio.create_task(self._tmdb_search_type("tv", title, year))
        movie_hit, tv_hit = await asyncio.gather(movie_task, tv_task)

        # Pick whichever has a poster; prefer movie if both have one and similar popularity
        candidates = [h for h in (movie_hit, tv_hit) if h and h[0]]
        if not candidates:
            return None
        # Sort by popularity descending, return the best poster URL
        candidates.sort(key=lambda h: h[1], reverse=True)
        return candidates[0][0]

    async def _tmdb_search_type(self, media_type: str, title: str, year: str | None = None) -> tuple[str | None, float]:
        """Search a specific TMDB media type. Returns (poster_url_or_None, popularity)."""
        params: dict = {"api_key": self._tmdb_key, "query": title}
        if year and media_type == "movie":
            params["year"] = year
        elif year and media_type == "tv":
            params["first_air_date_year"] = year
        try:
            resp = await self._client.get(
                f"https://api.themoviedb.org/3/search/{media_type}",
                params=params,
            )
            if resp.status_code != 200:
                logger.warning(f"TMDB {media_type} search returned {resp.status_code} for {title!r}")
                return None, 0.0
            results = resp.json().get("results", [])
            for result in results:
                poster = result.get("poster_path")
                if poster:
                    # w780 gives better quality than w500
                    url = f"https://image.tmdb.org/t/p/w780{poster}"
                    return url, float(result.get("popularity", 0))
        except Exception as e:
            logger.warning(f"TMDB {media_type} search error for {title!r}: {e}")
        return None, 0.0

    async def _search_omdb(self, title: str) -> str | None:
        cleaned, year = _clean_title(title)
        for t in dict.fromkeys([title, cleaned]):  # try original then cleaned, deduped
            params: dict = {"apikey": self._omdb_key, "t": t}
            if year:
                params["y"] = year
            try:
                resp = await self._client.get("https://www.omdbapi.com/", params=params)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                poster = data.get("Poster")
                if poster and poster != "N/A":
                    return poster
            except Exception as e:
                logger.warning(f"OMDB search error for {t!r}: {e}")
        return None

    async def _save_responsive(self, friendly_token: str, data: bytes, source: str, db) -> str:
        """Save image in multiple responsive widths."""
        token_hash = hashlib.md5(friendly_token.encode()).hexdigest()[:8]
        base_dir = self._image_dir / token_hash
        base_dir.mkdir(parents=True, exist_ok=True)

        img = Image.open(io.BytesIO(data))
        if img.mode != "RGB":
            img = img.convert("RGB")

        for width in self.WIDTHS:
            if img.width > width:
                ratio = width / img.width
                height = int(img.height * ratio)
                resized = img.resize((width, height), Image.LANCZOS)
            else:
                resized = img.copy()
            out_path = base_dir / f"{width}.webp"
            resized.save(out_path, "WEBP", quality=80)

        relative_path = f"{token_hash}"
        await db.update_cover_art(friendly_token, relative_path, source)
        return relative_path
