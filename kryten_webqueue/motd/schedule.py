"""Pure schedule math for MOTD masters and zone fragments.

A schedule is either a one-off window ``[starts_at, ends_at)`` or a recurring
series: an RRULE (no DTSTART/UNTIL/COUNT) expanded in the schedule's wall-clock
zone from ``starts_at``, each occurrence lasting ``duration_minutes``, bounded
by ``ends_at`` when set. Expanding in local time keeps "Thursday 18:00-23:00"
on the wall clock across DST changes.

Within a scope (the master, or one zone) the winner at an instant is the
highest ``priority``, then the shortest occurrence, then the most recently
updated schedule, then the highest id.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr  # type: ignore[import-untyped]

UTC = datetime.timezone.utc
MAX_DURATION_MINUTES = 7 * 24 * 60
_FORBIDDEN_RRULE_PARTS = re.compile(r"\b(DTSTART|UNTIL|COUNT)\b", re.I)


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Occurrence:
    schedule_id: int
    template_id: int
    zone: str | None
    start: datetime.datetime
    end: datetime.datetime
    priority: int
    updated_at: datetime.datetime

    def rank(self) -> tuple:
        return (
            -self.priority,
            self.end - self.start,
            -self.updated_at.timestamp(),
            -self.schedule_id,
        )


def _utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"Unknown timezone {tz!r}") from exc


def _rule(schedule: dict, tz: ZoneInfo):
    local_start = _utc(schedule["starts_at"]).astimezone(tz).replace(tzinfo=None)
    try:
        return rrulestr(schedule["rrule"], dtstart=local_start)
    except (ValueError, TypeError) as exc:
        raise ScheduleError(f"Invalid recurrence rule: {exc}") from exc


def validate(schedule: dict) -> None:
    """Raise ScheduleError if the schedule can never produce a sane window."""
    starts_at = schedule.get("starts_at")
    ends_at = schedule.get("ends_at")
    rrule = (schedule.get("rrule") or "").strip()
    if not isinstance(starts_at, datetime.datetime):
        raise ScheduleError("starts_at is required")
    tz = _zone(schedule.get("tz") or "America/New_York")
    if ends_at is not None and _utc(ends_at) <= _utc(starts_at):
        raise ScheduleError("ends_at must be after starts_at")
    if not rrule:
        if ends_at is None:
            raise ScheduleError("One-off windows need an end time")
        return
    if _FORBIDDEN_RRULE_PARTS.search(rrule):
        raise ScheduleError(
            "Recurrence rules must not contain DTSTART, UNTIL, or COUNT; "
            "use the series start and end instead"
        )
    duration = schedule.get("duration_minutes")
    if not isinstance(duration, int) or not 1 <= duration <= MAX_DURATION_MINUTES:
        raise ScheduleError(
            f"Recurring windows need a duration between 1 and {MAX_DURATION_MINUTES} minutes"
        )
    rule = _rule({**schedule, "rrule": rrule}, tz)
    first = rule.after(_utc(starts_at).astimezone(tz).replace(tzinfo=None), inc=True)
    if first is None or (
        ends_at is not None
        and first.replace(tzinfo=tz).astimezone(UTC) >= _utc(ends_at)
    ):
        raise ScheduleError("The recurrence produces no occurrence inside the series")


def occurrences(
    schedule: dict, start: datetime.datetime, end: datetime.datetime
) -> list[Occurrence]:
    """Occurrences of ``schedule`` that overlap ``[start, end)``."""
    start, end = _utc(start), _utc(end)
    updated_at = _utc(
        schedule.get("updated_at") or datetime.datetime.min.replace(tzinfo=UTC)
    )

    def make(occ_start: datetime.datetime, occ_end: datetime.datetime) -> Occurrence:
        return Occurrence(
            schedule_id=int(schedule["id"]),
            template_id=int(schedule["template_id"]),
            zone=schedule.get("zone") or None,
            start=occ_start,
            end=occ_end,
            priority=int(schedule.get("priority") or 0),
            updated_at=updated_at,
        )

    series_start = _utc(schedule["starts_at"])
    series_end = _utc(schedule["ends_at"]) if schedule.get("ends_at") else None
    rrule = (schedule.get("rrule") or "").strip()

    if not rrule:
        if series_end is None or series_end <= start or series_start >= end:
            return []
        return [make(series_start, series_end)]

    tz = _zone(schedule.get("tz") or "America/New_York")
    duration = datetime.timedelta(minutes=int(schedule["duration_minutes"]))
    rule = _rule({**schedule, "rrule": rrule}, tz)
    # Occurrences starting up to one duration before the range can still overlap it.
    lo = (start - duration - datetime.timedelta(hours=3)).astimezone(tz)
    hi = end.astimezone(tz)
    found: list[Occurrence] = []
    for local in rule.between(
        lo.replace(tzinfo=None), hi.replace(tzinfo=None), inc=True
    ):
        occ_start = local.replace(tzinfo=tz).astimezone(UTC)
        occ_end = (local + duration).replace(tzinfo=tz).astimezone(UTC)
        if occ_start < series_start:
            continue
        if series_end is not None:
            if occ_start >= series_end:
                continue
            occ_end = min(occ_end, series_end)
        if occ_end > start and occ_start < end and occ_end > occ_start:
            found.append(make(occ_start, occ_end))
    return found


def _scope(schedule: dict) -> str | None:
    return schedule.get("zone") or None


def active_at(
    schedules: list[dict], at: datetime.datetime
) -> dict[str | None, Occurrence]:
    """Winning occurrence per scope (None = master) at instant ``at``."""
    at = _utc(at)
    winners: dict[str | None, Occurrence] = {}
    for schedule in schedules:
        for occ in occurrences(schedule, at, at + datetime.timedelta(microseconds=1)):
            if not occ.start <= at < occ.end:
                continue
            scope = _scope(schedule)
            current = winners.get(scope)
            if current is None or occ.rank() < current.rank():
                winners[scope] = occ
    return winners


@dataclass(frozen=True)
class Segment:
    start: datetime.datetime
    end: datetime.datetime
    winners: dict[str | None, int]  # scope -> schedule_id


def timeline(
    schedules: list[dict], start: datetime.datetime, end: datetime.datetime
) -> tuple[list[Segment], set[int]]:
    """Contiguous segments of identical winners, plus ids that never win.

    Ids are reported only for schedules that occur in the range but are
    always outranked (fully shadowed).
    """
    start, end = _utc(start), _utc(end)
    boundaries = {start, end}
    occurring: set[int] = set()
    for schedule in schedules:
        for occ in occurrences(schedule, start, end):
            occurring.add(occ.schedule_id)
            boundaries.update(b for b in (occ.start, occ.end) if start < b < end)
    points = sorted(boundaries)
    segments: list[Segment] = []
    winning: set[int] = set()
    for left, right in zip(points, points[1:]):
        winners = {
            scope: occ.schedule_id for scope, occ in active_at(schedules, left).items()
        }
        winning.update(winners.values())
        if segments and segments[-1].winners == winners:
            segments[-1] = Segment(segments[-1].start, right, winners)
        else:
            segments.append(Segment(left, right, winners))
    return segments, occurring - winning
