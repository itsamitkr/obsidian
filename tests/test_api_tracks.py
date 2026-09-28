"""HTTP contract for the three-track endpoints.

These assert the exact JSON keys the Control Panel's inline JS reads. The panel
and the API are edited in different files, so a renamed key is otherwise only
discovered by a human clicking around.

The whole module skips without the optional 'api' extra.
"""

from __future__ import annotations

import csv

import pytest

pytest.importorskip("fastapi", reason="API tests need the 'api' extra")

from fastapi.testclient import TestClient  # noqa: E402

from knowledge.api.app import create_app  # noqa: E402
from knowledge.vault import init_vault  # noqa: E402

SERVICES_CSV = (
    "service,owner,region\n"
    "api,Platform,eu-west-1\n"
    "worker,Platform,eu-west-1\n"
    "ledger,Payments,us-east-1\n"
)


@pytest.fixture
def client(ks):
    init_vault(ks.settings, seed=False)
    with TestClient(create_app(ks)) as c:
        yield c


@pytest.fixture
def big_csv(ks, tmp_path):
    """A row-heavy CSV that must land on the analytical track."""
    p = tmp_path / "telemetry.csv"
    rows = ks.settings.classification.analytical.max_rows + 20
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "value"])
        for i in range(rows):
            w.writerow([i, i * 1.5])
    return p


def _upload(client, name: str, data: bytes):
    return client.post(
        "/ingest/upload",
        files=[("files", (name, data, "application/octet-stream"))],
    )


# ---- panel delivery ---------------------------------------------------------


def test_panel_is_served_uncached(client):
    """A cached panel runs stale JS against a fresh backend -- it looks broken."""
    r = client.get("/")
    assert r.status_code == 200
    assert "no-store" in r.headers["cache-control"]


# ---- /classify --------------------------------------------------------------


def test_classify_is_a_dry_run(client, ks, tmp_path):
    doc = tmp_path / "policy.md"
    doc.write_text("# Policy\n", encoding="utf-8")

    body = client.post("/classify", json={"path": str(doc)}).json()
    assert body["track"] == "document"
    assert body["reasons"]
    # Nothing was ingested.
    assert client.get("/stats").json()["documents_processed"] == 0


def test_classify_explains_an_analytical_verdict(client, big_csv):
    body = client.post("/classify", json={"path": str(big_csv)}).json()
    assert body["track"] == "analytical"
    assert body["requires_optin"] is True
    assert any("rows" in r for r in body["reasons"])


# ---- upload outcomes --------------------------------------------------------


def test_upload_outcome_carries_the_keys_the_panel_renders(client):
    rows = _upload(client, "services.csv", SERVICES_CSV.encode()).json()
    assert len(rows) == 1
    row = rows[0]
    for key in ("filename", "track", "track_reasons", "status",
                "markdown_path", "staged_path", "chunks", "reason", "error"):
        assert key in row, key
    assert row["track"] == "structured"
    assert row["status"] == "processed"
    assert row["chunks"] > 0


def test_upload_of_an_analytical_file_reports_staged(client, big_csv):
    row = _upload(client, big_csv.name, big_csv.read_bytes()).json()[0]
    assert row["status"] == "staged"
    assert row["track"] == "analytical"
    assert row["staged_path"]
    assert row["chunks"] == 0
    # The Detail column falls back to track_reasons for staged rows.
    assert row["track_reasons"]


# ---- /staged and /stats -----------------------------------------------------


def test_staged_endpoint_feeds_the_staged_section(client, big_csv):
    assert client.get("/staged").json() == []          # section stays hidden
    _upload(client, big_csv.name, big_csv.read_bytes())

    items = client.get("/staged").json()
    assert len(items) == 1
    assert {"name", "path", "bytes"} <= set(items[0])
    assert items[0]["name"] == "telemetry.csv"


def test_stats_reports_staged_count_and_per_track_totals(client, big_csv):
    _upload(client, "services.csv", SERVICES_CSV.encode())
    _upload(client, "policy.md", b"# Policy\n\nBody text.")
    _upload(client, big_csv.name, big_csv.read_bytes())

    stats = client.get("/stats").json()
    assert stats["staged_analytical"] == 1
    assert stats["tracks"] == {"document": 1, "structured": 1, "analytical": 1}


# ---- /ingest/promote --------------------------------------------------------


def test_promote_accepts_the_path_from_the_staged_list(client, big_csv):
    _upload(client, big_csv.name, big_csv.read_bytes())
    path = client.get("/staged").json()[0]["path"]

    out = client.post("/ingest/promote", json={"path": path}).json()
    assert out["status"] == "processed"
    assert out["track"] == "structured"       # used in the success toast
    assert out["chunks"] > 0
    assert client.get("/staged").json() == []


def test_promote_accepts_a_bare_filename(client, big_csv):
    _upload(client, big_csv.name, big_csv.read_bytes())
    out = client.post("/ingest/promote", json={"path": "telemetry.csv"}).json()
    assert out["status"] == "processed"


def test_promote_reports_a_missing_file_without_500(client):
    out = client.post("/ingest/promote", json={"path": "nope.csv"}).json()
    assert out["status"] == "skipped"
    assert "not found" in out["reason"]
