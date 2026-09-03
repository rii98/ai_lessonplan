"""Layer 1 of the model→domain boundary: deterministic, meaning-preserving
normalization of raw LLM output *before* it meets the strict LDD schema.

The problem this solves is a whole class, not one field: a probabilistic model
emits values a closed schema rejects — ``kind: "analogy"``, ``bloom:
"understanding"``, ``type: "multiple choice"``, ``minutes: "8"``, a string where
a list is expected — and a single deviation hard-fails the entire document. This
layer snaps those *cosmetic* deviations back onto the legal surface so validation
(and the expensive repair loop above it) only has to deal with genuinely
*semantic* problems.

Two hard rules keep it honest:

1. **Only meaning-preserving changes.** Snapping ``"analogy" → "scenario"`` relabels;
   it invents no pedagogy. Anything that would fabricate teaching content (a
   missing assessment, an unaddressed objective) is deliberately NOT touched here
   — that belongs to the model-driven repair loop.
2. **It can never throw.** Every transform is guarded; on anything unexpected it
   leaves the data untouched and lets validation speak. A normalization bug must
   never be able to sink a request that would otherwise have succeeded.

Allowed values are derived from the models themselves (``_allowed``), so this
layer can never drift out of sync with the schema it defends.
"""

from __future__ import annotations

import copy
import difflib
import re
from enum import Enum
from typing import Any, get_args

from ..domain.ldd import (
    CurriculumRef,
    Hook,
    LessonDesignDocument,
    Objective,
    Question,
)


# ── deriving the legal surface from the models (drift-proof) ──────────────────
def _allowed(model: type, field: str) -> list[Any]:
    """The legal values for an enum/Literal field, straight from the model."""
    ann = model.model_fields[field].annotation
    out: list[Any] = []
    for a in get_args(ann):
        if a is type(None):
            continue
        out.append(a.value if isinstance(a, Enum) else a)
    if not out and isinstance(ann, type) and issubclass(ann, Enum):
        out = [e.value for e in ann]
    return out


KINDS = _allowed(Hook, "kind")
BLOOMS = _allowed(Objective, "bloom")
Q_TYPES = _allowed(Question, "type")
BOARDS = _allowed(CurriculumRef, "board")
LANGUAGES = _allowed(LessonDesignDocument, "language")
FRAMEWORKS = _allowed(LessonDesignDocument, "framework")
DURATIONS = _allowed(LessonDesignDocument, "duration_min")  # [30, 45, 60]

# Synonyms a model reaches for, keyed by the *normalized* token (see ``_norm``).
_SYNONYMS: dict[str, dict[str, str]] = {
    "kind": {
        "analogy": "scenario", "story": "scenario", "example": "scenario",
        "real_world": "scenario", "roleplay": "scenario", "role_play": "scenario",
        "hypothetical": "scenario", "demo": "demonstration", "experiment": "demonstration",
        "activity": "demonstration", "hands_on": "demonstration", "predict": "prediction",
        "hypothesis": "prediction", "guess": "prediction", "questioning": "question",
        "discussion": "question", "inquiry": "question", "brainstorm": "question",
    },
    "bloom": {
        "understanding": "understand", "comprehend": "understand", "comprehension": "understand",
        "knowledge": "remember", "recall": "remember", "memorize": "remember",
        "applying": "apply", "application": "apply", "use": "apply",
        "analysis": "analyze", "analyse": "analyze", "analysing": "analyze", "analyzing": "analyze",
        "evaluation": "evaluate", "assess": "evaluate", "judge": "evaluate",
        "creating": "create", "synthesis": "create", "synthesize": "create", "design": "create",
    },
    "type": {
        "multiple_choice": "mcq", "mc": "mcq", "choice": "mcq", "select": "mcq",
        "true_or_false": "true_false", "truefalse": "true_false", "tf": "true_false",
        "t_f": "true_false", "boolean": "true_false", "yes_no": "true_false",
        "short": "short_answer", "open": "short_answer", "open_ended": "short_answer",
        "essay": "short_answer", "written": "short_answer", "fill_in_the_blank": "short_answer",
        "fill_in_the_blanks": "short_answer", "fill": "short_answer", "text": "short_answer",
    },
    "board": {
        "curriculum_development_centre": "CDC", "curriculum_development_center": "CDC",
        "national_examination_board": "NEB", "national_examinations_board": "NEB",
    },
    "language": {
        "english": "en", "eng": "en", "nepali": "ne", "nep": "ne",
        "bilingual": "en-ne", "en_ne": "en-ne", "english_nepali": "en-ne", "both": "en-ne",
    },
    "framework": {
        "five_e": "5E", "5_e": "5E", "5es": "5E", "gradual": "gradual_release",
        "gradual_release_of_responsibility": "gradual_release", "grr": "gradual_release",
        "release": "gradual_release", "inquiry_based": "inquiry", "inquiry_based_learning": "inquiry",
        "discovery": "inquiry",
    },
}


