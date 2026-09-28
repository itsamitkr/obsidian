"""Pluggable vector stores. The vector index is a *derived* artifact and can
be rebuilt from the Markdown repository at any time."""

from __future__ import annotations

from ..config import VectorConfig
from .base import VectorPoint, VectorStore
from .memory_store import MemoryVectorStore


def build_vector_store(
    cfg: VectorConfig, dim: int, persist_path=None, local_dir=None
) -> VectorStore:
    """Factory with graceful fallback to the in-memory store.

    ``persist_path`` makes the in-memory store durable across process
    invocations. ``local_dir`` is the on-disk location for embedded Qdrant
    (``provider='qdrant_local'``). A Qdrant server persists on its own.
    """
    if cfg.provider in ("qdrant", "qdrant_local"):
        try:
            from .qdrant_store import QdrantVectorStore

            local = str(local_dir) if cfg.provider == "qdrant_local" else None
            return QdrantVectorStore(cfg, dim, local_path=local)
        except ImportError:
            return MemoryVectorStore(cfg, dim, persist_path=persist_path)
    return MemoryVectorStore(cfg, dim, persist_path=persist_path)


__all__ = [
    "VectorStore",
    "VectorPoint",
    "MemoryVectorStore",
    "build_vector_store",
]
