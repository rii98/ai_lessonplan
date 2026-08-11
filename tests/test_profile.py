"""TeacherProfile domain behaviour + the ProfileStore backends + the wiring that
injects a profile into intake defaults and the enrichment prompt."""

from __future__ import annotations

import pytest

from lessonforge.config import CritiqueConfig, ProfileConfig
from lessonforge.domain.ldd import IntakeRequest, NormalizedBrief
from lessonforge.domain.profile import TeacherProfile
from lessonforge.services.generation import LessonGenerator
from lessonforge.services.intake import HeuristicIntake
from lessonforge.services.pipeline import LessonPipeline
from lessonforge.services.profile import (
    FileProfileStore,
    MemoryProfileStore,
    _safe_owner,
)
from lessonforge.services.registry import build_critic, build_profile_store
from lessonforge.services.revise import Reviser
from tests.conftest import FakeLLM


# ── defaults: profile fills unset request fields, explicit fields win ─────────
def test_apply_defaults_fills_only_unset_fields():
    profile = TeacherProfile(default_grade=6, default_subject="Science", default_framework="inquiry")
    req = IntakeRequest(topic="Water Cycle", framework="5E")  # framework explicit
    out = profile.apply_defaults(req)
    assert out.grade == 6
    assert out.subject == "Science"
    assert out.framework == "5E"  # explicit request beats the profile default
    assert out.topic == "Water Cycle"


def test_apply_defaults_noop_when_profile_empty():
    req = IntakeRequest(topic="X", grade=6)
    assert TeacherProfile().apply_defaults(req) is req  # unchanged instance, no copy churn


# ── personalization: voice + anchors flow onto the brief ──────────────────────
def test_personalize_stamps_voice_and_merges_anchors():
    profile = TeacherProfile(style_notes="warm, storytelling", local_anchors=["Phewa lake", "millet"])
    brief = NormalizedBrief(topic="T", grade=6, subject="Science", local_anchors=["millet", "goat"])
    out = profile.personalize(brief)
    assert out.style_notes == "warm, storytelling"
    # order-stable, de-duped union (brief anchors first)
    assert out.local_anchors == ["millet", "goat", "Phewa lake"]


def test_personalize_noop_when_nothing_to_add():
    brief = NormalizedBrief(topic="T", grade=6, subject="Science")
    assert TeacherProfile().personalize(brief) is brief


# ── ProfileStore backends ─────────────────────────────────────────────────────
def test_memory_store_roundtrip_and_empty_default():
    store = MemoryProfileStore()
    assert store.load().style_notes == ""  # never-saved → empty profile, not an error
    store.save(TeacherProfile(style_notes="hi", default_grade=7))
    assert store.load().style_notes == "hi"
    assert store.load().default_grade == 7


def test_file_store_persists_across_instances(tmp_path):
    FileProfileStore(tmp_path).save(TeacherProfile(teacher_name="Deepa", local_anchors=["khola"]))
    reopened = FileProfileStore(tmp_path)  # a fresh instance reads from disk
    loaded = reopened.load()
    assert loaded.teacher_name == "Deepa"
    assert loaded.local_anchors == ["khola"]


def test_file_store_owner_id_cannot_escape_directory(tmp_path):
    store = FileProfileStore(tmp_path)
    store.save(TeacherProfile(owner_id="../../etc/passwd", style_notes="x"))
    # the traversal is sanitized to a flat filename inside the profiles dir
    written = list(tmp_path.iterdir())
    assert len(written) == 1
    assert written[0].parent == tmp_path


@pytest.mark.parametrize("raw,expected", [("../evil", "evil"), ("a/b", "a_b"), ("", "default")])
def test_safe_owner(raw, expected):
    assert _safe_owner(raw) == expected


def test_build_profile_store_from_config(tmp_path):
    assert isinstance(build_profile_store(ProfileConfig(provider="memory")), MemoryProfileStore)
    fs = build_profile_store(ProfileConfig(provider="file", path=str(tmp_path)))
    assert isinstance(fs, FileProfileStore)


def test_build_profile_store_unknown_provider_fails_loudly():
    with pytest.raises(ValueError, match="Unknown profile store provider"):
        build_profile_store(ProfileConfig(provider="nope"))


# ── the prompt actually carries the personalization ───────────────────────────
def test_generation_prompt_includes_voice_and_anchors(valid_ldd_dict):
    llm = FakeLLM(response=valid_ldd_dict)
    brief = NormalizedBrief(
        topic="Sound", grade=7, subject="Science",
        style_notes="lots of pair work", local_anchors=["madal drum"],
    )
    LessonGenerator(llm=llm).generate(brief)
    prompt = llm.calls[0]["prompt"]
    assert "lots of pair work" in prompt
    assert "madal drum" in prompt


def test_generation_prompt_omits_personalization_when_absent(valid_ldd_dict):
    llm = FakeLLM(response=valid_ldd_dict)
    LessonGenerator(llm=llm).generate(NormalizedBrief(topic="Sound", grade=7, subject="Science"))
    assert "stored preferences" not in llm.calls[0]["prompt"]


# ── end to end: the pipeline applies the profile ──────────────────────────────
def _pipeline(llm: FakeLLM) -> LessonPipeline:
    critic = build_critic(CritiqueConfig(), llm=llm)  # structural, deterministic
    return LessonPipeline(
        intake=HeuristicIntake(),
        generator=LessonGenerator(llm=llm),
        reviser=Reviser(llm=None, critic=critic),
    )


def test_pipeline_profile_default_supplies_missing_grade(valid_ldd_dict):
    llm = FakeLLM(response=valid_ldd_dict)
    pipeline = _pipeline(llm)
    req = IntakeRequest(topic="Environment")  # no grade → intake would fail…
    with pytest.raises(ValueError, match="grade"):
        pipeline.run(req)
    # …but a profile default rescues it
    profile = TeacherProfile(default_grade=6, default_subject="Science", style_notes="use local rivers")
    ldd = pipeline.run(req, profile=profile)
    assert ldd.topic  # produced a valid LDD
    assert "use local rivers" in llm.calls[0]["prompt"]  # personalization reached the prompt
