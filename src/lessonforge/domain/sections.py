"""The refine surface — which LDD sections a teacher can reprompt, as data.

This is the *format-extensibility seam* for AI-assisted editing. A section is
addressed by its top-level LDD field name; its schema is derived from the LDD
itself (never duplicated here), and its ``coupled`` neighbours declare the blast
radius of an edit — the fields the LDD's structural validators tie it to.

Editing a section can leave the whole document invalid even when the section
itself is well-formed: change ``objectives`` and the taught/assessed coverage
validators fire on ``phases`` and ``formative_checks``. Declaring that coupling
here lets the reviser repair the neighbours in a bounded, testable cascade
instead of hoping a whole-document rewrite silently keeps everything consistent.

A different source-of-truth document (a unit plan, a v2 LDD) ships its own
section map and the :class:`~lessonforge.services.refine.Refiner` is unchanged —
that is the whole point of keeping this declarative and separate from the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from .ldd import LessonDesignDocument

# The pseudo-target that refines the entire lesson at once — the escape hatch for
# a cross-cutting instruction ("make the whole lesson about the river festival")
# that no single section can express. Everything else is scoped output.
WHOLE_DOCUMENT = "*"


@dataclass(frozen=True)
class Section:
    """One reprompt-able slice of a source-of-truth document (LDD or an artifact IR).

    ``coupled`` names the neighbour sections a change here may invalidate through
    the document's structural validators; the reviser tries to repair them in order.
    A section with no coupled neighbours is a *leaf*: editing it can never break
    another section, so it is the zero-risk, single-round-trip case.

    ``grounded`` marks a section whose content is *factual* — editing it should
    re-retrieve grounding so an AI change stays anchored in the reference material
    and its citation stays real. Cosmetic sections (a title, instructions) set it
    ``False`` to skip the retrieval on a purely structural edit.
    """

    name: str
    coupled: tuple[str, ...] = ()
    grounded: bool = True

    @property
    def is_leaf(self) -> bool:
        return not self.coupled


# The editable surface of the LDD. Identity/framing fields (topic, curriculum_ref,
# duration) are deliberately excluded — those are the teacher's decisions, edited
# directly, not reprompted. Everything the model reasons about is here.
LDD_SECTIONS: dict[str, Section] = {
    "engagement_hook": Section("engagement_hook"),
    "objectives": Section("objectives", coupled=("phases", "formative_checks")),
    "misconceptions": Section("misconceptions"),
    "prior_knowledge": Section("prior_knowledge"),
    "local_context": Section("local_context"),
    "phases": Section("phases"),
    "materials": Section("materials"),
    "differentiation": Section("differentiation"),
    "formative_checks": Section("formative_checks"),
    "homework": Section("homework"),
}


def is_valid_target(target: str, sections: dict[str, Section] | None = None) -> bool:
    return target == WHOLE_DOCUMENT or target in (sections or LDD_SECTIONS)


# ── generic (any model) section helpers ──────────────────────────────────────
@cache
def _adapter_for(model: type[BaseModel], target: str) -> TypeAdapter[Any]:
    """A validator/schema for one section of ``model``, derived from that field's
    own type annotation so the section schema can never drift from the document."""
    return TypeAdapter(model.model_fields[target].annotation)


def fragment_schema_for(model: type[BaseModel], target: str) -> dict[str, Any]:
    return _adapter_for(model, target).json_schema()


def validate_fragment_for(model: type[BaseModel], target: str, fragment: Any) -> str:
    try:
        _adapter_for(model, target).validate_python(fragment)
        return ""
    except ValidationError as exc:
        return first_error(exc)


# ── LDD-specific wrappers (unchanged public API) ──────────────────────────────
def fragment_schema(target: str) -> dict[str, Any]:
    """JSON schema for a single LDD section's value — handed to the model so it
    emits exactly the shape that will splice back into the LDD."""
    return fragment_schema_for(LessonDesignDocument, target)


def validate_fragment(target: str, fragment: Any) -> str:
    """Layer-1 check: is this LDD section value well-formed on its own? Returns a
    short error string, or ``""`` when valid. Coupling is checked separately by
    re-validating the assembled whole document."""
    return validate_fragment_for(LessonDesignDocument, target, fragment)


def first_error(exc: ValidationError) -> str:
    """The first pydantic error as ``loc: msg`` — concise enough for a teacher."""
    errors = exc.errors()
    if not errors:
        return str(exc)
    e = errors[0]
    loc = ".".join(str(p) for p in e["loc"])
    msg = str(e["msg"])
    return f"{loc}: {msg}" if loc else msg
