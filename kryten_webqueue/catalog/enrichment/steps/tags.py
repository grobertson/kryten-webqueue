"""Step: tags — push genres/MPAA/hosted-show tags to CMS; sync CMS-only tags locally."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, UTC

import httpx

from ..classify import ItemClassification
from ..report import StepResult

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_MPAA = frozenset({"G", "PG", "PG-13", "R", "NC-17", "NR", "TV-MA", "TV-14", "TV-PG"})

_MPAA_SLUG = {
    "G": "mpaag",
    "PG": "mpaapg",
    "PG-13": "mpaapg13",
    "R": "mpaar",
    "NC-17": "mpaanc17",
    "NR": "mpaanr",
    "TV-MA": "mpaatvma",
    "TV-14": "mpaatv14",
    "TV-PG": "mpaatvpg",
}


def _genre_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class TagsStep:
    def __init__(self, *, db, config):
        self._db = db
        self._base = (
            config.mediacms_url.rstrip("/").removesuffix("/api/v1").removesuffix("/api")
        )
        self._headers = {"Authorization": f"Token {config.mediacms_token}"}
        # When the token can manage all media (e.g. a manager/superuser on a
        # patched CMS), push tags for every item instead of only owned media.
        self._manage_all = bool(getattr(config, "mediacms_manage_all_media", False))

    async def _get_api_username(self, client: httpx.AsyncClient) -> str | None:
        """Return the API token's username, or None.

        Stock MediaCMS ``/media/user/bulk_actions`` filters ``Media.objects.filter(
        user=request.user, ...)``, so tag pushes only succeed on media this user
        owns. When ``mediacms_manage_all_media`` is not set we use this to skip
        futile pushes against media owned by others instead of flooding CMS.
        """
        try:
            resp = await client.get(f"{self._base}/api/v1/whoami")
            if resp.status_code != 200:
                return None
            return resp.json().get("username")
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            return None

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

        async with httpx.AsyncClient(headers=self._headers, timeout=_TIMEOUT) as client:
            api_user = (
                None
                if dry_run or self._manage_all
                else await self._get_api_username(client)
            )
            for cls in classifications:
                result.processed += 1
                try:
                    state = await self._db.get_enrichment_state(cls.friendly_token)
                    meta = self._db.parse_meta_json(state) if state else None

                    desired: list[str] = []

                    # Genre tags
                    for genre in (meta or {}).get("genres", []):
                        desired.append(_genre_slug(genre))

                    # Hosted-show tag
                    if cls.hosted:
                        desired.append(cls.hosted.cms_tag)

                    # MPAA rating tag
                    cr = (meta or {}).get("content_rating", "")
                    if cr in _MPAA:
                        desired.append(_MPAA_SLUG.get(cr, f"mpaa-{cr.lower()}"))

                    if not desired:
                        result.skipped += 1
                        await self._db.save_enrichment_state(
                            cls.friendly_token, last_tags_at=now
                        )
                        continue

                    if not dry_run:
                        # Persist computed tags locally first so item detail shows
                        # them even if the CMS write below fails (owner/permission).
                        for tag in desired:
                            await self._db.add_catalog_tag(cls.friendly_token, tag)
                        changed = await self._push_tags(
                            client,
                            cls.friendly_token,
                            desired,
                            api_user=api_user,
                        )
                        # Reverse sync: pull any CMS-only tags back to local DB
                        await self._reverse_sync(client, cls.friendly_token)
                        if changed:
                            result.changed += 1
                        else:
                            result.skipped += 1
                    else:
                        result.changed += 1

                    await self._db.save_enrichment_state(
                        cls.friendly_token, last_tags_at=now
                    )

                except Exception as exc:
                    logger.warning("[tags] %s error: %s", cls.friendly_token, exc)
                    result.record_error(f"{cls.friendly_token}: {exc}")

        return result

    async def _push_tags(
        self,
        client: httpx.AsyncClient,
        token: str,
        new_tags: list[str],
        *,
        api_user: str | None = None,
    ) -> bool:
        """Add tags to a CMS media item via the bulk_actions ``add_tags`` action.

        The ``PUT /media/{token}`` endpoint only accepts title/description/media_file
        and silently ignores a ``tags`` field, so tag changes must go through
        ``POST /media/user/bulk_actions`` with ``action: add_tags``. On stock
        MediaCMS that endpoint is owner-scoped, so unless ``manage_all`` is set we
        skip the push for media the API user does not own rather than flooding
        CMS with 400s.
        """
        detail_url = f"{self._base}/api/v1/media/{token}"
        try:
            resp = await client.get(detail_url)
            if resp.status_code != 200:
                return False
            data = resp.json()
            owner = data.get("user")
            if not self._manage_all and (api_user is None or owner != api_user):
                return False
            current = {
                (t.get("title") if isinstance(t, dict) else t)
                for t in (data.get("tags_info") or [])
            }
            to_add = [t for t in new_tags if t not in current]
            if not to_add:
                return False
            put = await client.post(
                f"{self._base}/api/v1/media/user/bulk_actions",
                json={
                    "action": "add_tags",
                    "media_ids": [token],
                    "tag_titles": to_add,
                },
            )
            return put.status_code in (200, 201)
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            return False

    async def _reverse_sync(self, client: httpx.AsyncClient, token: str) -> None:
        """Insert any CMS-only tags into the local catalog_tags table."""
        url = f"{self._base}/api/v1/media/{token}"
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return
            data = resp.json()
            for t in data.get("tags_info") or []:
                name = t.get("title") if isinstance(t, dict) else t
                if name:
                    await self._db.add_catalog_tag(token, str(name))
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("[tags] Failed reverse-sync for %s: %s", token, exc)
