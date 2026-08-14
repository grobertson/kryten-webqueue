"""People (cast/crew) and studio facet methods — M-to-many, same model as tags."""

# Valid role values stored in catalog_people.role.
VALID_ROLES = frozenset({"cast", "director", "producer", "writer"})


class _PeopleMixin:
    """Cast, crew, and studio facet CRUD methods."""

    # --- People ---

    async def upsert_person(self, name: str) -> int:
        existing = await self._fetch_one("SELECT id FROM people WHERE name = ?", [name])
        if existing:
            return existing["id"]
        cursor = await self._db.execute("INSERT INTO people (name) VALUES (?)", [name])
        await self._db.commit()
        return cursor.lastrowid

    async def set_catalog_people(self, friendly_token: str, people: list[dict]) -> None:
        """Replace all cast/crew entries for an item.

        Each dict in *people* must have ``name`` (str), ``role`` (str — one of
        cast/director/producer/writer), and optionally ``position`` (int, default 0
        for ordering within a role group).
        """
        await self._db.execute(
            "DELETE FROM catalog_people WHERE friendly_token = ?", [friendly_token]
        )
        for entry in people:
            role = entry.get("role", "cast")
            if role not in VALID_ROLES:
                continue
            person_id = await self.upsert_person(entry["name"])
            await self._db.execute(
                "INSERT OR IGNORE INTO catalog_people "
                "(friendly_token, person_id, role, position) VALUES (?, ?, ?, ?)",
                [friendly_token, person_id, role, entry.get("position", 0)],
            )
        await self._db.commit()

    async def get_item_people(self, friendly_token: str) -> dict[str, list[str]]:
        """Return people grouped by role, ordered by position then name."""
        rows = await self._fetch_all(
            "SELECT p.name, cp.role FROM people p "
            "JOIN catalog_people cp ON cp.person_id = p.id "
            "WHERE cp.friendly_token = ? "
            "ORDER BY cp.role, cp.position, p.name",
            [friendly_token],
        )
        result: dict[str, list[str]] = {r: [] for r in VALID_ROLES}
        for row in rows:
            result[row["role"]].append(row["name"])
        return result

    # --- Studios ---

    async def upsert_studio(self, name: str) -> int:
        existing = await self._fetch_one(
            "SELECT id FROM studios WHERE name = ?", [name]
        )
        if existing:
            return existing["id"]
        cursor = await self._db.execute("INSERT INTO studios (name) VALUES (?)", [name])
        await self._db.commit()
        return cursor.lastrowid

    async def set_catalog_studios(
        self, friendly_token: str, studio_names: list[str]
    ) -> None:
        """Replace all studio associations for an item."""
        await self._db.execute(
            "DELETE FROM catalog_studios WHERE friendly_token = ?", [friendly_token]
        )
        for name in studio_names:
            studio_id = await self.upsert_studio(name)
            await self._db.execute(
                "INSERT OR IGNORE INTO catalog_studios (friendly_token, studio_id) VALUES (?, ?)",
                [friendly_token, studio_id],
            )
        await self._db.commit()

    async def get_item_studios(self, friendly_token: str) -> list[str]:
        rows = await self._fetch_all(
            "SELECT s.name FROM studios s "
            "JOIN catalog_studios cs ON cs.studio_id = s.id "
            "WHERE cs.friendly_token = ? ORDER BY s.name",
            [friendly_token],
        )
        return [r["name"] for r in rows]
