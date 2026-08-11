"""LessonPipeline — composes the stages into the end-to-end path.

    IntakeRequest → [intake] → NormalizedBrief
                  → [enrichment] → draft LDD
                  → [critique & revise] → validated LDD

Each stage is an injected dependency (SRD §05), so the pipeline is trivially
testable with fakes and the stages can be reordered or stubbed. The critique
stage owns its own critic, so ``critique`` here just delegates — handy for the
``/lessons/critique`` endpoint that scores an already-generated LDD.
"""

from __future__ import annotations

from ..domain.ldd import IntakeRequest, LessonDesignDocument
from ..domain.profile import TeacherProfile
from ..domain.rubric import Critique
from .generation import LessonGenerator
from .intake import Intake
from .revise import Reviser


class LessonPipeline:
    def __init__(
        self, *, intake: Intake, generator: LessonGenerator, reviser: Reviser
    ) -> None:
        self.intake = intake
        self.generator = generator
        self.reviser = reviser

    def run(
        self, request: IntakeRequest, *, profile: TeacherProfile | None = None
    ) -> LessonDesignDocument:
        """Full path. When a ``profile`` is supplied its defaults fill any unset
        request field (explicit fields win) and its voice/anchors flavour the
        brief — so intake and generation stay profile-agnostic."""
        if profile is not None:
            request = profile.apply_defaults(request)
        brief = self.intake.normalize(request)
        if profile is not None:
            brief = profile.personalize(brief)
        draft = self.generator.generate(brief)
        return self.reviser.revise(draft, brief)

    def critique(self, ldd: LessonDesignDocument) -> Critique:
        return self.reviser.critic.critique(ldd)
