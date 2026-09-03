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

# Formatting contract. The chat UI renders answers with a full Markdown + KaTeX
# + code-highlighting pipeline, so the model must emit ONE predictable syntax for
# each construct. Getting the model to produce clean, well-delimited output is
# what keeps rendering robust — the renderer is forgiving, but consistent input
# means math, tables and code always render instead of leaking raw markup.
_FORMATTING = (
    "Format your answer in GitHub-Flavored Markdown so it renders cleanly:\n"
    "- Structure with Markdown: `##`/`###` headings, `-` bullet lists, `1.` "
    "numbered lists, `**bold**`, `*italic*`, `> ` blockquotes, and Markdown "
    "tables for tabular data.\n"
    "- Write ALL mathematics as LaTeX, never as plain text or Unicode symbols. "
    "Use `$ ... $` for inline math (e.g. `$x^2 = 16$`) and `$$ ... $$` on their "
    "own lines for displayed equations (e.g. `$$x = \\frac{-b \\pm "
    "\\sqrt{b^2 - 4ac}}{2a}$$`). Do not put spaces just inside single-dollar "
    "delimiters, and use `\\times`, `\\div`, `\\le`, `\\ge`, `\\frac{}{}` "
    "rather than ×, ÷, ≤, ≥ or ad-hoc fractions.\n"
    "- Put code in fenced blocks with a language tag, like ```python.\n"
    "- Keep source citations as bare brackets like [1] or [2]; do not wrap them "
    "in code, math, or links."
)
_SYSTEM_CITED = (
    "You are LessonForge's curriculum assistant for Nepali school teachers. Answer "
    "the question using ONLY the numbered sources provided. Cite the sources you "
    "use inline with their bracketed number, like [1] or [2]. If the sources do "
    "not contain the answer, say so plainly and answer from general knowledge only "
    "if you flag it as unsourced. Be concise, accurate, and practical for a "
    "classroom teacher.\n\n" + _FORMATTING
)
_SYSTEM_PLAIN = (
    "You are LessonForge's curriculum assistant for Nepali school teachers. Use the "
    "provided context where relevant. Be concise, accurate, and practical.\n\n"
    + _FORMATTING
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
