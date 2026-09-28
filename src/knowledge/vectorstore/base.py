"""VectorStore interface.

    VectorStore
        upsert()
        delete()
        search()
        get()
        rebuild()
        health_check()
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class VectorPoint:
    id: str
    vector: list[float]
    payload: dict = field(default_factory=dict)


@dataclass
class VectorMatch:
    id: str
    score: float
    payload: dict = field(default_factory=dict)


class VectorStore(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def upsert(self, points: list[VectorPoint]) -> None:
        ...

    @abc.abstractmethod
    def delete(self, ids: list[str]) -> None:
        ...

    @abc.abstractmethod
    def delete_by_document(self, document_id: str) -> None:
        ...

    @abc.abstractmethod
    def search(
        self,
        vector: list[float],
        limit: int = 10,
        filters: dict | None = None,
    ) -> list[VectorMatch]:
        ...

    @abc.abstractmethod
    def get(self, id: str) -> VectorPoint | None:
        ...

    @abc.abstractmethod
    def rebuild(self) -> None:
        """Drop and recreate the underlying collection (empty)."""

    @abc.abstractmethod
    def health_check(self) -> bool:
        ...
