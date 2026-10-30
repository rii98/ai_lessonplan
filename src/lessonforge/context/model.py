"""UserContext — what LessonForge knows about a user beyond the token, in OUR
vocabulary (D8, §6).

It personalises only: retrieval scope defaults, generation defaults, answer
language, local relevance. **No access decision ever reads it** — stale or
wrong context can make an answer less relevant, never grant anything.

Data minimisation (these are minors): no DOB, address, phone, guardians, photos,
marks or attendance. :meth:`UserContext.prompt_facts` is the only view that may
reach an LLM prompt, and it carries no names or ids.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Language = Literal["en", "ne"]


class SchoolRef(BaseModel):
    id: str
    name: str | None = None
    district: str | None = None
    province: str | None = None


class StudentContext(BaseModel):
    grade: int | None = Field(default=None, ge=1, le=12)
    section: str | None = None
    medium: Language | None = None
    stream: str | None = None                       # grades 11–12
    optional_subjects: list[str] = Field(default_factory=list)


class TeachingAssignment(BaseModel):
    grade: int = Field(ge=1, le=12)
    subject: str
    section: str | None = None
    class_id: str | None = None


class TeacherContext(BaseModel):
    assignments: list[TeachingAssignment] = Field(default_factory=list)

    @property
    def grades(self) -> list[int]:
        return sorted({a.grade for a in self.assignments})

    @property
    def subjects(self) -> list[str]:
        return sorted({a.subject for a in self.assignments})


class UserContext(BaseModel):
    school: SchoolRef | None = None
    language: Language | None = None
    student: StudentContext | None = None
    teacher: TeacherContext | None = None
    source: str = "claims"                          # claims | edboox_http | …
    synced_at: datetime | None = None

    def get_path(self, path: str) -> Any:
        """Resolve a dotted path (``student.grade``) for config-driven bindings."""
        node: Any = self
        for part in path.split("."):
            node = getattr(node, part, None) if node is not None else None
        return node

    def prompt_facts(self) -> dict[str, Any]:
        """The minimal, non-identifying facts a prompt may use."""
        facts: dict[str, Any] = {}
        if self.student and self.student.grade:
            facts["grade"] = self.student.grade
        if self.language:
            facts["language"] = self.language
        if self.school and self.school.district:
            facts["district"] = self.school.district
        return facts

    def summary(self) -> dict[str, Any]:
        """What ``GET /me`` shows the UI: enough to drive pickers and defaults."""
        out: dict[str, Any] = {"source": self.source, "language": self.language,
                               "synced_at": self.synced_at}
        if self.school:
            out["school"] = self.school.model_dump()
        if self.student:
            out["student"] = self.student.model_dump()
        if self.teacher:
            out["teacher"] = {"assignments": [a.model_dump() for a in self.teacher.assignments],
                              "grades": self.teacher.grades, "subjects": self.teacher.subjects}
        return out
