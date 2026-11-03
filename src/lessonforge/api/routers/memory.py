"""The learner's own view of their long-term memory and progress (docs/LEARN_MODE.md §7.4).

"What LessonForge remembers about me": list, forget one, forget everything, switch
memory off — and the mastery overview ("due for review") that learn mode builds.
Everything is scoped to the caller; there is no way to read another user's memory.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ...auth.principal import Perm, Principal
from ...container import Container
from ..deps import current_principal, get_container, require
from ..errors import not_found

router = APIRouter(prefix="/api/v1/me", tags=["me"], dependencies=[Depends(require(Perm.CHAT))])


class MemorySettings(BaseModel):
    enabled: bool


def _svc(c: Container):
    svc = c.get_memory_service()
    if svc is None:
        raise not_found("memory")
    return svc


@router.get("/memories")
def list_memories(c: Container = Depends(get_container),
                  p: Principal = Depends(current_principal)) -> dict[str, Any]:
    svc = _svc(c)
    return {"enabled": svc.enabled(p.id), "items": [m.public() for m in svc.list(p.id)]}


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
