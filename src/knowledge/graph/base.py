"""GraphStore interface. Concrete stores: in-memory, SQLite. Neo4j later."""

from __future__ import annotations

import abc

from ..models import GraphEdge, GraphNode, GraphView


class GraphStore(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def clear(self) -> None:
        ...

    @abc.abstractmethod
    def upsert_node(self, node: GraphNode) -> None:
        ...

    @abc.abstractmethod
    def add_edge(self, edge: GraphEdge) -> None:
        ...

    @abc.abstractmethod
    def remove_document(self, node_id: str) -> None:
        ...

    @abc.abstractmethod
    def get_node(self, node_id: str) -> GraphNode | None:
        ...

    @abc.abstractmethod
    def neighbors(self, node_id: str) -> GraphView:
        """Node plus its outgoing edges + target nodes."""

    @abc.abstractmethod
    def backlinks(self, node_id: str) -> list[GraphEdge]:
        """Edges pointing *to* this node."""

    @abc.abstractmethod
    def all(self) -> GraphView:
        ...

    @abc.abstractmethod
    def orphans(self) -> list[GraphNode]:
        """Nodes with no incoming and no outgoing edges."""

    @abc.abstractmethod
    def broken_links(self) -> list[GraphEdge]:
        """Edges whose target node does not exist."""
