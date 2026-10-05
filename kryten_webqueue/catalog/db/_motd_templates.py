"""MOTD templates, schedules, publications, media library, and admin audit CRUD.

One mixin serves all three storage layouts. SQLite (monolith and partitioned
catalog domain) talks to ``self._db`` directly; PostgreSQL overrides the four
``_mt_*`` primitives. Timestamps are bound as fixed-width UTC ISO strings on
SQLite and native ``datetime`` on PostgreSQL, and every row is normalized back
to aware datetimes / bools / parsed JSON so callers never see driver types.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

UTC = datetime.timezone.utc

_TS_COLUMNS = {
    "archived_at",
    "created_at",
    "updated_at",
    "starts_at",
    "ends_at",
    "published_at",
    "uploaded_at",
    "deleted_at",
    "at",
}
_BOOL_COLUMNS = {"is_default", "is_active", "animated"}
_JSON_COLUMNS = {"zones", "fragment_revisions", "before", "after"}


class MOTDTemplateConflict(Exception):
    """The template changed since the editor loaded it."""


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def to_utc(value: Any) -> datetime.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _normalize(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key, value in out.items():
        if key in _TS_COLUMNS:
            out[key] = to_utc(value)
        elif key in _BOOL_COLUMNS and value is not None:
            out[key] = bool(value)
        elif key in _JSON_COLUMNS and isinstance(value, str):
            out[key] = json.loads(value) if value else None
    return out


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str, sort_keys=True)


class _MOTDTemplatesMixin:
    _mt_pg = False
    _db: Any

    # --- storage primitives -------------------------------------------------

    def _mt_ts(self, value: datetime.datetime | None):
        utc = to_utc(value)
        if utc is None:
            return None
        if self._mt_pg:
            return utc
        return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _mt_bool(self, value: bool):
        return bool(value) if self._mt_pg else int(bool(value))

    async def _mt_exec(self, sql: str, params: list) -> int:
        cursor = await self._db.execute(sql, params)
        await self._db.commit()
        return cursor.rowcount or 0

    async def _mt_insert(self, sql: str, params: list) -> int:
        cursor = await self._db.execute(sql, params)
        await self._db.commit()
        return int(cursor.lastrowid)

    async def _mt_one(self, sql: str, params: list | None = None) -> dict | None:
        cursor = await self._db.execute(sql, params or [])
        row = await cursor.fetchone()
        return _normalize(dict(row)) if row else None

    async def _mt_all(self, sql: str, params: list | None = None) -> list[dict]:
        cursor = await self._db.execute(sql, params or [])
        return [_normalize(dict(r)) for r in await cursor.fetchall()]  # type: ignore[misc]

    # --- templates ------------------------------------------------------------

    _TEMPLATE_SELECT = """
        SELECT t.id, t.name, t.kind, t.display_name, t.description,
               t.current_revision_id, t.is_default, t.archived_at,
               t.created_by, t.created_at, t.updated_at,
               r.revision_no, r.body, r.body_sha256, r.zones,
               r.created_by AS revision_by, r.created_at AS revision_at, r.note
        FROM motd_templates t
        LEFT JOIN motd_template_revisions r ON r.id = t.current_revision_id
    """

    async def list_motd_templates(
        self, kind: str | None = None, include_archived: bool = False
    ) -> list[dict]:
        where, params = [], []
        if kind:
            where.append("t.kind = ?")
            params.append(kind)
        if not include_archived:
            where.append("t.archived_at IS NULL")
        sql = self._TEMPLATE_SELECT
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = await self._mt_all(sql + " ORDER BY t.kind, t.name", params)
        for row in rows:
            row["revision_at"] = to_utc(row.get("revision_at"))
        return rows

    async def get_motd_template(self, name: str) -> dict | None:
        row = await self._mt_one(self._TEMPLATE_SELECT + " WHERE t.name = ?", [name])
        if row:
            row["revision_at"] = to_utc(row.get("revision_at"))
        return row

    async def get_motd_template_by_id(self, template_id: int) -> dict | None:
        row = await self._mt_one(
            self._TEMPLATE_SELECT + " WHERE t.id = ?", [template_id]
        )
        if row:
            row["revision_at"] = to_utc(row.get("revision_at"))
        return row

    async def get_default_motd_template(self) -> dict | None:
        row = await self._mt_one(
            self._TEMPLATE_SELECT
            + " WHERE t.kind = 'master' AND t.is_default = ? AND t.archived_at IS NULL",
            [self._mt_bool(True)],
        )
        if row:
            row["revision_at"] = to_utc(row.get("revision_at"))
        return row

    async def count_motd_templates(self) -> int:
        row = await self._mt_one("SELECT COUNT(*) AS n FROM motd_templates")
        return int(row["n"]) if row else 0

    async def create_motd_template(
        self,
        *,
        name: str,
        kind: str,
        display_name: str,
        description: str | None,
        body: str,
        body_sha256: str,
        zones: list[str],
        note: str | None,
        created_by: str,
        is_default: bool = False,
    ) -> dict:
        now = utcnow()
        template_id = await self._mt_insert(
            """
            INSERT INTO motd_templates
                (name, kind, display_name, description, is_default,
                 created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                name,
                kind,
                display_name,
                description,
                self._mt_bool(is_default),
                created_by,
                self._mt_ts(now),
                self._mt_ts(now),
            ],
        )
        revision_id = await self._insert_revision(
            template_id, 1, body, body_sha256, zones, note, created_by, now
        )
        await self._mt_exec(
            "UPDATE motd_templates SET current_revision_id = ? WHERE id = ?",
            [revision_id, template_id],
        )
        created = await self.get_motd_template_by_id(template_id)
        assert created is not None
        return created

    async def _insert_revision(
        self,
        template_id: int,
        revision_no: int,
        body: str,
        body_sha256: str,
        zones: list[str],
        note: str | None,
        created_by: str,
        now: datetime.datetime,
    ) -> int:
        return await self._mt_insert(
            """
            INSERT INTO motd_template_revisions
                (template_id, revision_no, body, body_sha256, zones, note,
                 created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                template_id,
                revision_no,
                body,
                body_sha256,
                json.dumps(list(zones)),
                note,
                created_by,
                self._mt_ts(now),
            ],
        )

    async def save_motd_template_revision(
        self,
        template_id: int,
        *,
        body: str,
        body_sha256: str,
        zones: list[str],
        note: str | None,
        created_by: str,
        expected_revision_id: int | None,
    ) -> dict:
        """Append a revision and make it current, refusing stale editors."""
        current = await self.get_motd_template_by_id(template_id)
        if current is None:
            raise KeyError(template_id)
        if current["current_revision_id"] != expected_revision_id:
            raise MOTDTemplateConflict(
                "This template was saved by someone else since you opened it"
            )
        latest = await self._mt_one(
            "SELECT MAX(revision_no) AS n FROM motd_template_revisions WHERE template_id = ?",
            [template_id],
        )
        now = utcnow()
        try:
            revision_id = await self._insert_revision(
                template_id,
                int((latest or {}).get("n") or 0) + 1,
                body,
                body_sha256,
                zones,
                note,
                created_by,
                now,
            )
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise MOTDTemplateConflict(
                    "This template was saved by someone else since you opened it"
                ) from exc
            raise
        moved = await self._mt_exec(
            """
            UPDATE motd_templates SET current_revision_id = ?, updated_at = ?
            WHERE id = ? AND current_revision_id = ?
            """,
            [revision_id, self._mt_ts(now), template_id, expected_revision_id],
        )
        if not moved:
            await self._mt_exec(
                "DELETE FROM motd_template_revisions WHERE id = ?", [revision_id]
            )
            raise MOTDTemplateConflict(
                "This template was saved by someone else since you opened it"
            )
        saved = await self.get_motd_template_by_id(template_id)
        assert saved is not None
        return saved

    async def update_motd_template_meta(
        self,
        template_id: int,
        *,
        display_name: str | None = None,
        description: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if display_name is not None:
            sets.append("display_name = ?")
            params.append(display_name)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([self._mt_ts(utcnow()), template_id])
        await self._mt_exec(
            f"UPDATE motd_templates SET {', '.join(sets)} WHERE id = ?", params
        )

    async def set_default_motd_template(self, template_id: int) -> None:
        now = self._mt_ts(utcnow())
        await self._mt_exec(
            "UPDATE motd_templates SET is_default = ?, updated_at = ? WHERE is_default = ?",
            [self._mt_bool(False), now, self._mt_bool(True)],
        )
        await self._mt_exec(
            "UPDATE motd_templates SET is_default = ?, updated_at = ? WHERE id = ?",
            [self._mt_bool(True), now, template_id],
        )

    async def archive_motd_template(self, template_id: int) -> None:
        now = self._mt_ts(utcnow())
        await self._mt_exec(
            "UPDATE motd_templates SET archived_at = ?, updated_at = ? WHERE id = ?",
            [now, now, template_id],
        )

    async def list_motd_template_revisions(self, template_id: int) -> list[dict]:
        return await self._mt_all(
            """
            SELECT id, template_id, revision_no, body_sha256, zones, note,
                   created_by, created_at
            FROM motd_template_revisions WHERE template_id = ?
            ORDER BY revision_no DESC
            """,
            [template_id],
        )

    async def get_motd_template_revision(
        self, template_id: int, revision_no: int
    ) -> dict | None:
        return await self._mt_one(
            "SELECT * FROM motd_template_revisions WHERE template_id = ? AND revision_no = ?",
            [template_id, revision_no],
        )

    async def get_motd_revision(self, revision_id: int) -> dict | None:
        return await self._mt_one(
            "SELECT * FROM motd_template_revisions WHERE id = ?", [revision_id]
        )

    # --- schedules ------------------------------------------------------------

    _SCHEDULE_FIELDS = (
        "template_id",
        "zone",
        "label",
        "starts_at",
        "ends_at",
        "rrule",
        "duration_minutes",
        "tz",
        "priority",
        "is_active",
    )

    def _schedule_value(self, field: str, value: Any) -> Any:
        if field in ("starts_at", "ends_at"):
            return self._mt_ts(value)
        if field == "is_active":
            return self._mt_bool(value)
        return value

    async def list_motd_schedules(self, include_inactive: bool = True) -> list[dict]:
        sql = """
            SELECT s.*, t.name AS template_name, t.kind AS template_kind,
                   t.display_name AS template_display_name,
                   t.archived_at AS template_archived_at
            FROM motd_schedules s JOIN motd_templates t ON t.id = s.template_id
        """
        params: list = []
        if not include_inactive:
            sql += " WHERE s.is_active = ?"
            params.append(self._mt_bool(True))
        rows = await self._mt_all(sql + " ORDER BY s.starts_at, s.id", params)
        for row in rows:
            row["template_archived_at"] = to_utc(row.get("template_archived_at"))
        return rows

    async def get_motd_schedule(self, schedule_id: int) -> dict | None:
        return await self._mt_one(
            "SELECT * FROM motd_schedules WHERE id = ?", [schedule_id]
        )

    async def create_motd_schedule(self, *, created_by: str, **fields: Any) -> int:
        now = utcnow()
        columns = [f for f in self._SCHEDULE_FIELDS if f in fields]
        values = [self._schedule_value(f, fields[f]) for f in columns]
        columns += ["created_by", "created_at", "updated_at"]
        values += [created_by, self._mt_ts(now), self._mt_ts(now)]
        return await self._mt_insert(
            f"INSERT INTO motd_schedules ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )

    async def update_motd_schedule(self, schedule_id: int, **fields: Any) -> int:
        columns = [f for f in self._SCHEDULE_FIELDS if f in fields]
        if not columns:
            return 0
        values = [self._schedule_value(f, fields[f]) for f in columns]
        sets = ", ".join(f"{c} = ?" for c in columns) + ", updated_at = ?"
        return await self._mt_exec(
            f"UPDATE motd_schedules SET {sets} WHERE id = ?",
            [*values, self._mt_ts(utcnow()), schedule_id],
        )

    async def delete_motd_schedule(self, schedule_id: int) -> int:
        return await self._mt_exec(
            "DELETE FROM motd_schedules WHERE id = ?", [schedule_id]
        )

    async def count_live_motd_schedules_for_template(
        self, template_id: int, now: datetime.datetime
    ) -> int:
        """Active schedules that have not definitively ended."""
        row = await self._mt_one(
            """
            SELECT COUNT(*) AS n FROM motd_schedules
            WHERE template_id = ? AND is_active = ? AND (ends_at IS NULL OR ends_at > ?)
            """,
            [template_id, self._mt_bool(True), self._mt_ts(now)],
        )
        return int(row["n"]) if row else 0

    # --- publications ---------------------------------------------------------

    async def add_motd_publication(
        self,
        *,
        trigger: str,
        composition_key: str,
        master_revision_id: int,
        fragment_revisions: dict,
        week_key: str,
        html_sha256: str,
        html_chars: int,
        backup_path: str | None,
        job_run_id: int | None,
        published_by: str,
    ) -> int:
        return await self._mt_insert(
            """
            INSERT INTO motd_publications
                (published_at, trigger, composition_key, master_revision_id,
                 fragment_revisions, week_key, html_sha256, html_chars,
                 backup_path, job_run_id, published_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                self._mt_ts(utcnow()),
                trigger,
                composition_key,
                master_revision_id,
                _dump(fragment_revisions),
                week_key,
                html_sha256,
                html_chars,
                backup_path,
                job_run_id,
                published_by,
            ],
        )

    async def get_latest_motd_publication(self) -> dict | None:
        return await self._mt_one(
            "SELECT * FROM motd_publications ORDER BY published_at DESC, id DESC LIMIT 1"
        )

    async def list_motd_publications(self, limit: int = 50) -> list[dict]:
        return await self._mt_all(
            "SELECT * FROM motd_publications ORDER BY published_at DESC, id DESC LIMIT ?",
            [limit],
        )

    # --- media library --------------------------------------------------------

    async def create_media_asset(self, **fields: Any) -> int:
        columns = [
            "slug",
            "filename",
            "kind",
            "mime",
            "bytes",
            "width",
            "height",
            "animated",
            "frame_count",
            "sha256",
            "original_name",
            "description",
            "uploaded_by",
        ]
        values = [
            self._mt_bool(bool(fields.get(c))) if c == "animated" else fields.get(c)
            for c in columns
        ]
        columns.append("uploaded_at")
        values.append(self._mt_ts(utcnow()))
        return await self._mt_insert(
            f"INSERT INTO media_assets ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )

    async def get_media_asset(
        self, slug: str, include_deleted: bool = False
    ) -> dict | None:
        sql = "SELECT * FROM media_assets WHERE slug = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        return await self._mt_one(sql, [slug])

    async def media_slug_exists(self, slug: str) -> bool:
        """True if the slug was EVER used, so URLs are never recycled."""
        return (
            await self._mt_one("SELECT 1 AS x FROM media_assets WHERE slug = ?", [slug])
        ) is not None

    async def list_media_assets(
        self, q: str | None = None, limit: int = 60, offset: int = 0
    ) -> list[dict]:
        sql = "SELECT * FROM media_assets WHERE deleted_at IS NULL"
        params: list = []
        if q:
            sql += (
                " AND (LOWER(slug) LIKE ? OR LOWER(COALESCE(original_name, '')) LIKE ?"
                " OR LOWER(COALESCE(description, '')) LIKE ? OR LOWER(uploaded_by) LIKE ?)"
            )
            like = f"%{q.lower()}%"
            params += [like, like, like, like]
        sql += " ORDER BY uploaded_at DESC, id DESC LIMIT ? OFFSET ?"
        return await self._mt_all(sql, [*params, limit, offset])

    async def update_media_asset_description(
        self, slug: str, description: str | None
    ) -> int:
        return await self._mt_exec(
            "UPDATE media_assets SET description = ? WHERE slug = ? AND deleted_at IS NULL",
            [description, slug],
        )

    async def soft_delete_media_asset(self, slug: str, deleted_by: str) -> int:
        return await self._mt_exec(
            """
            UPDATE media_assets SET deleted_at = ?, deleted_by = ?
            WHERE slug = ? AND deleted_at IS NULL
            """,
            [self._mt_ts(utcnow()), deleted_by, slug],
        )

    # --- audit ----------------------------------------------------------------

    async def add_admin_audit(
        self,
        *,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str | int,
        summary: str | None = None,
        before: Any = None,
        after: Any = None,
    ) -> None:
        await self._mt_insert(
            """
            INSERT INTO admin_audit_log
                (at, actor, action, entity_type, entity_id, summary, before, after)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                self._mt_ts(utcnow()),
                actor,
                action,
                entity_type,
                str(entity_id),
                summary,
                _dump(before),
                _dump(after),
            ],
        )

    async def list_admin_audit(
        self,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        sql = "SELECT * FROM admin_audit_log"
        where, params = [], []
        if entity_type:
            where.append("entity_type = ?")
            params.append(entity_type)
        if entity_id:
            where.append("entity_id = ?")
            params.append(str(entity_id))
        if where:
            sql += " WHERE " + " AND ".join(where)
        return await self._mt_all(
            sql + " ORDER BY at DESC, id DESC LIMIT ?", [*params, limit]
        )

    # --- retention ------------------------------------------------------------

    async def prune_motd_history(self, cutoff: datetime.datetime) -> dict:
        """Drop audit rows and publications older than ``cutoff``.

        The newest publication always survives: reconcile compares against it.
        Returns the backup paths of removed publications for file cleanup.
        """
        latest = await self.get_latest_motd_publication()
        keep_id = latest["id"] if latest else -1
        stale = await self._mt_all(
            "SELECT id, backup_path FROM motd_publications WHERE published_at < ? AND id <> ?",
            [self._mt_ts(cutoff), keep_id],
        )
        publications = await self._mt_exec(
            "DELETE FROM motd_publications WHERE published_at < ? AND id <> ?",
            [self._mt_ts(cutoff), keep_id],
        )
        audit = await self._mt_exec(
            "DELETE FROM admin_audit_log WHERE at < ?", [self._mt_ts(cutoff)]
        )
        return {
            "publications": publications,
            "audit": audit,
            "backup_paths": [r["backup_path"] for r in stale if r.get("backup_path")],
            "kept_backup_path": latest.get("backup_path") if latest else None,
        }


class _PgMOTDTemplatesMixin(_MOTDTemplatesMixin):
    _mt_pg = True

    async def _mt_exec(self, sql: str, params: list) -> int:
        result = await self._execute(sql, params)  # type: ignore[attr-defined]
        return result.rowcount or 0

    async def _mt_insert(self, sql: str, params: list) -> int:
        return int(await self._execute_returning_id(sql + " RETURNING id", params))  # type: ignore[attr-defined]

    async def _mt_one(self, sql: str, params: list | None = None) -> dict | None:
        return _normalize(await self._fetch_one(sql, params))  # type: ignore[attr-defined]

    async def _mt_all(self, sql: str, params: list | None = None) -> list[dict]:
        rows = await self._fetch_all(sql, params)  # type: ignore[attr-defined]
        return [_normalize(r) for r in rows]  # type: ignore[misc]
