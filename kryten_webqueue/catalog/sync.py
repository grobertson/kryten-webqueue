import asyncio
import httpx
import logging
from datetime import datetime, UTC
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _describe_httpx_error(exc: Exception, url: str) -> str:
    """Return a human-readable description of an httpx network error."""
    host = urlparse(url).netloc or url
    if isinstance(exc, httpx.ConnectTimeout):
        return f"Connection to {host} timed out — check host/port and firewall"
    if isinstance(exc, httpx.ConnectError):
        cause = str(exc.__cause__ or exc).lower()
        if (
            "name or service not known" in cause
            or "nodename nor servname" in cause
            or "getaddrinfo" in cause
        ):
            return f"DNS lookup failed for {host} — check mediacms_url in config"
        if "connection refused" in cause:
            return f"Connection refused by {host} — service may be down"
        return f"Could not connect to {host}: {exc.__cause__ or exc}"
    if isinstance(exc, httpx.ReadTimeout):
        return f"Read timed out waiting for response from {host}"
    if isinstance(exc, httpx.TimeoutException):
        return f"Request to {host} timed out"
    return f"{type(exc).__name__} connecting to {host}: {exc}"


class CatalogSync:
    """Synchronizes catalog data from MediaCMS."""

    def __init__(self, *, mediacms_url: str, mediacms_token: str, db, cover_art=None):
        # Strip any accidental /api/v1 suffix — the sync code appends it itself
        url = mediacms_url.rstrip("/")
        for suffix in ("/api/v1", "/api"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        self._url = url
        self._token = mediacms_token
        self._db = db
        self._cover_art = cover_art
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
            # /manage_media returns all items (9k+); /media is capped at 1000
            next_url: str | None = f"{self._url}/api/v1/manage_media"
            params: dict = {"page_size": 50}
            page = 0

            while next_url:
                page += 1
                try:
                    resp = await self._client.get(
                        next_url, params=params if page == 1 else None
                    )
                except httpx.TransportError as exc:
                    logger.error(_describe_httpx_error(exc, next_url))
                    stats["errors"] += 1
                    break

                if resp.status_code != 200:
                    logger.error(
                        f"MediaCMS API returned {resp.status_code} for URL {resp.url} — "
                        f"body: {resp.text[:200]}"
                    )
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
                        logger.warning(
                            f"Error processing {media.get('friendly_token')}: {e}"
                        )
                        stats["errors"] += 1

                # Follow the next URL from the response — don't construct it ourselves
                next_url = data.get("next") if isinstance(data, dict) else None
                logger.debug(
                    f"Catalog sync page {page}: seen={stats['seen']} next={next_url!r}"
                )

            await self._db.finish_sync_log(log_id, stats, "completed")
            logger.info(f"Catalog sync complete: {stats} ({page} pages)")
        except asyncio.CancelledError:
            # Server is shutting down — propagate cleanly so JobManager can
            # record the cancelled status while the DB is still open.
            logger.info("Catalog sync cancelled (shutdown in progress)")
            raise
        except httpx.TransportError as exc:
            logger.error(_describe_httpx_error(exc, f"{self._url}/api/v1/media"))
            stats["errors"] += 1
            try:
                await self._db.finish_sync_log(log_id, stats, "error")
            except Exception:  # noqa: BLE001
                logger.debug("Could not persist sync error status (DB unavailable)")
        except Exception as exc:
            logger.exception(f"Catalog sync failed: {type(exc).__name__}: {exc}")
            stats["errors"] += 1
            try:
                await self._db.finish_sync_log(log_id, stats, "error")
            except Exception:  # noqa: BLE001
                logger.debug("Could not persist sync error status (DB unavailable)")

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
            # True MediaCMS publish time so "Newest first" browse ordering is
            # accurate; falls back to now when the field is absent.
            "added_at": media.get("add_date") or now,
            "synced_at": now,
        }

        existing = await self._db.get_item_admin(token)
        if existing:
            await self._db.update_catalog(token, row)
            stats["updated"] += 1
        else:
            await self._db.insert_catalog(row)
            stats["new"] += 1

        # Categories & tags require a per-item detail fetch — the manage_media
        # list serializer omits them. Best-effort; never fail the item over this.
        try:
            await self._sync_item_facets(token)
        except Exception as e:
            logger.debug(f"Facet sync failed for {token}: {e}")

        # Fetch TMDB/OMDB cover art if not already cached
        if self._cover_art and not (existing and existing.get("cover_art_path")):
            try:
                await self._cover_art.resolve(token, row["title"], self._db)
            except Exception as e:
                logger.debug(f"Cover art resolve failed for {token}: {e}")
            await asyncio.sleep(0.25)

    async def _sync_item_facets(self, token: str):
        """Populate category/tag memberships from the media detail endpoint.

        The list (manage_media) serializer omits categories/tags, so we fetch
        the per-item detail (``/api/v1/media/{token}``) which exposes
        ``categories_info`` and ``tags_info``.
        """
        resp = await self._client.get(f"{self._url}/api/v1/media/{token}")
        if resp.status_code != 200:
            return
        data = resp.json()

        cat_names = [
            c.get("title")
            for c in (data.get("categories_info") or [])
            if isinstance(c, dict) and c.get("title")
        ]
        tag_names = []
        for t in data.get("tags_info") or []:
            name = t.get("title") if isinstance(t, dict) else t
            if name:
                tag_names.append(name)

        cat_ids = [await self._db.upsert_category(n) for n in cat_names]
        tag_ids = [await self._db.upsert_tag(n) for n in tag_names]
        await self._db.set_catalog_categories(token, cat_ids)
        await self._db.set_catalog_tags(token, tag_ids)

    def _build_manifest_url(self, media: dict) -> str:
        # CyTube custom media ("cm") requires the manifest JSON URL, NOT the
        # human watch page (/view?m=TOKEN). MediaCMS exposes a CyTube manifest at
        # /api/v1/media/cytube/<token>.json?format=json
        token = media.get("friendly_token")
        if token:
            return f"{self._url}/api/v1/media/cytube/{token}.json?format=json"
        # Fallback: derive token from the watch URL if friendly_token is missing
        url = media.get("url", "")
        return url if url.startswith("http") else f"{self._url}{url}"
