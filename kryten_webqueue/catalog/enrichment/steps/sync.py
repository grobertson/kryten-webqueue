"""Step: sync — thin wrapper around CatalogSync that seeds enrichment state."""

import logging

from ...sync import CatalogSync
from ..report import StepResult

logger = logging.getLogger(__name__)


class SyncStep:
    def __init__(self, *, db, config, cover_art=None):
        self._db = db
        self._sync = CatalogSync(
            mediacms_url=config.mediacms_url,
            mediacms_token=config.mediacms_token,
            db=db,
            cover_art=cover_art,
        )

    async def close(self) -> None:
        await self._sync.close()

    async def run(self, *, dry_run: bool = False, ctx=None) -> StepResult:
        result = StepResult()
        if dry_run:
            logger.info("[sync] dry_run — skipping CMS pull")
            return result
        try:
            await self._sync.sync()
            result.processed += 1
        except Exception as exc:
            logger.exception("[sync] sync failed: %s", exc)
            result.record_error(str(exc))
        return result
