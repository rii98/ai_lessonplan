"""DocumentService — version-graph policy and access control over a DocumentStore.

The store is a dumb append-and-point log; this service owns *what a version
means* and *who may do what*:

- :meth:`save_lesson` / :meth:`save_unit` open a document with its first version;
- ``commit_*`` append a new version on top of the head (refine, manual edit,
  regenerated day); :meth:`undo` / :meth:`redo` move the head — history is never
  destroyed;
- sharing (§17): a teacher may share a plan with their school (read + copy only)
  and colleagues may :meth:`copy` it into a private document of their own.

Access is decided in ONE place, :class:`DocumentAccess`:

=====================  ==============================================
read (view, export,    owner, or a teacher whose SIGNED school matches
copy, history)         a document shared with that school
write (everything      owner only
else)
=====================  ==============================================

A caller who cannot read a document gets :class:`DocumentNotFound` (404 — we
don't reveal what exists); one who can read but not write gets
:class:`DocumentForbidden` (403 — they already know it exists).
"""

from __future__ import annotations

from typing import Any

from ...auth.principal import Principal
from ...domain.document import (
    DocumentKind,
    DocumentVersion,
    DocumentVersionInfo,
    StoredDocument,
    VersionOrigin,
    Visibility,
)
from ...domain.ldd import LessonDesignDocument
from ...domain.refine import RefineResult
from ...domain.unit import UnitDesignDocument
from ...pagination import Keyset, Page
from .store import DocumentStore


class DocumentNotFound(LookupError):
    pass


class DocumentForbidden(PermissionError):
    pass


class DocumentConflict(ValueError):
    """The operation is valid but not possible in the current state (409)."""


class DocumentAccess:
    """The sharing rule, in one place. The store's shared-list query mirrors
    :meth:`can_read` in SQL; a test asserts the two agree."""

    @staticmethod
    def can_read(p: Principal, d: StoredDocument) -> bool:
        return d.owner_id == p.id or (
            d.visibility == Visibility.school and d.tenant_id is not None
            and d.tenant_id == p.tenant_id)

    @staticmethod
    def can_write(p: Principal, d: StoredDocument) -> bool:
        return d.owner_id == p.id


