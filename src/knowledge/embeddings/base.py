"""EmbeddingProvider interface."""

from __future__ import annotations

import abc


class EmbeddingProvider(abc.ABC):
    name: str = "base"

    @property
    @abc.abstractmethod
    def dim(self) -> int:
        """Dimensionality of the produced vectors."""

    @abc.abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts into vectors."""

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
