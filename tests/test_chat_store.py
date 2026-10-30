"""ChatStore contract: every test runs against the memory AND the Postgres store
(Postgres when TEST_DATABASE_URL is set), so the two cannot drift."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from lessonforge.config import ChatStoreConfig
from lessonforge.domain.chat import (
    ArtifactAttempt,
    ChatArtifact,
    ChatDefaults,
    ChatMessage,
    Citation,
    Conversation,
    FilterMode,
    Role,
)
from lessonforge.pagination import Keyset
from lessonforge.services.chat.store import MemoryChatStore, PostgresChatStore
from lessonforge.services.registry import build_chat_store
from tests.conftest import make_principal, pg_principal


@pytest.fixture(params=["memory", "postgres"])
def env(request):
    """(store, new_owner_id) for each backend."""
    if request.param == "memory":
        return MemoryChatStore(), lambda: make_principal().id
    db = request.getfixturevalue("pg")
    return PostgresChatStore(db), lambda: pg_principal(db).id


def _conv(owner: str, **kw) -> Conversation:
    return Conversation(owner_id=owner, **kw)


def test_create_get_list_and_owner_isolation(env):
    store, owner = env
    a, b = owner(), owner()
    c1 = store.create(_conv(a, title="one", defaults=ChatDefaults(grade=6, subject="Science")))
    store.create(_conv(b, title="theirs"))
    got = store.get(c1.id, owner_id=a)
    assert got is not None and got.title == "one" and got.defaults.grade == 6
    assert store.get(c1.id, owner_id=b) is None            # another owner: not found
    assert store.get("not-a-uuid", owner_id=a) is None     # malformed id: not found, not 500
    assert [c.title for c in store.list(a, limit=10).items] == ["one"]


def test_list_is_keyset_paginated_newest_first(env):
    store, owner = env
    a = owner()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(5):
        store.create(_conv(a, title=f"c{i}", created_at=base, updated_at=base + timedelta(minutes=i)))
    p1 = store.list(a, limit=2)
    assert [c.title for c in p1.items] == ["c4", "c3"] and p1.next_cursor
    p2 = store.list(a, limit=2, cursor=Keyset.decode(p1.next_cursor))
    p3 = store.list(a, limit=2, cursor=Keyset.decode(p2.next_cursor))
    assert [c.title for c in p2.items] == ["c2", "c1"]
    assert [c.title for c in p3.items] == ["c0"] and p3.next_cursor is None


def test_messages_order_tail_and_ranges(env):
    store, owner = env
    conv = store.create(_conv(owner()))
    for i in range(5):
        store.add_message(conv.id, ChatMessage(role=Role.user if i % 2 == 0 else Role.assistant,
                                               content=f"m{i}"))
    assert [m.content for m in store.messages(conv.id)] == [f"m{i}" for i in range(5)]
    tail, total = store.tail(conv.id, limit=2)
    assert [m.content for m in tail] == ["m3", "m4"] and total == 5
    assert [m.content for m in store.messages_between(conv.id, 1, 3)] == ["m1", "m2"]
    assert store.get(conv.id, owner_id=conv.owner_id).message_count == 5


def test_concurrent_appends_never_collide(env):
    """ord is allocated by an atomic counter: concurrent writers can't duplicate it."""
    store, owner = env
    conv = store.create(_conv(owner()))
    errors: list[Exception] = []

    def write(n: int) -> None:
        try:
            for i in range(5):
                store.add_message(conv.id, ChatMessage(role=Role.user, content=f"{n}-{i}"))
        except Exception as exc:  # pragma: no cover - the failure we guard against
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.messages(conv.id)) == 20
    assert store.tail(conv.id, limit=1)[1] == 20


def test_citations_and_meta_round_trip(env):
    store, owner = env
    conv = store.create(_conv(owner()))
    store.add_message(conv.id, ChatMessage(
        role=Role.assistant, content="x", meta={"mode": "scoped"},
        citations=[Citation(n=1, source="Book", collection="reference", text="t", score=0.5,
                            metadata={"chapter": "Ch 1"})]))
    m = store.messages(conv.id)[0]
    assert m.citations[0].metadata == {"chapter": "Ch 1"} and m.meta == {"mode": "scoped"}


