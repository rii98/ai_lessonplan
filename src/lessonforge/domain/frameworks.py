"""Instructional-framework registry — the canonical phase skeleton per framework.

A lesson's ``framework`` (5E, gradual_release, inquiry) is not just a label: each
one prescribes a *specific, ordered sequence of phases* with its own pedagogy. A
5E lesson runs Engage→Explore→Explain→Elaborate→Evaluate; a gradual-release
lesson runs I-do→We-do→You-do-together→You-do-alone. If the model is shown a 5E
skeleton it will produce 5E phases no matter which framework the teacher chose —
which is exactly the bug this registry closes.

Why typed config and not RAG: the phase skeleton must be *exact and complete* —
the right labels, in the right order, none missing. That is a poor fit for fuzzy
vector retrieval (which optimises for "relevant", not "complete"). So the
skeleton lives here as the reliability floor, and the corpus/RAG layer carries
the *free-text* framework exemplars authors add on top (tagged by ``framework``)
to flavour — never to define — the structure.

Adding a framework is a two-step, no-rewrite change: add a :class:`FrameworkSpec`
here and add its key to the ``Literal`` in :mod:`domain.ldd`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FrameworkPhase:
    """One prescribed phase: the bilingual label the LDD carries, plus a one-line
    ``intent`` the enrichment prompt uses to steer what happens in it."""

    name_en: str
    name_ne: str
    intent: str


@dataclass(frozen=True, slots=True)
class FrameworkSpec:
    key: str        # matches the LDD ``framework`` Literal
    label: str      # human-facing name for UI
    summary: str    # one-line description of the framework's arc
    phases: tuple[FrameworkPhase, ...]

    def phase_directive(self) -> str:
        """The hard instruction injected into the prompt: the exact phases this
        framework requires, in order, so the model cannot fall back to 5E."""
        lines = [
            f"{i}. {p.name_en} ({p.name_ne}) — {p.intent}"
            for i, p in enumerate(self.phases, 1)
        ]
        return (
            f"This is a {self.key} lesson. Its `phases` MUST be EXACTLY these "
            f"{len(self.phases)} phases, in this order, using these bilingual "
            f"name_en/name_ne labels (do NOT use 5E phase names unless this is a "
            f"5E lesson):\n" + "\n".join(lines)
        )

    def example_phases(self, objective_id: str = "O1") -> list[dict[str, object]]:
        """Structurally-valid phase dicts for the shown few-shot example. Labels
        are the real ones (so the model copies the right skeleton); activities are
        deliberately generic one-liners so the model copies *shape*, not content."""
        n = len(self.phases)
        # spread ~45 illustrative minutes across the phases; the model rescales.
        base, extra = divmod(45, n)
        return [
            {
                "name_en": p.name_en,
                "name_ne": p.name_ne,
                "teacher_activities": [p.intent[0].upper() + p.intent[1:]],
                "student_activities": ["Take part and respond during this phase"],
                "minutes": base + (1 if i < extra else 0),
                "objective_ids": [objective_id],
            }
            for i, p in enumerate(self.phases)
        ]


FRAMEWORKS: dict[str, FrameworkSpec] = {
    "5E": FrameworkSpec(
        key="5E",
        label="5E Learning Cycle",
        summary="Engage → Explore → Explain → Elaborate → Evaluate.",
        phases=(
            FrameworkPhase("Engage", "संलग्न गराउनु",
                           "hook curiosity with a local scenario or demonstration"),
            FrameworkPhase("Explore", "अन्वेषण गर्नु",
                           "let students investigate hands-on before any definition"),
            FrameworkPhase("Explain", "व्याख्या गर्नु",
                           "draw out the concept and introduce precise terms"),
            FrameworkPhase("Elaborate", "विस्तार गर्नु",
                           "apply the idea to a new local situation"),
            FrameworkPhase("Evaluate", "मूल्याङ्कन गर्नु",
                           "check understanding against the objectives"),
        ),
    ),
    "gradual_release": FrameworkSpec(
        key="gradual_release",
        label="Gradual Release of Responsibility (I do / We do / You do)",
        summary="Focused instruction → guided practice → collaborative → independent.",
        phases=(
            FrameworkPhase("Focused Instruction (I do)", "केन्द्रित शिक्षण (म गर्छु)",
                           "model the skill aloud, showing your thinking"),
            FrameworkPhase("Guided Instruction (We do)", "निर्देशित अभ्यास (हामी गर्छौं)",
                           "work through examples together, releasing responsibility"),
            FrameworkPhase("Collaborative Learning (You do together)", "सहकार्य (तिमीहरू सँगै गर्छौ)",
                           "students practise in pairs or groups with peer support"),
            FrameworkPhase("Independent Practice (You do alone)", "स्वतन्त्र अभ्यास (तिमी आफैँ गर्छौ)",
                           "each student applies the skill on their own"),
        ),
    ),
    "inquiry": FrameworkSpec(
        key="inquiry",
        label="Inquiry-Based Learning",
        summary="Orient → question → investigate → conclude → reflect.",
        phases=(
            FrameworkPhase("Orientation", "अभिमुखीकरण",
                           "surface a puzzling local phenomenon to wonder about"),
            FrameworkPhase("Questioning", "प्रश्न निर्माण",
                           "students raise and refine their own investigable questions"),
            FrameworkPhase("Investigation", "अनुसन्धान",
                           "gather and analyse evidence to test those questions"),
            FrameworkPhase("Conclusion", "निष्कर्ष",
                           "build an explanation from the evidence they found"),
            FrameworkPhase("Reflection & Discussion", "चिन्तन र छलफल",
                           "share findings and reflect on how they know"),
        ),
    ),
}

DEFAULT_FRAMEWORK = "5E"


def get_framework(key: str | None) -> FrameworkSpec:
    """Resolve a framework key to its spec, falling back to 5E for an unknown or
    missing key so generation never crashes on a bad value."""
    return FRAMEWORKS.get(key or DEFAULT_FRAMEWORK, FRAMEWORKS[DEFAULT_FRAMEWORK])
