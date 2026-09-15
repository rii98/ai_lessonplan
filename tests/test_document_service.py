"""DocumentService (Phase 3): the version-graph policy — save, refine-commit,
manual edit, and undo/redo — over the store. This is what makes a lesson
revisitable, editable, and undoable."""

from __future__ import annotations

from lessonforge.domain.document import VersionOrigin
from lessonforge.domain.refine import RefineRequest
from lessonforge.services.critique import StructuralCritic
from lessonforge.services.documents.service import DocumentService
from lessonforge.services.documents.store import MemoryDocumentStore
from lessonforge.services.refine import Refiner

# reuse the refine test's section-LLM fake
from tests.test_refine import SectionLLM


def _service() -> DocumentService:
    return DocumentService(MemoryDocumentStore())


def test_save_lesson_creates_document_and_generate_version(valid_ldd):
    svc = _service()
    doc, version = svc.save_lesson(valid_ldd, owner_id="teacher-1")
    assert doc.title == valid_ldd.topic
    assert doc.grade == valid_ldd.curriculum_ref.grade
    assert doc.head_version_id == version.id
    assert version.origin is VersionOrigin.generate
    assert version.parent_id is None
    # the head rehydrates back to an equivalent LDD
    head = svc.head_lesson(doc.id)
    assert head.topic == valid_ldd.topic


def test_commit_refine_appends_version_with_audit(valid_ldd):
    svc = _service()
    doc, v1 = svc.save_lesson(valid_ldd)
    refiner = Refiner(
        llm=SectionLLM({"materials": ["Leaf samples", "Beaker", "Iodine"]}),
        critic=StructuralCritic(),
    )
    result = refiner.refine(
        valid_ldd, RefineRequest(target="materials", instruction="use lab equipment")
    )
    assert result.ok
    v2 = svc.commit_refine(doc.id, result)
    assert v2 is not None
    assert v2.origin is VersionOrigin.refine
    assert v2.parent_id == v1.id           # branched off the head
    assert v2.instruction == "use lab equipment"
    assert "materials" in v2.diff
    # head advanced; new content is live
    assert svc.head_lesson(doc.id).materials == ["Leaf samples", "Beaker", "Iodine"]


def test_commit_refine_ignores_a_failed_refine(valid_ldd):
    svc = _service()
    doc, _ = svc.save_lesson(valid_ldd)
    from lessonforge.domain.refine import RefineResult

    failed = RefineResult(ok=False, target="materials", instruction="x", candidate=None)
    assert svc.commit_refine(doc.id, failed) is None
    assert len(svc.store.versions(doc.id)) == 1  # nothing appended


def test_manual_edit_appends_version(valid_ldd):
    svc = _service()
    doc, _ = svc.save_lesson(valid_ldd)
    edited = valid_ldd.model_copy(deep=True)
    edited.materials = ["Edited by hand"]
    v = svc.commit_manual(doc.id, edited)
    assert v is not None and v.origin is VersionOrigin.manual
    assert svc.head_lesson(doc.id).materials == ["Edited by hand"]


def test_undo_returns_previous_version_content(valid_ldd):
    svc = _service()
    doc, v1 = svc.save_lesson(valid_ldd)
    edited = valid_ldd.model_copy(deep=True)
    edited.materials = ["New materials"]
    svc.commit_manual(doc.id, edited)
    assert svc.head_lesson(doc.id).materials == ["New materials"]
    # undo → head back to the generate version, byte-faithful
    moved = svc.undo(doc.id)
    assert moved.head_version_id == v1.id
    assert svc.head_lesson(doc.id).materials == valid_ldd.materials


def test_undo_at_root_is_a_noop(valid_ldd):
    svc = _service()
    doc, _ = svc.save_lesson(valid_ldd)
    assert svc.undo(doc.id) is None  # nothing before the first version


def test_redo_moves_forward_again(valid_ldd):
    svc = _service()
    doc, _ = svc.save_lesson(valid_ldd)
    edited = valid_ldd.model_copy(deep=True)
    edited.materials = ["New materials"]
    svc.commit_manual(doc.id, edited)
    svc.undo(doc.id)
    moved = svc.redo(doc.id)
    assert moved is not None
    assert svc.head_lesson(doc.id).materials == ["New materials"]


def test_get_specific_version(valid_ldd):
    svc = _service()
    doc, v1 = svc.save_lesson(valid_ldd)
    edited = valid_ldd.model_copy(deep=True)
    edited.materials = ["Later"]
    svc.commit_manual(doc.id, edited)
    # the old version is still addressable regardless of where the head is
    old = svc.get_lesson_version(doc.id, v1.id)
    assert old.materials == valid_ldd.materials


def test_operations_on_unknown_document_degrade(valid_ldd):
    svc = _service()
    assert svc.head_lesson("ghost") is None
    assert svc.undo("ghost") is None
    assert svc.redo("ghost") is None
    assert svc.commit_manual("ghost", valid_ldd) is None
