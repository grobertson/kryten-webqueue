"""Minimal MediaCMS edit client for the admin Hide/Unhide action (B6).

This is the Phase 1 standalone helper called for by the spec ("write a minimal
standalone MediaCMS tag-write helper now and fold it into ``_common.py`` later").
It performs a read-modify-write of an item's tags so existing tags are
preserved, using the same edit path the enrich tools use:

    PUT /api/v1/media/{friendly_token}   (form field ``tags``)

MediaCMS has a known quirk where a PUT silently reassigns the owner to whoever
holds the API token, so we restore the original owner afterwards via
``/api/v1/media/user/bulk_actions``.

NOTE: the exact tag field/verb should be confirmed against the live instance
(see spec OQ-6 / residual risks). The local catalog mirror is updated
independently by the caller, so the admin UI hides items immediately even if
this remote write needs adjustment.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


def _normalize_base(mediacms_url: str) -> str:
    url = (mediacms_url or "").rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url


class MediaCMSClient:
    """Tiny async wrapper for the MediaCMS media-edit endpoints."""

    def __init__(self, *, mediacms_url: str, token: str):
        self._base = _normalize_base(mediacms_url)
        self._headers = {"Authorization": f"Token {token}"}

    async def _get_media(
        self, client: httpx.AsyncClient, friendly_token: str
    ) -> dict | None:
        resp = await client.get(f"{self._base}/api/v1/media/{friendly_token}")
        if resp.status_code != 200:
            logger.warning(
                "MediaCMS GET media %s failed: HTTP %s",
                friendly_token,
                resp.status_code,
            )
            return None
        return resp.json()

    @staticmethod
    def _current_tags(media: dict) -> list[str]:
        tags: list[str] = []
        for t in media.get("tags_info") or []:
            name = t.get("title") if isinstance(t, dict) else t
            if name:
                tags.append(str(name))
        # Fall back to a flat ``tags`` list if present.
        if not tags:
            for t in media.get("tags") or []:
                if t:
                    tags.append(str(t))
        return tags

    async def _restore_owner(
        self, client: httpx.AsyncClient, friendly_token: str, owner: str | None
    ):
        if not owner:
            return
        try:
            await client.post(
                f"{self._base}/api/v1/media/user/bulk_actions",
                json={
                    "action": "change_owner",
                    "media_ids": [friendly_token],
                    "owner": owner,
                },
            )
        except httpx.HTTPError:
            pass  # best-effort; never fail the hide over ownership restoration

    async def set_tag(self, friendly_token: str, tag: str, *, present: bool) -> bool:
        """Add or remove ``tag`` on an item, preserving existing tags.

        Returns True if the remote write reported success.
        """
        async with httpx.AsyncClient(headers=self._headers, timeout=_TIMEOUT) as client:
            media = await self._get_media(client, friendly_token)
            if media is None:
                return False

            current = self._current_tags(media)
            has = tag in current
            if present and has:
                return True
            if not present and not has:
                return True

            new_tags = [t for t in current if t != tag]
            if present:
                new_tags.append(tag)

            owner = media.get("user")
            resp = await client.put(
                f"{self._base}/api/v1/media/{friendly_token}",
                data={"tags": ",".join(new_tags)},
            )
            ok = resp.status_code in (200, 201)
            if not ok:
                logger.warning(
                    "MediaCMS tag write for %s failed: HTTP %s — %s",
                    friendly_token,
                    resp.status_code,
                    resp.text[:200],
                )
            await self._restore_owner(client, friendly_token, owner)
            return ok

    async def update_item(
        self,
        friendly_token: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> bool:
        """Update item title and/or description in MediaCMS.

        Returns True if the remote write succeeded.
        """
        if not title and not description:
            return True

        async with httpx.AsyncClient(headers=self._headers, timeout=_TIMEOUT) as client:
            media = await self._get_media(client, friendly_token)
            if media is None:
                return False

            owner = media.get("user")
            payload = {}
            if title is not None:
                payload["title"] = title
            if description is not None:
                payload["description"] = description

            resp = await client.put(
                f"{self._base}/api/v1/media/{friendly_token}",
                data=payload,
            )
            ok = resp.status_code in (200, 201)
            if not ok:
                logger.warning(
                    "MediaCMS update for %s failed: HTTP %s — %s",
                    friendly_token,
                    resp.status_code,
                    resp.text[:200],
                )
            await self._restore_owner(client, friendly_token, owner)
            return ok
