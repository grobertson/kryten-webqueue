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
    "G": "mpaa-g",
    "PG": "mpaa-pg",
    "PG-13": "mpaa-pg13",
    "R": "mpaa-r",
    "NC-17": "mpaa-nc17",
    "NR": "mpaa-nr",
    "TV-MA": "mpaa-tvma",
    "TV-14": "mpaa-tv14",
    "TV-PG": "mpaa-tvpg",
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
                        changed = await self._push_tags(
                            client, cls.friendly_token, desired
                        )
                        # Reverse sync: pull CMS tags back to local DB
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
        self, client: httpx.AsyncClient, token: str, new_tags: list[str]
    ) -> bool:
        url = f"{self._base}/api/v1/media/{token}"
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return False
            data = resp.json()
            owner = data.get("user")
            current = {
                (t.get("title") if isinstance(t, dict) else t)
                for t in (data.get("tags_info") or [])
            }
            to_add = [t for t in new_tags if t not in current]
            if not to_add:
                return False
            merged = list(current | set(to_add))
            put = await client.put(url, data={"tags": ",".join(merged)})
            if put.status_code in (200, 201) and owner:
                await client.post(
                    f"{self._base}/api/v1/media/user/bulk_actions",
                    json={"action": "change_owner", "media_ids": [token], "owner": owner},
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
