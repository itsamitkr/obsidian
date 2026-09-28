"""Retrieval service: embed a query and search the vector index, with
optional metadata filtering (domain/topic)."""

from __future__ import annotations

from .models import SearchResult
from .system import KnowledgeSystem


class SearchService:
    def __init__(self, ks: KnowledgeSystem):
        self.ks = ks

    def search(
        self,
        query: str,
        limit: int = 10,
        domain: str | None = None,
        topic: str | None = None,
    ) -> list[SearchResult]:
        vector = self.ks.embedder.embed_one(query)
        filters: dict = {}
        if domain:
            filters["domain"] = domain
        if topic:
            filters["topic"] = topic
        matches = self.ks.vector_store.search(
            vector, limit=limit, filters=filters or None
        )
        results: list[SearchResult] = []
        for m in matches:
            p = m.payload
            results.append(
                SearchResult(
                    chunk_id=m.id,
                    document_id=p.get("document_id", ""),
                    score=m.score,
                    markdown_path=p.get("markdown_path", ""),
                    heading_path=p.get("heading_path", []) or [],
                    domain=p.get("domain"),
                    topic=p.get("topic"),
                    text=p.get("text", ""),
                )
            )
        return results
