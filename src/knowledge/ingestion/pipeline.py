"""The ingestion pipeline.

Given a source file dropped into the inbox, this runs the full,
checkpointed, idempotent pipeline:

  1. detect + hash (SHA-256)
  2. manifest lookup (skip if same hash already processed)
  3. preserve original source file
  4. MarkItDown conversion (deterministic, no LLM)
  5. frontmatter / metadata
  6. write canonical Markdown
  7. semantic chunking
  8. incremental embedding (only changed chunks) + vector upsert
  9. deterministic graph relationships
 10. optional selective LLM / Jev enrichment
 11. Markdown lint / validation
 12. manifest update + optional git commit
 13. move / mark source as processed

Each stage advances the manifest ``stage`` so a crash can resume rather
than corrupt state.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..classification import Classification, Track
from ..enrichment import EnrichmentResult
from ..markdown.frontmatter import dump_frontmatter
from ..models import (
    Chunk,
    Document,
    Frontmatter,
    JobStage,
    ManifestEntry,
    NoteType,
    document_id_for,
    sha256_bytes,
    sha256_text,
    utcnow,
)
from ..structured import record_blocks, render_markdown
from ..system import KnowledgeSystem
from ..vectorstore.base import VectorPoint
from .lint import lint_markdown


@dataclass
class IngestOutcome:
    source_path: str
    status: str                      # processed | skipped | staged | failed
    document_id: str | None = None
    markdown_path: str | None = None
    chunks: int = 0
    embedded: int = 0
    reused_chunks: int = 0
    enrichment: EnrichmentResult | None = None
    lint_issues: list[str] = field(default_factory=list)
    error: str | None = None
    reason: str | None = None
    # Which track handled the file, and why (surfaced in the UI/CLI).
    track: str | None = None
    track_reasons: list[str] = field(default_factory=list)
    staged_path: str | None = None
    requires_optin: bool = False


class IngestionPipeline:
    def __init__(self, ks: KnowledgeSystem):
        self.ks = ks
        self.settings = ks.settings

    # ---- note routing (flat layout: everything lands in content_dir) -----
    #
    # Organization is metadata-driven (frontmatter domain/topic, tags,
    # [[wiki links]]), NOT folder-driven. A note's domain/topic are set from
    # frontmatter or optional enrichment, never inferred from its folder.

    def _target_markdown_path(self, document_id: str) -> Path:
        return self.settings.content_path / f"{document_id}.md"

    # ---- source preservation --------------------------------------------

    def _preserve_source(self, source_path: Path) -> str:
        """Copy the original into the Sources tree, keeping the original."""
        dest_dir = self.settings.sources_path
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / source_path.name
        if source_path.resolve() != dest.resolve():
            shutil.copy2(source_path, dest)
        return str(dest.relative_to(self.settings.vault_path)).replace("\\", "/")

    # ---- git -------------------------------------------------------------

    def _git_commit(self, message: str) -> bool:
        if not self.settings.ingest.auto_git_commit:
            return False
        vault = self.settings.vault_path
        if not (vault / ".git").exists():
            return False
        try:
            subprocess.run(["git", "add", "-A"], cwd=vault, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-m", message], cwd=vault,
                           check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            return False

    # ---- main ------------------------------------------------------------

    def process_file(
        self,
        source_path: Path,
        force: bool = False,
        track: Track | None = None,
    ) -> IngestOutcome:
        """Ingest one file.

        ``track`` overrides classification -- used by "Add to Knowledge Base"
        to promote a staged analytical file into the structured track.
        """
        source_path = Path(source_path)
        rel_source = self._rel(source_path)

        if not source_path.exists():
            return IngestOutcome(rel_source, "skipped", reason="file no longer exists")

        # Route the file to a track before doing any work. Classification is
        # deterministic (extension + a cheap shape probe), never an LLM.
        classification = self.ks.classifier.classify(source_path, force_track=track)
        if classification.track is Track.unsupported:
            return IngestOutcome(
                rel_source, "skipped", track=classification.track.value,
                track_reasons=classification.reasons,
                reason=f"unsupported extension {source_path.suffix.lower()}",
            )

        data = source_path.read_bytes()
        source_hash = sha256_bytes(data)

        # Idempotency: same path + same hash already completed -> skip.
        existing = self.ks.state.get_manifest(rel_source)
        if existing and existing.source_hash == source_hash \
                and existing.stage == JobStage.completed and not force:
            return IngestOutcome(
                rel_source, "skipped", document_id=existing.document_id,
                markdown_path=existing.markdown_path,
                reason="already processed (unchanged hash)",
            )

        # Duplicate content under a different path.
        dup = self.ks.state.find_by_hash(source_hash)
        if dup and dup.source_path != rel_source and dup.stage == JobStage.completed \
                and not force:
            return IngestOutcome(
                rel_source, "skipped", document_id=dup.document_id,
                markdown_path=dup.markdown_path,
                reason=f"duplicate content of {dup.source_path}",
            )

        entry = ManifestEntry(source_path=rel_source, source_hash=source_hash,
                              stage=JobStage.detected,
                              track=classification.track.value)
        self.ks.state.upsert_manifest(entry)

        # Analytical files are working material, not knowledge: stage them and
        # stop. They only get indexed when the user explicitly promotes them.
        if (
            classification.track is Track.analytical
            and classification.requires_optin
        ):
            return self._stage_analytical(
                source_path, rel_source, entry, classification
            )

        try:
            if classification.track is Track.structured:
                return self._run_structured_stages(
                    source_path, rel_source, source_hash, entry, classification
                )
            return self._run_stages(
                source_path, rel_source, source_hash, entry, force, classification
            )
        except Exception as exc:  # noqa: BLE001 - record failure in manifest
            entry.error = str(exc)
            entry.stage = JobStage.failed
            self.ks.state.upsert_manifest(entry)
            return IngestOutcome(
                rel_source, "failed", error=str(exc),
                track=classification.track.value,
                track_reasons=classification.reasons,
            )

    def _run_stages(
        self,
        source_path: Path,
        rel_source: str,
        source_hash: str,
        entry: ManifestEntry,
        force: bool,
        classification: Classification | None = None,
    ) -> IngestOutcome:
        # 3. preserve original
        preserved_rel = self._preserve_source(source_path)

        # 4. convert (deterministic)
        conversion = self.ks.converter.convert(source_path)
        entry.stage = JobStage.converted
        self.ks.state.upsert_manifest(entry)

        # 5. metadata / frontmatter. Domain/topic stay unset here -- they are
        #    authored in frontmatter or assigned by optional enrichment, not
        #    derived from the folder (flat, metadata-driven organization).
        title = (conversion.title or source_path.stem).strip() or source_path.stem
        document_id = document_id_for(source_path, title)
        now = utcnow()
        fm = Frontmatter(
            id=document_id,
            title=title,
            domain=None,
            type=NoteType.document,
            source_type=source_path.suffix.lower().lstrip("."),
            source_file=preserved_rel,
            created_at=now,
            updated_at=now,
            tags=[],
        )

        md_path = self._target_markdown_path(document_id)
        rel_md = str(md_path.relative_to(self.settings.vault_path)).replace("\\", "/")

        document = Document(
            id=document_id, title=title, markdown_path=rel_md,
            source_file=preserved_rel, source_hash=source_hash,
            domain=None, topic=None, frontmatter=fm,
            body=conversion.markdown.strip(),
        )

        # 7. chunk (needed before enrichment brief)
        document.chunks = self.ks.chunker.chunk_document(document)
        entry.document_id = document_id
        entry.markdown_path = rel_md
        entry.stage = JobStage.chunked
        self.ks.state.upsert_manifest(entry)

        # 10. optional enrichment (compact, token-conscious) BEFORE writing so
        #     enrichment output can be persisted into frontmatter.
        enrichment = self.ks.enricher.enrich(document, force=force)
        extra_fm: dict = {}
        if enrichment.topic:
            document.topic = enrichment.topic
            fm.topic = enrichment.topic
        if enrichment.summary:
            extra_fm["summary"] = enrichment.summary
        if enrichment.entities:
            extra_fm["entities"] = enrichment.entities
        if enrichment.suggested_links:
            existing_related = set(fm.related)
            for link in enrichment.suggested_links:
                wl = f"[[{link}]]"
                if wl not in existing_related:
                    fm.related.append(wl)
        entry.stage = JobStage.enriched
        self.ks.state.upsert_manifest(entry)

        # 6. write canonical Markdown (body may include enrichment-added links)
        body = document.body
        if enrichment.suggested_links:
            related_md = "\n".join(f"- [[{link}]]"
                                   for link in enrichment.suggested_links)
            body = f"{body}\n\n## Related\n{related_md}\n"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        fm_dict = {**fm.model_dump(mode="json"), **extra_fm}
        md_path.write_text(dump_frontmatter(fm_dict, body), encoding="utf-8")
        document.body = body
        entry.stage = JobStage.written
        self.ks.state.upsert_manifest(entry)

        # 8. incremental embedding + vector upsert
        embedded, reused = self._index_chunks(document)
        entry.chunk_hashes = [c.content_hash for c in document.chunks]
        entry.stage = JobStage.embedded
        self.ks.state.upsert_manifest(entry)

        # 9. deterministic graph rebuild for this document's neighbourhood.
        #    (Full graph is cheaply rebuildable; here we refresh incrementally.)
        self.ks.rebuild_graph()
        entry.stage = JobStage.graphed
        self.ks.state.upsert_manifest(entry)

        # 11. markdown lint / validation
        lint_issues: list[str] = []
        if self.settings.ingest.markdown_lint:
            lint_issues = lint_markdown(body, fm.model_dump(mode="json"))
        entry.stage = JobStage.linted
        self.ks.state.upsert_manifest(entry)

        # 13. clear the source out of the inbox (already archived in Sources/)
        self._finalize_source(source_path)

        # 12. manifest complete, then optional git commit. `completed` is the
        #     terminal stage; the commit is a side effect and does not change it.
        entry.stage = JobStage.completed
        entry.error = None
        self.ks.state.upsert_manifest(entry)
        self._git_commit(f"ingest: {document_id}")

        return IngestOutcome(
            rel_source, "processed", document_id=document_id, markdown_path=rel_md,
            chunks=len(document.chunks), embedded=embedded, reused_chunks=reused,
            enrichment=enrichment, lint_issues=lint_issues,
            track=(classification.track.value if classification
                   else Track.document.value),
            track_reasons=(classification.reasons if classification else []),
        )

    # ---- track 2: structured knowledge ----------------------------------

    def _run_structured_stages(
        self,
        source_path: Path,
        rel_source: str,
        source_hash: str,
        entry: ManifestEntry,
        classification: Classification,
    ) -> IngestOutcome:
        """Tabular/record data -> schema note, record chunks, graph entities.

        The Markdown note stays the canonical artifact, but its body is a
        *schema summary plus a row sample* rather than a dump of every cell.
        Vector chunks are built from labelled records instead of the Markdown,
        so retrieval sees ``vendor: Acme / region: EU`` rather than a naked CSV
        line.
        """
        preserved_rel = self._preserve_source(source_path)

        doc_struct = self.ks.structured_parser.parse(source_path)
        entry.stage = JobStage.converted
        self.ks.state.upsert_manifest(entry)

        title = source_path.stem
        document_id = document_id_for(source_path, title)
        now = utcnow()
        fm = Frontmatter(
            id=document_id,
            title=title,
            type=NoteType.reference,
            source_type=source_path.suffix.lower().lstrip("."),
            source_file=preserved_rel,
            created_at=now,
            updated_at=now,
            tags=["structured", doc_struct.fmt],
        )

        md_path = self._target_markdown_path(document_id)
        rel_md = str(md_path.relative_to(self.settings.vault_path)).replace("\\", "/")
        body = render_markdown(doc_struct, title, self.settings.structured)

        document = Document(
            id=document_id, title=title, markdown_path=rel_md,
            source_file=preserved_rel, source_hash=source_hash,
            frontmatter=fm, body=body,
        )

        # Record-level chunks (not Markdown-derived) for useful embeddings.
        blocks = record_blocks(doc_struct, self.settings.structured)
        document.chunks = [
            Chunk(
                id=f"{document_id}::{i}",
                document_id=document_id,
                source_file=preserved_rel,
                markdown_path=rel_md,
                heading_path=heading,
                chunk_index=i,
                text=text,
                content_hash=sha256_text(text),
                token_estimate=max(1, len(text) // 4),
            )
            for i, (heading, text) in enumerate(blocks)
        ]
        entry.document_id = document_id
        entry.markdown_path = rel_md
        entry.stage = JobStage.chunked
        self.ks.state.upsert_manifest(entry)

        # Entities become wiki links so they join the graph deterministically.
        extra_fm: dict = {
            "record_count": doc_struct.record_count,
            "tables": [t.name for t in doc_struct.tables],
        }
        if doc_struct.entities:
            fm.related = [f"[[{e}]]" for e in doc_struct.entities[:50]]
            extra_fm["entities"] = doc_struct.entities[:100]
            body += "\n\n## Related\n" + "\n".join(
                f"- [[{e}]]" for e in doc_struct.entities[:50]
            ) + "\n"
            document.body = body

        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(
            dump_frontmatter({**fm.model_dump(mode="json"), **extra_fm}, body),
            encoding="utf-8",
        )
        entry.stage = JobStage.written
        self.ks.state.upsert_manifest(entry)

        embedded, reused = self._index_chunks(document)
        entry.chunk_hashes = [c.content_hash for c in document.chunks]
        entry.stage = JobStage.embedded
        self.ks.state.upsert_manifest(entry)

        self.ks.rebuild_graph()
        entry.stage = JobStage.graphed
        self.ks.state.upsert_manifest(entry)

        lint_issues: list[str] = []
        if self.settings.ingest.markdown_lint:
            lint_issues = lint_markdown(body, fm.model_dump(mode="json"))
        entry.stage = JobStage.linted
        self.ks.state.upsert_manifest(entry)

        self._finalize_source(source_path)
        entry.stage = JobStage.completed
        entry.error = None
        self.ks.state.upsert_manifest(entry)
        self._git_commit(f"ingest(structured): {document_id}")

        return IngestOutcome(
            rel_source, "processed", document_id=document_id, markdown_path=rel_md,
            chunks=len(document.chunks), embedded=embedded, reused_chunks=reused,
            lint_issues=lint_issues,
            track=classification.track.value,
            track_reasons=classification.reasons,
        )

    # ---- track 3: analytical staging ------------------------------------

    def _stage_analytical(
        self,
        source_path: Path,
        rel_source: str,
        entry: ManifestEntry,
        classification: Classification,
    ) -> IngestOutcome:
        """Park the file for analysis without touching the knowledge base.

        Nothing is converted, chunked, embedded or linked. The file is moved to
        the staging area and recorded, so it can be listed and promoted later.
        """
        staging = self.settings.analytical_path
        staging.mkdir(parents=True, exist_ok=True)
        dest = staging / source_path.name
        if dest.exists():
            dest.unlink()
        shutil.move(str(source_path), str(dest))
        rel_staged = str(dest.relative_to(self.settings.vault_path)).replace("\\", "/")

        entry.stage = JobStage.completed   # terminal for this track
        entry.markdown_path = None
        entry.error = None
        self.ks.state.upsert_manifest(entry)

        return IngestOutcome(
            rel_source, "staged",
            staged_path=rel_staged,
            track=classification.track.value,
            track_reasons=classification.reasons,
            requires_optin=True,
            reason=(
                "analytical file staged for analysis; use 'Add to Knowledge "
                "Base' to index it"
            ),
        )

    # ---- promotion -------------------------------------------------------

    def promote(self, staged: Path, force: bool = True) -> IngestOutcome:
        """Index a staged analytical file on explicit user request.

        It is routed through the structured track, which is what makes a
        spreadsheet or dataset searchable.
        """
        staged = Path(staged)
        if not staged.is_absolute():
            staged = self.settings.vault_path / staged
        if not staged.exists():
            return IngestOutcome(
                self._rel(staged), "skipped", reason="staged file not found"
            )
        return self.process_file(staged, force=force, track=Track.structured)

    def list_staged(self) -> list[dict]:
        """Analytical files awaiting an explicit decision."""
        staging = self.settings.analytical_path
        if not staging.exists():
            return []
        out = []
        for path in sorted(staging.iterdir()):
            # Skip bookkeeping files: the folder's own README, .keep, dotfiles.
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() == ".md":
                continue
            out.append({
                "name": path.name,
                "path": str(
                    path.relative_to(self.settings.vault_path)
                ).replace("\\", "/"),
                "bytes": path.stat().st_size,
            })
        return out

    # ---- incremental embedding ------------------------------------------

    def _index_chunks(self, document: Document) -> tuple[int, int]:
        """Embed only chunks whose content hash changed; delete stale ones."""
        prev = self.ks.state.get_document_chunk_hashes(document.id)
        current = {c.id: c.content_hash for c in document.chunks}

        # delete chunks that no longer exist
        stale_ids = [cid for cid in prev if cid not in current]
        if stale_ids:
            self.ks.vector_store.delete(stale_ids)

        to_embed = [
            c for c in document.chunks
            if prev.get(c.id) != c.content_hash
        ]
        reused = len(document.chunks) - len(to_embed)

        if to_embed:
            vectors = self.ks.embedder.embed([c.text for c in to_embed])
            points = [
                VectorPoint(id=c.id, vector=v, payload=c.payload())
                for c, v in zip(to_embed, vectors, strict=True)
            ]
            self.ks.vector_store.upsert(points)

        self.ks.state.replace_document_chunks(document.id, current)
        return len(to_embed), reused

    # ---- source finalisation --------------------------------------------

    def _finalize_source(self, source_path: Path) -> None:
        """Clear a processed file out of the inbox.

        The original was already archived under ``Sources/`` by
        :meth:`_preserve_source`, so keeping a second copy in
        ``00-Inbox/.processed/`` would duplicate every uploaded file. We remove
        the inbox copy instead, and only fall back to moving it into
        ``.processed/`` if the archive copy is missing for any reason (so a
        file is never silently lost).
        """
        if not self.settings.ingest.move_processed_sources:
            return

        # A promoted analytical file lives in the staging area, not the inbox.
        # Once it's archived in Sources/ and indexed it should leave staging,
        # otherwise it stays listed as "awaiting a decision" forever.
        try:
            source_path.resolve().relative_to(
                self.settings.analytical_path.resolve()
            )
        except ValueError:
            pass
        else:
            archived = self.settings.sources_path / source_path.name
            if archived.exists():
                source_path.unlink()
            return

        try:
            rel = source_path.resolve().relative_to(
                self.settings.inbox_path.resolve()
            )
        except ValueError:
            return  # not in inbox; leave it alone

        archived = self.settings.sources_path / source_path.name
        if archived.exists() and archived.stat().st_size == source_path.stat().st_size:
            source_path.unlink()
            return

        # Archive copy missing/mismatched -> preserve by moving, don't delete.
        processed_dir = self.settings.inbox_path / self.settings.ingest.processed_marker
        processed_dir.mkdir(parents=True, exist_ok=True)
        dest = processed_dir / rel.name
        if dest.exists():
            dest.unlink()
        shutil.move(str(source_path), str(dest))

    # ---- deletion handling ----------------------------------------------

    def handle_deleted(self, source_path: Path) -> IngestOutcome:
        rel_source = self._rel(source_path)
        entry = self.ks.state.get_manifest(rel_source)
        if not entry or not entry.document_id:
            return IngestOutcome(rel_source, "skipped", reason="unknown source")
        self.ks.vector_store.delete_by_document(entry.document_id)
        self.ks.graph_store.remove_document(entry.document_id)
        self.ks.state.replace_document_chunks(entry.document_id, {})
        self.ks.state.delete_manifest(rel_source)
        return IngestOutcome(rel_source, "processed",
                             document_id=entry.document_id, reason="removed from index")

    # ---- utils -----------------------------------------------------------

    def _rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(
                self.settings.vault_path.resolve())).replace("\\", "/")
        except ValueError:
            return str(path).replace("\\", "/")

    def process_inbox(self, force: bool = False) -> list[IngestOutcome]:
        """Process every file currently sitting in the inbox."""
        outcomes: list[IngestOutcome] = []
        inbox = self.settings.inbox_path
        if not inbox.exists():
            return outcomes
        marker = self.settings.ingest.processed_marker
        for path in sorted(inbox.rglob("*")):
            if path.is_dir() or marker in path.parts:
                continue
            outcomes.append(self.process_file(path, force=force))
        return outcomes
