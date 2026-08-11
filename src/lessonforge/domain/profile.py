"""TeacherProfile — stored preferences injected into every LDD build (SRD §11).

A teacher shouldn't re-type "Grade 6, Science, en-ne, warm storytelling voice,
use Phewa lake and millet farming" on every request. The profile carries two
kinds of preference:

- **defaults** — grade/subject/duration/language/framework used only when the
  request leaves them unset. An explicit request field always wins, so the
  profile never overrides an intentional choice.
- **personalization** — a teaching *voice* (``style_notes``) and favourite
  ``local_anchors`` that flavour the generated lesson so it feels like *hers*.

The model is multi-tenant-ready from day one (``owner_id``) even though the v1
UI is single-teacher — matching the SRD's "teacher-first, school-ready" data
model, so a shared department profile in v1.5 is a new scope, not a migration.

Application is deliberately kept here (not in intake/generation) so those stages
stay profile-agnostic: the pipeline calls :meth:`apply_defaults` before intake
and :meth:`personalize` after it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .ldd import IntakeRequest, NormalizedBrief

# request field ← profile field. Only these are filled from the profile, and only
# when the request leaves them unset (an explicit request value always wins).
_DEFAULTABLE: dict[str, str] = {
    "grade": "default_grade",
    "subject": "default_subject",
    "duration_min": "default_duration_min",
    "language": "default_language",
    "framework": "default_framework",
}


class TeacherProfile(BaseModel):
    """A teacher's stored preferences. All fields optional so an empty profile is
    valid and simply contributes nothing."""

    owner_id: str = "default"  # single-teacher v1; scopes the store for v1.5 multi-tenancy
    teacher_name: str = ""
    school: str = ""

    # defaults — applied only to unset request fields
    default_grade: int | None = Field(default=None, ge=1, le=12)
    default_subject: str | None = None
    default_duration_min: Literal[30, 45, 60] | None = None
    default_language: Literal["en", "ne", "en-ne"] | None = None
    default_framework: Literal["5E", "gradual_release", "inquiry"] | None = None

    # personalization — flavour injected into every build
    style_notes: str = ""  # e.g. "warm, storytelling, lots of pair work"
    local_anchors: list[str] = Field(default_factory=list)  # e.g. ["Phewa lake", "millet farming"]

    def apply_defaults(self, request: IntakeRequest) -> IntakeRequest:
        """Return a copy of ``request`` with any unset field filled from the
        profile's defaults. Explicit request fields are left untouched."""
        patch = {
            req_field: getattr(self, prof_field)
            for req_field, prof_field in _DEFAULTABLE.items()
            if getattr(request, req_field) is None and getattr(self, prof_field) is not None
        }
        return request.model_copy(update=patch) if patch else request

    def personalize(self, brief: NormalizedBrief) -> NormalizedBrief:
        """Stamp the teaching voice and local anchors onto the brief so the
        enrichment prompt can honour them. Anchors merge (de-duped, order-stable)
        with anything already on the brief; explicit brief style_notes wins."""
        if not self.style_notes and not self.local_anchors:
            return brief
        anchors = list(dict.fromkeys([*brief.local_anchors, *self.local_anchors]))
        return brief.model_copy(
            update={
                "style_notes": brief.style_notes or self.style_notes,
                "local_anchors": anchors,
            }
        )
