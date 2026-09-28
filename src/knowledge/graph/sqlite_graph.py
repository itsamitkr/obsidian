"""SQLite-backed GraphStore. Persists the derived graph so navigation is
available without re-scanning the whole vault. Still fully rebuildable."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..models import GraphEdge, GraphNode, GraphView
from .base import GraphStore


class SqliteGraphStore(GraphStore):
    name = "sqlite"

    def __init__(self, db_path: Path | None):
        self._path = str(db_path) if db_path else ":memory:"
        if db_path:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                title TEXT,
                type TEXT,
                domain TEXT,
                topic TEXT,
                markdown_path TEXT,
                tags TEXT
            );
            CREATE TABLE IF NOT EXISTS edges (
                source TEXT,
                target TEXT,
                relation TEXT,
                origin TEXT,
                PRIMARY KEY (source, target, relation)
            );
            CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
            """
        )
        self._conn.commit()

    def clear(self) -> None:
        self._conn.executescript("DELETE FROM nodes; DELETE FROM edges;")
        self._conn.commit()

    def upsert_node(self, node: GraphNode) -> None:
        self._conn.execute(
            """INSERT INTO nodes (id,title,type,domain,topic,markdown_path,tags)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, type=excluded.type, domain=excluded.domain,
                 topic=excluded.topic, markdown_path=excluded.markdown_path,
                 tags=excluded.tags""",
            (
                node.id, node.title, node.type, node.domain, node.topic,
                node.markdown_path, ",".join(node.tags),
            ),
        )
        self._conn.commit()

    def add_edge(self, edge: GraphEdge) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO edges (source,target,relation,origin)
               VALUES (?,?,?,?)""",
            (edge.source, edge.target, edge.relation, edge.origin),
        )
        self._conn.commit()

    def remove_document(self, node_id: str) -> None:
        self._conn.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        self._conn.execute(
            "DELETE FROM edges WHERE source=? OR target=?", (node_id, node_id)
        )
        self._conn.commit()

    def _row_to_node(self, row: sqlite3.Row) -> GraphNode:
        return GraphNode(
            id=row["id"], title=row["title"], type=row["type"],
            domain=row["domain"], topic=row["topic"],
            markdown_path=row["markdown_path"],
            tags=[t for t in (row["tags"] or "").split(",") if t],
        )

    def get_node(self, node_id: str) -> GraphNode | None:
        row = self._conn.execute(
            "SELECT * FROM nodes WHERE id=?", (node_id,)
        ).fetchone()
        return self._row_to_node(row) if row else None

    def _edges_from(self, node_id: str) -> list[GraphEdge]:
        rows = self._conn.execute(
            "SELECT * FROM edges WHERE source=?", (node_id,)
        ).fetchall()
        return [
            GraphEdge(source=r["source"], target=r["target"],
                      relation=r["relation"], origin=r["origin"])
            for r in rows
        ]

    def neighbors(self, node_id: str) -> GraphView:
        node = self.get_node(node_id)
        nodes = [node] if node else []
        edges = self._edges_from(node_id)
        for e in edges:
            tgt = self.get_node(e.target)
            if tgt and tgt not in nodes:
                nodes.append(tgt)
        return GraphView(nodes=nodes, edges=edges)

    def backlinks(self, node_id: str) -> list[GraphEdge]:
        rows = self._conn.execute(
            "SELECT * FROM edges WHERE target=?", (node_id,)
        ).fetchall()
        return [
            GraphEdge(source=r["source"], target=r["target"],
                      relation=r["relation"], origin=r["origin"])
            for r in rows
        ]

    def all(self) -> GraphView:
        nodes = [
            self._row_to_node(r)
            for r in self._conn.execute("SELECT * FROM nodes").fetchall()
        ]
        edges = [
            GraphEdge(source=r["source"], target=r["target"],
                      relation=r["relation"], origin=r["origin"])
            for r in self._conn.execute("SELECT * FROM edges").fetchall()
        ]
        return GraphView(nodes=nodes, edges=edges)

    def orphans(self) -> list[GraphNode]:
        rows = self._conn.execute(
            """SELECT * FROM nodes WHERE id NOT IN (
                   SELECT source FROM edges UNION SELECT target FROM edges
               )"""
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def broken_links(self) -> list[GraphEdge]:
        rows = self._conn.execute(
            """SELECT * FROM edges WHERE target NOT IN (SELECT id FROM nodes)"""
        ).fetchall()
        return [
            GraphEdge(source=r["source"], target=r["target"],
                      relation=r["relation"], origin=r["origin"])
            for r in rows
        ]
