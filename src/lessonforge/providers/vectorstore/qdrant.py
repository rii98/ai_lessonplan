"""Qdrant vector-store adapter.

Collection names are namespaced with ``collection_prefix`` so multiple logical
corpora (curriculum, pedagogical, exemplars, ...) live in one Qdrant instance
without collision. The client is imported lazily to keep unit tests light.

Knowledge spaces (§16) use placement ``shared``: every space lives in the same
collection, isolated by the ``_space`` payload key — a keyword index created with
``is_tenant=True`` (Qdrant's built-in multitenancy optimisation, which co-locates a
tenant's vectors). Every read/delete ANDs ``_space ∈ scope`` into the filter.
"""

from __future__ import annotations

import uuid
from typing import Any

from ...config import VectorStoreConfig
from ...knowledge.scope import SPACE_KEY, AccessScope
from ...rag.documents import DOC_ID_KEY
from ..base import ScoredRecord, SparseVector, StoredRecord, VectorRecord, VectorStore
from ..registry import register_vector_store

# Qdrant requires point IDs to be unsigned ints or UUIDs. Our interface uses
# arbitrary string IDs (e.g. content hashes), so we map each to a stable UUID5
# and keep the original under this payload key for round-tripping.
_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")
_SOURCE_ID_KEY = "_source_id"

# Named-vector keys for hybrid collections. Dense-only collections (created before
# hybrid, or with sparse=False) use Qdrant's unnamed vector for back-compat.
_DENSE = "dense"
_SPARSE = "text"


def _point_id(raw: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, raw))


