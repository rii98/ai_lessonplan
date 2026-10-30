"""Who is calling, and what they may do.

:class:`Perm` is the closed set of capabilities routes declare (D6): code names
permissions, config maps roles onto them. :class:`Principal` is the authenticated
caller as LessonForge sees it — built from a *verified* token plus local grants,
immutable for the life of a request, and the only thing authorization reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Perm(str, Enum):
    CHAT = "chat"                     # /chat/*: conversations, messages, quizzes
    AUTHOR = "author"                 # lessons, units, artifacts, documents, export, profile
    CORPUS_MANAGE = "corpus.manage"   # /corpus/*: the knowledge base
    OPS = "ops"                       # provider health, admin reads
    REPORTS_READ = "reports.read"     # /events, /reports/* (tenant-scoped)


SUPERADMIN = "superadmin"
SERVICE = "service"


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller.

    ``id`` is our UUID for ``(issuer, subject)``. ``tenant_id`` is the SIGNED school
    id from the token — the only tenant authorization ever trusts (cached user
    context never grants anything). ``roles`` = the mapped issuer role plus local
    grants; ``permissions`` are resolved from them by the policy."""

    id: str
    issuer: str
    subject: str
    role: str                                   # the mapped issuer role
    roles: frozenset[str]                       # role + local grants
    permissions: frozenset[Perm]
    tenant_id: str | None = None
    display_name: str | None = None
    grade: int | None = None                    # signed claim (students), for scoping
    extra: dict[str, str] = field(default_factory=dict, compare=False, hash=False)

    def can(self, perm: Perm) -> bool:
        return perm in self.permissions

    @property
    def is_superadmin(self) -> bool:
        return SUPERADMIN in self.roles

    @property
    def is_service(self) -> bool:
        return SERVICE in self.roles

    @property
    def primary_role(self) -> str:
        """The role reported in activity events and used for role-keyed config:
        superadmin wins over the issuer role."""
        return SUPERADMIN if self.is_superadmin else self.role
