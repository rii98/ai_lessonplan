"""AnswerSynthesizer — stream a grounded, cited answer from the retrieved context.

The last pipeline stage. Assembles the prompt (rolling summary + recent turns +
numbered grounding context + question) and streams the answer token-by-token via
:meth:`LLMClient.stream`. When ``require_citations`` is on, the model is told to
ground every claim in the numbered sources and cite them ``[n]`` — the same
provenance discipline the lesson generator enforces, adapted to free-form QA.

Synthesis owns prompt-building and streaming only; it does not retrieve, persist,
or number citations (the context builder did that) — one responsibility.
"""

from __future__ import annotations

from collections.abc import Iterator

from ...config import ChatSynthesisConfig
from ...domain.chat import Role
from ...providers.base import LLMClient
from .context import BuiltContext
from .memory import MemoryView

_SYSTEM_CITED = (
    "You are LessonForge's curriculum assistant for Nepali school teachers. Answer "
    "the question using ONLY the numbered sources provided. Cite the sources you "
    "use inline with their bracketed number, like [1] or [2]. If the sources do "
    "not contain the answer, say so plainly and answer from general knowledge only "
    "if you flag it as unsourced. Be concise, accurate, and practical for a "
    "classroom teacher."
)
_SYSTEM_PLAIN = (
    "You are LessonForge's curriculum assistant for Nepali school teachers. Use the "
    "provided context where relevant. Be concise, accurate, and practical."
)
_NO_CONTEXT = (
    "No grounding sources were retrieved for this question. Answer from general "
    "knowledge and state clearly that it is not grounded in the curriculum corpus."
)


class AnswerSynthesizer:
    def __init__(self, llm: LLMClient, config: ChatSynthesisConfig) -> None:
        self.llm = llm
        self.config = config

    def stream(
        self, question: str, context: BuiltContext, memory: MemoryView
    ) -> Iterator[str]:
        system = _SYSTEM_CITED if self.config.require_citations else _SYSTEM_PLAIN
        prompt = self._build_prompt(question, context, memory)
        yield from self.llm.stream(prompt, system=system, temperature=self.config.temperature)

    @staticmethod
    def _build_prompt(question: str, context: BuiltContext, memory: MemoryView) -> str:
        parts: list[str] = []
        if memory.summary:
            parts.append(f"Summary of earlier conversation:\n{memory.summary}")
        if memory.recent:
            convo = "\n".join(
                f"{m.role.value.capitalize()}: {m.content}"
                for m in memory.recent
                if m.role in (Role.user, Role.assistant)
            )
            if convo:
                parts.append(f"Recent conversation:\n{convo}")
        parts.append(context.text if not context.is_empty else _NO_CONTEXT)
        parts.append(f"Question: {question}\n\nAnswer:")
        return "\n\n".join(parts)
