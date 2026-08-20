"""Catalog identity-coverage report (idea #6).

Summarises which catalog items have a resolved TMDB/IMDb identity and why the
rest do not — a cleanup worklist. Reads only cached identify-step state; performs
no network calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CoverageRow:
    friendly_token: str
    title: str
    content_type: str | None
    resolved: bool
    tmdb_id: str | None
    imdb_tt: str | None
    reason: str


@dataclass
class CoverageReport:
    rows: list[CoverageRow] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    total: int = 0

    def to_dict(self) -> dict:
        return {"total": self.total, "summary": self.summary}


def _reason_for(row: dict) -> str:
    """Derive a coverage reason from a joined catalog/identify-state row."""
    if not row.get("last_identify_at"):
        return "not_identified"
    reason = row.get("identify_reason")
    if reason:
        return reason
    return (
        "resolved" if (row.get("imdb_tt") or row.get("tmdb_id")) else "no_local_match"
    )


async def build_coverage_report(db) -> CoverageReport:
    """Build the identity-coverage report from cached identify state."""
    raw = await db.get_identify_coverage()
    report = CoverageReport(total=len(raw))
    for r in raw:
        reason = _reason_for(r)
        resolved = bool(r.get("imdb_tt") or r.get("tmdb_id"))
        report.rows.append(
            CoverageRow(
                friendly_token=r["friendly_token"],
                title=r["title"],
                content_type=r.get("content_type"),
                resolved=resolved,
                tmdb_id=r.get("tmdb_id"),
                imdb_tt=r.get("imdb_tt"),
                reason=reason,
            )
        )
        report.summary[reason] = report.summary.get(reason, 0) + 1
    return report
