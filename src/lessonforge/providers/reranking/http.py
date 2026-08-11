"""HTTP reranker adapter — points at a standalone FastAPI reranker service.

Contract the remote service must honor:

    POST {endpoint}
    body:  {"query": str, "documents": [str, ...], "top_n": int}
    resp:  {"results": [{"index": int, "score": float}, ...]}   # any order

This lets the reranker scale/deploy independently of the API while the rest of
the system keeps depending only on the ``Reranker`` interface.
"""

from __future__ import annotations

import httpx

from ...config import RerankerConfig
from ..base import Reranker, RerankResult
from ..registry import register_reranker


@register_reranker("http")
class HttpReranker(Reranker):
    def __init__(self, endpoint: str, timeout_s: int = 30) -> None:
        self.endpoint = endpoint
        self.timeout_s = timeout_s

    @classmethod
    def from_config(cls, cfg: RerankerConfig) -> HttpReranker:
        if not cfg.endpoint:
            raise ValueError("reranker.endpoint is required for the http provider")
        return cls(endpoint=cfg.endpoint)

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[RerankResult]:
        if not documents:
            return []
        with httpx.Client(timeout=self.timeout_s) as client:
            resp = client.post(
                self.endpoint,
                json={"query": query, "documents": documents, "top_n": top_n},
            )
            resp.raise_for_status()
            payload = resp.json()
        results = [
            RerankResult(index=r["index"], score=float(r["score"]), document=documents[r["index"]])
            for r in payload["results"]
        ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_n]
