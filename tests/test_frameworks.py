"""The framework registry is the source of truth for each framework's phase
skeleton, and generation must anchor the shown example on the *chosen* framework
— not a fixed 5E — which is the root-cause fix for the copy-paste-5E bug."""

from __future__ import annotations

import pytest

from lessonforge.domain.frameworks import FRAMEWORKS, get_framework
from lessonforge.domain.ldd import NormalizedBrief
from lessonforge.services.generation import LessonGenerator
from tests.conftest import FakeLLM


def test_every_ldd_framework_has_a_spec():
    # the LDD Literal and the registry must stay in lock-step
    from lessonforge.domain.ldd import LessonDesignDocument

    literal = LessonDesignDocument.model_fields["framework"].annotation.__args__
    assert set(FRAMEWORKS) == set(literal)


def test_get_framework_falls_back_to_5e_on_unknown():
    assert get_framework("nonsense").key == "5E"
    assert get_framework(None).key == "5E"


def test_specs_have_ordered_bilingual_phases():
    for spec in FRAMEWORKS.values():
        assert len(spec.phases) >= 3
        for p in spec.phases:
            assert p.name_en and p.name_ne and p.intent


def test_example_phases_are_structurally_usable():
    spec = get_framework("gradual_release")
    phases = spec.example_phases(objective_id="O1")
    assert [p["name_en"] for p in phases] == [p.name_en for p in spec.phases]
    assert all(p["objective_ids"] == ["O1"] for p in phases)
    assert all(p["minutes"] >= 1 for p in phases)


@pytest.mark.parametrize("framework", ["5E", "gradual_release", "inquiry"])
def test_prompt_shows_the_chosen_frameworks_phases_not_5e(framework, valid_ldd_dict):
    llm = FakeLLM(response=valid_ldd_dict)
    gen = LessonGenerator(llm=llm, grounding=None)
    gen.generate(NormalizedBrief(topic="Water cycle", grade=6, subject="Science",
                                 framework=framework))
    prompt = llm.calls[0]["prompt"]
    spec = get_framework(framework)

    # the explicit directive names the framework and lists its phases
    assert f"This is a {framework} lesson" in prompt
    for phase in spec.phases:
        assert phase.name_en in prompt

    # the shown JSON example's framework value matches the chosen framework
    assert f'"framework": "{framework}"' in prompt

    # and, crucially, a non-5E lesson does NOT show 5E-only phase names
    if framework != "5E":
        assert "Elaborate" not in prompt
        assert "संलग्न गराउनु" not in prompt  # 5E "Engage" ne label
