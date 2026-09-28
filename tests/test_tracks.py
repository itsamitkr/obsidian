"""Pipeline behaviour per track, including the analytical opt-in gate."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from knowledge.classification import Track
from knowledge.ingestion.pipeline import IngestionPipeline
from knowledge.models import JobStage
from knowledge.vault import init_vault


@pytest.fixture
def pipeline(ks) -> IngestionPipeline:
    init_vault(ks.settings, seed=False)
    return IngestionPipeline(ks)


def _drop(ks, name: str, text: str) -> Path:
    ks.settings.inbox_path.mkdir(parents=True, exist_ok=True)
    p = ks.settings.inbox_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _drop_csv(ks, name: str, rows: int) -> Path:
    ks.settings.inbox_path.mkdir(parents=True, exist_ok=True)
    p = ks.settings.inbox_path / name
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["service", "owner"])
        for i in range(rows):
            w.writerow([f"svc-{i}", "Platform"])
    return p


SERVICES_CSV = (
    "service,owner,region\n"
    "api,Platform,eu-west-1\n"
    "worker,Platform,eu-west-1\n"
    "ledger,Payments,us-east-1\n"
)


# ---- track 1: documents -----------------------------------------------------


def test_document_outcome_reports_its_track(pipeline, ks):
    out = pipeline.process_file(_drop(ks, "policy.md", "# Policy\n\nBody text."))
    assert out.status == "processed"
    assert out.track == "document"
    assert out.track_reasons
    assert out.requires_optin is False
    assert out.staged_path is None


def test_unsupported_file_is_skipped_with_a_reason(pipeline, ks):
    p = ks.settings.inbox_path / "archive.zip"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"PK\x03\x04")
    out = pipeline.process_file(p)
    assert out.status == "skipped"
    assert out.track == "unsupported"


# ---- track 2: structured ----------------------------------------------------


def test_structured_csv_produces_a_schema_note(pipeline, ks):
    out = pipeline.process_file(_drop(ks, "services.csv", SERVICES_CSV))
    assert out.status == "processed"
    assert out.track == "structured"
    assert out.chunks >= 2          # schema block + at least one record block

    md = (ks.settings.vault_path / out.markdown_path).read_text(encoding="utf-8")
    assert "### Schema" in md
    assert "structured" in md       # tagged as such in frontmatter
    assert "record_count: 3" in md


def test_structured_chunks_are_labelled_records_not_raw_rows(pipeline, ks):
    out = pipeline.process_file(_drop(ks, "services.csv", SERVICES_CSV))
    texts = [
        ks.vector_store.get(f"{out.document_id}::{i}").payload["text"]
        for i in range(out.chunks)
    ]
    body = "\n".join(texts)
    assert "owner: Platform" in body
    assert "region: eu-west-1" in body


def test_structured_content_is_searchable(pipeline, ks):
    from knowledge.search import SearchService

    pipeline.process_file(_drop(ks, "services.csv", SERVICES_CSV))
    hits = SearchService(ks).search("ledger Payments us-east-1", limit=5)
    assert hits


def test_structured_entities_become_real_graph_nodes(pipeline, ks):
    """Entities are linked, and those links must resolve.

    Writing [[Platform]] without creating a node would make every entity show
    up as a broken link, which poisons the vault's link hygiene reporting.
    """
    out = pipeline.process_file(_drop(ks, "services.csv", SERVICES_CSV))
    graph = ks.graph_store.all()
    entity_ids = {n.id for n in graph.nodes if n.type == "entity"}
    assert {"platform", "payments"} <= {e.lower() for e in entity_ids}

    relations = {e.relation for e in graph.edges if e.source == out.document_id}
    assert "mentions" in relations
    assert [e for e in ks.graph_store.broken_links()
            if e.source == out.document_id] == []


def test_json_and_xml_also_take_the_structured_track(pipeline, ks):
    j = _drop(ks, "vendors.json",
              json.dumps([{"vendor": "Acme", "region": "EU"}]))
    x = _drop(ks, "catalog.xml",
              "<catalog><item><name>Alpha</name></item></catalog>")
    for src in (j, x):
        out = pipeline.process_file(src)
        assert out.status == "processed", out.error
        assert out.track == "structured"


# ---- track 3: analytical ----------------------------------------------------


def test_analytical_file_is_staged_and_never_indexed(pipeline, ks):
    rows = ks.settings.classification.analytical.max_rows + 20
    src = _drop_csv(ks, "telemetry.csv", rows=rows)

    out = pipeline.process_file(src)
    assert out.status == "staged"
    assert out.track == "analytical"
    assert out.requires_optin is True
    assert out.track_reasons

    # Nothing entered the knowledge base.
    assert out.chunks == 0
    assert out.markdown_path is None
    assert len(ks.vector_store) == 0
    assert not list(ks.settings.content_path.glob("telemetry*.md"))

    # The file itself is parked, not lost, and no longer in the inbox.
    staged = ks.settings.vault_path / out.staged_path
    assert staged.exists()
    assert not src.exists()


def test_staged_files_are_listed_for_the_ui(pipeline, ks):
    rows = ks.settings.classification.analytical.max_rows + 20
    pipeline.process_file(_drop_csv(ks, "telemetry.csv", rows=rows))

    listed = pipeline.list_staged()
    assert [it["name"] for it in listed] == ["telemetry.csv"]
    entry = listed[0]
    assert entry["bytes"] > 0
    assert entry["path"].endswith("telemetry.csv")


def test_staging_listing_ignores_its_own_readme(pipeline, ks):
    """Regression: the folder's README showed up as a file awaiting a decision."""
    staging = ks.settings.analytical_path
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "README.md").write_text("# Analytical\n", encoding="utf-8")
    (staging / ".keep").write_text("", encoding="utf-8")
    assert pipeline.list_staged() == []


