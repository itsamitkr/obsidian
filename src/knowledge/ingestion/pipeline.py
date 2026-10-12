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
from ..markdown.frontmatter import dump_frontmatter, load_note
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

# ---- human edits vs. regenerated notes -------------------------------------

USER_SECTION = "## My notes"

# Frontmatter the pipeline writes; regenerated on every ingest.
_GENERATED_KEYS = set(Frontmatter.model_fields) | {
    "summary", "entities", "record_count", "records_indexed", "tables",
    "truncated_tables",
}
# Generated fields the user may set or change; an existing value wins.
_USER_WINS = ("domain", "topic", "status", "created_at")
# Lists merged from both sides.
_UNION = ("tags", "related", "aliases")


def user_section(body: str) -> str | None:
    """The ``## My notes`` section of a note (heading included), if any."""
    lines = body.splitlines()
    start = next(
        (i for i, ln in enumerate(lines)
         if ln.strip().lower() == USER_SECTION.lower()),
        None,
    )
    if start is None:
        return None
    end = next(
        (j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")),
        len(lines),
    )
    section = "\n".join(lines[start:end]).strip()
    return section if section != USER_SECTION else None  # heading only: nothing to keep


def _merge_frontmatter(new: dict, old: dict) -> dict:
    merged = dict(new)
    for key, value in old.items():
        if key in _UNION and isinstance(value, list):
            merged[key] = list(dict.fromkeys([*(merged.get(key) or []), *value]))
        elif key in _USER_WINS and value not in (None, "", []):
            merged[key] = value
        elif key not in _GENERATED_KEYS:
            merged[key] = value  # added by the user
    return merged


def structured_chunks(
    document: Document,
    doc_struct,
    cfg,
    my_notes: str | None = None,
) -> list[Chunk]:
    """Chunks for a structured note: schema + record groups from the data.

    Shared by ingestion and rebuild. Rebuilding from the note's Markdown would
    only see the schema summary and a 10-row sample, so record-level search
    would be lost; the data has to be re-parsed from the archived original.
    """
    blocks = record_blocks(doc_struct, cfg)
    if my_notes:
        blocks.append((["My notes"], my_notes))
    return [
        Chunk(
            id=f"{document.id}::{i}",
            document_id=document.id,
            source_file=document.source_file,
            markdown_path=document.markdown_path,
            heading_path=heading,
            domain=document.domain,
            topic=document.topic,
            chunk_index=i,
            text=text,
            content_hash=sha256_text(text),
            token_estimate=max(1, len(text) // 4),
        )
        for i, (heading, text) in enumerate(blocks)
    ]


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

    def _document_id(
        self, source_path: Path, rel_source: str, previous_id: str | None
    ) -> str:
        """Note id for a source.

        A source that was ingested before keeps its existing id, so its note
        is updated in place (existing notes are never renamed). A new source
        gets an id from its file name, not its title, so two documents titled
        "Meeting Notes" no longer share one note. If that name is already taken
        by another note, a short hash of the source path keeps them apart.
        """
        if previous_id:
            return previous_id
        base = document_id_for(source_path)
        if not self._target_markdown_path(base).exists():
            return base
        return f"{base}-{sha256_text(rel_source)[:6]}"

    # ---- writing notes (human edits survive re-ingest) -------------------

    def _write_note(
        self, md_path: Path, fm_dict: dict, body: str, document: Document
    ) -> tuple[dict, str, str | None]:
        """Write the generated note, keeping what the user added by hand.

        Re-ingesting regenerates the note from its source, except:

        * a ``## My notes`` section (up to the next ``##`` heading) is carried
          over and appended to the new body;
        * frontmatter the user owns is kept: ``domain``, ``topic``, ``status``
          and ``created_at`` when set, ``tags``/``related``/``aliases`` merged,
          and any key the pipeline doesn't generate.

        Returns ``(frontmatter, body, kept_section)``; also updates
        ``document`` so chunking and indexing see the written note.
        """
        kept: str | None = None
        if md_path.exists():
            try:
                old_fm, old_body = load_note(md_path)
            except Exception:  # noqa: BLE001 - unreadable note: regenerate it
                old_fm, old_body = {}, ""
            fm_dict = _merge_frontmatter(fm_dict, old_fm)
            kept = user_section(old_body)
            if kept and kept == user_section(body):
                kept = None  # the source itself has this section; don't duplicate it
            if kept:
                body = f"{body.rstrip()}\n\n{kept}\n"

        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(dump_frontmatter(fm_dict, body), encoding="utf-8")

        document.body = body
        document.domain = fm_dict.get("domain")
        document.topic = fm_dict.get("topic")
        for c in document.chunks:
            c.domain, c.topic = document.domain, document.topic
        return fm_dict, body, kept

    # ---- source preservation --------------------------------------------

    def _archive_path(self, source_path: Path, source_hash: str) -> Path:
        """Where this exact content is (or will be) archived in Sources/.

        Keeps the plain file name when it's free or already holds the same
        content. A different file with the same name gets a content-hash
        suffix instead of overwriting the earlier original.
        """
        dest = self.settings.sources_path / source_path.name
        if dest.exists() and sha256_bytes(dest.read_bytes()) != source_hash:
            dest = dest.with_name(f"{dest.stem}-{source_hash[:8]}{dest.suffix}")
        return dest

    def _preserve_source(self, source_path: Path, source_hash: str) -> Path:
        """Copy the original into the Sources tree, keeping the original."""
        self.settings.sources_path.mkdir(parents=True, exist_ok=True)
        dest = self._archive_path(source_path, source_hash)
        if source_path.resolve() == dest.resolve():
            return dest
        if dest.exists() and sha256_bytes(dest.read_bytes()) == source_hash:
            return dest  # already archived
        # New file, or a hash-named copy left incomplete by a crash.
        shutil.copy2(source_path, dest)
        return dest

    def _vault_rel(self, path: Path) -> str:
        return str(path.relative_to(self.settings.vault_path)).replace("\\", "/")

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

        # Carry the previous document id forward: a re-ingested source updates
        # its existing note even if the converted title changed.
        entry = ManifestEntry(source_path=rel_source, source_hash=source_hash,
                              stage=JobStage.detected,
                              track=classification.track.value,
                              document_id=existing.document_id if existing else None)
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
        archived = self._preserve_source(source_path, source_hash)
        preserved_rel = self._vault_rel(archived)

        # 4. convert (deterministic)
        conversion = self.ks.converter.convert(source_path)
        entry.stage = JobStage.converted
        self.ks.state.upsert_manifest(entry)

        # 5. metadata / frontmatter. Domain/topic stay unset here -- they are
        #    authored in frontmatter or assigned by optional enrichment, not
        #    derived from the folder (flat, metadata-driven organization).
        title = (conversion.title or source_path.stem).strip() or source_path.stem
        document_id = self._document_id(source_path, rel_source, entry.document_id)
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
        fm_dict = {**fm.model_dump(mode="json"), **extra_fm}
        fm_dict, body, kept = self._write_note(md_path, fm_dict, body, document)
        if kept:
            # Re-chunk so the preserved "## My notes" section is searchable.
            document.chunks = self.ks.chunker.chunk_document(document)
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
        self._finalize_source(source_path, archived, source_hash)

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
        archived = self._preserve_source(source_path, source_hash)
        preserved_rel = self._vault_rel(archived)

        doc_struct = self.ks.structured_parser.parse(source_path)
        entry.stage = JobStage.converted
        self.ks.state.upsert_manifest(entry)

        title = source_path.stem
        document_id = self._document_id(source_path, rel_source, entry.document_id)
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
        document.chunks = structured_chunks(
            document, doc_struct, self.settings.structured
        )
        entry.document_id = document_id
        entry.markdown_path = rel_md
        entry.stage = JobStage.chunked
        self.ks.state.upsert_manifest(entry)

        # Entities become wiki links so they join the graph deterministically.
        extra_fm: dict = {
            "record_count": doc_struct.record_count,
            "records_indexed": doc_struct.records_indexed,
            "tables": [t.name for t in doc_struct.tables],
        }
        truncated = [t.name for t in doc_struct.tables if t.truncated]
        if truncated:
            extra_fm["truncated_tables"] = truncated
        if doc_struct.entities:
            fm.related = [f"[[{e}]]" for e in doc_struct.entities[:50]]
            extra_fm["entities"] = doc_struct.entities[:100]
            body += "\n\n## Related\n" + "\n".join(
                f"- [[{e}]]" for e in doc_struct.entities[:50]
            ) + "\n"
            document.body = body

        _, body, kept = self._write_note(
            md_path, {**fm.model_dump(mode="json"), **extra_fm}, body, document
        )
        if kept:
            # Record chunks come from the data; add the user's section on top.
            document.chunks = structured_chunks(
                document, doc_struct, self.settings.structured, my_notes=kept
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

        self._finalize_source(source_path, archived, source_hash)
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
        # Don't mix vectors from a different embedding setup into the index.
        self.ks.ensure_index_matches()
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
            # First vectors written: stamp the index with the setup that made them.
            if self.ks.index_built_with() is None:
                self.ks.record_index_fingerprint()

        self.ks.state.replace_document_chunks(document.id, current)
        return len(to_embed), reused

    # ---- source finalisation --------------------------------------------

    def _finalize_source(
        self, source_path: Path, archived: Path, source_hash: str
    ) -> None:
        """Clear a processed file out of the inbox.

        The original was already archived under ``Sources/`` by
        :meth:`_preserve_source`, so keeping a second copy in
        ``00-Inbox/.processed/`` would duplicate every uploaded file. We remove
        the inbox copy only after checking the archive holds the same content
        (hash, not just size), and otherwise move it into ``.processed/`` so a
        file is never silently lost.
        """
        if not self.settings.ingest.move_processed_sources:
            return
        if source_path.resolve() == archived.resolve():
            return  # ingested straight from Sources/; nothing to clear

        archive_ok = (
            archived.exists() and sha256_bytes(archived.read_bytes()) == source_hash
        )

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
            if archive_ok:
                source_path.unlink()
            return

        try:
            source_path.resolve().relative_to(self.settings.inbox_path.resolve())
        except ValueError:
            return  # not in inbox; leave it alone

        if archive_ok:
            source_path.unlink()
            return

        # Archive copy missing/mismatched -> preserve by moving, don't delete.
        processed_dir = self.settings.inbox_path / self.settings.ingest.processed_marker
        processed_dir.mkdir(parents=True, exist_ok=True)
        dest = processed_dir / source_path.name
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}-{source_hash[:8]}{dest.suffix}")
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
