"""DocumentService: version-graph policy and the §17 access rule (read = owner or
same-school share; write = owner only). Runs on the memory AND Postgres stores."""

from __future__ import annotations

import pytest

from lessonforge.domain.document import DocumentKind, VersionOrigin, Visibility
from lessonforge.domain.refine import RefineResult
from lessonforge.services.documents.service import (
    DocumentAccess,
    DocumentConflict,
    DocumentForbidden,
    DocumentNotFound,
    DocumentService,
)
from lessonforge.services.documents.store import MemoryDocumentStore, PostgresDocumentStore
from tests.conftest import make_principal, pg_principal


@pytest.fixture(params=["memory", "postgres"])
def env(request):
    """(service, person(tenant, name))."""
    if request.param == "memory":
        store = MemoryDocumentStore()

        def person(tenant="sch-1", name=None, role="teacher"):
            p = make_principal(role, tenant=tenant, name=name)
            store.owner_names[p.id] = p.display_name
            return p
        return DocumentService(store), person
    db = request.getfixturevalue("pg")
    return DocumentService(PostgresDocumentStore(db)), (
        lambda tenant="sch-1", name=None, role="teacher": pg_principal(db, role, tenant=tenant,
                                                                        name=name))


def _edited(ldd, topic):
    return ldd.model_copy(update={"topic": topic})


# ── version graph ────────────────────────────────────────────────────────────
def test_save_lesson_creates_document_with_generate_version(env, valid_ldd):
    svc, person = env
    me = person()
    doc, v = svc.save_lesson(valid_ldd, owner=me)
    assert doc.owner_id == me.id and doc.tenant_id == me.tenant_id
    assert doc.title == valid_ldd.topic and doc.grade == 6 and doc.kind == DocumentKind.lesson
    assert v.origin == VersionOrigin.generate and doc.head_version_id == v.id
    assert svc.head_lesson(doc.id, me)[1].topic == valid_ldd.topic


def test_commit_refine_records_audit_and_ignores_failures(env, valid_ldd):
    svc, person = env
    me = person()
    doc, _ = svc.save_lesson(valid_ldd, owner=me)
    ok = RefineResult(ok=True, target="materials", instruction="use lab kit",
                      candidate=_edited(valid_ldd, "Refined"), diff={"materials": "changed"})
    v = svc.commit_refine(doc.id, ok, me)
    assert v.origin == VersionOrigin.refine and v.instruction == "use lab kit"
    assert svc.head_lesson(doc.id, me)[1].topic == "Refined"
    failed = RefineResult(ok=False, target="materials", instruction="x", errors=["bad"])
    assert svc.commit_refine(doc.id, failed, me) is None
    assert len(svc.versions(doc.id, me)) == 2


def test_undo_redo_walk_the_graph_and_conflict_at_the_ends(env, valid_ldd):
    svc, person = env
    me = person()
    doc, root = svc.save_lesson(valid_ldd, owner=me)
    with pytest.raises(DocumentConflict):
        svc.undo(doc.id, me)
    with pytest.raises(DocumentConflict):
        svc.redo(doc.id, me)
    svc.commit_manual(doc.id, _edited(valid_ldd, "Edited"), me)
    assert svc.undo(doc.id, me).head_version_id == root.id
    assert svc.head_lesson(doc.id, me)[1].topic == valid_ldd.topic
    svc.redo(doc.id, me)
    assert svc.head_lesson(doc.id, me)[1].topic == "Edited"


def test_specific_version_and_unknown_ids(env, valid_ldd):
    svc, person = env
    me = person()
    doc, root = svc.save_lesson(valid_ldd, owner=me)
    svc.commit_manual(doc.id, _edited(valid_ldd, "Later"), me)
    assert svc.lesson_version(doc.id, root.id, me)[1].topic == valid_ldd.topic
    for call in (lambda: svc.lesson_version(doc.id, "nope", me),
                 lambda: svc.head_lesson("nope", me), lambda: svc.undo("nope", me)):
        with pytest.raises(DocumentNotFound):
            call()


def test_kind_mismatch_is_not_found(env, valid_ldd):
    svc, person = env
    me = person()
    doc, _ = svc.save_lesson(valid_ldd, owner=me)
    with pytest.raises(DocumentNotFound):
        svc.head_unit(doc.id, me)


# ── access: private by default (IDOR closed) ────────────────────────────────
def test_private_documents_are_invisible_to_everyone_else(env, valid_ldd):
    svc, person = env
    owner, colleague, stranger = person(), person(), person(tenant="sch-9")
    doc, _ = svc.save_lesson(valid_ldd, owner=owner)
    for other in (colleague, stranger):
        for call in (lambda o=other: svc.head_lesson(doc.id, o),
                     lambda o=other: svc.versions(doc.id, o),
                     lambda o=other: svc.copy(doc.id, o),
                     lambda o=other: svc.commit_manual(doc.id, valid_ldd, o),
                     lambda o=other: svc.delete(doc.id, o)):
            with pytest.raises(DocumentNotFound):   # 404: existence is not revealed
                call()


