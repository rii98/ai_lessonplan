"""MemoryReconciler — decide how candidate facts change the store.

Stage 2 of the write path (extract → policy → **reconcile** → policy → apply). For each
candidate the service gathers the learner's existing memories most similar to *that
candidate* (one batched embedding call for the whole set), and the reconciler emits:

- ``add``     genuinely new
- ``update``  refines / extends / supersedes one existing memory → the MERGED fact
- ``retire``  an existing memory is now wrong (a reversed preference, a resolved need);
              any new information the candidate carries is a separate ``add``
- (no op)     redundant — nothing to do

This is what keeps the store converging to *one current statement per fact* instead of
accumulating near-duplicates and contradictions (Mem0's ADD/UPDATE/DELETE/NOOP).

Robustness:
- the model sees short aliases (``c1``, ``m2``), never real ids, so it cannot invent a
  target; any alias it makes up is dropped;
- with no neighbours at all there is nothing to reconcile: every candidate is an add and
  no model is called;
- malformed output is retried once, then the ``rules`` reconciler takes over (add unless a
  near-exact duplicate exists) so writes never stop when the model is down;
- something the learner explicitly asked us to remember is only rewritten or retired by
  another explicit request.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ...domain.memory import Candidate, MemoryItem, MemoryOp
from ...providers.base import LLMClient
from ...util import extract_json

log = logging.getLogger("lessonforge.memory.reconcile")

Neighbours = list[list[tuple[MemoryItem, float]]]   # per candidate: (existing item, similarity)


class MemoryReconciler(ABC):
    @abstractmethod
    def reconcile(self, candidates: list[Candidate], neighbours: Neighbours) -> list[MemoryOp]:
        """Ops for the candidates. ``neighbours[i]`` belongs to ``candidates[i]``."""


def _add(c: Candidate, content: str | None = None) -> MemoryOp:
    return MemoryOp(op="add", kind=c.kind, content=(content or c.content).strip(),
                    confidence=c.confidence, importance=c.importance, explicit=c.explicit)


class RulesReconciler(MemoryReconciler):
    """Degraded mode and no-LLM deployments: add unless a near-exact duplicate exists."""

    def __init__(self, *, duplicate_similarity: float = 0.92) -> None:
        self.duplicate_similarity = duplicate_similarity

    def reconcile(self, candidates, neighbours):
        ops = []
        for i, c in enumerate(candidates):
            best = max((s for _, s in (neighbours[i] if i < len(neighbours) else [])), default=0.0)
            if best < self.duplicate_similarity:
                ops.append(_add(c))
        return ops


_SYSTEM = (
    "You keep a student's memory accurate and free of duplicates. You get NEW candidate facts "
    "and the EXISTING memories most similar to them. For each candidate output operations:\n"
    '- {"candidate": "c1", "op": "add"} — genuinely new (optionally "content": refined wording)\n'
    '- {"candidate": "c1", "op": "update", "target": "m2", "content": "<merged fact>"} — the '
    "candidate refines, extends or supersedes ONE existing memory about the SAME thing; content is "
    "the single up-to-date sentence (keep what is still true)\n"
    '- {"candidate": "c1", "op": "retire", "target": "m2"} — the candidate proves m2 is now WRONG '
    "(a reversed preference, a difficulty that is resolved). If the candidate also carries new "
    "information, add a separate add op for it.\n"
    "- nothing for a candidate that adds nothing new.\n"
    "Never update or retire a memory that is not clearly about the same thing. Prefer update over "
    "add when they are about the same thing: one memory per fact. Keep every sentence short, third "
    "person and self-contained.\n"
    'Output ONLY JSON: {"operations": [...]}'
)
_EXAMPLE = (
    "Example\nEXISTING:\nm1: [preference] wants answers in Nepali\nm2: [interest] likes cricket\n"
    "NEW:\nc1: [preference] wants answers in English\nc2: [interest] likes cricket and uses it to "
    "remember statistics\nc3: [goal] is preparing for the SEE exam\n"
    'JSON: {"operations": [{"candidate": "c1", "op": "retire", "target": "m1"}, '
    '{"candidate": "c1", "op": "add"}, {"candidate": "c2", "op": "update", "target": "m2", '
    '"content": "likes cricket; cricket statistics help them learn"}, '
    '{"candidate": "c3", "op": "add"}]}\n'
)


class LLMReconciler(MemoryReconciler):
    def __init__(self, llm: LLMClient, *, fallback: MemoryReconciler | None = None,
                 attempts: int = 2) -> None:
        self.llm = llm
        self.fallback = fallback or RulesReconciler()
        self.attempts = max(1, attempts)

    def reconcile(self, candidates, neighbours):
        if not candidates:
            return []
        existing: dict[str, MemoryItem] = {}
        for hits in neighbours:
            for item, _ in hits:
                existing.setdefault(item.id, item)
        if not existing:
            return [_add(c) for c in candidates]          # nothing to conflict with
        m_keys = {f"m{n + 1}": item for n, item in enumerate(existing.values())}
        c_keys = {f"c{n + 1}": c for n, c in enumerate(candidates)}
        prompt = (_EXAMPLE + "\nEXISTING:\n"
                  + "\n".join(f"{k}: [{m.kind}] {m.content}" for k, m in m_keys.items())
                  + "\nNEW:\n" + "\n".join(f"{k}: [{c.kind}] {c.content}" for k, c in c_keys.items())
                  + "\nJSON:")
        for attempt in range(1, self.attempts + 1):
            try:
                data = extract_json(self.llm.complete(prompt, system=_SYSTEM, temperature=0.0).text)
                raw = data.get("operations") if isinstance(data, dict) else None
                if not isinstance(raw, list):
                    raise TypeError("no operations list")
                return self._validate(raw, c_keys, m_keys)
            except Exception as e:
                log.warning("memory reconcile attempt %d failed: %s", attempt, e)
        log.warning("memory reconcile fell back to rules")
        return self.fallback.reconcile(candidates, neighbours)

    @staticmethod
    def _validate(raw: list, c_keys: dict[str, Candidate], m_keys: dict[str, MemoryItem]) -> list[MemoryOp]:
        ops: list[MemoryOp] = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            c = c_keys.get(str(r.get("candidate", "")))
            target = m_keys.get(str(r.get("target", "")))
            op = str(r.get("op", "")).strip().lower()
            if op in ("delete", "remove"):
                op = "retire"
            content = " ".join(str(r.get("content") or "").split())
            if c is None:
                continue
            if op == "add":
                ops.append(_add(c, content))
            elif op == "update":
                if target is None:
                    ops.append(_add(c))                   # an update with no real target is new
                elif target.source.get("explicit") and not c.explicit:
                    continue                              # only the learner rewrites their own request
                else:
                    ops.append(MemoryOp(op="update", target_id=target.id, kind=c.kind,
                                        content=content or c.content, confidence=c.confidence,
                                        importance=c.importance, explicit=c.explicit))
            elif op == "retire" and target is not None:
                if target.source.get("explicit") and not c.explicit:
                    continue
                ops.append(MemoryOp(op="retire", target_id=target.id))
        return ops


def build_reconciler(provider: str, *, llm: LLMClient | None,
                     duplicate_similarity: float = 0.92) -> MemoryReconciler:
    rules = RulesReconciler(duplicate_similarity=duplicate_similarity)
    if provider == "rules" or llm is None:
        return rules
    if provider == "llm":
        return LLMReconciler(llm, fallback=rules)
    raise ValueError(f"unknown memory reconciler {provider!r}; known: ['llm', 'rules']")
