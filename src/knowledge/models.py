"""Core domain models shared across the system.

These are the in-memory representations that flow through the ingestion
pipeline, the search layer, and the MCP server. Persistence models live in
``knowledge.state``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class NoteType(str, Enum):
    concept = "concept"
    reference = "reference"
    document = "document"
    index = "index"
    note = "note"


class NoteStatus(str, Enum):
    active = "active"
    draft = "draft"
    archived = "archived"


class Frontmatter(BaseModel):
    """YAML frontmatter for a Markdown note. Extra keys are preserved."""

    model_config = {"extra": "allow"}

    id: str
    title: str
    domain: str | None = None
    topic: str | None = None
    type: NoteType = NoteType.note
    status: NoteStatus = NoteStatus.active
    source_type: str | None = None
    source_file: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    tags: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


class Chunk(BaseModel):
    """A semantic chunk of a Markdown document, ready for embedding."""

    id: str                       # deterministic: document_id + chunk_index
    document_id: str
    source_file: str | None = None
    markdown_path: str
    heading_path: list[str] = Field(default_factory=list)
    domain: str | None = None
    topic: str | None = None
    chunk_index: int
    text: str
    content_hash: str
    token_estimate: int = 0

    def payload(self) -> dict:
        """Metadata stored alongside the vector."""
        return {
            "document_id": self.document_id,
            "source_file": self.source_file,
            "markdown_path": self.markdown_path,
            "heading_path": self.heading_path,
            "domain": self.domain,
            "topic": self.topic,
            "chunk_index": self.chunk_index,
            "content_hash": self.content_hash,
            "text": self.text,
        }


class Document(BaseModel):
    """A processed Markdown document (canonical representation)."""

    id: str
    title: str
    markdown_path: str
    source_file: str | None = None
    source_hash: str | None = None
    domain: str | None = None
    topic: str | None = None
    frontmatter: Frontmatter
    body: str
    chunks: list[Chunk] = Field(default_factory=list)


class SearchResult(BaseModel):
    chunk_id: str
    document_id: str
    score: float
    markdown_path: str
    heading_path: list[str] = Field(default_factory=list)
    domain: str | None = None
    topic: str | None = None
    text: str


# --------------------------------------------------------------------------
# Graph models
# --------------------------------------------------------------------------


class GraphNode(BaseModel):
    id: str                       # slug / note id
    title: str
    type: str = "note"
    domain: str | None = None
    topic: str | None = None
    markdown_path: str | None = None
    tags: list[str] = Field(default_factory=list)


class GraphEdge(BaseModel):
    source: str
    target: str
    relation: str = "related_to"   # links_to, related_to, tagged, uses, etc.
    # Provenance: was this edge derived deterministically or by the LLM?
    origin: str = "markdown"       # markdown | llm | jev | manual


class GraphView(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Ingestion job / manifest models
# --------------------------------------------------------------------------


class JobStage(str, Enum):
    detected = "detected"
    converted = "converted"
    written = "written"
    chunked = "chunked"
    embedded = "embedded"
    graphed = "graphed"
    enriched = "enriched"
    linted = "linted"
    committed = "committed"
    completed = "completed"
    failed = "failed"


class ManifestEntry(BaseModel):
    """Records what happened to a given source file (idempotency key)."""

    source_path: str
    source_hash: str
    document_id: str | None = None
    markdown_path: str | None = None
    stage: JobStage = JobStage.detected
    # Which ingestion track handled this file: document | structured |
    # analytical. Persisted so staged analytical files can be listed and
    # promoted later without re-classifying.
    track: str | None = None
    chunk_hashes: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


def slugify(text: str) -> str:
    """Deterministic slug used for document ids / node ids."""
    out = []
    prev_dash = False
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif ch in " -_/\\":
            if not prev_dash:
                out.append("-")
                prev_dash = True
        # drop everything else
    slug = "".join(out).strip("-")
    return slug or "untitled"


def document_id_for(source_path: str | Path, title: str | None = None) -> str:
    """Stable document id derived from the relative source/markdown path."""
    name = Path(source_path).stem
    base = slugify(title or name)
    return base
