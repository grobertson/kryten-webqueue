"""Step: title — normalise and reformat CMS titles per content type."""

import json
import logging
from datetime import datetime, UTC

import httpx

from ..classify import ItemClassification
from ..normalise import normalize_movie_title
from ..report import StepResult

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_MPAA = frozenset({"G", "PG", "PG-13", "R", "NC-17", "NR", "TV-MA", "TV-14", "TV-PG"})


class TitleStep:
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
                    new_title = self._compute_title(cls)
                    if new_title is None or new_title == cls.raw_title:
                        result.skipped += 1
                        await self._db.save_enrichment_state(
                            cls.friendly_token, last_title_at=now
                        )
                        continue

                    logger.info(
                        "[title] %s: %r → %r",
                        cls.friendly_token,
                        cls.raw_title,
                        new_title,
                    )
                    if not dry_run:
                        ok = await self._push_title(
                            client, cls.friendly_token, new_title
                        )
                        if ok:
                            await self._db.update_catalog(
                                cls.friendly_token, {"title": new_title}
                            )
                            result.changed += 1
                        else:
                            result.record_error(
                                f"{cls.friendly_token}: CMS write failed"
                            )
                    else:
                        result.changed += 1

                    await self._db.save_enrichment_state(
                        cls.friendly_token, last_title_at=now
                    )
                except Exception as exc:
                    logger.warning("[title] %s error: %s", cls.friendly_token, exc)
                    result.record_error(f"{cls.friendly_token}: {exc}")

        return result

    @staticmethod
    def _compute_title(cls: ItemClassification) -> str | None:
        """Return the desired CMS title, or None if no change needed."""
        ct = cls.content_type
        if ct == "movie":
            # Display title keeps sub markers (dubbed is stripped); mirrors the
            # search cleanup so pipe/YouTube titles are tidied consistently.
            clean, year = normalize_movie_title(cls.raw_title, for_search=False)
            if year:
                candidate = f"{clean} ({year})"
            else:
                candidate = clean
            return candidate if candidate != cls.raw_title else None
        if ct == "hosted_movie" and cls.hosted:
            # Format as "Movie Title (YYYY) - Show Name"
            t = cls.hosted.movie_title
            y = cls.hosted.movie_year
            show = cls.hosted.show_name
            if y:
                candidate = f"{t} ({y}) - {show}"
            else:
                candidate = f"{t} - {show}"
            return candidate if candidate != cls.raw_title else None
        if ct == "riffed_movie" and cls.hosted:
            if "{title}" in (cls.hosted.show_name or ""):
                return None  # template handled below
            # Only Rifftrax gets reformat; MST3K keeps original
            if cls.hosted.show_name == "RiffTrax":
                t = cls.hosted.movie_title
                y = cls.hosted.movie_year
                return (
                    f"RiffTrax Presents: {t} ({y})" if y else f"RiffTrax Presents: {t}"
                )
        # archive / unknown / tv_episode: no change
        return None

    async def _push_title(
        self, client: httpx.AsyncClient, token: str, title: str
    ) -> bool:
        url = f"{self._base}/api/v1/media/{token}"
        try:
            # read current owner before PUT (MediaCMS reassigns on PUT)
            resp = await client.get(url)
            owner = resp.json().get("user") if resp.status_code == 200 else None
            put = await client.put(url, data={"title": title})
            ok = put.status_code in (200, 201)
            if ok and owner:
                await client.post(
                    f"{self._base}/api/v1/media/user/bulk_actions",
                    json={
                        "action": "change_owner",
                        "media_ids": [token],
                        "owner": owner,
                    },
                )
            return ok
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            return False
