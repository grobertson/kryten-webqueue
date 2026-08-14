"""Step: meta — TMDB+OMDB lookup → structured description → CMS + people/studios."""

from __future__ import annotations

import json
import logging
from datetime import datetime, UTC

import httpx

from ..classify import ItemClassification, HostedInfo
from ..providers import TMDBProvider, OMDBProvider, MovieMetadata, merge_metadata
from ..report import StepResult

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_QUALITY_MARKERS = [
    "Cast & Crew:",
    "Director(s):",
    "Cast:",
    "Genres:",
    "Content Rating:",
    "Release Year:",
    "Synopsis:",
    "Ratings:",
    "Original URL:",
    "Hosted Version:",
]


def score_description(desc: str) -> int:
    desc = desc or ""
    score = sum(10 for m in _QUALITY_MARKERS if m in desc)
    score += min(30, len(desc) // 100)
    return score


def format_description(meta: MovieMetadata, hosted: HostedInfo | None) -> str:
    lines: list[str] = []
    if hosted:
        lines += [f"Hosted Version: {hosted.show_name}", ""]
    if meta.synopsis:
        lines += ["Synopsis:", meta.synopsis, ""]
    if meta.tagline:
        lines += [f"Tagline: {meta.tagline}", ""]
    if meta.year:
        lines.append(f"Release Year: {meta.year}")
    if meta.content_rating and meta.content_rating not in ("N/A", ""):
        lines.append(f"Content Rating: {meta.content_rating}")
    if meta.runtime_min:
        lines.append(f"Runtime: {meta.runtime_min} min")
    if meta.year or meta.content_rating or meta.runtime_min:
        lines.append("")
    if meta.genres:
        lines += [f"Genres: {', '.join(meta.genres)}", ""]
    # Ratings
    rparts = []
    if meta.imdb_rating and meta.imdb_rating != "N/A":
        r = f"IMDb: {meta.imdb_rating}"
        if meta.imdb_id:
            r += f" (https://www.imdb.com/title/{meta.imdb_id}/)"
        rparts.append(r)
    if meta.rotten_tomatoes:
        rparts.append(f"Rotten Tomatoes: {meta.rotten_tomatoes}")
    if meta.metacritic:
        rparts.append(f"Metacritic: {meta.metacritic}")
    if meta.tmdb_rating:
        rparts.append(f"TMDb: {meta.tmdb_rating}")
    if rparts:
        lines += ["Ratings:"] + [f"  {r}" for r in rparts] + [""]
    # Cast & Crew
    has_crew = any(
        [
            meta.director,
            meta.cast,
            meta.producer,
            meta.writer,
            meta.cinematographer,
            meta.composer,
            meta.editor,
        ]
    )
    if has_crew:
        lines.append("Cast & Crew:")
        if meta.director:
            lines.append(f"  Director(s): {', '.join(meta.director)}")
        if meta.writer:
            lines.append(f"  Writer(s): {', '.join(meta.writer)}")
        if meta.producer:
            lines.append(f"  Producer(s): {', '.join(meta.producer)}")
        if meta.cinematographer:
            lines.append(f"  Cinematography: {', '.join(meta.cinematographer)}")
        if meta.composer:
            lines.append(f"  Music: {', '.join(meta.composer)}")
        if meta.cast:
            lines.append(f"  Cast: {', '.join(meta.cast)}")
        lines.append("")
    return "\n".join(lines).strip()


class MetaStep:
    def __init__(self, *, db, config, min_score: int = 50):
        self._db = db
        self._min_score = min_score
        self._base = (
            config.mediacms_url.rstrip("/").removesuffix("/api/v1").removesuffix("/api")
        )
        self._cms_headers = {"Authorization": f"Token {config.mediacms_token}"}
        self._tmdb = TMDBProvider(config.tmdb_api_key)
        self._omdb = OMDBProvider(config.omdb_api_key)
        # TV show lookup cache: show_name → MovieMetadata skeleton for the series
        self._tv_cache: dict[str, int | None] = {}

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
                # Skip if already well-described (unless force)
                if not force and cls.description_score >= self._min_score:
                    result.skipped += 1
                    continue
                if not cls.lookup_title:
                    result.skipped += 1
                    continue

                if cls.content_type == "tv_episode":
                    meta = await self._lookup_tv(cls)
                else:
                    meta = await self._lookup_movie(cls)

                if not meta.found:
                    result.skipped += 1
                    logger.debug(
                        "[meta] MISS %s %r", cls.friendly_token, cls.lookup_title
                    )
                    continue

                desc = format_description(meta, cls.hosted)
                new_score = score_description(desc)
                meta_json = json.dumps(self._meta_to_dict(meta))

                logger.info(
                    "[meta] %s: %r → score %d",
                    cls.friendly_token,
                    meta.title,
                    new_score,
                )

                if not dry_run:
                    await self._push_description(cls.friendly_token, desc)
                    # Write people + studios to local DB
                    await self._write_people(cls.friendly_token, meta)
                    await self._db.save_enrichment_state(
                        cls.friendly_token,
                        tmdb_id=meta.tmdb_id,
                        imdb_id=meta.imdb_id,
                        meta_json=meta_json,
                        description_score=new_score,
                        last_meta_at=now,
                    )
                    await self._db.update_catalog(
                        cls.friendly_token,
                        {"description": desc},
                    )

                result.changed += 1

            except Exception as exc:
                logger.warning("[meta] %s error: %s", cls.friendly_token, exc)
                result.record_error(f"{cls.friendly_token}: {exc}")

        return result

    async def _lookup_movie(self, cls: ItemClassification) -> MovieMetadata:
        tmdb = await self._tmdb.search_movie(cls.lookup_title, cls.lookup_year)
        imdb_id = tmdb.imdb_id if tmdb.found else None
        omdb = await self._omdb.search_movie(
            cls.lookup_title, cls.lookup_year, imdb_id=imdb_id
        )
        meta = merge_metadata(tmdb, omdb)
        if not meta.genres and cls.genre_hints:
            meta.genres = cls.genre_hints
        return meta

    async def _lookup_tv(self, cls: ItemClassification) -> MovieMetadata:
        if not cls.tv_season or not cls.tv_episode:
            return MovieMetadata()
        show = cls.tv_show or cls.lookup_title
        tmdb = await self._tmdb.search_tv_episode(show, cls.tv_season, cls.tv_episode)
        return tmdb

    async def _push_description(self, token: str, desc: str) -> None:
        url = f"{self._base}/api/v1/media/{token}"
        async with httpx.AsyncClient(
            headers=self._cms_headers, timeout=_TIMEOUT
        ) as client:
            resp = await client.get(url)
            owner = resp.json().get("user") if resp.status_code == 200 else None
            await client.put(url, data={"description": desc})
            if owner:
                await client.post(
                    f"{self._base}/api/v1/media/user/bulk_actions",
                    json={
                        "action": "change_owner",
                        "media_ids": [token],
                        "owner": owner,
                    },
                )

    async def _write_people(self, token: str, meta: MovieMetadata) -> None:
        people: list[dict] = []
        for i, name in enumerate(meta.director):
            people.append({"name": name, "role": "director", "position": i})
        for i, name in enumerate(meta.cast):
            people.append({"name": name, "role": "cast", "position": i})
        for i, name in enumerate(meta.producer):
            people.append({"name": name, "role": "producer", "position": i})
        for i, name in enumerate(meta.writer):
            people.append({"name": name, "role": "writer", "position": i})
        if people:
            await self._db.set_catalog_people(token, people)
        if meta.studio:
            await self._db.set_catalog_studios(token, meta.studio)

    @staticmethod
    def _meta_to_dict(meta: MovieMetadata) -> dict:
        return {
            "title": meta.title,
            "year": meta.year,
            "synopsis": meta.synopsis,
            "director": meta.director,
            "producer": meta.producer,
            "cast": meta.cast,
            "genres": meta.genres,
            "content_rating": meta.content_rating,
            "runtime_min": meta.runtime_min,
            "tagline": meta.tagline,
            "imdb_rating": meta.imdb_rating,
            "imdb_id": meta.imdb_id,
            "tmdb_id": meta.tmdb_id,
            "rotten_tomatoes": meta.rotten_tomatoes,
            "metacritic": meta.metacritic,
            "tmdb_rating": meta.tmdb_rating,
            "writer": meta.writer,
            "studio": meta.studio,
            "poster_url": meta.poster_url,
        }
