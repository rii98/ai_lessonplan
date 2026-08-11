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
