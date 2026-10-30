"""Contract 4 (§15): the activity feed and summary reports Edboox PULLS.
Requires ``reports.read``.

Tenant scoping: a principal with a signed school sees only that school; the
Edboox ``service`` principal and superadmin see every school (optionally
narrowed with ``school=``). No message text is ever stored, so none can leak.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query

from ...activity.events import CATALOGUE
from ...activity.store import ReportFilter
from ...auth.principal import Perm, Principal
from ...container import Container
from ..deps import current_principal, get_container, require
from ..errors import ApiError, unprocessable

router = APIRouter(prefix="/api/v1", tags=["reports"],
                   dependencies=[Depends(require(Perm.REPORTS_READ))])

_MAX_RANGE = timedelta(days=366)


def _tenant(p: Principal, school: str | None) -> str | None:
    if p.is_service or p.is_superadmin:
        return school
    if not p.tenant_id:
        raise ApiError(403, "perm.denied", "reports need a school (or the service role)")
    if school is not None and school != p.tenant_id:
        raise ApiError(403, "perm.denied", "you can only read your own school's reports")
    return p.tenant_id


def _filter(p: Principal, start: datetime | None, end: datetime | None, school: str | None,
            grade: int | None = None, subject: str | None = None) -> ReportFilter:
    end = _utc(end) or datetime.now(UTC)
    start = _utc(start) or end - timedelta(days=30)
    if start >= end:
        raise unprocessable("`from` must be before `to`")
    if end - start > _MAX_RANGE:
        raise unprocessable("the report range may not exceed 366 days")
    return ReportFilter(start=start, end=end, tenant_id=_tenant(p, school), grade=grade,
                        subject=subject)


def _utc(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


@router.get("/events")
def events_feed(after: str = Query("0", max_length=20, description="cursor (event id)"),
                limit: int = Query(500, ge=1, le=1000),
                types: str | None = Query(None, max_length=1000,
                                          description="comma-separated event types"),
                school: str | None = Query(None, max_length=80),
                c: Container = Depends(get_container),
                p: Principal = Depends(current_principal)) -> dict[str, object]:
    """Raw, incremental, replayable: store ``next_cursor`` and poll with
    ``after=<it>``. The cursor is the event id, so the feed is gap-free."""
    try:
        after_id = int(after)
    except ValueError:
        raise unprocessable("`after` must be an event cursor") from None
    wanted = [t.strip() for t in types.split(",") if t.strip()] if types else None
    unknown = [t for t in wanted or [] if t not in CATALOGUE]
    if unknown:
        raise unprocessable(f"unknown event types: {unknown}")
    page = c.platform.activity_reader.feed(after=max(0, after_id), limit=limit, types=wanted,
                                           tenant_id=_tenant(p, school))
    return {"items": [e.view() for e in page.items], "next_cursor": page.next_cursor}


@router.get("/reports/usage")
def report_usage(start: datetime | None = Query(None, alias="from"),
                 end: datetime | None = Query(None, alias="to"),
                 group_by: str = Query("day", max_length=40,
                                       description="comma list of day, school, role"),
                 school: str | None = Query(None, max_length=80),
                 c: Container = Depends(get_container),
                 p: Principal = Depends(current_principal)) -> dict[str, object]:
    """Active users, chats, quizzes, lessons and tokens, grouped."""
    groups = [g.strip() for g in group_by.split(",") if g.strip()]
    bad = [g for g in groups if g not in ("day", "school", "role")]
    if bad or len(set(groups)) != len(groups):
        raise unprocessable(f"group_by must be distinct values of day, school, role: {bad}")
    f = _filter(p, start, end, school)
    return {"from": f.start, "to": f.end, "group_by": groups,
            "rows": c.platform.activity_reader.usage(f, groups)}  # type: ignore[arg-type]


@router.get("/reports/chat-topics")
def report_chat_topics(start: datetime | None = Query(None, alias="from"),
                       end: datetime | None = Query(None, alias="to"),
                       school: str | None = Query(None, max_length=80),
                       grade: int | None = Query(None, ge=1, le=12),
                       c: Container = Depends(get_container),
                       p: Principal = Depends(current_principal)) -> dict[str, object]:
    """Top subjects/chapters asked about; share of turns answered from sources."""
    f = _filter(p, start, end, school, grade)
    return {"from": f.start, "to": f.end, **c.platform.activity_reader.chat_topics(f)}


@router.get("/reports/quizzes")
def report_quizzes(start: datetime | None = Query(None, alias="from"),
                   end: datetime | None = Query(None, alias="to"),
                   school: str | None = Query(None, max_length=80),
                   grade: int | None = Query(None, ge=1, le=12),
                   subject: str | None = Query(None, max_length=120),
                   c: Container = Depends(get_container),
                   p: Principal = Depends(current_principal)) -> dict[str, object]:
    """Quizzes made and taken, average score, completion rate, weakest chapters."""
    f = _filter(p, start, end, school, grade, subject)
    return {"from": f.start, "to": f.end, **c.platform.activity_reader.quizzes(f)}


@router.get("/reports/users/{subject:path}/summary")
def report_user(subject: str, start: datetime | None = Query(None, alias="from"),
                end: datetime | None = Query(None, alias="to"),
                school: str | None = Query(None, max_length=80),
                c: Container = Depends(get_container),
                p: Principal = Depends(current_principal)) -> dict[str, object]:
    """One user's activity (what Edboox shows on a student/teacher page)."""
    if len(subject) > 128:
        raise unprocessable("subject too long")
    f = _filter(p, start, end, school)
    return {"from": f.start, "to": f.end,
            **c.platform.activity_reader.user_summary(subject, f)}