def _norm(s: str) -> str:
    """Lowercase, collapse separators to ``_``, strip surrounding punctuation —
    so ``"Multiple-Choice!"`` and ``"multiple choice"`` compare equal."""
    s = s.strip().lower()
    s = re.sub(r"[\s\-/]+", "_", s)
    s = re.sub(r"[^a-z0-9_]+", "", s)
    return s.strip("_")


def _snap(value: Any, allowed: list[str], default: str, syn_key: str) -> tuple[str, bool]:
    """Snap ``value`` onto the nearest legal enum member. Returns ``(canonical,
    changed)``. Tries: exact → normalized-equal → synonym → substring → fuzzy →
    default. Meaning-preserving: it only ever relabels to the closest legal term."""
    if isinstance(value, str) and value in allowed:
        return value, False
    if not isinstance(value, (str, int, float, bool)):
        return default, True
    n = _norm(str(value))
    if not n:
        return default, True
    by_norm = {_norm(a): a for a in allowed}
    if n in by_norm:
        return by_norm[n], by_norm[n] != value
    syn = _SYNONYMS.get(syn_key, {})
    if n in syn:
        return syn[n], True
    for na, a in by_norm.items():  # substring either direction ("quiz_question" → question)
        if na in n or n in na:
            return a, True
    close = difflib.get_close_matches(n, list(by_norm), n=1, cutoff=0.6)
    if close:
        return by_norm[close[0]], True
    return default, True


