"""Pluggable vector stores. The vector index is a *derived* artifact and can
be rebuilt from the Markdown repository at any time."""

from __future__ import annotations

from ..config import VectorConfig
from .base import VectorPoint, VectorStore
from .memory_store import MemoryVectorStore


def build_vector_store(
    cfg: VectorConfig, dim: int, persist_path=None, local_dir=None
) -> VectorStore:
    """Build the configured store. The memory store is used only when configured.

    No silent fallback: quietly swapping Qdrant for the memory store would
    point search at a different (often empty) index without any error.

    ``persist_path`` makes the in-memory store durable across process
    invocations. ``local_dir`` is the on-disk location for embedded Qdrant
    (``provider='qdrant_local'``). A Qdrant server persists on its own.
    """
    if cfg.provider in ("qdrant", "qdrant_local"):
        # QdrantVectorStore raises a clear ImportError if the extra is missing.
        from .qdrant_store import QdrantVectorStore

        local = str(local_dir) if cfg.provider == "qdrant_local" else None
        return QdrantVectorStore(cfg, dim, local_path=local)
    return MemoryVectorStore(cfg, dim, persist_path=persist_path)


__all__ = [
    "VectorStore",
    "VectorPoint",
    "MemoryVectorStore",
    "build_vector_store",
]
