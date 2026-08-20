"""Step: identify — resolve each item to a stable TMDB id + IMDb tt#.

Accuracy-first waterfall (cheapest/most authoritative signals first, fuzzy last)
so the maximum number of items are identified with zero manual editing:

    admin tt# → scraped tt# → original-language title → English title → API search

On any authoritative or exact/high-confidence hit the resolved IMDb tt# is
auto-promoted to ``catalog.imdb_tt`` (guarded by the v23 unique index) so every
downstream step (art, meta) keys off a stable identity instead of re-matching
titles. Every promotion writes an ``item_edit_log`` row for audit.
"""

from __future__ import annotations

import logging
from datetime import datetime, UTC

from ...tmdb_index import TMDBLocalIndex, extract_imdb_tt
from ..classify import ItemClassification
from ..providers import TMDBProvider
from ..report import StepResult

logger = logging.getLogger(__name__)

_MOVIE_TYPES = frozenset({"movie", "hosted_movie", "riffed_movie"})
_PROMOTE_SOURCES = frozenset(
    {"scraped_url", "scraped_desc", "original_title", "english_title", "api_search"}
)


class IdentifyStep:
    def __init__(self, *, db, config):
        self._db = db
        self._tmdb = TMDBProvider(config.tmdb_api_key)
        self._index = TMDBLocalIndex(config.tmdb_index_path)

    async def close(self) -> None:
        await self._tmdb.close()
        await self._index.close()

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
                source, tmdb_id, imdb_id, reason = await self._identify(cls)

                if not dry_run:
                    fields: dict = {"last_identify_at": now, "identify_reason": reason}
                    if source:
                        fields["identify_source"] = source
                    if tmdb_id:
                        fields["tmdb_id"] = str(tmdb_id)
                    if imdb_id:
                        fields["imdb_id"] = imdb_id
                    await self._db.save_enrichment_state(cls.friendly_token, **fields)

                    if imdb_id and source in _PROMOTE_SOURCES and not cls.imdb_tt:
                        promoted = await self._promote(cls, imdb_id, source)
                        if not promoted:
                            await self._db.save_enrichment_state(
                                cls.friendly_token, identify_reason="ambiguous"
                            )

                if reason == "resolved":
                    result.changed += 1
                else:
                    result.skipped += 1
            except Exception as exc:  # noqa: BLE001 - per-item isolation
                logger.warning("[identify] %s error: %s", cls.friendly_token, exc)
                result.record_error(f"{cls.friendly_token}: {exc}")

        return result

    async def _identify(
        self, cls: ItemClassification
    ) -> tuple[str | None, int | None, str | None, str]:
        """Return ``(source, tmdb_id, imdb_id, reason)`` for a classification."""
        # Non-movie content isn't resolved to a movie tt# here.
        if cls.content_type not in _MOVIE_TYPES:
            return (None, None, None, "non_movie")

        # 1. Admin-set tt# — already authoritative.
        if cls.imdb_tt:
            return ("admin", None, cls.imdb_tt, "resolved")

        # 2. Scraped tt# from the item's own text (primary YouTube-rip fix).
        tt = extract_imdb_tt(cls.source_url, cls.description, cls.raw_title)
        if tt:
            source = (
                "scraped_url"
                if extract_imdb_tt(cls.source_url) == tt
                else "scraped_desc"
            )
            meta = await self._tmdb.search_by_imdb_id(tt)
            tmdb_id = int(meta.tmdb_id) if meta.tmdb_id else None
            return (source, tmdb_id, tt, "resolved")

        # 3. Original-language title (highest-yield offline signal when present).
        # 4. English title — both via the local resolver.
        low_hit = await self._index.resolve(cls.lookup_title, cls.lookup_year)
        if low_hit and low_hit.confidence in ("exact", "high"):
            src = (
                "original_title"
                if low_hit.matched_on == "original_title"
                else "english_title"
            )
            meta = await self._tmdb.fetch_by_tmdb_id(low_hit.tmdb_id)
            imdb_id = meta.imdb_id or None
            return (src, low_hit.tmdb_id, imdb_id, "resolved")

        # 5. API fuzzy fallback.
        meta = await self._tmdb.search_movie(cls.lookup_title, cls.lookup_year)
        if meta.found and (meta.tmdb_id or meta.imdb_id):
            tmdb_id = int(meta.tmdb_id) if meta.tmdb_id else None
            return ("api_search", tmdb_id, meta.imdb_id or None, "resolved")

        if low_hit:  # matched locally but only at low confidence — do not promote
            return (None, low_hit.tmdb_id, None, "low_confidence")
        return (None, None, None, "no_local_match")

    async def _promote(
        self, cls: ItemClassification, imdb_id: str, source: str
    ) -> bool:
        """Write ``catalog.imdb_tt`` if free; return False on a collision."""
        existing = await self._db.get_item_by_imdb_tt(imdb_id)
        if existing and existing.get("friendly_token") != cls.friendly_token:
            logger.warning(
                "[identify] %s: tt %s already owned by %s — not promoting",
                cls.friendly_token,
                imdb_id,
                existing.get("friendly_token"),
            )
            return False
        await self._db.set_imdb_tt(cls.friendly_token, imdb_id)
        await self._db.log_item_edit(
            cls.friendly_token, f"identify:{source}", "imdb_tt", None, imdb_id
        )
        logger.info(
            "[identify] %s: promoted imdb_tt=%s (source=%s)",
            cls.friendly_token,
            imdb_id,
            source,
        )
        return True
