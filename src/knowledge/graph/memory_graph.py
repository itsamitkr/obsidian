"""In-memory GraphStore. Fast, dependency-free, rebuilt from the vault."""

from __future__ import annotations

from ..models import GraphEdge, GraphNode, GraphView
from .base import GraphStore


class MemoryGraphStore(GraphStore):
    name = "memory"

    def __init__(self):
        self._nodes: dict[str, GraphNode] = {}
        self._edges: list[GraphEdge] = []

    def clear(self) -> None:
        self._nodes.clear()
        self._edges.clear()

    def upsert_node(self, node: GraphNode) -> None:
        self._nodes[node.id] = node

    def add_edge(self, edge: GraphEdge) -> None:
        for e in self._edges:
            if (e.source, e.target, e.relation) == (
                edge.source, edge.target, edge.relation
            ):
                return
        self._edges.append(edge)

    def remove_document(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)
        self._edges = [
            e for e in self._edges if e.source != node_id and e.target != node_id
        ]

    def get_node(self, node_id: str) -> GraphNode | None:
        return self._nodes.get(node_id)

    def neighbors(self, node_id: str) -> GraphView:
        node = self._nodes.get(node_id)
        nodes: list[GraphNode] = [node] if node else []
        edges = [e for e in self._edges if e.source == node_id]
        for e in edges:
            tgt = self._nodes.get(e.target)
            if tgt and tgt not in nodes:
                nodes.append(tgt)
        return GraphView(nodes=nodes, edges=edges)

    def backlinks(self, node_id: str) -> list[GraphEdge]:
        return [e for e in self._edges if e.target == node_id]

    def all(self) -> GraphView:
        return GraphView(nodes=list(self._nodes.values()), edges=list(self._edges))

    def orphans(self) -> list[GraphNode]:
        connected: set[str] = set()
        for e in self._edges:
            connected.add(e.source)
            connected.add(e.target)
        return [n for nid, n in self._nodes.items() if nid not in connected]

    def broken_links(self) -> list[GraphEdge]:
        return [e for e in self._edges if e.target not in self._nodes]
