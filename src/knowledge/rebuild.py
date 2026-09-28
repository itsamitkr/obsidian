"""Rebuild derived indexes (vector + graph) from the Markdown repository.

This proves the core invariant: the Markdown/Git vault is the source of
truth and every derived index is reconstructible from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .markdown.frontmatter import parse_frontmatter
from .models import Document, Frontmatter, NoteType, slugify
from .system import KnowledgeSystem
from .vectorstore.base import VectorPoint


@dataclass
class RebuildReport:
    documents: int = 0
    chunks: int = 0
    embedded: int = 0
    reused: int = 0


class IndexRebuilder:
    def __init__(self, ks: KnowledgeSystem):
        self.ks = ks
        self.settings = ks.settings

    def _iter_markdown(self):
        for path in sorted(self.settings.vault_path.rglob("*.md")):
            if ".knowledge" in path.parts:
                continue
            yield path

    def _load_document(self, path: Path) -> Document:
        fm_dict, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        doc_id = fm_dict.get("id") or slugify(path.stem)
        title = fm_dict.get("title") or path.stem
        rel = str(path.relative_to(self.settings.vault_path)).replace("\\", "/")
        fm = Frontmatter(
            id=doc_id,
            title=title,
            domain=fm_dict.get("domain"),
            topic=fm_dict.get("topic"),
            type=NoteType(fm_dict["type"]) if fm_dict.get("type") in
            NoteType._value2member_map_ else NoteType.note,
        )
        doc = Document(
            id=doc_id, title=title, markdown_path=rel,
            source_file=fm_dict.get("source_file"),
            domain=fm_dict.get("domain"), topic=fm_dict.get("topic"),
            frontmatter=fm, body=body,
        )
        doc.chunks = self.ks.chunker.chunk_document(doc)
        return doc

    def rebuild_vector_index(self, full: bool = True) -> RebuildReport:
        """Rebuild the vector index from Markdown.

        With ``full=True`` the collection is dropped and recreated. Content
        hashes still avoid re-embedding unchanged chunks when ``full=False``.
        """
        report = RebuildReport()
        if full:
            self.ks.vector_store.rebuild()
            self.ks.state.clear()

        for path in self._iter_markdown():
            doc = self._load_document(path)
            report.documents += 1
            report.chunks += len(doc.chunks)

            prev = self.ks.state.get_document_chunk_hashes(doc.id)
            current = {c.id: c.content_hash for c in doc.chunks}
            stale = [cid for cid in prev if cid not in current]
            if stale:
                self.ks.vector_store.delete(stale)

            to_embed = [c for c in doc.chunks if prev.get(c.id) != c.content_hash]
            report.embedded += len(to_embed)
            report.reused += len(doc.chunks) - len(to_embed)

            if to_embed:
                vecs = self.ks.embedder.embed([c.text for c in to_embed])
                self.ks.vector_store.upsert([
                    VectorPoint(id=c.id, vector=v, payload=c.payload())
                    for c, v in zip(to_embed, vecs, strict=True)
                ])
            self.ks.state.replace_document_chunks(doc.id, current)

        return report

    def rebuild_graph(self) -> int:
        self.ks.rebuild_graph()
        return len(self.ks.graph_store.all().nodes)
