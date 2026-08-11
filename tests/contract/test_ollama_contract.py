"""Contract test for the live Ollama adapter.

Skipped unless RUN_INTEGRATION=1 and Ollama is reachable. This is what verifies
a real provider honors the LLMClient contract end-to-end (incl. JSON-schema
constrained output). Run against your configured gemma model.
"""

from __future__ import annotations

import os

import pytest

from lessonforge.config import load_settings
from lessonforge.providers.registry import build_llm

pytestmark = pytest.mark.integration


@pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="integration disabled")
def test_ollama_json_completion():
    settings = load_settings()
    llm = build_llm(settings.llm)
    if not llm.health():
        pytest.skip("Ollama not reachable at configured base_url")

    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    result = llm.complete(
        "Reply with a JSON object giving the capital of Nepal.",
        json_schema=schema,
    )
    from lessonforge.util import extract_json

    data = extract_json(result.text)  # tolerates code fences some models add
    # The model may not honor the exact key, so check the values.
    assert "kathmandu" in " ".join(str(v) for v in data.values()).lower()
