# Categories step — deferred (see SPEC_CATALOG_ENRICHMENT_PIPELINE.md §6.7)

from ..report import StepResult


class CategoriesStep:
    def __init__(self, *, db, config):
        pass

    async def run(
        self, *, classifications, dry_run=False, force=False, ctx=None
    ) -> StepResult:
        return StepResult(skipped=len(classifications))