def test_update_is_owner_scoped_and_partial(env):
    store, owner = env
    a, b = owner(), owner()
    conv = store.create(_conv(a, title="old"))
    assert store.update(conv.id, owner_id=b, title="hijack") is None
    up = store.update(conv.id, owner_id=a, summary="sum")
    assert up.title == "old" and up.summary == "sum"
    up = store.update(conv.id, owner_id=a,
                      defaults=ChatDefaults(mode=FilterMode.broad, class_="8A"))
    assert up.defaults.mode is FilterMode.broad and up.defaults.class_ == "8A"
    assert store.get(conv.id, owner_id=a).defaults.class_ == "8A"   # alias round-trips


def test_delete_is_owner_scoped_and_cascades(env):
    store, owner = env
    a, b = owner(), owner()
    conv = store.create(_conv(a))
    store.add_message(conv.id, ChatMessage(role=Role.user, content="hi"))
    art = store.add_artifact(ChatArtifact(conversation_id=conv.id, kind="quiz", title="q"))
    store.save_attempt(ArtifactAttempt(artifact_id=art.id, conversation_id=conv.id, owner_id=a,
                                       kind="quiz"))
    assert store.delete(conv.id, owner_id=b) is False
    assert store.delete(conv.id, owner_id=a) is True
    assert store.messages(conv.id) == [] and store.list_artifacts(conv.id) == []
    assert store.attempts_by_artifact(conv.id) == {}


def test_artifacts_are_reachable_only_through_their_owner(env):
    store, owner = env
    a, b = owner(), owner()
    conv = store.create(_conv(a))
    first = store.add_artifact(ChatArtifact(conversation_id=conv.id, kind="quiz", title="1",
                                            payload={"k": 1}))
    store.add_artifact(ChatArtifact(conversation_id=conv.id, kind="quiz", title="2"))
    assert [x.title for x in store.list_artifacts(conv.id)] == ["1", "2"]
    assert store.get_artifact(first.id, owner_id=a).payload == {"k": 1}
    assert store.get_artifact(first.id, owner_id=b) is None
    assert store.delete_artifact(first.id, owner_id=b) is False
    assert store.delete_artifact(first.id, owner_id=a) is True


def test_attempts_upsert_owner_scope_and_batch_read(env):
    store, owner = env
    a, b = owner(), owner()
    conv = store.create(_conv(a))
    q1 = store.add_artifact(ChatArtifact(conversation_id=conv.id, kind="quiz", title="1"))
    q2 = store.add_artifact(ChatArtifact(conversation_id=conv.id, kind="quiz", title="2"))
    t0 = datetime.now(UTC)
    old = store.save_attempt(ArtifactAttempt(artifact_id=q1.id, conversation_id=conv.id,
                                             owner_id=a, kind="quiz", created_at=t0))
    new = store.save_attempt(ArtifactAttempt(artifact_id=q1.id, conversation_id=conv.id,
                                             owner_id=a, kind="quiz",
                                             created_at=t0 + timedelta(seconds=1)))
    store.save_attempt(ArtifactAttempt(artifact_id=q2.id, conversation_id=conv.id, owner_id=a,
                                       kind="quiz"))
    old.status, old.score = "completed", 3.0
    store.save_attempt(old)                                  # upsert, not a new row
    assert [t.id for t in store.list_attempts(q1.id)] == [new.id, old.id]
    assert store.get_attempt(old.id, owner_id=a).score == 3.0
    assert store.get_attempt(old.id, owner_id=b) is None
    grouped = store.attempts_by_artifact(conv.id)
    assert set(grouped) == {q1.id, q2.id} and len(grouped[q1.id]) == 2


def test_from_config_builds_expected_backend():
    assert isinstance(build_chat_store(ChatStoreConfig(provider="memory")), MemoryChatStore)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        build_chat_store(ChatStoreConfig(provider="postgres"))
    with pytest.raises(ValueError, match="Unknown chat store"):
        build_chat_store(ChatStoreConfig(provider="sqlite"))


def test_ids_are_canonical_uuids():
    cid = Conversation(owner_id="x").id
    assert str(uuid.UUID(cid)) == cid