def _to_int(value: Any) -> int | None:
    """Best-effort int from ``8``, ``8.0``, ``"8"``, ``"8 minutes"``. ``None`` if
    no integer is recoverable — the schema/repair loop then handles it."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        m = re.search(r"-?\d+", value)
        if m:
            return int(m.group())
    return None


def _snap_field(obj: dict, key: str, allowed: list[str], default: str, syn: str,
                where: str, notes: list[str]) -> None:
    if not isinstance(obj, dict) or key not in obj:
        return
    new, changed = _snap(obj[key], allowed, default, syn)
    if changed:
        notes.append(f"{where}.{key}: {obj[key]!r} → {new!r}")
        obj[key] = new


def _wrap_list(obj: dict, key: str, notes: list[str], where: str, split_csv: bool = False) -> None:
    """Coerce a scalar into a list for a list-typed field. IDs may be comma-split;
    prose is only wrapped (never split — a comma inside an activity is not a
    delimiter)."""
    if not isinstance(obj, dict) or key not in obj:
        return
    v = obj[key]
    if isinstance(v, list) or v is None:
        return
    if isinstance(v, str) and split_csv and "," in v:
        obj[key] = [p.strip() for p in v.split(",") if p.strip()]
    else:
        obj[key] = [v]
    notes.append(f"{where}.{key}: wrapped scalar into a list")


# ── the public entry point ────────────────────────────────────────────────────
def normalize_ldd(data: Any) -> tuple[Any, list[str]]:
    """Return ``(normalized_data, notes)``. Never raises: on any unexpected shape
    it returns the input untouched and lets validation report the problem."""
    notes: list[str] = []
    if not isinstance(data, dict):
        return data, notes
    try:
        d = copy.deepcopy(data)
        _norm_top(d, notes)
        _norm_curriculum(d, notes)
        _norm_hook(d, notes)
        _norm_objectives(d, notes)
        _norm_phases(d, notes)
        _norm_checks(d, notes)
        _norm_homework(d, notes)
        _norm_top_prose_lists(d, notes)
        return d, notes
    except Exception:  # a normalization bug must never sink an otherwise-fine request
        return data, notes


def _norm_top(d: dict, notes: list[str]) -> None:
    _snap_field(d, "language", LANGUAGES, "en-ne", "language", "ldd", notes)
    _snap_field(d, "framework", FRAMEWORKS, "5E", "framework", "ldd", notes)
    if "duration_min" in d:
        n = _to_int(d["duration_min"])
        if n is not None and n not in DURATIONS:
            nearest = min(DURATIONS, key=lambda a: abs(a - n))
            notes.append(f"ldd.duration_min: {d['duration_min']!r} → {nearest}")
            d["duration_min"] = nearest
        elif n is not None and n != d["duration_min"]:
            d["duration_min"] = n


def normalize_curriculum_ref(cr: Any, notes: list[str], where: str = "curriculum_ref") -> None:
    """Snap a curriculum_ref's board onto CDC/NEB and coerce grade to int, in place.
    Shared by the LDD normalizer and the artifact normalizer (quiz/worksheet)."""
    if not isinstance(cr, dict):
        return
    _snap_field(cr, "board", BOARDS, "CDC", "board", where, notes)
    if "grade" in cr:
        n = _to_int(cr["grade"])
        if n is not None and n != cr["grade"]:
            notes.append(f"{where}.grade: {cr['grade']!r} → {n}")
            cr["grade"] = n


def normalize_question(q: Any, notes: list[str], where: str) -> None:
    """Snap one question's type onto the legal set, wrap scalar objective_ids into
    a list, and clear options on non-MCQs — in place. Shared by the LDD normalizer
    and the artifact normalizer so a standalone quiz gets the same defenses."""
    if not isinstance(q, dict):
        return
    _snap_field(q, "type", Q_TYPES, "short_answer", "type", where, notes)
    _wrap_list(q, "objective_ids", notes, where, split_csv=True)
    if q.get("type") != "mcq" and q.get("options") is not None:
        q["options"] = None
        notes.append(f"{where}.options: cleared (not an MCQ)")


def _norm_curriculum(d: dict, notes: list[str]) -> None:
    normalize_curriculum_ref(d.get("curriculum_ref"), notes)


def _norm_hook(d: dict, notes: list[str]) -> None:
    _snap_field(d.get("engagement_hook"), "kind", KINDS, "question", "kind",
                "engagement_hook", notes)


def _norm_objectives(d: dict, notes: list[str]) -> None:
    for i, o in enumerate(d.get("objectives") or []):
        _snap_field(o, "bloom", BLOOMS, "understand", "bloom", f"objectives[{i}]", notes)


def _norm_phases(d: dict, notes: list[str]) -> None:
    for i, p in enumerate(d.get("phases") or []):
        if not isinstance(p, dict):
            continue
        if "minutes" in p:
            n = _to_int(p["minutes"])
            if n is not None and n != p["minutes"]:
                notes.append(f"phases[{i}].minutes: {p['minutes']!r} → {n}")
                p["minutes"] = n
        _wrap_list(p, "teacher_activities", notes, f"phases[{i}]")
        _wrap_list(p, "student_activities", notes, f"phases[{i}]")
        _wrap_list(p, "objective_ids", notes, f"phases[{i}]", split_csv=True)


def _norm_checks(d: dict, notes: list[str]) -> None:
    for i, q in enumerate(d.get("formative_checks") or []):
        normalize_question(q, notes, f"formative_checks[{i}]")


def _norm_homework(d: dict, notes: list[str]) -> None:
    hw = d.get("homework")
    if not isinstance(hw, dict):
        return
    _wrap_list(hw, "instructions", notes, "homework")
    _wrap_list(hw, "objective_ids", notes, "homework", split_csv=True)


def normalize_artifact(data: Any) -> tuple[Any, list[str]]:
    """Light normalizer for the standalone artifact IRs (quiz/worksheet/slides):
    snap the curriculum_ref board, normalize each question, and wrap a scalar
    ``tasks`` into a list. Reuses the LDD normalizer's helpers so a targeted
    artifact gets the same meaning-preserving defenses. Never raises."""
    notes: list[str] = []
    if not isinstance(data, dict):
        return data, notes
    try:
        d = copy.deepcopy(data)
        normalize_curriculum_ref(d.get("curriculum_ref"), notes)
        for i, q in enumerate(d.get("questions") or []):
            normalize_question(q, notes, f"questions[{i}]")
        _wrap_list(d, "tasks", notes, "worksheet")
        return d, notes
    except Exception:
        return data, notes


def _norm_top_prose_lists(d: dict, notes: list[str]) -> None:
    for key in ("prior_knowledge", "local_context", "materials"):
        _wrap_list(d, key, notes, "ldd")
    diff = d.get("differentiation")
    if isinstance(diff, dict):
        for key in ("struggling", "on_level", "advanced"):
            _wrap_list(diff, key, notes, "differentiation")
