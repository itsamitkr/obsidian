"""Knowledge graph derived from Markdown links / properties / tags.

The graph is a *derived* index (rebuildable from the vault). Neo4j is not
required initially; a GraphStore interface keeps that door open.
"""

from __future__ import annotations

from ..config import GraphConfig
from .base import GraphStore
from .builder import GraphBuilder
from .memory_graph import MemoryGraphStore


def build_graph_store(cfg: GraphConfig, db_path=None) -> GraphStore:
    if cfg.provider == "sqlite":
        from .sqlite_graph import SqliteGraphStore

        return SqliteGraphStore(db_path)
    return MemoryGraphStore()


__all__ = ["GraphStore", "GraphBuilder", "MemoryGraphStore", "build_graph_store"]
