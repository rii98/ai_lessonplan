"""Small shared utilities."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Parse JSON from an LLM response, tolerating common wrappers.

    Real models (esp. some Ollama Cloud models) wrap JSON in ```json fences or
    add prose around it despite a schema constraint. We try, in order:
      1. direct parse
      2. parse after stripping a surrounding markdown code fence
      3. parse the substring from the first '{' to the last '}'
    Raises ValueError if none succeed.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    unfenced = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(unfenced)
    except json.JSONDecodeError:
        pass

    start, end = unfenced.find("{"), unfenced.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(unfenced[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("could not extract JSON from LLM response")


# ── cheap lexical matching (learn-mode tracer, retrieval coverage, memory recall) ──
_WORD = re.compile(r"[\w\u0900-\u097F]+", re.IGNORECASE | re.UNICODE)
_STOP_WORDS = {"the", "a", "an", "of", "to", "in", "and", "is", "are", "it", "its", "that", "this",
               "for", "on", "with", "by", "from", "as", "be", "or", "their", "they", "them", "into"}


def words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(text)]


def _stem(w: str) -> str:
    return w[:5] if len(w) > 5 else w


def content_terms(text: str) -> set[str]:
    return {_stem(w) for w in words(text) if w not in _STOP_WORDS and len(w) > 2}


def overlap(a: str, b: str) -> float:
    """Share of ``a``'s content terms that appear in ``b`` (0..1)."""
    ta, tb = content_terms(a), content_terms(b)
    return len(ta & tb) / len(ta) if ta else 0.0