@register_vector_store("qdrant")
class QdrantVectorStore(VectorStore):
    def __init__(self, url: str, api_key: str | None, collection_prefix: str) -> None:
        self.url = url
        self.api_key = api_key or None
        self.prefix = collection_prefix
        self._client = None  # lazy
        self._named: dict[str, bool] = {}  # cache: does collection use named vectors?
        self._indexed: set[str] = set()     # collections whose payload indexes exist

    @classmethod
    def from_config(cls, cfg: VectorStoreConfig) -> QdrantVectorStore:
        return cls(url=cfg.url, api_key=cfg.api_key, collection_prefix=cfg.collection_prefix)

    @property
    def supports_hybrid(self) -> bool:
        return True

    def _c(self):
        if self._client is None:
            from qdrant_client import QdrantClient

            self._client = QdrantClient(url=self.url, api_key=self.api_key)
        return self._client

    def _name(self, name: str) -> str:
        return f"{self.prefix}_{name}"

    def _is_named(self, full: str) -> bool:
        """Whether ``full`` uses the named dense+sparse layout (hybrid) vs Qdrant's
        unnamed vector (a dense-only collection, incl. ones created before hybrid).
        Detected from the live config once, then cached."""
        if full not in self._named:
            try:
                vectors = self._c().get_collection(full).config.params.vectors
                self._named[full] = isinstance(vectors, dict)  # named → {name: params}
            except Exception:
                self._named[full] = False
        return self._named[full]

    def ensure_collection(self, name: str, dim: int, *, sparse: bool = False) -> None:
        from qdrant_client.models import (
            Distance,
            SparseVectorParams,
            VectorParams,
        )

        client = self._c()
        full = self._name(name)
        if client.collection_exists(full):
            self._ensure_indexes(full)
            return
        if sparse:
            client.create_collection(
                collection_name=full,
                vectors_config={_DENSE: VectorParams(size=dim, distance=Distance.COSINE)},
                sparse_vectors_config={_SPARSE: SparseVectorParams()},
            )
            self._named[full] = True
        else:
            client.create_collection(
                collection_name=full,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
            self._named[full] = False
        self._ensure_indexes(full)

    def _ensure_indexes(self, full: str) -> None:
        """Keyword indexes on ``_space`` (tenant-optimised) and ``doc_id``. Idempotent;
        done once per collection per process."""
        if full in self._indexed:
            return
        from qdrant_client.models import KeywordIndexParams, PayloadSchemaType

        client = self._c()
        client.create_payload_index(
            collection_name=full, field_name=SPACE_KEY,
            field_schema=KeywordIndexParams(type="keyword", is_tenant=True),
        )
        client.create_payload_index(collection_name=full, field_name=DOC_ID_KEY,
                                    field_schema=PayloadSchemaType.KEYWORD)
        self._indexed.add(full)

    def upsert(self, name: str, records: list[VectorRecord]) -> None:
        from qdrant_client.models import PointStruct
        from qdrant_client.models import SparseVector as QSparse

        if not records:
            return
        missing = [r.id for r in records if not r.payload.get(SPACE_KEY)]
        if missing:
            raise ValueError(f"points without a knowledge space ({SPACE_KEY}): {missing[:3]}…")
        full = self._name(name)
        named = self._is_named(full)
        points = []
        for r in records:
            if named:
                vec: Any = {_DENSE: r.vector}
                if r.sparse_vector is not None:
                    vec[_SPARSE] = QSparse(
                        indices=r.sparse_vector.indices, values=r.sparse_vector.values
                    )
            else:
                vec = r.vector
            points.append(
                PointStruct(id=_point_id(r.id), vector=vec, payload={**r.payload, _SOURCE_ID_KEY: r.id})
            )
        self._c().upsert(collection_name=full, points=points)

    def search(
        self,
        name: str,
        vector: list[float],
        top_k: int,
        where: dict[str, Any] | None = None,
        *,
        scope: AccessScope,
    ) -> list[ScoredRecord]:
        if _denies_all(scope):
            return []
        full = self._name(name)
        using = _DENSE if self._is_named(full) else None
        hits = self._c().query_points(
            collection_name=full,
            query=vector,
            using=using,
            limit=top_k,
            query_filter=self._build_filter(where, scope),
            with_payload=True,
        ).points
        return [self._scored(h) for h in hits]

    def hybrid_search(
        self,
        name: str,
        dense_vector: list[float],
        sparse_vector: SparseVector,
        top_k: int,
        where: dict[str, Any] | None = None,
        *,
        scope: AccessScope,
    ) -> list[ScoredRecord]:
        if _denies_all(scope):
            return []
        full = self._name(name)
        if not self._is_named(full):
            # a dense-only collection (not yet re-ingested) can't answer a sparse
            # query — degrade to dense so old data keeps working.
            return self.search(name, dense_vector, top_k, where=where, scope=scope)
        from qdrant_client.models import Fusion, FusionQuery, Prefetch
        from qdrant_client.models import SparseVector as QSparse

        qfilter = self._build_filter(where, scope)
        hits = self._c().query_points(
            collection_name=full,
            prefetch=[
                Prefetch(query=dense_vector, using=_DENSE, filter=qfilter, limit=top_k),
                Prefetch(
                    query=QSparse(indices=sparse_vector.indices, values=sparse_vector.values),
                    using=_SPARSE, filter=qfilter, limit=top_k,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=top_k,
            with_payload=True,
        ).points
        return [self._scored(h) for h in hits]

    @staticmethod
    def _scored(h: Any) -> ScoredRecord:
        return ScoredRecord(
            id=(h.payload or {}).get(_SOURCE_ID_KEY, str(h.id)),
            score=float(h.score),
            payload=h.payload or {},
        )

    def count(self, name: str, *, scope: AccessScope) -> int:
        client = self._c()
        full = self._name(name)
        if _denies_all(scope) or not client.collection_exists(full):
            return 0
        return int(client.count(collection_name=full, count_filter=self._build_filter(None, scope),
                                exact=True).count)

    def scroll(
        self, name: str, *, limit: int, offset: str | None = None, scope: AccessScope
    ) -> tuple[list[StoredRecord], str | None]:
        client = self._c()
        full = self._name(name)
        if _denies_all(scope) or not client.collection_exists(full):
            return [], None
        points, next_offset = client.scroll(
            collection_name=full,
            scroll_filter=self._build_filter(None, scope),
            limit=limit,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        records = [
            StoredRecord(
                id=(p.payload or {}).get(_SOURCE_ID_KEY, str(p.id)),
                payload=p.payload or {},
            )
            for p in points
        ]
        return records, (str(next_offset) if next_offset is not None else None)

    def fetch(
        self, name: str, where: dict[str, Any] | None = None, *, scope: AccessScope,
        limit: int = 2000,
    ) -> list[StoredRecord]:
        """Payload-filtered fetch via Qdrant's native scroll filter — pages until
        ``limit`` records are collected or the collection is exhausted, so a whole
        section/chapter comes back in one call regardless of page size."""
        client = self._c()
        full = self._name(name)
        if _denies_all(scope) or not client.collection_exists(full):
            return []
        out: list[StoredRecord] = []
        offset: Any = None
        qfilter = self._build_filter(where, scope)
        while len(out) < limit:
            points, offset = client.scroll(
                collection_name=full,
                scroll_filter=qfilter,
                limit=min(512, limit - len(out)),
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            out.extend(
                StoredRecord(
                    id=(p.payload or {}).get(_SOURCE_ID_KEY, str(p.id)),
                    payload=p.payload or {},
                )
                for p in points
            )
            if offset is None or not points:
                break
        return out

    def delete(self, name: str, ids: list[str], *, scope: AccessScope) -> int:
        from qdrant_client.models import Filter, FilterSelector, HasIdCondition

        if not ids or _denies_all(scope):
            return 0
        client = self._c()
        full = self._name(name)
        if not client.collection_exists(full):
            return 0
        must: list[Any] = [HasIdCondition(has_id=[_point_id(i) for i in ids])]
        space = self._space_condition(scope)
        if space is not None:
            must.append(space)
        client.delete(collection_name=full, points_selector=FilterSelector(filter=Filter(must=must)))
        return len(ids)

    def delete_where(self, name: str, where: dict[str, Any], *, scope: AccessScope) -> None:
        from qdrant_client.models import FilterSelector

        if not where:
            raise ValueError("delete_where needs a non-empty filter")
        client = self._c()
        full = self._name(name)
        if _denies_all(scope) or not client.collection_exists(full):
            return
        client.delete(collection_name=full,
                      points_selector=FilterSelector(filter=self._build_filter(where, scope)))

    def backfill_space(self, name: str, space: str) -> int:
        from qdrant_client.models import Filter, IsEmptyCondition, PayloadField

        client = self._c()
        full = self._name(name)
        if not client.collection_exists(full):
            return 0
        self._ensure_indexes(full)
        missing = Filter(must=[IsEmptyCondition(is_empty=PayloadField(key=SPACE_KEY))])
        n = int(client.count(collection_name=full, count_filter=missing, exact=True).count)
        if n:
            client.set_payload(collection_name=full, payload={SPACE_KEY: space}, points=missing,
                               wait=True)
        return n

    @staticmethod
    def _space_condition(scope: AccessScope):
        if scope.unrestricted:
            return None
        from qdrant_client.models import FieldCondition, MatchAny

        return FieldCondition(key=SPACE_KEY, match=MatchAny(any=sorted(scope.spaces)))

    @classmethod
    def _build_filter(cls, where: dict[str, Any] | None, scope: AccessScope):
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        must: list[Any] = [FieldCondition(key=k, match=MatchValue(value=v))
                           for k, v in (where or {}).items()]
        space = cls._space_condition(scope)
        if space is not None:
            must.append(space)
        return Filter(must=must) if must else None

    def health(self) -> bool:
        try:
            self._c().get_collections()
            return True
        except Exception:  # pragma: no cover - network
            return False


def _denies_all(scope: AccessScope) -> bool:
    """A scope with no spaces reads nothing (never "no filter")."""
    return not scope.unrestricted and not scope.spaces
