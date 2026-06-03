import httpx
import logging
from pathlib import Path
from PIL import Image
import io
import hashlib

logger = logging.getLogger(__name__)


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

    async def close(self):
        await self._client.aclose()

    async def resolve(self, friendly_token: str, title: str, db) -> str | None:
        """Try to fetch cover art; return relative path or None."""
        # Check if already cached
        existing = await db.get_item_admin(friendly_token)
        if existing and existing.get("cover_art_path"):
            return existing["cover_art_path"]

        if not self._tmdb_key and not self._omdb_key:
            logger.warning("No TMDB or OMDB API keys configured — cover art lookup skipped")
            return None

        # Try TMDB first
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

        if not image_url:
            return None

        # Download and generate responsive variants
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
        try:
            resp = await self._client.get(
                "https://api.themoviedb.org/3/search/multi",
                params={"api_key": self._tmdb_key, "query": title},
            )
            if resp.status_code != 200:
                logger.warning(f"TMDB API returned {resp.status_code} for {title!r}")
                return None
            results = resp.json().get("results", [])
            # Prefer movie/tv results (have poster_path); skip person results
            for result in results:
                if result.get("media_type") in ("movie", "tv"):
                    poster = result.get("poster_path")
                    if poster:
                        return f"https://image.tmdb.org/t/p/w500{poster}"
        except Exception as e:
            logger.warning(f"TMDB search error for {title!r}: {e}")
        return None

    async def _search_omdb(self, title: str) -> str | None:
        try:
            resp = await self._client.get(
                "https://www.omdbapi.com/",
                params={"apikey": self._omdb_key, "t": title},
            )
            if resp.status_code != 200:
                logger.warning(f"OMDB API returned {resp.status_code} for {title!r}")
                return None
            data = resp.json()
            poster = data.get("Poster")
            if poster and poster != "N/A":
                return poster
        except Exception as e:
            logger.warning(f"OMDB search error for {title!r}: {e}")
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