def test_opt_out_indexes_analytical_files_directly(pipeline, ks):
    """With require_optin off, analytical files are treated as structured."""
    ks.settings.classification.analytical.require_optin = False
    rows = ks.settings.classification.analytical.max_rows + 20
    out = pipeline.process_file(_drop_csv(ks, "telemetry.csv", rows=rows))
    assert out.status == "processed"
    assert out.chunks > 0


# ---- promotion ("Add to Knowledge Base") ------------------------------------


def test_promote_indexes_a_staged_file_on_the_structured_track(pipeline, ks):
    rows = ks.settings.classification.analytical.max_rows + 20
    staged = pipeline.process_file(_drop_csv(ks, "telemetry.csv", rows=rows))
    assert staged.status == "staged"

    out = pipeline.promote(Path(staged.staged_path))
    assert out.status == "processed", out.error
    assert out.track == "structured"
    assert out.chunks > 0
    assert (ks.settings.vault_path / out.markdown_path).exists()
    assert len(ks.vector_store) == out.chunks


def test_promoted_file_leaves_the_staging_area(pipeline, ks):
    """Regression: a promoted file stayed listed as awaiting a decision."""
    rows = ks.settings.classification.analytical.max_rows + 20
    staged = pipeline.process_file(_drop_csv(ks, "telemetry.csv", rows=rows))
    pipeline.promote(Path(staged.staged_path))

    assert pipeline.list_staged() == []
    # The original is preserved in Sources/, so nothing is lost.
    assert (ks.settings.sources_path / "telemetry.csv").exists()


def test_promote_accepts_a_bare_name_and_reports_a_missing_file(pipeline, ks):
    out = pipeline.promote(Path("90-Indexes/Analytical/nope.csv"))
    assert out.status == "skipped"
    assert "not found" in (out.reason or "")


# ---- state / manifest -------------------------------------------------------


def test_manifest_records_the_track_for_each_source(pipeline, ks):
    pipeline.process_file(_drop(ks, "policy.md", "# Policy\n\nBody."))
    pipeline.process_file(_drop(ks, "services.csv", SERVICES_CSV))
    rows = ks.settings.classification.analytical.max_rows + 20
    pipeline.process_file(_drop_csv(ks, "telemetry.csv", rows=rows))

    tracks = {Path(m.source_path).name: m.track for m in ks.state.all_manifest()}
    assert tracks["policy.md"] == "document"
    assert tracks["services.csv"] == "structured"
    assert tracks["telemetry.csv"] == "analytical"


def test_staged_entry_reaches_a_terminal_stage(pipeline, ks):
    """Staging is a complete outcome, not an interrupted job to be retried."""
    rows = ks.settings.classification.analytical.max_rows + 20
    out = pipeline.process_file(_drop_csv(ks, "telemetry.csv", rows=rows))
    entry = ks.state.get_manifest(out.source_path)
    assert entry.stage == JobStage.completed
    assert entry.track == "analytical"
    assert entry.markdown_path is None


def test_inbox_run_routes_a_mixed_batch(pipeline, ks):
    _drop(ks, "policy.md", "# Policy\n\nBody.")
    _drop(ks, "services.csv", SERVICES_CSV)
    _drop_csv(ks, "telemetry.csv",
              rows=ks.settings.classification.analytical.max_rows + 20)

    outcomes = {Path(o.source_path).name: o for o in pipeline.process_inbox()}
    assert outcomes["policy.md"].status == "processed"
    assert outcomes["services.csv"].status == "processed"
    assert outcomes["telemetry.csv"].status == "staged"


def test_force_track_can_send_a_spreadsheet_down_the_document_path(pipeline, ks):
    """The track override is a general escape hatch, not just for promotion."""
    out = pipeline.process_file(
        _drop(ks, "services.csv", SERVICES_CSV), track=Track.document
    )
    assert out.status == "processed"
    assert out.track == "document"
    md = (ks.settings.vault_path / out.markdown_path).read_text(encoding="utf-8")
    assert "### Schema" not in md
