"""Embedding providers behind a common interface."""

from __future__ import annotations

from ..config import EmbeddingConfig
from .base import EmbeddingProvider
from .hash_provider import HashEmbeddingProvider


def build_embedding_provider(cfg: EmbeddingConfig) -> EmbeddingProvider:
    """Build the configured provider. Hash is used only when configured.

    No silent fallback: the hash provider has the same width (384) as the
    default model, so a quiet swap would degrade search without any error.
    """
    provider = cfg.provider
    if provider == "sentence_transformers":
        from .sentence_transformers_provider import SentenceTransformersProvider

        # Raises a clear ImportError if the 'embed' extra is missing.
        return SentenceTransformersProvider(cfg)
    if provider == "openai":
        from .openai_provider import OpenAIEmbeddingProvider

        return OpenAIEmbeddingProvider(cfg)
    return HashEmbeddingProvider(cfg)


__all__ = [
    "EmbeddingProvider",
    "HashEmbeddingProvider",
    "build_embedding_provider",
]
