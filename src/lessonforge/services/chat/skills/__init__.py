"""Chat skills. Importing this package registers every built-in skill — add a new
capability by dropping a module here and importing it below."""

from . import quiz  # noqa: F401  (registration side effect)
from .base import (
    QA,
    SKILL_REGISTRY,
    RoutedIntent,
    Skill,
    SkillContext,
    SkillDeps,
    SkillSpec,
    Trigger,
    build_skills,
    register_skill,
)

__all__ = [
    "QA", "SKILL_REGISTRY", "RoutedIntent", "Skill", "SkillContext", "SkillDeps",
    "SkillSpec", "Trigger", "build_skills", "register_skill",
]
