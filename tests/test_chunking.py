from __future__ import annotations

from knowledge.chunking import MarkdownChunker
from knowledge.config import ChunkingConfig
from knowledge.models import Document, Frontmatter


def _doc(body: str) -> Document:
    fm = Frontmatter(id="doc", title="Doc")
    return Document(id="doc", title="Doc", markdown_path="doc.md",
                    frontmatter=fm, body=body)


def test_chunk_respects_headings():
    body = (
        "# Title\n\n" + "intro paragraph. " * 5 + "\n\n"
        "## Section A\n\n" + "alpha content. " * 20 + "\n\n"
        "## Section B\n\n" + "beta content. " * 20 + "\n"
    )
    cfg = ChunkingConfig(max_tokens=64, min_tokens=4, chars_per_token=4)
    chunks = MarkdownChunker(cfg).chunk_document(_doc(body))
    assert len(chunks) >= 2
    # every chunk carries metadata
    for c in chunks:
        assert c.document_id == "doc"
        assert c.content_hash
        assert c.chunk_index >= 0
    # heading paths preserved somewhere
    joined = [tuple(c.heading_path) for c in chunks]
    assert any("Section A" in hp for hp in [h for tup in joined for h in tup])


def test_chunk_deterministic_hash_stable():
    body = "# T\n\n" + "content. " * 50
    cfg = ChunkingConfig(max_tokens=64, chars_per_token=4)
    c1 = MarkdownChunker(cfg).chunk_document(_doc(body))
    c2 = MarkdownChunker(cfg).chunk_document(_doc(body))
    assert [c.content_hash for c in c1] == [c.content_hash for c in c2]


def test_large_section_is_split():
    body = "# Big\n\n" + ("word " * 2000)
    cfg = ChunkingConfig(max_tokens=64, min_tokens=4, chars_per_token=4, overlap_tokens=8)
    chunks = MarkdownChunker(cfg).chunk_document(_doc(body))
    assert len(chunks) > 1
