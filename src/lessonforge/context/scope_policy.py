"""ScopePolicy — how user context becomes retrieval scope and defaults (§6).

Config binds UserContext paths to corpus metadata keys (``student.grade → grade``)
and gives each role a mode:

=========  ==================================================================
``off``     context is not applied; the user's chosen filters only
``default`` context PRE-FILLS a new conversation's scope; the user may change it,
            and the lenient broad fallback still applies
``locked``  bound filters are applied server-side on EVERY turn; client scope for
            bound keys is ignored; no broad fallback
=========  ==================================================================

Linking a new Edboox field to a new corpus key is a YAML line. Note this is
*relevance* scoping inside what a caller may read; *which shelves* they may read
at all is :class:`~lessonforge.knowledge.scope.KnowledgePolicy` — a separate,
signed-claims-only decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..config import ScopeConfig
from ..domain.chat import ChatDefaults, FilterMode
from .model import UserContext

if TYPE_CHECKING:
    from ..auth.principal import Principal
    from ..domain.profile import TeacherProfile


@dataclass(frozen=True, slots=True)
class TurnScope:
    """Server-side constraints on one chat turn (built per request)."""

    mode: str = "off"
    locked_filters: dict[str, Any] = field(default_factory=dict)
    allow_broad_fallback: bool = True
    collections: tuple[str, ...] | None = None      # role allowlist; None = no limit

    def apply(self, mode: FilterMode, filters: dict[str, Any],
              collections: list[str] | None) -> tuple[FilterMode, dict[str, Any], list[str] | None]:
        """Constrain a turn's (mode, filters, collections) — the client's choice
        survives only where the policy leaves room."""
        if self.mode == "locked" and self.locked_filters:
            mode = FilterMode.scoped
            filters = {**filters, **self.locked_filters}
        if self.collections is not None:
            allowed = set(self.collections)
            collections = [c for c in (collections or self.collections) if c in allowed]
        return mode, filters, collections


class ScopePolicy:
    def __init__(self, cfg: ScopeConfig) -> None:
        self.cfg = cfg

    def _role_cfg(self, principal: Principal):
        return self.cfg.roles.get(principal.role)

    def mode_for(self, principal: Principal) -> str:
        if principal.is_superadmin:
            return "off"
        rc = self._role_cfg(principal)
        return rc.mode if rc else "off"

    def bound_filters(self, ctx: UserContext) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for path, key in self.cfg.bindings.items():
            value = ctx.get_path(path)
            if value not in (None, "", []):
                out[key] = value
        return out

    def conversation_defaults(self, principal: Principal, ctx: UserContext,
                              requested: ChatDefaults) -> ChatDefaults:
        """``default``/``locked``: fill bound fields the client left unset (locked
        overrides them). ``off``: the request as-is."""
        mode = self.mode_for(principal)
        if mode == "off":
            return requested
        bound = self.bound_filters(ctx)
        data = requested.model_dump(by_alias=True)
        named = {"grade", "subject", "class"}
        for key, value in bound.items():
            if key in named:
                if mode == "locked" or data.get(key) is None:
                    data[key] = value
            elif mode == "locked" or key not in data["tags"]:
                data["tags"][key] = value
        if mode == "locked":
            data["mode"] = FilterMode.scoped.value
        rc = self._role_cfg(principal)
        if rc and rc.collections is not None:
            data["collections"] = [c for c in (data.get("collections") or rc.collections)
                                   if c in rc.collections]
        return ChatDefaults.model_validate(data)

    def turn_scope(self, principal: Principal, ctx: UserContext) -> TurnScope:
        mode = self.mode_for(principal)
        rc = self._role_cfg(principal)
        collections = tuple(rc.collections) if rc and rc.collections is not None else None
        if mode != "locked":
            return TurnScope(mode=mode, collections=collections)
        return TurnScope(mode=mode, locked_filters=self.bound_filters(ctx),
                         allow_broad_fallback=False, collections=collections)

    @staticmethod
    def profile_defaults(profile: TeacherProfile, ctx: UserContext) -> TeacherProfile:
        """A teacher's assignments and language fill UNSET profile defaults; what
        the teacher saved always wins (and an explicit request beats both, via
        ``TeacherProfile.apply_defaults``)."""
        patch: dict[str, Any] = {}
        assignments = ctx.teacher.assignments if ctx.teacher else []
        if assignments:
            first = assignments[0]
            if profile.default_grade is None:
                patch["default_grade"] = first.grade
            if profile.default_subject is None:
                patch["default_subject"] = first.subject
        if profile.default_language is None and ctx.language:
            patch["default_language"] = ctx.language
        if not profile.school and ctx.school and ctx.school.name:
            patch["school"] = ctx.school.name
        return profile.model_copy(update=patch) if patch else profile
