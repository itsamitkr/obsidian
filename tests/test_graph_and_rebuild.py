from __future__ import annotations

from knowledge.rebuild import IndexRebuilder
from knowledge.search import SearchService
from knowledge.vault import init_vault


def test_graph_builds_from_seed(ks):
    init_vault(ks.settings, seed=True)
    ks.rebuild_graph()
    view = ks.graph_store.all()
    ids = {n.id for n in view.nodes}
    assert "welcome" in ids
    assert "getting-started" in ids
    # welcome links/relates to getting-started
    neighbors = ks.graph_store.neighbors("welcome")
    targets = {e.target for e in neighbors.edges}
    assert "getting-started" in targets


def test_broken_links_detected(ks):
    init_vault(ks.settings, seed=True)
    ks.rebuild_graph()
    broken = ks.graph_store.broken_links()
    # getting-started links to [[Advanced Usage]] which has no note -> broken
    broken_targets = {e.target for e in broken}
    assert "advanced-usage" in broken_targets


def test_backlinks(ks):
    init_vault(ks.settings, seed=True)
    ks.rebuild_graph()
    backs = ks.graph_store.backlinks("getting-started")
    assert any(b.source == "welcome" for b in backs)


def test_notes_land_in_content_folder(ks):
    init_vault(ks.settings, seed=True)
    # seed notes live in the flat content folder, not a domain tree
    assert (ks.settings.content_path / "welcome.md").exists()
    assert (ks.settings.content_path / "getting-started.md").exists()


def test_rebuild_vector_index_from_markdown(ks):
    init_vault(ks.settings, seed=True)
    rebuilder = IndexRebuilder(ks)
    report = rebuilder.rebuild_vector_index(full=True)
    assert report.documents >= 2
    assert report.embedded >= 2
    assert len(ks.vector_store) == report.chunks

    results = SearchService(ks).search("getting started next steps", limit=3)
    assert results
    assert any("getting-started" in r.markdown_path for r in results)


def test_rebuild_is_reproducible(ks):
    init_vault(ks.settings, seed=True)
    r1 = IndexRebuilder(ks).rebuild_vector_index(full=True)
    r2 = IndexRebuilder(ks).rebuild_vector_index(full=True)
    assert r1.chunks == r2.chunks


def test_full_rebuild_removes_stale_vectors(ks):
    """A full rebuild must leave ONLY chunks for notes currently on disk.

    Regression: a rebuild that fails to clear the store leaves vectors for
    deleted notes, so search returns results whose files no longer exist.
    """
    init_vault(ks.settings, seed=True)
    rebuilder = IndexRebuilder(ks)
    rebuilder.rebuild_vector_index(full=True)

    # Add a note, index it, then delete the file from disk.
    extra = ks.settings.content_path / "temporary.md"
    extra.write_text(
        "---\nid: temporary\ntitle: Temporary\n---\n\n# Temporary\n\nThrowaway.\n",
        encoding="utf-8",
    )
    rebuilder.rebuild_vector_index(full=True)
    assert ks.vector_store.get("temporary::0") is not None

    extra.unlink()
    report = rebuilder.rebuild_vector_index(full=True)

    # The deleted note must be gone, and totals must agree with the report.
    assert ks.vector_store.get("temporary::0") is None
    assert len(ks.vector_store) == report.chunks
