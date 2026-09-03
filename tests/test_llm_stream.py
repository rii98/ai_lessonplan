"""Ollama streaming: NDJSON deltas are parsed into text pieces; the base
LLMClient default streams via a single complete() call (back-compat)."""

from __future__ import annotations

import httpx
import respx

from lessonforge.providers.base import LLMClient, LLMResult
from lessonforge.providers.llm.ollama import OllamaLLM


@respx.mock
def test_ollama_stream_parses_ndjson_deltas():
    lines = [
        '{"response": "Photo", "done": false}',
        '{"response": "synthesis", "done": false}',
        '{"response": ".", "done": true}',
    ]
    respx.post("http://ollama:11434/api/generate").mock(
        return_value=httpx.Response(200, text="\n".join(lines))
    )
    llm = OllamaLLM(model="m", base_url="http://ollama:11434")
    assert "".join(llm.stream("q")) == "Photosynthesis."


@respx.mock
def test_ollama_stream_sets_stream_flag():
    captured = {}

    def handler(request):
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(200, text='{"response": "hi", "done": true}')

    respx.post("http://ollama:11434/api/generate").mock(side_effect=handler)
    llm = OllamaLLM(model="m", base_url="http://ollama:11434")
    list(llm.stream("q", system="sys", temperature=0.1))
    assert captured["stream"] is True
    assert captured["system"] == "sys"
    assert captured["options"]["temperature"] == 0.1


def test_base_client_default_stream_uses_complete():
    class OneShot(LLMClient):
        def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
            return LLMResult(text="whole answer")

        def health(self):
            return True

    assert list(OneShot().stream("q")) == ["whole answer"]
