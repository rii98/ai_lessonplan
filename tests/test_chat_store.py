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
