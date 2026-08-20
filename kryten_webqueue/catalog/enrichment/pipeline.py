"""CatalogEnrichmentPipeline — unified orchestrator for all enrichment steps."""

from __future__ import annotations

import logging
import time
from datetime import datetime, UTC

from .classify import classify_item, ItemClassification
from .report import EnrichmentReport, StepResult
from .steps import SyncStep, TitleStep, MetaStep, ArtStep, TagsStep, CategoriesStep

logger = logging.getLogger(__name__)

ALL_STEPS = ["sync", "classify", "title", "meta", "art", "tags", "categories"]


class CatalogEnrichmentPipeline:
    """Single entry point for all catalog enrichment operations.

    Each step can run independently or as part of a full sequential run.
    Classification is always run (in memory) before any downstream step so that
    lookup_title / hosted context is available; it is cheap (no network).
    """

    def __init__(self, *, db, config, cover_art=None):
        self._db = db
        self._config = config
        self._cover_art = cover_art

    async def run(
        self,
        *,
        steps: list[str] | None = None,
        tokens: list[str] | None = None,
        force: bool = False,
        dry_run: bool = False,
        limit: int | None = None,
        min_score: int = 50,
        ctx=None,
    ) -> EnrichmentReport:
        steps_to_run = steps if steps is not None else ALL_STEPS
        # Validate
        unknown = [s for s in steps_to_run if s not in ALL_STEPS]
        if unknown:
            raise ValueError(f"Unknown steps: {unknown}")

        logger.info(
            "[enrichment] Starting pipeline: steps=%s tokens=%s force=%s dry_run=%s limit=%s",
            steps_to_run,
            "specific" if tokens else "all",
            force,
            dry_run,
            limit,
        )

        start = time.monotonic()
        by_step: dict[str, StepResult] = {}
        total_items = 0

        for step_name in steps_to_run:
            if step_name == "sync":
                sync = SyncStep(
                    db=self._db, config=self._config, cover_art=self._cover_art
                )
                try:
                    r = await sync.run(dry_run=dry_run, ctx=ctx)
                    by_step["sync"] = r
                finally:
                    await sync.close()
                await self._report_progress(ctx, "sync", r)
                continue

            # For all non-sync steps: load catalog rows + classify
            rows = await self._db.get_catalog_for_enrichment(
                step=step_name, tokens=tokens, force=force, limit=limit
            )
            logger.info(
                "[enrichment] step=%s found %d item(s) (force=%s)",
                step_name,
                len(rows),
                force,
            )
            if not rows:
                by_step[step_name] = StepResult(skipped=0)
                continue

            classifications = [self._classify_row(row, force=force) for row in rows]
            total_items = max(total_items, len(classifications))

            if step_name == "classify":
                r = await self._run_classify(
                    classifications, force=force, dry_run=dry_run
                )
            elif step_name == "title":
                step = TitleStep(db=self._db, config=self._config)
                r = await step.run(
                    classifications=classifications,
                    dry_run=dry_run,
                    force=force,
                    ctx=ctx,
                )
            elif step_name == "meta":
                step_obj = MetaStep(
                    db=self._db, config=self._config, min_score=min_score
                )
                try:
                    r = await step_obj.run(
                        classifications=classifications,
                        dry_run=dry_run,
                        force=force,
                        ctx=ctx,
                    )
                finally:
                    await step_obj.close()
            elif step_name == "art":
                step_obj = ArtStep(db=self._db, config=self._config)
                try:
                    r = await step_obj.run(
                        classifications=classifications,
                        dry_run=dry_run,
                        force=force,
                        ctx=ctx,
                    )
                finally:
                    await step_obj.close()
            elif step_name == "tags":
                tags_step = TagsStep(db=self._db, config=self._config)
                r = await tags_step.run(
                    classifications=classifications,
                    dry_run=dry_run,
                    force=force,
                    ctx=ctx,
                )
            elif step_name == "categories":
                cats = CategoriesStep(db=self._db, config=self._config)
                r = await cats.run(
                    classifications=classifications,
                    dry_run=dry_run,
                    force=force,
                    ctx=ctx,
                )
            else:
                r = StepResult()

            by_step[step_name] = r
            logger.info(
                "[enrichment] step=%s complete: processed=%d changed=%d skipped=%d errors=%d",
                step_name,
                r.processed,
                r.changed,
                r.skipped,
                len(r.errors),
            )
            await self._report_progress(ctx, step_name, r)

        elapsed = time.monotonic() - start
        logger.info(
            "[enrichment] Pipeline complete in %.1fs: %d item(s), %d step(s)",
            elapsed,
            total_items,
            len(steps_to_run),
        )

        return EnrichmentReport(
            steps_run=steps_to_run,
            total_items=total_items,
            by_step=by_step,
            elapsed_sec=elapsed,
            dry_run=dry_run,
        )

    def _classify_row(self, row: dict, *, force: bool = False) -> ItemClassification:
        """Classify a DB row, using cached enrichment state when available.

        ``force`` bypasses the cache and re-derives the classification from the
        raw title so classify-logic improvements apply to already-classified
        items.
        """
        token = row["friendly_token"]
        # If enrichment state already has lookup_title from a prior classify run,
        # we reconstruct the classification from that cached data.
        if not force and row.get("content_type") and row.get("lookup_title"):
            from .classify import ItemClassification, HostedInfo, HOSTED_SHOW_REGISTRY

            hosted: HostedInfo | None = None
            if row.get("hosted_show"):
                # Reconstruct HostedInfo from stored show name
                for entry in HOSTED_SHOW_REGISTRY:
                    if entry.show_name == row["hosted_show"]:
                        hosted = HostedInfo(
                            show_name=entry.show_name,
                            movie_title=row["lookup_title"],
                            movie_year=row.get("lookup_year"),
                            cms_tag=entry.cms_tag,
                        )
                        break
                if not hosted:
                    hosted = HostedInfo(
                        show_name=row["hosted_show"],
                        movie_title=row["lookup_title"],
                        movie_year=row.get("lookup_year"),
                        cms_tag="",
                    )
            return ItemClassification(
                friendly_token=token,
                raw_title=row["title"],
                content_type=row["content_type"],
                hosted=hosted,
                lookup_title=row["lookup_title"],
                lookup_year=row.get("lookup_year"),
                tv_show=row.get("tv_show"),
                tv_season=row.get("tv_season"),
                tv_episode=row.get("tv_episode_num"),
                duration_sec=row.get("duration_sec", 0),
                description_score=row.get("description_score") or 0,
                has_real_art=(row.get("cover_art_source") or "") in ("tmdb", "omdb"),
                imdb_tt=row.get("imdb_tt") or None,
            )
        # Fresh classification
        return classify_item(
            token,
            row["title"],
            row.get("duration_sec", 0),
            cover_art_source=row.get("cover_art_source"),
            description=row.get("description"),
            description_score=0,
            imdb_tt=row.get("imdb_tt") or None,
        )

    async def _run_classify(
        self,
        classifications: list[ItemClassification],
        *,
        force: bool,
        dry_run: bool,
    ) -> StepResult:
        result = StepResult()
        now = datetime.now(UTC).isoformat()
        for cls in classifications:
            result.processed += 1
            if not dry_run:
                await self._db.save_enrichment_state(
                    cls.friendly_token,
                    content_type=cls.content_type,
                    hosted_show=cls.hosted.show_name if cls.hosted else None,
                    lookup_title=cls.lookup_title,
                    lookup_year=cls.lookup_year,
                    tv_show=cls.tv_show,
                    tv_season=cls.tv_season,
                    tv_episode_num=cls.tv_episode,
                    description_score=cls.description_score,
                    last_classify_at=now,
                )
            result.changed += 1
        return result

    @staticmethod
    async def _report_progress(ctx, step: str, result: StepResult) -> None:
        if ctx is None:
            return
        try:
            await ctx.progress(
                {
                    "step": step,
                    "processed": result.processed,
                    "changed": result.changed,
                    "skipped": result.skipped,
                    "failed": result.failed,
                }
            )
        except Exception:
            pass
