"""ChatStore contract tests — run against every non-networked provider.

memory + sqlite must behave identically (the loose-coupling contract); postgres
shares the same ``_SqlChatStore`` code path as sqlite, exercised under the
``integration`` marker elsewhere.
"""

from __future__ import annotations

import pytest

from lessonforge.config import ChatStoreConfig
from lessonforge.domain.chat import (
    ChatDefaults,
    ChatMessage,
    Citation,
    Conversation,
    FilterMode,
    Role,
)
from lessonforge.services.chat.store import MemoryChatStore, SqliteChatStore


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return MemoryChatStore()
    return SqliteChatStore(tmp_path / "chat.db")


def test_create_get_and_list(store):
    a = store.create(Conversation(owner_id="t1", title="A"))
    store.create(Conversation(owner_id="t1", title="B"))
    store.create(Conversation(owner_id="other", title="C"))

    assert store.get(a.id).title == "A"
    assert store.get("nope") is None
    titles = {c.title for c in store.list("t1")}
    assert titles == {"A", "B"}  # other owner excluded


def test_messages_preserve_order_and_limit(store):
    conv = store.create(Conversation())
    for i in range(5):
        role = Role.user if i % 2 == 0 else Role.assistant
        store.add_message(conv.id, ChatMessage(role=role, content=f"m{i}"))

    all_msgs = store.messages(conv.id)
    assert [m.content for m in all_msgs] == ["m0", "m1", "m2", "m3", "m4"]

    # limit keeps the most recent, still oldest-first
    assert [m.content for m in store.messages(conv.id, limit=2)] == ["m3", "m4"]


def test_citations_round_trip(store):
    conv = store.create(Conversation())
    cite = Citation(n=1, source="Book p.3", collection="reference", text="chunk text",
                    score=0.9, metadata={"grade": 6})
    store.add_message(conv.id, ChatMessage(role=Role.assistant, content="ans", citations=[cite]))

    back = store.messages(conv.id)[0]
    assert back.citations[0].source == "Book p.3"
    assert back.citations[0].metadata == {"grade": 6}


def test_update_patches_only_given_fields(store):
    defaults = ChatDefaults(mode=FilterMode.scoped, grade=6, subject="Science")
    conv = store.create(Conversation(title="orig", defaults=defaults, summary="s0"))

    store.update(conv.id, title="renamed")
    got = store.get(conv.id)
    assert got.title == "renamed"
    assert got.defaults.grade == 6  # untouched
    assert got.summary == "s0"

    store.update(conv.id, summary="rolled up")
    assert store.get(conv.id).summary == "rolled up"
    assert store.get(conv.id).title == "renamed"

    assert store.update("missing", title="x") is None


def test_defaults_alias_class_round_trips(store):
    conv = store.create(Conversation(defaults=ChatDefaults.model_validate({"class": "6A"})))
    assert store.get(conv.id).defaults.class_ == "6A"


def test_delete_removes_conversation_and_messages(store):
    conv = store.create(Conversation())
    store.add_message(conv.id, ChatMessage(role=Role.user, content="hi"))
    assert store.delete(conv.id) is True
    assert store.get(conv.id) is None
    assert store.messages(conv.id) == []
    assert store.delete(conv.id) is False  # already gone


def test_sqlite_persists_across_instances(tmp_path):
    path = tmp_path / "chat.db"
    s1 = SqliteChatStore(path)
    conv = s1.create(Conversation(title="persisted"))
    s1.add_message(conv.id, ChatMessage(role=Role.user, content="remember me"))

    s2 = SqliteChatStore(path)  # fresh connection, same file
    assert s2.get(conv.id).title == "persisted"
    assert s2.messages(conv.id)[0].content == "remember me"


def test_from_config_builds_expected_type(tmp_path):
    mem = MemoryChatStore.from_config(ChatStoreConfig(provider="memory"))
    assert isinstance(mem, MemoryChatStore)
    sql = SqliteChatStore.from_config(ChatStoreConfig(provider="sqlite", path=str(tmp_path / "c.db")))
    assert isinstance(sql, SqliteChatStore)


# ── artifacts + attempts (a quiz a turn produced, and its play-throughs) ──────
def _artifact(conv_id, title="Quiz"):
    from lessonforge.domain.chat import ChatArtifact
    return ChatArtifact(conversation_id=conv_id, kind="quiz", title=title,
                        payload={"questions": [1, 2]}, meta={"slots": {"topic": "x"}})


def test_artifacts_round_trip_and_list_oldest_first(store):
    c = store.create(Conversation(title="A"))
    a1 = store.add_artifact(_artifact(c.id, "first"))
    a2 = store.add_artifact(_artifact(c.id, "second"))
    store.add_artifact(_artifact("someone-else"))
    got = store.get_artifact(a1.id)
    assert got.title == "first" and got.payload == {"questions": [1, 2]} and got.meta["slots"]["topic"] == "x"
    assert [a.id for a in store.list_artifacts(c.id)] == [a1.id, a2.id]
    assert store.get_artifact("nope") is None


def test_attempts_upsert_and_list_newest_first(store):
    from lessonforge.domain.chat import ArtifactAttempt
    c = store.create(Conversation(title="A"))
    art = store.add_artifact(_artifact(c.id))
    a = ArtifactAttempt(artifact_id=art.id, conversation_id=c.id, kind="quiz", total=2)
    store.save_attempt(a)
    a.state = {"answers": {"Q1": {"score": 1.0}}}
    a.score, a.status = 1.0, "completed"
    store.save_attempt(a)                                   # upsert, not a duplicate
    b = store.save_attempt(ArtifactAttempt(artifact_id=art.id, conversation_id=c.id, kind="quiz", total=2))
    got = store.get_attempt(a.id)
    assert (got.score, got.status, got.state["answers"]["Q1"]["score"]) == (1.0, "completed", 1.0)
    assert [x.id for x in store.list_attempts(art.id)] == [b.id, a.id]
    assert store.get_attempt("nope") is None


def test_deleting_an_artifact_or_a_conversation_cascades(store):
    from lessonforge.domain.chat import ArtifactAttempt
    c = store.create(Conversation(title="A"))
    a1, a2 = store.add_artifact(_artifact(c.id)), store.add_artifact(_artifact(c.id))
    t1 = store.save_attempt(ArtifactAttempt(artifact_id=a1.id, conversation_id=c.id, kind="quiz"))
    t2 = store.save_attempt(ArtifactAttempt(artifact_id=a2.id, conversation_id=c.id, kind="quiz"))
    assert store.delete_artifact(a1.id) and not store.delete_artifact(a1.id)
    assert store.get_attempt(t1.id) is None and store.get_attempt(t2.id) is not None
    store.delete(c.id)
    assert store.list_artifacts(c.id) == [] and store.get_attempt(t2.id) is None
