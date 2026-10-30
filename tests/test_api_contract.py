"""The checked-in /api/v1 contract (docs/openapi.v1.json) may only grow."""

from __future__ import annotations

import copy
import json

from lessonforge.api.contract import SNAPSHOT, breaking_changes, current_schema


def test_live_api_has_no_breaking_changes_against_the_snapshot():
    problems = breaking_changes(json.loads(SNAPSHOT.read_text()), current_schema())
    assert not problems, "breaking /api/v1 change — add /api/v2 instead:\n" + "\n".join(problems)


def test_every_api_operation_is_in_the_snapshot():
    """New endpoints must be published (python -m lessonforge.api.contract --write)."""
    old, new = json.loads(SNAPSHOT.read_text()), current_schema()
    missing = [f"{m.upper()} {p}" for p, ops in new["paths"].items() for m in ops
               if m not in old["paths"].get(p, {})]
    assert not missing, "unpublished endpoints: " + ", ".join(missing)


def _spec():
    return current_schema()


def test_checker_flags_removed_paths_methods_and_fields():
    old = _spec()
    new = copy.deepcopy(old)
    del new["paths"]["/api/v1/me"]
    del new["paths"]["/api/v1/profile"]["put"]
    schemas = new["components"]["schemas"]
    del schemas["Conversation"]["properties"]["title"]
    out = "\n".join(breaking_changes(old, new))
    assert "removed path /api/v1/me" in out
    assert "removed PUT /api/v1/profile" in out
    assert "response field 'title' removed" in out


def test_checker_flags_new_required_inputs_and_type_changes():
    old = _spec()
    new = copy.deepcopy(old)
    ops = new["paths"]["/api/v1/documents"]["get"]
    ops["parameters"].append({"in": "query", "name": "tenant", "required": True,
                              "schema": {"type": "string"}})
    new["components"]["schemas"]["CreateConversationRequest"].setdefault("required", []).append(
        "title")
    new["components"]["schemas"]["Conversation"]["properties"]["message_count"] = {"type": "string"}
    out = "\n".join(breaking_changes(old, new))
    assert "'tenant' is now required" in out
    assert "request field 'title' is now required" in out
    assert "'message_count' changed type" in out


def test_additive_changes_are_allowed():
    old = _spec()
    new = copy.deepcopy(old)
    new["paths"]["/api/v1/new-thing"] = {"get": {"responses": {}}}
    new["components"]["schemas"]["Conversation"]["properties"]["extra"] = {"type": "string"}
    new["paths"]["/api/v1/documents"]["get"]["parameters"].append(
        {"in": "query", "name": "optional_new", "required": False, "schema": {"type": "string"}})
    assert breaking_changes(old, new) == []
