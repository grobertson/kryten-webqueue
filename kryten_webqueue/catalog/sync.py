import httpx
import logging
from datetime import datetime, UTC

logger = logging.getLogger(__name__)


class CatalogSync:
    """Synchronizes catalog data from MediaCMS."""

    def __init__(self, *, mediacms_url: str, mediacms_token: str, db):
        self._url = mediacms_url.rstrip("/")
        self._token = mediacms_token
        self._db = db
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Token {mediacms_token}"},
            timeout=30.0,
        )

    async def close(self):
        await self._client.aclose()

    async def sync(self):
        """Full catalog sync from MediaCMS API."""
        log_id = await self._db.start_sync_log()
        stats = {"seen": 0, "new": 0, "updated": 0, "errors": 0}

        try:
            page = 1
            while True:
                resp = await self._client.get(
                    f"{self._url}/api/v1/media",
                    params={"page": page, "page_size": 50},
                )
                if resp.status_code != 200:
                    logger.error(f"MediaCMS API returned {resp.status_code}")
                    stats["errors"] += 1
                    break

                data = resp.json()
                results = data if isinstance(data, list) else data.get("results", [])

                if not results:
                    break

                for media in results:
                    stats["seen"] += 1
                    try:
                        await self._process_item(media, stats)
                    except Exception as e:
                        logger.warning(f"Error processing {media.get('friendly_token')}: {e}")
                        stats["errors"] += 1

                # Check for next page
                if isinstance(data, dict) and not data.get("next"):
                    break
                page += 1

            await self._db.finish_sync_log(log_id, stats, "completed")
            logger.info(f"Catalog sync: {stats}")
        except Exception as e:
            logger.error(f"Catalog sync failed: {e}")
            stats["errors"] += 1
            await self._db.finish_sync_log(log_id, stats, "error")

    async def _process_item(self, media: dict, stats: dict):
        token = media.get("friendly_token")
        if not token:
            return

        now = datetime.now(UTC).isoformat()
        row = {
            "friendly_token": token,
            "title": media.get("title", "Untitled"),
            "description": media.get("description", ""),
            "duration_sec": media.get("duration") or 0,
            "manifest_url": self._build_manifest_url(media),
            "thumbnail_url": media.get("thumbnail_url", ""),
            "synced_at": now,
        }

        existing = await self._db.get_item_admin(token)
        if existing:
            await self._db.update_catalog(token, row)
            stats["updated"] += 1
        else:
            await self._db.insert_catalog(row)
            stats["new"] += 1

    def _build_manifest_url(self, media: dict) -> str:
        hls_file = media.get("hls_file")
        if hls_file:
            return hls_file if hls_file.startswith("http") else f"{self._url}{hls_file}"
        # Fallback to original URL
        original = media.get("original_media_url", "")
        return original if original.startswith("http") else f"{self._url}{original}"
