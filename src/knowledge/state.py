"""Local relational state: ingestion manifest + job checkpoints.

This is the durability layer that makes ingestion idempotent and
restart-safe. It is *derived* data (rebuildable from the vault + sources)
and therefore lives under ``.knowledge/state`` and is git-ignored.

The manifest maps a source file (by path + SHA-256) to the work already
performed on it, so re-processing an unchanged file is a no-op and a job
that failed halfway through can resume from its last checkpoint.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    DateTime,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .models import JobStage, ManifestEntry, utcnow


class Base(DeclarativeBase):
    pass


class ManifestRow(Base):
    __tablename__ = "manifest"

    source_path: Mapped[str] = mapped_column(String, primary_key=True)
    source_hash: Mapped[str] = mapped_column(String, index=True)
    document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    markdown_path: Mapped[str | None] = mapped_column(String, nullable=True)
    stage: Mapped[str] = mapped_column(String, default=JobStage.detected.value)
    track: Mapped[str | None] = mapped_column(String, nullable=True)
    chunk_hashes: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def to_entry(self) -> ManifestEntry:
        return ManifestEntry(
            source_path=self.source_path,
            source_hash=self.source_hash,
            document_id=self.document_id,
            markdown_path=self.markdown_path,
            stage=JobStage(self.stage),
            track=self.track,
            chunk_hashes=json.loads(self.chunk_hashes or "[]"),
            error=self.error,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class DocumentChunkRow(Base):
    """Tracks the chunk content hashes currently indexed for a document so we
    can re-embed only what changed."""

    __tablename__ = "document_chunks"

    document_id: Mapped[str] = mapped_column(String, primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String, primary_key=True)
    content_hash: Mapped[str] = mapped_column(String)


class StateStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{db_path}", future=True)
        Base.metadata.create_all(self.engine)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        ``create_all`` only creates missing *tables*, so a new column on an
        existing manifest would otherwise raise OperationalError on every
        query. This keeps derived state usable without forcing a rebuild.
        """
        with self.engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(manifest)")
            }
            for column, ddl in (("track", "ALTER TABLE manifest ADD COLUMN track TEXT"),):
                if column not in existing:
                    conn.exec_driver_sql(ddl)

    @contextmanager
    def session(self) -> Iterable[Session]:
        with Session(self.engine, expire_on_commit=False) as s:
            yield s
            s.commit()

    # ---- Manifest --------------------------------------------------------

    def get_manifest(self, source_path: str) -> ManifestEntry | None:
        with self.session() as s:
            row = s.get(ManifestRow, source_path)
            return row.to_entry() if row else None

    def find_by_hash(self, source_hash: str) -> ManifestEntry | None:
        with self.session() as s:
            row = s.execute(
                select(ManifestRow).where(ManifestRow.source_hash == source_hash)
            ).scalars().first()
            return row.to_entry() if row else None

    def upsert_manifest(self, entry: ManifestEntry) -> None:
        with self.session() as s:
            row = s.get(ManifestRow, entry.source_path)
            if row is None:
                row = ManifestRow(source_path=entry.source_path)
                s.add(row)
            row.source_hash = entry.source_hash
            row.document_id = entry.document_id
            row.markdown_path = entry.markdown_path
            row.stage = entry.stage.value
            row.track = entry.track
            row.chunk_hashes = json.dumps(entry.chunk_hashes)
            row.error = entry.error
            row.updated_at = utcnow()

    def set_stage(self, source_path: str, stage: JobStage, error: str | None = None) -> None:
        with self.session() as s:
            row = s.get(ManifestRow, source_path)
            if row is None:
                return
            row.stage = stage.value
            row.error = error
            row.updated_at = utcnow()

    def all_manifest(self) -> list[ManifestEntry]:
        with self.session() as s:
            rows = s.execute(select(ManifestRow)).scalars().all()
            return [r.to_entry() for r in rows]

    def delete_manifest(self, source_path: str) -> None:
        with self.session() as s:
            row = s.get(ManifestRow, source_path)
            if row:
                s.delete(row)

    # ---- Document chunk tracking -----------------------------------------

    def get_document_chunk_hashes(self, document_id: str) -> dict[str, str]:
        with self.session() as s:
            rows = s.execute(
                select(DocumentChunkRow).where(
                    DocumentChunkRow.document_id == document_id
                )
            ).scalars().all()
            return {r.chunk_id: r.content_hash for r in rows}

    def document_ids(self) -> set[str]:
        """Every document that currently has chunks in the vector index."""
        with self.session() as s:
            return set(
                s.execute(select(DocumentChunkRow.document_id).distinct()).scalars()
            )

    def replace_document_chunks(
        self, document_id: str, chunk_hashes: dict[str, str]
    ) -> None:
        with self.session() as s:
            existing = s.execute(
                select(DocumentChunkRow).where(
                    DocumentChunkRow.document_id == document_id
                )
            ).scalars().all()
            for row in existing:
                s.delete(row)
            for chunk_id, content_hash in chunk_hashes.items():
                s.add(
                    DocumentChunkRow(
                        document_id=document_id,
                        chunk_id=chunk_id,
                        content_hash=content_hash,
                    )
                )

    def clear(self) -> None:
        with self.session() as s:
            s.query(ManifestRow).delete()
            s.query(DocumentChunkRow).delete()
