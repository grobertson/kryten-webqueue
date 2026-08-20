"""Enrichment pipeline state — per-item caching of classification + metadata."""

import json


class _EnrichmentMixin:
    """item_enrichment_state CRUD methods."""

    async def ensure_enrichment_state(self, token: str) -> None:
        """Create a skeleton enrichment-state row if none exists."""
        await self._db.execute(
            "INSERT OR IGNORE INTO item_enrichment_state (friendly_token) VALUES (?)",
            [token],
        )
        await self._db.commit()

    async def get_enrichment_state(self, token: str) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM item_enrichment_state WHERE friendly_token = ?", [token]
        )

    async def save_enrichment_state(self, token: str, **fields) -> None:
        """Upsert enrichment state fields for a token.

        Safely handles any subset of columns; unknown keys are silently ignored.
        """
        if not fields:
            return
        allowed = {
            "content_type",
            "hosted_show",
            "lookup_title",
            "lookup_year",
            "tv_show",
            "tv_season",
            "tv_episode_num",
            "description_score",
            "tmdb_id",
            "imdb_id",
            "meta_json",
            "last_classify_at",
            "last_title_at",
            "last_meta_at",
            "last_art_at",
            "last_tags_at",
            "last_categories_at",
        }
        safe = {k: v for k, v in fields.items() if k in allowed}
        if not safe:
            return
        await self.ensure_enrichment_state(token)
        sets = ", ".join(f"{k} = ?" for k in safe)
        await self._execute(
            f"UPDATE item_enrichment_state SET {sets} WHERE friendly_token = ?",
            [*safe.values(), token],
        )

    async def update_enrichment_state(self, token: str, fields: dict) -> None:
        """Update specific enrichment state fields for an item.

        Unlike save_enrichment_state (which is for the enrichment pipeline),
        this is for manual admin edits and only accepts a subset of fields.
        """
        if not fields:
            return
        allowed = {
            "content_type",
            "hosted_show",
            "lookup_title",
            "lookup_year",
            "tv_show",
            "tv_season",
            "tv_episode_num",
        }
        safe = {k: v for k, v in fields.items() if k in allowed}
        if not safe:
            return
        await self.ensure_enrichment_state(token)
        sets = ", ".join(f"{k} = ?" for k in safe)
        await self._execute(
            f"UPDATE item_enrichment_state SET {sets} WHERE friendly_token = ?",
            [*safe.values(), token],
        )

    async def get_catalog_for_enrichment(
        self,
        *,
        step: str,
        tokens: list[str] | None = None,
        force: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        """Return catalog rows joined with enrichment state, filtered to items that
        need the given step.

        In normal mode an item needs a step when ``last_{step}_at IS NULL``.
        ``force=True`` returns all items regardless.
        """
        step_col = f"last_{step}_at"
        sql = """
            SELECT c.friendly_token, c.title, c.duration_sec,
                   c.cover_art_path, c.cover_art_source, c.thumbnail_url,
                   c.description, c.manifest_url, c.imdb_tt,
                   e.content_type, e.hosted_show, e.lookup_title, e.lookup_year,
                   e.tv_show, e.tv_season, e.tv_episode_num,
                   e.description_score, e.tmdb_id, e.imdb_id, e.meta_json,
                   e.last_classify_at, e.last_title_at, e.last_meta_at,
                   e.last_art_at, e.last_tags_at, e.last_categories_at
            FROM catalog c
            LEFT JOIN item_enrichment_state e ON e.friendly_token = c.friendly_token
        """
        params: list = []
        conditions: list[str] = []

        if tokens:
            ph = ",".join("?" * len(tokens))
            conditions.append(f"c.friendly_token IN ({ph})")
            params.extend(tokens)

        if not force:
            conditions.append(f"(e.{step_col} IS NULL)")

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        sql += " ORDER BY c.title"

        if limit:
            sql += " LIMIT ?"
            params.append(limit)

        return await self._fetch_all(sql, params)

    def parse_meta_json(self, row: dict) -> dict | None:
        """Deserialise the cached meta_json field from an enrichment row."""
        raw = (row or {}).get("meta_json")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            import logging

            logger = logging.getLogger(__name__)
            token = (row or {}).get("friendly_token", "?")
            logger.warning(
                "Failed to parse meta_json for %s: %s. Raw value (first 200 chars): %r",
                token,
                exc,
                raw[:200] if isinstance(raw, str) else raw,
            )
            return None
