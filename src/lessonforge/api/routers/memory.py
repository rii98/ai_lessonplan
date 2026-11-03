"""The learner's own view of their long-term memory and progress (docs/LEARN_MODE.md §7.4).

"What LessonForge remembers about me": list (with the profile digest lessons see), add a
note ("remember that …"), edit one, forget one, forget everything, switch memory off,
preview exactly what a question would recall — and the mastery overview ("due for
review") that learn mode builds.
Everything is scoped to the caller; there is no way to read another user's memory.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...auth.principal import Perm, Principal
from ...container import Container
from ...domain.memory import MemoryKind
from ...services.memory import MemoryRefused
from ..deps import current_principal, get_container, require
from ..errors import not_found, unprocessable

router = APIRouter(prefix="/api/v1/me", tags=["me"], dependencies=[Depends(require(Perm.CHAT))])


class MemorySettings(BaseModel):
    enabled: bool


class NewMemory(BaseModel):
    content: str = Field(min_length=3, max_length=200)
    kind: MemoryKind = "preference"


class EditMemory(BaseModel):
    content: str = Field(min_length=3, max_length=200)


def _svc(c: Container):
    svc = c.get_memory_service()
    if svc is None:
        raise not_found("memory")
    return svc


@router.get("/memories")
def list_memories(c: Container = Depends(get_container),
                  p: Principal = Depends(current_principal)) -> dict[str, Any]:
    svc = _svc(c)
    return {"enabled": svc.enabled(p.id), "profile": svc.profile_text(p.id),
            "items": [m.public() for m in svc.list(p.id)]}


@router.post("/memories", status_code=201)
def add_memory(req: NewMemory, c: Container = Depends(get_container),
               p: Principal = Depends(current_principal)) -> dict[str, Any]:
    """The learner adds a note themselves. Same policy as anything extracted (minors-
    first), and reconciled — so "answer in English" replaces "answer in Nepali"."""
    try:
        items = _svc(c).remember(p.id, req.content, req.kind)
    except MemoryRefused as e:
        raise unprocessable(f"Not remembered: {e}") from None
    return {"items": [m.public() for m in items]}


@router.patch("/memories/{memory_id}")
def edit_memory(memory_id: str, req: EditMemory, c: Container = Depends(get_container),
                p: Principal = Depends(current_principal)) -> dict[str, Any]:
    try:
        return _svc(c).edit(p.id, memory_id, req.content).public()
    except KeyError:
        raise not_found("memory") from None
    except MemoryRefused as e:
        raise unprocessable(f"Not saved: {e}") from None


@router.get("/memories/recall")
def preview_recall(q: str = "", c: Container = Depends(get_container),
                   p: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Exactly what a question would bring back (transparency, and debugging)."""
    ctx = _svc(c).recall(p.id, q or None)
    return {"profile": ctx.profile, "relevant": ctx.relevant, "digest": ctx.digest}


@router.delete("/memories/{memory_id}")
def forget_memory(memory_id: str, c: Container = Depends(get_container),
                  p: Principal = Depends(current_principal)) -> dict[str, bool]:
    if not _svc(c).forget(p.id, memory_id):
        raise not_found("memory")
    return {"deleted": True}


@router.delete("/memories")
def forget_all_memories(c: Container = Depends(get_container),
                        p: Principal = Depends(current_principal)) -> dict[str, int]:
    """Clear every memory AND the mastery history (a fresh start)."""
    return {"deleted": _svc(c).forget_all(p.id)}


@router.put("/memory-settings")
def update_memory_settings(req: MemorySettings, c: Container = Depends(get_container),
                           p: Principal = Depends(current_principal)) -> MemorySettings:
    svc = _svc(c)
    svc.set_enabled(p.id, req.enabled)
    return MemorySettings(enabled=svc.enabled(p.id))


@router.get("/learning")
def learning_progress(c: Container = Depends(get_container),
                      p: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Mastery per topic part, discounted by forgetting, soonest review first."""
    records = [r.public() for r in _svc(c).mastery(p.id)]
    return {"items": records, "due": [r for r in records if r["due"]]}
