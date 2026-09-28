"""Embedding providers behind a common interface."""

from __future__ import annotations

from ..config import EmbeddingConfig
from .base import EmbeddingProvider
from .hash_provider import HashEmbeddingProvider


def build_embedding_provider(cfg: EmbeddingConfig) -> EmbeddingProvider:
    """Factory with graceful fallback to the deterministic hash provider."""
    provider = cfg.provider
    if provider == "sentence_transformers":
        try:
            from .sentence_transformers_provider import (
                SentenceTransformersProvider,
            )

            return SentenceTransformersProvider(cfg)
        except ImportError:
            # Fall back so the system stays functional without the extra.
            return HashEmbeddingProvider(cfg)
    if provider == "openai":
        from .openai_provider import OpenAIEmbeddingProvider

        return OpenAIEmbeddingProvider(cfg)
    return HashEmbeddingProvider(cfg)


__all__ = [
    "EmbeddingProvider",
    "HashEmbeddingProvider",
    "build_embedding_provider",
]
