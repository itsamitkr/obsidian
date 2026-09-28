"""Heading-aware semantic chunker.

Instead of blindly splitting every N characters, we chunk around the
Markdown heading hierarchy (Document -> H1 -> H2 -> H3 -> paragraphs) while
respecting configurable token/char limits. A section that fits within the
budget becomes a single chunk; larger sections are split on paragraph
boundaries with a small overlap. Each chunk keeps the metadata needed for
retrieval and incremental re-embedding.
"""

from __future__ import annotations

from ..config import ChunkingConfig
from ..markdown.parser import HeadingSection, parse_sections
from ..models import Chunk, Document, sha256_text


class MarkdownChunker:
    def __init__(self, cfg: ChunkingConfig):
        self.cfg = cfg

    # ---- token estimation ------------------------------------------------

    def _tokens(self, text: str) -> int:
        # Cheap deterministic estimate; avoids a tokenizer dependency.
        return max(1, len(text) // self.cfg.chars_per_token)

    @property
    def _max_chars(self) -> int:
        return self.cfg.max_tokens * self.cfg.chars_per_token

    @property
    def _overlap_chars(self) -> int:
        return self.cfg.overlap_tokens * self.cfg.chars_per_token

    # ---- splitting -------------------------------------------------------

    def _split_long_text(self, text: str) -> list[str]:
        """Split an oversized section on paragraph boundaries, then hard-wrap
        any remaining oversized paragraph, applying overlap between pieces."""
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        pieces: list[str] = []
        buf = ""
        for para in paragraphs:
            candidate = f"{buf}\n\n{para}".strip() if buf else para
            if len(candidate) <= self._max_chars:
                buf = candidate
                continue
            if buf:
                pieces.append(buf)
            if len(para) <= self._max_chars:
                buf = para
            else:
                # hard wrap oversized paragraph
                for i in range(0, len(para), self._max_chars):
                    pieces.append(para[i : i + self._max_chars])
                buf = ""
        if buf:
            pieces.append(buf)

        # apply overlap
        if self._overlap_chars > 0 and len(pieces) > 1:
            overlapped = [pieces[0]]
            for prev, cur in zip(pieces, pieces[1:], strict=False):
                tail = prev[-self._overlap_chars :]
                overlapped.append((tail + "\n\n" + cur).strip())
            pieces = overlapped
        return pieces

    def _section_texts(self, section: HeadingSection) -> list[str]:
        heading_line = ""
        if section.heading_path:
            heading_line = "#" * max(1, section.level) + " " + section.heading_path[-1]
        content = section.text.strip()
        full = (heading_line + "\n\n" + content).strip() if heading_line else content
        if not full.strip():
            return []
        if len(full) <= self._max_chars:
            return [full]
        # keep the heading prepended to each split piece for context
        parts = self._split_long_text(content)
        return [(heading_line + "\n\n" + p).strip() if heading_line else p
                for p in parts]

    # ---- public API ------------------------------------------------------

    def chunk_document(self, document: Document) -> list[Chunk]:
        if self.cfg.respect_headings:
            sections = parse_sections(document.body)
        else:
            sections = [HeadingSection(heading_path=[], level=0, text=document.body)]

        chunks: list[Chunk] = []
        index = 0
        pending: tuple[list[str], str] | None = None

        def emit(heading_path: list[str], text: str):
            nonlocal index
            content_hash = sha256_text(text)
            chunk = Chunk(
                id=f"{document.id}::{index}",
                document_id=document.id,
                source_file=document.source_file,
                markdown_path=document.markdown_path,
                heading_path=heading_path,
                domain=document.domain,
                topic=document.topic,
                chunk_index=index,
                text=text,
                content_hash=content_hash,
                token_estimate=self._tokens(text),
            )
            chunks.append(chunk)
            index += 1

        for section in sections:
            texts = self._section_texts(section)
            for text in texts:
                # Merge tiny sections forward to respect min_tokens.
                if self._tokens(text) < self.cfg.min_tokens and pending is None:
                    pending = (section.heading_path, text)
                    continue
                if pending is not None:
                    prev_path, prev_text = pending
                    merged = (prev_text + "\n\n" + text).strip()
                    if self._tokens(merged) <= self.cfg.max_tokens:
                        emit(section.heading_path or prev_path, merged)
                        pending = None
                        continue
                    emit(prev_path, prev_text)
                    pending = None
                emit(section.heading_path, text)

        if pending is not None:
            emit(pending[0], pending[1])

        return chunks
