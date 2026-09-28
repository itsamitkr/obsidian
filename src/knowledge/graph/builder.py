"""Build the knowledge graph from the Markdown vault (deterministic).

Nodes come from every Markdown note. Edges come from:
  * wiki links      -> relation "links_to"
  * frontmatter.related -> relation "related_to"
  * tags            -> relation "tagged" to a synthetic tag node

Targets are resolved by note title/id/alias. Unresolved targets are still
recorded as edges so ``broken_links()`` can surface them.
"""

from __future__ import annotations

from pathlib import Path

from ..markdown.frontmatter import parse_frontmatter
from ..markdown.links import extract_links
from ..models import GraphEdge, GraphNode, slugify
from .base import GraphStore


class GraphBuilder:
    def __init__(self, vault_path: Path, store: GraphStore):
        self.vault_path = vault_path
        self.store = store

    def _iter_markdown(self):
        for path in sorted(self.vault_path.rglob("*.md")):
            # skip state/templates dirs
            parts = set(path.parts)
            if ".knowledge" in parts:
                continue
            yield path

    def rebuild(self) -> GraphStore:
        self.store.clear()

        # First pass: register nodes and a title/alias -> id index.
        title_index: dict[str, str] = {}
        note_meta: list[tuple[Path, dict, str]] = []

        for path in self._iter_markdown():
            fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            node_id = fm.get("id") or slugify(path.stem)
            title = fm.get("title") or path.stem
            rel_path = str(path.relative_to(self.vault_path)).replace("\\", "/")
            node = GraphNode(
                id=node_id,
                title=title,
                type=str(fm.get("type", "note")),
                domain=fm.get("domain"),
                topic=fm.get("topic"),
                markdown_path=rel_path,
                tags=[str(t) for t in (fm.get("tags") or [])],
            )
            self.store.upsert_node(node)
            title_index[title.lower()] = node_id
            title_index[node_id.lower()] = node_id
            for alias in fm.get("aliases", []) or []:
                title_index[str(alias).lower()] = node_id

            # Structured sources declare their categorical values in
            # `entities:`. Register them as real nodes so the graph is
            # navigable and they don't show up as broken links.
            for raw in fm.get("entities", []) or []:
                name = str(raw).strip()
                if not name:
                    continue
                ent_id = slugify(name)
                if ent_id and ent_id not in title_index.values():
                    self.store.upsert_node(
                        GraphNode(id=ent_id, title=name, type="entity")
                    )
                title_index.setdefault(name.lower(), ent_id)

            note_meta.append((path, fm, node_id))

        def resolve(target: str) -> str:
            return title_index.get(target.lower(), slugify(target))

        # Second pass: edges.
        for path, fm, node_id in note_meta:
            _, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            links = extract_links(body, fm)
            for target in links.wiki_links:
                self.store.add_edge(
                    GraphEdge(source=node_id, target=resolve(target),
                              relation="links_to", origin="markdown")
                )
            for target in links.related:
                self.store.add_edge(
                    GraphEdge(source=node_id, target=resolve(target),
                              relation="related_to", origin="markdown")
                )
            # Structured entities: an explicit, typed relationship rather than
            # a generic link, so callers can distinguish "this dataset mentions
            # Acme" from "this note links to Acme".
            for raw in fm.get("entities", []) or []:
                name = str(raw).strip()
                if name:
                    self.store.add_edge(
                        GraphEdge(source=node_id, target=resolve(name),
                                  relation="mentions", origin="structured")
                    )

            for tag in links.tags:
                tag_id = f"tag:{tag.lower()}"
                self.store.upsert_node(
                    GraphNode(id=tag_id, title=f"#{tag}", type="tag")
                )
                self.store.add_edge(
                    GraphEdge(source=node_id, target=tag_id,
                              relation="tagged", origin="markdown")
                )
        return self.store