class DocumentService:
    def __init__(self, store: DocumentStore, access: DocumentAccess | None = None) -> None:
        self.store = store
        self.access = access or DocumentAccess()

    # ── access helpers ────────────────────────────────────────────────────────
    def readable(self, document_id: str, p: Principal,
                 kind: DocumentKind | None = None) -> StoredDocument:
        doc = self.store.get(document_id)
        if doc is None or not self.access.can_read(p, doc) or (kind and doc.kind != kind):
            raise DocumentNotFound(document_id)
        return doc

    def writable(self, document_id: str, p: Principal,
                 kind: DocumentKind | None = None) -> StoredDocument:
        doc = self.readable(document_id, p, kind)
        if not self.access.can_write(p, doc):
            raise DocumentForbidden("only the owner can change this document")
        return doc

    def _head(self, document_id: str, p: Principal, kind: DocumentKind | None,
              *, write: bool = False) -> tuple[StoredDocument, DocumentVersion]:
        found = self.store.head(document_id)
        if found is None or not self.access.can_read(p, found[0]) or (
                kind and found[0].kind != kind):
            raise DocumentNotFound(document_id)
        doc, head = found
        if write and not self.access.can_write(p, doc):
            raise DocumentForbidden("only the owner can change this document")
        if head is None:
            raise DocumentNotFound(document_id)
        return doc, head

    # ── create ────────────────────────────────────────────────────────────────
    def save_lesson(self, ldd: LessonDesignDocument, *, owner: Principal,
                    title: str | None = None) -> tuple[StoredDocument, DocumentVersion]:
        """Persist a freshly generated lesson as a new document + its first version."""
        return self._create(owner, DocumentKind.lesson, title or ldd.topic,
                            ldd.curriculum_ref.grade, ldd.curriculum_ref.subject,
                            ldd.model_dump(mode="json"),
                            grounding_sources=list(ldd.quality.grounding_sources),
                            scores=_scores(ldd))

    def save_unit(self, unit: UnitDesignDocument, *, owner: Principal,
                  title: str | None = None) -> tuple[StoredDocument, DocumentVersion]:
        return self._create(owner, DocumentKind.unit, title or unit.title,
                            unit.curriculum_ref.grade, unit.curriculum_ref.subject,
                            unit.model_dump(mode="json"))

    def _create(self, owner: Principal, kind: DocumentKind, title: str, grade: int | None,
                subject: str | None, snapshot: dict[str, Any], *,
                origin: VersionOrigin = VersionOrigin.generate, copied_from: str | None = None,
                grounding_sources: list[str] | None = None,
                scores: dict[str, Any] | None = None) -> tuple[StoredDocument, DocumentVersion]:
        doc = StoredDocument(owner_id=owner.id, tenant_id=owner.tenant_id, kind=kind,
                             title=title[:300], grade=grade, subject=subject,
                             copied_from=copied_from)
        version = DocumentVersion(document_id=doc.id, origin=origin, snapshot=snapshot,
                                  grounding_sources=grounding_sources or [], scores=scores or {})
        return self.store.create_with_version(doc, version)

    # ── read ──────────────────────────────────────────────────────────────────
    def head_lesson(self, document_id: str, p: Principal
                    ) -> tuple[StoredDocument, LessonDesignDocument]:
        doc, head = self._head(document_id, p, DocumentKind.lesson)
        return doc, LessonDesignDocument.model_validate(head.snapshot)

    def lesson_version(self, document_id: str, version_id: str, p: Principal
                       ) -> tuple[StoredDocument, LessonDesignDocument]:
        doc = self.readable(document_id, p, DocumentKind.lesson)
        v = self.store.get_version(document_id, version_id)
        if v is None:
            raise DocumentNotFound(version_id)
        return doc, LessonDesignDocument.model_validate(v.snapshot)

    def head_unit(self, document_id: str, p: Principal, *, write: bool = False
                  ) -> tuple[StoredDocument, UnitDesignDocument]:
        doc, head = self._head(document_id, p, DocumentKind.unit, write=write)
        return doc, UnitDesignDocument.model_validate(head.snapshot)

    def unit_version(self, document_id: str, version_id: str, p: Principal
                     ) -> tuple[StoredDocument, UnitDesignDocument]:
        doc = self.readable(document_id, p, DocumentKind.unit)
        v = self.store.get_version(document_id, version_id)
        if v is None:
            raise DocumentNotFound(version_id)
        return doc, UnitDesignDocument.model_validate(v.snapshot)

    def versions(self, document_id: str, p: Principal) -> list[DocumentVersionInfo]:
        self.readable(document_id, p)
        return self.store.versions(document_id)

    def list_mine(self, p: Principal, *, kind: DocumentKind | None, limit: int,
                  cursor: Keyset | None) -> Page[StoredDocument]:
        return self.store.list_owned(p.id, kind=kind, limit=limit, cursor=cursor)

    def list_school(self, p: Principal, *, kind: DocumentKind | None, grade: int | None,
                    subject: str | None, q: str | None, limit: int,
                    cursor: Keyset | None) -> Page[StoredDocument]:
        if not p.tenant_id:
            return Page()
        return self.store.list_shared(p.tenant_id, kind=kind, grade=grade, subject=subject, q=q,
                                      limit=limit, cursor=cursor)

    # ── evolve (owner only) ──────────────────────────────────────────────────
    def refinable_lesson(self, document_id: str, p: Principal
                         ) -> tuple[StoredDocument, LessonDesignDocument]:
        """The head lesson, for a refine the caller is allowed to commit."""
        doc, head = self._head(document_id, p, DocumentKind.lesson, write=True)
        return doc, LessonDesignDocument.model_validate(head.snapshot)

    def commit_refine(self, document_id: str, result: RefineResult,
                      p: Principal) -> DocumentVersion | None:
        if not result.ok or result.candidate is None:
            return None
        return self._commit_lesson(document_id, result.candidate, p, origin=VersionOrigin.refine,
                                   instruction=result.instruction, diff=result.diff)

    def commit_manual(self, document_id: str, ldd: LessonDesignDocument,
                      p: Principal) -> DocumentVersion:
        return self._commit_lesson(document_id, ldd, p, origin=VersionOrigin.manual)

    def _commit_lesson(self, document_id: str, ldd: LessonDesignDocument, p: Principal, *,
                       origin: VersionOrigin, instruction: str = "",
                       diff: dict[str, Any] | None = None) -> DocumentVersion:
        doc = self.writable(document_id, p, DocumentKind.lesson)
        return self._append(doc, p, ldd.model_dump(mode="json"), origin=origin,
                            instruction=instruction, diff=diff,
                            grounding_sources=list(ldd.quality.grounding_sources),
                            scores=_scores(ldd))

    def commit_unit(self, document_id: str, unit: UnitDesignDocument, p: Principal, *,
                    origin: VersionOrigin = VersionOrigin.manual, instruction: str = "",
                    diff: dict[str, Any] | None = None) -> DocumentVersion:
        doc = self.writable(document_id, p, DocumentKind.unit)
        return self._append(doc, p, unit.model_dump(mode="json"), origin=origin,
                            instruction=instruction, diff=diff)

    def _append(self, doc: StoredDocument, p: Principal, snapshot: dict[str, Any], *,
                origin: VersionOrigin, instruction: str = "", diff: dict[str, Any] | None = None,
                grounding_sources: list[str] | None = None,
                scores: dict[str, Any] | None = None) -> DocumentVersion:
        try:
            return self.store.add_version(DocumentVersion(
                document_id=doc.id, parent_id=doc.head_version_id, origin=origin,
                snapshot=snapshot, instruction=instruction, diff=diff or {},
                grounding_sources=grounding_sources or [], scores=scores or {},
            ), owner_id=p.id)
        except KeyError as exc:   # deleted between the check and the write
            raise DocumentNotFound(doc.id) from exc

    # ── time travel (owner only) ─────────────────────────────────────────────
    def undo(self, document_id: str, p: Principal) -> StoredDocument:
        _, head = self._head(document_id, p, None, write=True)
        if head.parent_id is None:
            raise DocumentConflict("nothing to undo")
        moved = self.store.set_head(document_id, head.parent_id, owner_id=p.id)
        if moved is None:
            raise DocumentNotFound(document_id)
        return moved

    def redo(self, document_id: str, p: Principal) -> StoredDocument:
        doc = self.writable(document_id, p)
        child = (self.store.latest_child(document_id, doc.head_version_id)
                 if doc.head_version_id else None)
        if child is None:
            raise DocumentConflict("nothing to redo")
        moved = self.store.set_head(document_id, child.id, owner_id=p.id)
        if moved is None:
            raise DocumentNotFound(document_id)
        return moved

    def delete(self, document_id: str, p: Principal) -> None:
        self.writable(document_id, p)
        if not self.store.delete(document_id, owner_id=p.id):
            raise DocumentNotFound(document_id)

    # ── sharing (§17) ────────────────────────────────────────────────────────
    def set_visibility(self, document_id: str, visibility: Visibility,
                       p: Principal) -> StoredDocument:
        doc = self.writable(document_id, p)
        if visibility == Visibility.school and not doc.tenant_id:
            raise DocumentConflict("this document has no school to be shared with")
        try:
            updated = self.store.set_visibility(document_id, visibility, owner_id=p.id)
        except ValueError as exc:
            raise DocumentConflict(str(exc)) from exc
        if updated is None:
            raise DocumentNotFound(document_id)
        return updated

    def copy(self, document_id: str, p: Principal, *,
             title: str | None = None) -> tuple[StoredDocument, DocumentVersion]:
        """A new PRIVATE document owned by the caller (in the caller's current
        school), whose first version is the source's head snapshot. Independent of
        the original in both directions; ``copied_from`` records provenance."""
        src, head = self._head(document_id, p, None)
        return self._create(p, src.kind, title or src.title, src.grade, src.subject,
                            head.snapshot, origin=VersionOrigin.copy, copied_from=src.id,
                            grounding_sources=head.grounding_sources, scores=head.scores)


def _scores(ldd: LessonDesignDocument) -> dict[str, Any]:
    """The rubric scores stamped on the LDD's quality block — the audit snapshot
    of how good this version was."""
    q = ldd.quality
    return {
        "engagement": q.engagement,
        "alignment": q.alignment,
        "misconception_coverage": q.misconception_coverage,
        "specificity": q.specificity,
        "local_relevance": q.local_relevance,
    }
