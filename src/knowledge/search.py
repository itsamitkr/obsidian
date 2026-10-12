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
        self.ks.ensure_index_matches()
        vector = self.ks.embedder.embed_one(query)
        filters: dict = {}
        if domain:
            filters["domain"] = domain
        if topic:
            filters["topic"] = topic
        # Over-fetch so dropping stale hits still fills the requested limit.
        matches = self.ks.vector_store.search(
            vector, limit=limit * 2 + 5, filters=filters or None
        )
        vault = self.ks.settings.vault_path
        results: list[SearchResult] = []
        for m in matches:
            p = m.payload
            # A note deleted or renamed in the vault (outside ingestion) leaves
            # its vectors behind until the next rebuild; never return those.
            md = p.get("markdown_path", "")
            if md and not (vault / md).exists():
                continue
            if len(results) >= limit:
                break
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
