"""Recall — the read path's scoring, kept free of storage and of the LLM.

**Snapshot cache.** A turn must not re-read and re-parse a learner's whole memory (every
row's embedding) from the database. :class:`SnapshotCache` keeps one immutable
:class:`Snapshot` per learner — the live items plus a pre-normalised vector matrix —
keyed by the store's ``version``. A read costs one tiny state lookup; the item rows are
fetched again only after something changed, in *any* replica (the version lives in the
database). Bounded LRU, thread-safe.

**Hybrid relevance.** ``relevance = max(dense, lexical)``, each gated by its own floor:
dense cosine catches paraphrase ("footy" ≈ "football"), content-term overlap catches
exact terms a small embedder blurs, and either alone still works (no embedder, or an item
embedded by a different model — vectors from different models are never compared).

**Ranking.** ``score = w_rel·relevance + w_rec·recency + w_imp·importance`` with recency
an exponential half-life on when the fact became true, then a diversity pass drops
near-duplicates, then a character budget.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ...domain.memory import MemoryItem
from ...providers.base import Embedder
from ...util import overlap

try:  # numpy is present with fastembed; pure Python keeps the module importable without it
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None


def embedder_id(embedder: Embedder | None) -> str | None:
    """A stable identity for "vectors from this embedder are comparable"."""
    if embedder is None:
        return None
    name = getattr(embedder, "model_name", None) or type(embedder).__name__
    try:
        return f"{name}:{embedder.dim}"
    except Exception:
        return str(name)


def lexical(a: str, b: str) -> float:
    return max(overlap(a, b), overlap(b, a))


def _unit_vec(v: Sequence[float]) -> list[float] | None:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else None


@dataclass(slots=True)
class Snapshot:
    version: int
    items: list[MemoryItem]
    model: str | None
    _rows: dict[str, int] = field(default_factory=dict)
    _matrix: object = None          # numpy (n, d) of unit vectors, or list[list[float]]

    @classmethod
    def build(cls, version: int, items: list[MemoryItem], model: str | None) -> Snapshot:
        snap = cls(version=version, items=items, model=model)
        vecs = []
        for it in items:
            if model and it.embedding and it.embedding_model == model:
                u = _unit_vec(it.embedding)
                if u is not None and (not vecs or len(u) == len(vecs[0])):
                    snap._rows[it.id] = len(vecs)
                    vecs.append(u)
        if vecs:
            snap._matrix = _np.asarray(vecs, dtype=_np.float32) if _np is not None else vecs
        return snap

    @property
    def has_vectors(self) -> bool:
        return self._matrix is not None

    def dense(self, query_vec: Sequence[float] | None) -> dict[str, float]:
        """Cosine of the query against every comparable item (one mat-vec)."""
        if query_vec is None or self._matrix is None:
            return {}
        q = _unit_vec(query_vec)
        if q is None:
            return {}
        if _np is not None:
            m = self._matrix
            if m.shape[1] != len(q):
                return {}
            sims = m @ _np.asarray(q, dtype=_np.float32)
            return {iid: float(sims[r]) for iid, r in self._rows.items()}
        rows = self._matrix
        if len(rows[0]) != len(q):
            return {}
        return {iid: sum(a * b for a, b in zip(rows[r], q, strict=False)) for iid, r in self._rows.items()}

    def pair(self, a: str, b: str) -> float | None:
        """Cosine between two cached items (``None`` if either has no comparable vector)."""
        ra, rb = self._rows.get(a), self._rows.get(b)
        if ra is None or rb is None:
            return None
        va, vb = self._matrix[ra], self._matrix[rb]
        return float(sum(x * y for x, y in zip(va, vb, strict=False)))


class SnapshotCache:
    def __init__(self, max_owners: int = 2048) -> None:
        self.max_owners = max(1, max_owners)
        self._data: OrderedDict[str, Snapshot] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, owner_id: str, version: int, model: str | None) -> Snapshot | None:
        with self._lock:
            snap = self._data.get(owner_id)
            if snap is None or snap.version != version or snap.model != model:
                return None
            self._data.move_to_end(owner_id)
            return snap

    def put(self, owner_id: str, snap: Snapshot) -> None:
        with self._lock:
            self._data[owner_id] = snap
            self._data.move_to_end(owner_id)
            while len(self._data) > self.max_owners:
                self._data.popitem(last=False)

    def drop(self, owner_id: str) -> None:
        with self._lock:
            self._data.pop(owner_id, None)


@dataclass(slots=True)
class Scoring:
    dense_min: float = 0.65
    lexical_min: float = 0.25
    w_relevance: float = 0.6
    w_recency: float = 0.2
    w_importance: float = 0.2
    half_life_days: float = 30.0
    diversity: float = 0.9          # drop an item this similar to one already chosen


def relevance(text: str, items: Iterable[MemoryItem], dense: dict[str, float],
              s: Scoring) -> dict[str, float]:
    """Hybrid relevance per item; items below both floors are absent."""
    out: dict[str, float] = {}
    for it in items:
        d = dense.get(it.id, 0.0)
        lx = lexical(text, it.content)
        rel = max(d if d >= s.dense_min else 0.0, lx if lx >= s.lexical_min else 0.0)
        if rel > 0:
            out[it.id] = rel
    return out


def recency(item: MemoryItem, now: datetime, half_life_days: float) -> float:
    ref = item.valid_from or item.created_at
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    days = max(0.0, (now - ref).total_seconds() / 86400)
    return math.exp(-math.log(2) * days / max(half_life_days, 0.1))


def rank(items: list[MemoryItem], rel: dict[str, float], snap: Snapshot | None, s: Scoring, *,
         k: int, now: datetime) -> list[MemoryItem]:
    scored = sorted(
        ((s.w_relevance * rel[i.id] + s.w_recency * recency(i, now, s.half_life_days)
          + s.w_importance * i.importance, i) for i in items if i.id in rel),
        key=lambda t: t[0], reverse=True)
    chosen: list[MemoryItem] = []
    for _, it in scored:
        if len(chosen) >= k:
            break
        if any(_too_similar(it, c, snap, s.diversity) for c in chosen):
            continue
        chosen.append(it)
    return chosen


def _too_similar(a: MemoryItem, b: MemoryItem, snap: Snapshot | None, threshold: float) -> bool:
    cos = snap.pair(a.id, b.id) if snap is not None else None
    if cos is not None and cos >= threshold:
        return True
    return lexical(a.content, b.content) >= 0.8


def within_budget(lines: Iterable[str], budget: int) -> list[str]:
    out, used = [], 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        out.append(line)
        used += len(line) + 1
    return out
