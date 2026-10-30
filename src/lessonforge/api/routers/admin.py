"""The owner console API (``/api/v1/admin``). Requires ``admin`` (superadmin).

Thin HTTP adapter over :class:`~lessonforge.services.admin.AdminService` — the
same service the CLI uses, so every operation is guarded and audited identically.
Superadmin grants are listed read-only (D5: granting stays in the CLI).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ...auth.directory import PeopleFilter
from ...auth.principal import Perm, Principal
from ...container import Container
from ...pagination import DEFAULT_LIMIT, MAX_LIMIT, Keyset
from ...services.admin import Actor, AdminService
from ..deps import current_principal, get_container, require
from ..errors import not_found

router = APIRouter(prefix="/api/v1/admin", tags=["admin"],
                   dependencies=[Depends(require(Perm.ADMIN))])


def _svc(c: Container = Depends(get_container)) -> AdminService:
    return c.get_admin_service()


class ForgetRequest(BaseModel):
    confirm: str = Field(..., max_length=128, description="type the person's subject")


@router.get("/overview")
def overview(svc: AdminService = Depends(_svc)) -> dict[str, object]:
    """Health, schema status, Edboox signing keys, people and content counts."""
    return svc.overview()


@router.get("/people")
def people(q: str | None = Query(None, max_length=120),
           role: Literal["student", "teacher", "service", "superadmin", "pending"] | None = None,
           school: str | None = Query(None, max_length=80),
           disabled: bool | None = None,
           limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
           cursor: str | None = Query(None, max_length=512),
           svc: AdminService = Depends(_svc)) -> dict[str, object]:
    """Everyone who has used LessonForge, most recently seen first."""
    page = svc.people(PeopleFilter(q=q, role=role, tenant_id=school, disabled=disabled),
                      limit=limit, cursor=Keyset.decode(cursor))
    return {"items": page.items, "next_cursor": page.next_cursor}


@router.get("/people/{principal_id}")
def person(principal_id: str, svc: AdminService = Depends(_svc)) -> dict[str, object]:
    try:
        return svc.person(principal_id)
    except LookupError:
        raise not_found("person") from None


def _act(fn, principal_id: str, *args):
    try:
        return fn(principal_id, *args)
    except LookupError:
        raise not_found("person") from None


@router.post("/people/{principal_id}/disable")
def disable(principal_id: str, svc: AdminService = Depends(_svc),
            p: Principal = Depends(current_principal)) -> dict[str, object]:
    """Local kill switch: their next request is refused (within the auth cache)."""
    return _act(svc.set_disabled, principal_id, True, Actor.of(p))


@router.post("/people/{principal_id}/enable")
def enable(principal_id: str, svc: AdminService = Depends(_svc),
           p: Principal = Depends(current_principal)) -> dict[str, object]:
    return _act(svc.set_disabled, principal_id, False, Actor.of(p))


@router.post("/people/{principal_id}/sync-context")
def sync_context(principal_id: str, svc: AdminService = Depends(_svc),
                 p: Principal = Depends(current_principal)) -> dict[str, object]:
    """Pull their details from Edboox now (instead of waiting for the daily refresh)."""
    return _act(svc.sync_context, principal_id, Actor.of(p))


@router.post("/people/{principal_id}/forget")
def forget(principal_id: str, req: ForgetRequest, svc: AdminService = Depends(_svc),
           p: Principal = Depends(current_principal)) -> dict[str, bool]:
    """Delete everything LessonForge holds about them (documents, chats, profile);
    their usage events are anonymised. Irreversible."""
    _act(svc.forget, principal_id, req.confirm, Actor.of(p))
    return {"deleted": True}


@router.get("/grants")
def grants(svc: AdminService = Depends(_svc)) -> dict[str, object]:
    """Superadmins (read-only: grant/revoke with lessonforge-admin, rule D5)."""
    return {"items": svc.grants(),
            "how_to_change": "lessonforge-admin grant|revoke --subject <sub> --role superadmin"}


@router.get("/audit")
def audit(limit: int = Query(50, ge=1, le=200),
          before: int | None = Query(None, ge=1, description="cursor (entry id)"),
          action: str | None = Query(None, max_length=60, description="prefix, e.g. corpus."),
          svc: AdminService = Depends(_svc)) -> dict[str, object]:
    return svc.audit_log(limit=limit, before=before, action=action)


@router.post("/maintenance/purge")
def purge(svc: AdminService = Depends(_svc),
          p: Principal = Depends(current_principal)) -> dict[str, int]:
    """Delete expired launch-token ids and activity events past retention."""
    return svc.purge(Actor.of(p))


@router.post("/maintenance/backfill-spaces")
def backfill_spaces(svc: AdminService = Depends(_svc),
                    p: Principal = Depends(current_principal)) -> dict[str, int]:
    """Put corpus points that predate knowledge spaces on the shared shelf."""
    return svc.backfill_spaces(Actor.of(p))
