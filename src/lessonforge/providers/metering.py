"""MeteredLLM — a decorator that sums every call's token usage into the current
request (§15 "Cost"), so activity events can report ``tokens_in``/``tokens_out``
per chat turn or generated lesson without any service knowing about billing.

Wraps any :class:`LLMClient`; adds nothing when no request is bound (CLI, evals).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from .. import request_context
from .base import LLMClient, LLMResult, LLMUsage


def _account(usage: LLMUsage | None) -> None:
    if usage is None:
        return
    ctx = request_context.current()
    if ctx is not None:
        ctx.usage.add(tokens_in=usage.tokens_in, tokens_out=usage.tokens_out, model=usage.model)


class MeteredLLM(LLMClient):
    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner

    def complete(self, prompt: str, *, system: str | None = None,
                 json_schema: dict[str, Any] | None = None,
                 temperature: float | None = None) -> LLMResult:
        result = self.inner.complete(prompt, system=system, json_schema=json_schema,
                                     temperature=temperature)
        _account(result.usage)
        return result

    def stream(self, prompt: str, *, system: str | None = None,
               temperature: float | None = None,
               on_usage: Callable[[LLMUsage], None] | None = None) -> Iterator[str]:
        def report(usage: LLMUsage) -> None:
            _account(usage)
            if on_usage is not None:
                on_usage(usage)

        yield from self.inner.stream(prompt, system=system, temperature=temperature,
                                     on_usage=report)

    def health(self) -> bool:
        return self.inner.health()

    @property
    def supports_structured_output(self) -> bool:
        return self.inner.supports_structured_output

    def __getattr__(self, name: str) -> Any:   # model name etc. for diagnostics
        return getattr(self.inner, name)
