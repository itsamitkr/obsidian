"""In-memory vector store with optional JSON persistence.

Zero external dependencies. Used as the default so the system works out of
the box and as a deterministic backend for tests. Cosine similarity search.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from ..config import VectorConfig
from .base import VectorMatch, VectorPoint, VectorStore


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class MemoryVectorStore(VectorStore):
    name = "memory"

    def __init__(self, cfg: VectorConfig, dim: int, persist_path: Path | None = None):
        self._dim = dim
        self._points: dict[str, VectorPoint] = {}
        self._persist_path = persist_path
        if persist_path and persist_path.exists():
            self._load()

    def _load(self) -> None:
        data = json.loads(self._persist_path.read_text(encoding="utf-8"))
        for item in data:
            self._points[item["id"]] = VectorPoint(
                id=item["id"], vector=item["vector"], payload=item["payload"]
            )

    def _save(self) -> None:
        if not self._persist_path:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        self._persist_path.write_text(
            json.dumps(
                [
                    {"id": p.id, "vector": p.vector, "payload": p.payload}
                    for p in self._points.values()
                ]
            ),
            encoding="utf-8",
        )

    def upsert(self, points: list[VectorPoint]) -> None:
        for p in points:
            self._points[p.id] = p
        self._save()

    def delete(self, ids: list[str]) -> None:
        for i in ids:
            self._points.pop(i, None)
        self._save()

    def delete_by_document(self, document_id: str) -> None:
        to_del = [
            pid for pid, p in self._points.items()
            if p.payload.get("document_id") == document_id
        ]
        self.delete(to_del)

    @staticmethod
    def _matches_filters(payload: dict, filters: dict | None) -> bool:
        if not filters:
            return True
        for key, value in filters.items():
            if payload.get(key) != value:
                return False
        return True

    def search(
        self,
        vector: list[float],
        limit: int = 10,
        filters: dict | None = None,
    ) -> list[VectorMatch]:
        scored = [
            VectorMatch(id=p.id, score=_cosine(vector, p.vector), payload=p.payload)
            for p in self._points.values()
            if self._matches_filters(p.payload, filters)
        ]
        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:limit]

    def get(self, id: str) -> VectorPoint | None:
        return self._points.get(id)

    def rebuild(self) -> None:
        self._points.clear()
        self._save()

    def health_check(self) -> bool:
        return True

    def __len__(self) -> int:
        return len(self._points)
