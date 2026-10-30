"""Knowledge spaces (§16): which shelves of the corpus a caller may read or write.

A *space* says who may see a chunk (``global`` = every school; ``school:<id>`` =
that school only); it is orthogonal to the *collection* (what kind of material).
Every chunk carries the reserved payload key ``_space``; every vector-store read
takes a required :class:`AccessScope`, ANDed in by the adapter separately from the
caller's metadata filter — so the lenient "broaden to unfiltered" fallback can drop
the filter but never the scope.

:class:`KnowledgePolicy` turns a principal into a scope from config. The tenant
comes from the SIGNED ``school_id`` claim, never from cached context or a request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..config import KnowledgeConfig
from ..rag.documents import GLOBAL_SPACE, SPACE_KEY  # single definition, re-exported

if TYPE_CHECKING:
    from ..auth.principal import Principal

__all__ = ["GLOBAL_SPACE", "SPACE_KEY", "AccessScope", "KnowledgePolicy", "school_space",
           "valid_space"]
_TENANT_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_WILDCARD = "*"


def school_space(tenant_id: str) -> str:
    if not _TENANT_OK.match(tenant_id or ""):
        raise ValueError(f"invalid tenant id for a knowledge space: {tenant_id!r}")
    return f"school:{tenant_id}"


def valid_space(space: str) -> bool:
    if space == GLOBAL_SPACE:
        return True
    prefix, _, tenant = space.partition(":")
    return prefix == "school" and bool(_TENANT_OK.match(tenant))


@dataclass(frozen=True, slots=True)
class AccessScope:
    """The spaces a read may touch. ``unrestricted`` is explicit (admin CLI and
    superadmin only) — there is no implicit "everything"."""

    spaces: frozenset[str] = frozenset({GLOBAL_SPACE})
    unrestricted: bool = False

    @classmethod
    def public(cls) -> AccessScope:
        """Shared curriculum only — the least-privileged readable scope, and what
        any read gets when no request scope was bound."""
        return cls(frozenset({GLOBAL_SPACE}))

    @classmethod
    def all(cls) -> AccessScope:
        return cls(frozenset(), unrestricted=True)

    @classmethod
    def of(cls, *spaces: str) -> AccessScope:
        return cls(frozenset(spaces))

    def allows(self, space: str | None) -> bool:
        return self.unrestricted or (space is not None and space in self.spaces)

    @property
    def key(self) -> str:
        """Stable cache key — any future retrieval/answer cache must include it."""
        return "*" if self.unrestricted else ",".join(sorted(self.spaces))


class KnowledgePolicy:
    """Config-driven read/write rules (``knowledge.read`` / ``knowledge.write``)."""

    def __init__(self, cfg: KnowledgeConfig) -> None:
        self.cfg = cfg

    def scope_for(self, principal: Principal | None) -> AccessScope:
        if principal is None:
            return AccessScope.public()
        spaces: set[str] = set()
        for role in principal.roles:
            for template in self.cfg.read.get(role, ()):
                if template == _WILDCARD:
                    return AccessScope.all()
                space = _expand(template, principal.tenant_id)
                if space is not None:
                    spaces.add(space)
        return AccessScope(frozenset(spaces))

    def write_scope(self, principal: Principal) -> AccessScope:
        """The spaces ``principal`` may write (ingest into, delete from)."""
        spaces: set[str] = set()
        for role in principal.roles:
            for template in self.cfg.write.get(role, ()):
                if template == _WILDCARD:
                    return AccessScope.all()
                space = _expand(template, principal.tenant_id)
                if space is not None:
                    spaces.add(space)
        return AccessScope(frozenset(spaces))

    def can_write(self, principal: Principal, space: str) -> bool:
        if not valid_space(space):
            return False
        for role in principal.roles:
            for template in self.cfg.write.get(role, ()):
                if template == _WILDCARD or _expand(template, principal.tenant_id) == space:
                    return True
        return False


def _expand(template: str, tenant_id: str | None) -> str | None:
    """``school:{tenant}`` → ``school:sch-7``; ``None`` when the principal has no
    (valid) signed school, so the template simply grants nothing."""
    if "{tenant}" not in template:
        return template if valid_space(template) else None
    if not tenant_id or not _TENANT_OK.match(tenant_id):
        return None
    return template.replace("{tenant}", tenant_id)