# ── access: school sharing (§17) ─────────────────────────────────────────────
def test_shared_plan_colleague_can_read_and_copy_but_never_write(env, valid_ldd):
    svc, person = env
    owner, colleague = person(name="Sita"), person()
    doc, _ = svc.save_lesson(valid_ldd, owner=owner)
    svc.set_visibility(doc.id, Visibility.school, owner)

    assert svc.head_lesson(doc.id, colleague)[1].topic == valid_ldd.topic
    assert len(svc.versions(doc.id, colleague)) == 1
    for write in (lambda: svc.commit_manual(doc.id, valid_ldd, colleague),
                  lambda: svc.refinable_lesson(doc.id, colleague),
                  lambda: svc.undo(doc.id, colleague), lambda: svc.redo(doc.id, colleague),
                  lambda: svc.delete(doc.id, colleague),
                  lambda: svc.set_visibility(doc.id, Visibility.private, colleague)):
        with pytest.raises(DocumentForbidden):     # 403: they can see it, not change it
            write()
    listed = svc.list_school(colleague, kind=None, grade=None, subject=None, q=None, limit=10,
                             cursor=None).items
    assert [d.id for d in listed] == [doc.id] and listed[0].owner_name == "Sita"


def test_another_schools_teacher_cannot_see_a_shared_plan(env, valid_ldd):
    svc, person = env
    owner, stranger = person(), person(tenant="sch-9")
    doc, _ = svc.save_lesson(valid_ldd, owner=owner)
    svc.set_visibility(doc.id, Visibility.school, owner)
    with pytest.raises(DocumentNotFound):
        svc.head_lesson(doc.id, stranger)
    assert svc.list_school(stranger, kind=None, grade=None, subject=None, q=None, limit=10,
                           cursor=None).items == []


def test_unsharing_removes_access_immediately(env, valid_ldd):
    svc, person = env
    owner, colleague = person(), person()
    doc, _ = svc.save_lesson(valid_ldd, owner=owner)
    svc.set_visibility(doc.id, Visibility.school, owner)
    svc.set_visibility(doc.id, Visibility.private, owner)
    with pytest.raises(DocumentNotFound):
        svc.head_lesson(doc.id, colleague)


def test_copy_is_private_owned_by_the_copier_and_independent(env, valid_ldd):
    svc, person = env
    owner, colleague = person(), person()
    doc, _ = svc.save_lesson(valid_ldd, owner=owner)
    svc.set_visibility(doc.id, Visibility.school, owner)
    copy, v = svc.copy(doc.id, colleague, title="My version")
    assert copy.owner_id == colleague.id and copy.visibility == Visibility.private
    assert copy.copied_from == doc.id and v.origin == VersionOrigin.copy
    svc.commit_manual(copy.id, _edited(valid_ldd, "Mine now"), colleague)
    assert svc.head_lesson(doc.id, owner)[1].topic == valid_ldd.topic   # original untouched
    svc.commit_manual(doc.id, _edited(valid_ldd, "Owner edit"), owner)
    assert svc.head_lesson(copy.id, colleague)[1].topic == "Mine now"   # copy untouched


def test_cannot_share_without_a_school(env, valid_ldd):
    svc, person = env
    homeless = person(tenant=None)
    doc, _ = svc.save_lesson(valid_ldd, owner=homeless)
    with pytest.raises(DocumentConflict):
        svc.set_visibility(doc.id, Visibility.school, homeless)
    assert svc.list_school(homeless, kind=None, grade=None, subject=None, q=None, limit=10,
                           cursor=None).items == []


def test_sql_shared_list_agrees_with_can_read(env, valid_ldd):
    """The store's list query mirrors DocumentAccess.can_read (§17)."""
    svc, person = env
    a, b, far = person(), person(), person(tenant="sch-9")
    mine, _ = svc.save_lesson(valid_ldd, owner=a)
    shared, _ = svc.save_lesson(valid_ldd, owner=b)
    svc.set_visibility(shared.id, Visibility.school, b)
    elsewhere, _ = svc.save_lesson(valid_ldd, owner=far)
    svc.set_visibility(elsewhere.id, Visibility.school, far)
    listed = {d.id for d in svc.list_school(a, kind=None, grade=None, subject=None, q=None,
                                            limit=50, cursor=None).items}
    access = DocumentAccess()
    for d in (mine, shared, elsewhere):
        stored = svc.store.get(d.id)
        visible_to_colleague = access.can_read(a, stored) and stored.owner_id != a.id
        assert (d.id in listed) == visible_to_colleague
