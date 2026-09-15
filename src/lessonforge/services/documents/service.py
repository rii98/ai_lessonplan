"""DocumentService — version-graph policy over a :class:`DocumentStore`.

The store is a dumb append-and-point log; this service owns *what a version means*:

- :meth:`save_lesson` opens a document with its first ``generate`` version.
- :meth:`commit_lesson` appends a new version onto the current head — an accepted
  refine (with its instruction, diff, and score delta recorded) or a manual edit.
- :meth:`undo` moves the head back to the current version's parent; :meth:`redo`
  moves it forward to a child. History is never destroyed — undo only re-points.
- :meth:`head_lesson` / :meth:`get_lesson_version` rehydrate a snapshot back into a
  validated :class:`LessonDesignDocument`.

Keeping the graph policy here (not in the store) means a new backend is pure
persistence, and a new document kind — the multi-day unit in Phase 4 — reuses the
same store and the same undo/redo by snapshotting its own JSON.
"""

from __future__ import annotations

from typing import Any

from ...domain.document import (
    DocumentKind,
    DocumentVersion,
    StoredDocument,
    VersionOrigin,
)
from ...domain.ldd import LessonDesignDocument
from ...domain.refine import RefineResult
from ...domain.unit import UnitDesignDocument
from .store import DocumentStore


class DocumentService:
    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    # ── create ────────────────────────────────────────────────────────────────
    def save_lesson(
        self,
        ldd: LessonDesignDocument,
        *,
        owner_id: str = "default",
        title: str | None = None,
    ) -> tuple[StoredDocument, DocumentVersion]:
        """Persist a freshly generated lesson as a new document + its first version.
        The title defaults to the lesson topic; grade/subject are lifted for listing."""
        doc = self.store.create(
            StoredDocument(
                owner_id=owner_id,
                kind=DocumentKind.lesson,
                title=title or ldd.topic,
                grade=ldd.curriculum_ref.grade,
                subject=ldd.curriculum_ref.subject,
            )
        )
        version = self.store.add_version(
            DocumentVersion(
                document_id=doc.id,
                parent_id=None,
                origin=VersionOrigin.generate,
                snapshot=ldd.model_dump(mode="json"),
                grounding_sources=list(ldd.quality.grounding_sources),
                scores=_scores(ldd),
            )
        )
        return self.store.get(doc.id) or doc, version

    def save_unit(
        self,
        unit: UnitDesignDocument,
        *,
        owner_id: str = "default",
        title: str | None = None,
    ) -> tuple[StoredDocument, DocumentVersion]:
        """Persist a generated multi-day unit as a new document + its first version.
        The unit snapshot is stored whole, so undo/versioning work uniformly with
        lessons — the only difference is how the snapshot is rehydrated."""
        doc = self.store.create(
            StoredDocument(
                owner_id=owner_id,
                kind=DocumentKind.unit,
                title=title or unit.title,
                grade=unit.curriculum_ref.grade,
                subject=unit.curriculum_ref.subject,
            )
        )
        version = self.store.add_version(
            DocumentVersion(
                document_id=doc.id,
                parent_id=None,
                origin=VersionOrigin.generate,
                snapshot=unit.model_dump(mode="json"),
            )
        )
        return self.store.get(doc.id) or doc, version

    def commit_unit(
        self,
        document_id: str,
        unit: UnitDesignDocument,
        *,
        origin: VersionOrigin = VersionOrigin.manual,
        instruction: str = "",
        diff: dict[str, Any] | None = None,
    ) -> DocumentVersion | None:
        """Append a new unit version on top of the head — a regenerated day or a
        manual edit. Returns ``None`` if the document is unknown."""
        doc = self.store.get(document_id)
        if doc is None:
            return None
        return self.store.add_version(
            DocumentVersion(
                document_id=document_id,
                parent_id=doc.head_version_id,
                origin=origin,
                snapshot=unit.model_dump(mode="json"),
                instruction=instruction,
                diff=diff or {},
            )
        )

    def head_unit(self, document_id: str) -> UnitDesignDocument | None:
        head = self._head_version(document_id)
        return UnitDesignDocument.model_validate(head.snapshot) if head else None

    def get_unit_version(
        self, document_id: str, version_id: str
    ) -> UnitDesignDocument | None:
        v = self.store.get_version(version_id)
        if v is None or v.document_id != document_id:
            return None
        return UnitDesignDocument.model_validate(v.snapshot)

    # ── evolve ─────────────────────────────────────────────────────────────────
    def commit_refine(self, document_id: str, result: RefineResult) -> DocumentVersion | None:
        """Append an accepted refine as a new version on top of the current head,
        recording its instruction, diff, and score delta for the audit trail.
        Returns ``None`` if the document is unknown or the refine did not succeed."""
        if not result.ok or result.candidate is None:
            return None
        return self._commit(
            document_id,
            result.candidate,
            origin=VersionOrigin.refine,
            instruction=result.instruction,
            diff=result.diff,
        )

    def commit_manual(
        self, document_id: str, ldd: LessonDesignDocument
    ) -> DocumentVersion | None:
        """Append a teacher's direct edit as a new version on top of the head."""
        return self._commit(document_id, ldd, origin=VersionOrigin.manual)

    def _commit(
        self,
        document_id: str,
        ldd: LessonDesignDocument,
        *,
        origin: VersionOrigin,
        instruction: str = "",
        diff: dict[str, Any] | None = None,
    ) -> DocumentVersion | None:
        doc = self.store.get(document_id)
        if doc is None:
            return None
        return self.store.add_version(
            DocumentVersion(
                document_id=document_id,
                parent_id=doc.head_version_id,  # branch off whatever is current
                origin=origin,
                snapshot=ldd.model_dump(mode="json"),
                instruction=instruction,
                diff=diff or {},
                grounding_sources=list(ldd.quality.grounding_sources),
                scores=_scores(ldd),
            )
        )

    # ── time travel ─────────────────────────────────────────────────────────────
    def undo(self, document_id: str) -> StoredDocument | None:
        """Move the head to the current version's parent. Returns the updated
        document, or ``None`` if there is nothing before the current version."""
        head = self._head_version(document_id)
        if head is None or head.parent_id is None:
            return None
        return self.store.set_head(document_id, head.parent_id)

    def redo(self, document_id: str) -> StoredDocument | None:
        """Move the head forward to a version whose parent is the current head —
        the inverse of undo. Picks the most-recently-created child when several
        exist (a linear history has at most one)."""
        doc = self.store.get(document_id)
        if doc is None or doc.head_version_id is None:
            return None
        children = [
            v for v in self.store.versions(document_id)
            if v.parent_id == doc.head_version_id
        ]
        if not children:
            return None
        target = max(children, key=lambda v: v.created_at)
        return self.store.set_head(document_id, target.id)

    # ── read ────────────────────────────────────────────────────────────────────
    def head_lesson(self, document_id: str) -> LessonDesignDocument | None:
        head = self._head_version(document_id)
        return _rehydrate(head) if head else None

    def get_lesson_version(
        self, document_id: str, version_id: str
    ) -> LessonDesignDocument | None:
        v = self.store.get_version(version_id)
        if v is None or v.document_id != document_id:
            return None
        return _rehydrate(v)

    def _head_version(self, document_id: str) -> DocumentVersion | None:
        doc = self.store.get(document_id)
        if doc is None or doc.head_version_id is None:
            return None
        return self.store.get_version(doc.head_version_id)


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


def _rehydrate(version: DocumentVersion) -> LessonDesignDocument:
    return LessonDesignDocument.model_validate(version.snapshot)
