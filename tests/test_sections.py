"""The section registry — the declarative refine surface / format seam."""

from __future__ import annotations

from lessonforge.domain.ldd import LessonDesignDocument
from lessonforge.domain.sections import (
    LDD_SECTIONS,
    WHOLE_DOCUMENT,
    fragment_schema,
    is_valid_target,
    validate_fragment,
)


def test_every_section_maps_to_a_real_ldd_field():
    fields = set(LessonDesignDocument.model_fields)
    assert set(LDD_SECTIONS) <= fields  # no section names a field that doesn't exist


def test_coupled_neighbours_are_real_sections():
    for section in LDD_SECTIONS.values():
        for neighbour in section.coupled:
            assert neighbour in LDD_SECTIONS


def test_objectives_are_coupled_leaves_are_not():
    assert LDD_SECTIONS["objectives"].coupled == ("phases", "formative_checks")
    assert not LDD_SECTIONS["objectives"].is_leaf
    assert LDD_SECTIONS["local_context"].is_leaf


def test_is_valid_target_accepts_sections_and_whole_document():
    assert is_valid_target("engagement_hook")
    assert is_valid_target(WHOLE_DOCUMENT)
    assert not is_valid_target("topic")  # identity field, not reprompt-able
    assert not is_valid_target("nope")


def test_fragment_schema_is_derived_from_the_ldd():
    # schema comes from the LDD field's own annotation — never hand-maintained
    schema = fragment_schema("engagement_hook")
    assert "prompt" in schema.get("properties", {})


def test_validate_fragment_flags_a_bad_section():
    ok_hook = {"prompt": "Why does the river flood every monsoon?", "kind": "question"}
    assert validate_fragment("engagement_hook", ok_hook) == ""
    err = validate_fragment("engagement_hook", {"kind": "question"})  # missing prompt
    assert err  # non-empty error string


def test_validate_fragment_allows_none_for_optional_homework():
    assert validate_fragment("homework", None) == ""
