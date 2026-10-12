"""Rebuild derived indexes (vector + graph) from the Markdown repository.

This proves the core invariant: the Markdown/Git vault is the source of
truth and every derived index is reconstructible from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .ingestion.pipeline import structured_chunks, user_section
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
    removed: int = 0    # documents whose note no longer exists


class IndexRebuilder:
    def __init__(self, ks: KnowledgeSystem):
        self.ks = ks
        self.settings = ks.settings

    def _iter_markdown(self):
        # Notes only. Archived originals (Sources/), files still waiting in
        # the inbox, staged analytical files and templates are not notes:
        # indexing an archived .md source would bring a deleted note back.
        s = self.settings
        skip = [p.resolve() for p in (
            s.sources_path, s.inbox_path, s.analytical_path, s.templates_path
        )]
        for path in sorted(s.vault_path.rglob("*.md")):
            if ".knowledge" in path.parts:
                continue
            resolved = path.resolve()
            if any(resolved.is_relative_to(d) for d in skip):
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
        doc.chunks = self._structured_chunks(doc, fm_dict, body) \
            or self.ks.chunker.chunk_document(doc)
        return doc

    def _structured_chunks(self, doc: Document, fm_dict: dict, body: str):
        """Record chunks for a spreadsheet/data note, re-parsed from its original.

        The note itself only holds a schema summary and a row sample, so
        chunking its Markdown would drop record-level search. Returns None for
        ordinary notes, or if the archived original is missing (then the
        Markdown is the best we have).
        """
        tags = fm_dict.get("tags") or []
        if "structured" not in tags or not doc.source_file:
            return None
        original = self.settings.vault_path / doc.source_file
        if not original.is_file():
            return None
        doc_struct = self.ks.structured_parser.parse(original)
        if not doc_struct.tables:
            return None
        return structured_chunks(
            doc, doc_struct, self.settings.structured,
            my_notes=user_section(body),
        )

    def _indexed_path(self, doc: Document) -> str | None:
        """The note path stored with this document's indexed chunks."""
        for chunk_id in self.ks.state.get_document_chunk_hashes(doc.id):
            point = self.ks.vector_store.get(chunk_id)
            if point:
                return point.payload.get("markdown_path")
        return None

    def rebuild_vector_index(self, full: bool = True) -> RebuildReport:
        """Rebuild the vector index from Markdown.

        With ``full=True`` the collection is dropped and recreated. Content
        hashes still avoid re-embedding unchanged chunks when ``full=False``.
        """
        report = RebuildReport()
        if full:
            self.ks.vector_store.rebuild()
            self.ks.state.clear()
        else:
            self.ks.ensure_index_matches()

        seen: set[str] = set()
        for path in self._iter_markdown():
            doc = self._load_document(path)
            seen.add(doc.id)
            report.documents += 1
            report.chunks += len(doc.chunks)

            prev = self.ks.state.get_document_chunk_hashes(doc.id)
            if prev and self._indexed_path(doc) not in (None, doc.markdown_path):
                # Renamed/moved note: same id and text, but every stored chunk
                # still points at the old path. Re-upsert them all.
                prev = {}
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

        # Notes deleted from the vault: drop their vectors so search can't
        # return them. (A full rebuild already started from an empty index.)
        for doc_id in self.ks.state.document_ids() - seen:
            self.ks.vector_store.delete_by_document(doc_id)
            self.ks.graph_store.remove_document(doc_id)
            self.ks.state.replace_document_chunks(doc_id, {})
            report.removed += 1

        # A full rebuild re-embedded everything, so it adopts the current setup.
        if full or self.ks.index_built_with() is None:
            self.ks.record_index_fingerprint()
        return report

    def rebuild_graph(self) -> int:
        self.ks.rebuild_graph()
        return len(self.ks.graph_store.all().nodes)
