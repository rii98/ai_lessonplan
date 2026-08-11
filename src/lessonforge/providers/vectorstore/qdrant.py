"""Qdrant vector-store adapter.

Collection names are namespaced with ``collection_prefix`` so multiple logical
corpora (curriculum, pedagogical, exemplars, ...) live in one Qdrant instance
without collision. The client is imported lazily to keep unit tests light.
"""

from __future__ import annotations

import uuid
from typing import Any

from ...config import VectorStoreConfig
from ..base import ScoredRecord, VectorRecord, VectorStore
from ..registry import register_vector_store

# Qdrant requires point IDs to be unsigned ints or UUIDs. Our interface uses
# arbitrary string IDs (e.g. content hashes), so we map each to a stable UUID5
# and keep the original under this payload key for round-tripping.
_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")
_SOURCE_ID_KEY = "_source_id"


def _point_id(raw: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, raw))


@register_vector_store("qdrant")
class QdrantVectorStore(VectorStore):
    def __init__(self, url: str, api_key: str | None, collection_prefix: str) -> None:
        self.url = url
        self.api_key = api_key or None
        self.prefix = collection_prefix
        self._client = None  # lazy

    @classmethod
    def from_config(cls, cfg: VectorStoreConfig) -> QdrantVectorStore:
        return cls(url=cfg.url, api_key=cfg.api_key, collection_prefix=cfg.collection_prefix)

    def _c(self):
        if self._client is None:
            from qdrant_client import QdrantClient

            self._client = QdrantClient(url=self.url, api_key=self.api_key)
        return self._client

    def _name(self, name: str) -> str:
        return f"{self.prefix}_{name}"

    def ensure_collection(self, name: str, dim: int) -> None:
        from qdrant_client.models import Distance, VectorParams

        client = self._c()
        full = self._name(name)
        if not client.collection_exists(full):
            client.create_collection(
                collection_name=full,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

    def upsert(self, name: str, records: list[VectorRecord]) -> None:
        from qdrant_client.models import PointStruct

        if not records:
            return
        self._c().upsert(
            collection_name=self._name(name),
            points=[
                PointStruct(
                    id=_point_id(r.id),
                    vector=r.vector,
                    payload={**r.payload, _SOURCE_ID_KEY: r.id},
                )
                for r in records
            ],
        )

    def search(
        self,
        name: str,
        vector: list[float],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[ScoredRecord]:
        query_filter = self._build_filter(where)
        hits = self._c().query_points(
            collection_name=self._name(name),
            query=vector,
            limit=top_k,
            query_filter=query_filter,
            with_payload=True,
        ).points
        return [
            ScoredRecord(
                id=(h.payload or {}).get(_SOURCE_ID_KEY, str(h.id)),
                score=float(h.score),
                payload=h.payload or {},
            )
            for h in hits
        ]

    @staticmethod
    def _build_filter(where: dict[str, Any] | None):
        if not where:
            return None
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        return Filter(
            must=[FieldCondition(key=k, match=MatchValue(value=v)) for k, v in where.items()]
        )

    def health(self) -> bool:
        try:
            self._c().get_collections()
            return True
        except Exception:  # pragma: no cover - network
            return False
