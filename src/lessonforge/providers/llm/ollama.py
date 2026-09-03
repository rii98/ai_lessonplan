"""Ollama LLM adapter (works with local models and Ollama Cloud models).

Talks to the Ollama HTTP API (`/api/generate`). For structured output it passes
the JSON schema via Ollama's ``format`` field so the model is constrained to
valid JSON; the caller still validates against the Pydantic model.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from ...config import LLMConfig
from ..base import LLMClient, LLMResult
from ..registry import register_llm


@register_llm("ollama")
class OllamaLLM(LLMClient):
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        temperature: float = 0.7,
        timeout_s: int = 180,
        max_retries: int = 2,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    @classmethod
    def from_config(cls, cfg: LLMConfig) -> OllamaLLM:
        return cls(
            model=cfg.model,
            base_url=cfg.base_url,
            temperature=cfg.temperature,
            timeout_s=cfg.timeout_s,
            max_retries=cfg.max_retries,
        )

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> LLMResult:
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature if temperature is None else temperature
            },
        }
        if system:
            body["system"] = system
        if json_schema is not None:
            # Ollama accepts a JSON schema object in `format` to constrain output.
            body["format"] = json_schema

        last_exc: Exception | None = None
        for _ in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    resp = client.post(f"{self.base_url}/api/generate", json=body)
                    resp.raise_for_status()
                    data = resp.json()
                    return LLMResult(text=data.get("response", ""), raw=data)
            except (httpx.HTTPError, json.JSONDecodeError) as exc:  # pragma: no cover - network
                last_exc = exc
        raise RuntimeError(f"Ollama request failed after retries: {last_exc}") from last_exc

    def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
    ) -> Iterator[str]:
        """Stream free-form answer text as NDJSON deltas from Ollama's
        ``/api/generate`` (``stream: true``). Each line is a JSON object with a
        ``response`` fragment; the final line carries ``done: true``. Network
        errors surface as a RuntimeError so the caller can emit an error event —
        streaming has no retry loop (a partially-sent answer can't be replayed)."""
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": True,
            "options": {
                "temperature": self.temperature if temperature is None else temperature
            },
        }
        if system:
            body["system"] = system
        try:
            with (
                httpx.Client(timeout=self.timeout_s) as client,
                client.stream("POST", f"{self.base_url}/api/generate", json=body) as resp,
            ):
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:  # pragma: no cover - defensive
                        continue
                    piece = chunk.get("response")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        break
        except httpx.HTTPError as exc:  # pragma: no cover - network
            raise RuntimeError(f"Ollama stream failed: {exc}") from exc

    def health(self) -> bool:
        try:
            with httpx.Client(timeout=5) as client:
                return client.get(f"{self.base_url}/api/tags").status_code == 200
        except httpx.HTTPError:  # pragma: no cover - network
            return False
