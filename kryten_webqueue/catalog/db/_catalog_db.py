"""Catalog domain database connection for catalog.sqlite3."""

import logging
from ._base_domain import _DomainDB
from ._catalog import _CatalogMixin, HIDDEN_CATEGORY_NAMES, HIDDEN_TAG_NAMES
from ._people import _PeopleMixin
from ._enrichment import _EnrichmentMixin
from ._motd import _MOTDMixin
from .schemas.catalog_schema import CATALOG_MIGRATIONS

logger = logging.getLogger(__name__)


class _CatalogDB(_CatalogMixin, _PeopleMixin, _EnrichmentMixin, _MOTDMixin, _DomainDB):
    """Catalog domain database handling catalog metadata, categories, tags, people, studios, and enrichment."""

    def __init__(self, db_path: str):
        super().__init__(db_path, CATALOG_MIGRATIONS, domain_name="catalog")

    async def get_item(
        self, friendly_token: str, is_partitioned: bool = True
    ) -> dict | None:
        return await super().get_item(friendly_token, is_partitioned=is_partitioned)

    async def get_items_by_tokens(self, tokens: list[str]) -> list[dict]:
        """Fetch catalog rows for a list of friendly_tokens, chunked for parameter limits."""
        if not tokens:
            return []
        items: list[dict] = []
        for i in range(0, len(tokens), 500):
            chunk = tokens[i : i + 500]
            ph = ",".join("?" * len(chunk))
            sql = f"""
                SELECT friendly_token, title, duration_sec, cover_art_path,
                       cover_art_source, thumbnail_url, manifest_url
                FROM catalog
                WHERE friendly_token IN ({ph})
            """
            rows = await self._fetch_all(sql, chunk)
            items.extend(rows)
        return items

    async def get_hidden_category_and_tag_tokens(self) -> set[str]:
        """Fetch tokens associated with hidden categories and tags."""
        cat_ph = ",".join("?" * len(HIDDEN_CATEGORY_NAMES))
        tag_ph = ",".join("?" * len(HIDDEN_TAG_NAMES))
        sql = f"""
            SELECT cc.friendly_token FROM catalog_categories cc
            JOIN categories cat ON cc.category_id = cat.id
            WHERE cat.name IN ({cat_ph})
            UNION
            SELECT ct.friendly_token FROM catalog_tags ct
            JOIN tags t ON ct.tag_id = t.id
            WHERE t.name IN ({tag_ph})
        """
        rows = await self._fetch_all(sql, [*HIDDEN_CATEGORY_NAMES, *HIDDEN_TAG_NAMES])
        return {r["friendly_token"] for r in rows if r.get("friendly_token")}
