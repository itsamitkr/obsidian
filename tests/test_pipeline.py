from __future__ import annotations

from pathlib import Path

from knowledge.ingestion.pipeline import IngestionPipeline
from knowledge.models import JobStage
from knowledge.vault import init_vault


def _drop(ks, name: str, content: str) -> Path:
    ks.settings.inbox_path.mkdir(parents=True, exist_ok=True)
    p = ks.settings.inbox_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_end_to_end_ingest_txt(ks):
    init_vault(ks.settings, seed=False)
    src = _drop(ks, "note.txt", "# AWS\n\nLanding zone content about security.")
    pipeline = IngestionPipeline(ks)
    outcome = pipeline.process_file(src)

    assert outcome.status == "processed"
    assert outcome.document_id == "note"
    assert outcome.chunks >= 1
    assert outcome.embedded >= 1

    # canonical markdown written
    md = ks.settings.vault_path / outcome.markdown_path
    assert md.exists()
    assert "AWS" in md.read_text(encoding="utf-8")

    # original source preserved
    assert list(ks.settings.sources_path.glob("note.txt"))

    # manifest completed
    entry = ks.state.get_manifest(outcome.source_path)
    assert entry.stage == JobStage.completed

    # vectors indexed
    assert len(ks.vector_store) == outcome.chunks


def test_idempotent_reingest_skips(ks):
    init_vault(ks.settings, seed=False)
    src = _drop(ks, "dup.txt", "content here")
    pipeline = IngestionPipeline(ks)
    first = pipeline.process_file(src)
    assert first.status == "processed"

    # re-drop identical content -> skipped by hash
    src2 = _drop(ks, "dup.txt", "content here")
    second = pipeline.process_file(src2)
    assert second.status == "skipped"
    assert "already processed" in (second.reason or "")


def test_incremental_reembed_on_change(ks):
    init_vault(ks.settings, seed=False)
    src = _drop(ks, "chg.txt", "# H\n\noriginal body paragraph one.")
    pipeline = IngestionPipeline(ks)
    r1 = pipeline.process_file(src)
    embedded_first = r1.embedded

    # modify content and force reprocess
    src2 = _drop(ks, "chg.txt", "# H\n\noriginal body paragraph one. plus new text.")
    r2 = pipeline.process_file(src2, force=True)
    assert r2.status == "processed"
    assert embedded_first >= 1


def test_delete_removes_from_index(ks):
    init_vault(ks.settings, seed=False)
    src = _drop(ks, "del.txt", "to be deleted content")
    pipeline = IngestionPipeline(ks)
    out = pipeline.process_file(src)
    doc_id = out.document_id
    assert len(ks.vector_store) >= 1

    # deleting by the original source path (its manifest key) clears the index
    result = pipeline.handle_deleted(src)
    assert result.status == "processed"
    assert ks.vector_store.get(f"{doc_id}::0") is None
    assert ks.state.get_manifest(out.source_path) is None


def test_watcher_does_not_deindex_consumed_source(ks):
    """Regression: the pipeline clears the inbox file after archiving it. A
    follow-up filesystem event must NOT be treated as a user deletion, or the
    note we just indexed gets wiped from the vector store and graph."""
    from knowledge.ingestion.watcher import InboxWatcher

    init_vault(ks.settings, seed=False)
    src = _drop(ks, "keepme.txt", "# Keep Me\n\nThis must stay indexed.")

    watcher = InboxWatcher(ks)
    outcome = watcher.pipeline.process_file(src)
    assert outcome.status == "processed"
    doc_id = outcome.document_id
    assert ks.vector_store.get(f"{doc_id}::0") is not None

    # The source no longer exists (archived + cleared from the inbox).
    assert not src.exists()
    assert watcher._was_consumed(src) is True

    # Simulate the trailing create/modify event for the vanished path.
    watcher._schedule(str(src))
    watcher.debounce = 0
    watcher._process_ready()

    # Still indexed: the note was not de-indexed by the phantom delete.
    assert ks.vector_store.get(f"{doc_id}::0") is not None
    assert ks.state.get_manifest(outcome.source_path) is not None


def test_watcher_handles_real_deletion(ks):
    """A source that was never completed should still be removable."""
    from knowledge.ingestion.watcher import InboxWatcher

    init_vault(ks.settings, seed=False)
    watcher = InboxWatcher(ks)
    ghost = ks.settings.inbox_path / "never-processed.txt"
    # No manifest entry -> not consumed -> deletion path is allowed to run.
    assert watcher._was_consumed(ghost) is False
