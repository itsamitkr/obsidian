"""Structured parsing: schema inference, record blocks, entity extraction."""

from __future__ import annotations

import json

import pytest

from knowledge.structured import (
    StructuredParser,
    record_blocks,
    render_markdown,
    summarize,
)


@pytest.fixture
def parser(settings) -> StructuredParser:
    return StructuredParser(settings.structured)


CSV_BODY = (
    "service,owner,region,instances,active,created\n"
    "api,Platform,eu-west-1,4,true,2024-01-05\n"
    "worker,Platform,eu-west-1,2,true,2024-02-11\n"
    "legacy,Payments,us-east-1,1,false,2023-07-30\n"
)


def test_csv_schema_and_types(parser, tmp_path):
    p = tmp_path / "services.csv"
    p.write_text(CSV_BODY, encoding="utf-8")

    doc = parser.parse(p)
    assert doc.fmt == "csv"
    assert len(doc.tables) == 1
    table = doc.tables[0]
    assert table.row_count == 3
    assert table.header == [
        "service", "owner", "region", "instances", "active", "created",
    ]

    types = {c.name: c.dtype for c in table.columns}
    assert types["service"] == "string"
    assert types["instances"] == "number"
    assert types["active"] == "bool"
    assert types["created"] == "date"


def test_numeric_id_column_is_not_mistaken_for_boolean(parser, tmp_path):
    """Regression: 0/1 in an id column used to be read as bool -> 'mixed'."""
    p = tmp_path / "ids.csv"
    p.write_text("id,label\n0,zero\n1,one\n2,two\n", encoding="utf-8")
    col = {c.name: c for c in parser.parse(p).tables[0].columns}["id"]
    assert col.dtype == "number"


def test_tsv_is_parsed_with_tab_delimiter(parser, tmp_path):
    p = tmp_path / "t.tsv"
    p.write_text("a\tb\n1\tx\n2\ty\n", encoding="utf-8")
    table = parser.parse(p).tables[0]
    assert table.header == ["a", "b"]
    assert table.row_count == 2


def test_semicolon_delimiter_is_sniffed(parser, tmp_path):
    p = tmp_path / "eu.csv"
    p.write_text("name;city\nAda;Paris\nGrace;Berlin\n", encoding="utf-8")
    assert parser.parse(p).tables[0].header == ["name", "city"]


def test_empty_file_reports_a_note_rather_than_crashing(parser, tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    doc = parser.parse(p)
    assert doc.tables == []
    assert doc.notes


def test_row_cap_truncates_and_records_the_true_total(parser, settings, tmp_path):
    settings.structured.max_rows_indexed = 5
    p = tmp_path / "many.csv"
    p.write_text("a\n" + "\n".join(str(i) for i in range(50)) + "\n",
                 encoding="utf-8")
    table = StructuredParser(settings.structured).parse(p).tables[0]
    assert len(table.rows) == 5
    assert table.row_count == 50
    assert table.truncated is True


# ---- entities ---------------------------------------------------------------


def test_categorical_columns_become_entities(parser, tmp_path):
    p = tmp_path / "services.csv"
    p.write_text(CSV_BODY, encoding="utf-8")
    entities = parser.parse(p).entities
    assert "Platform" in entities
    assert "eu-west-1" in entities
    # Numbers are not knowledge -- linking every count would be noise.
    assert "4" not in entities


def test_high_cardinality_columns_are_not_entities(parser, settings, tmp_path):
    settings.structured.max_entity_distinct = 3
    p = tmp_path / "wide.csv"
    p.write_text("code\n" + "\n".join(f"c{i}" for i in range(20)) + "\n",
                 encoding="utf-8")
    assert StructuredParser(settings.structured).parse(p).entities == []


def test_serialised_containers_and_placeholders_are_not_entities(parser, tmp_path):
    """Regression: a flattened JSON array is a container, not an entity."""
    p = tmp_path / "nested.json"
    p.write_text(
        json.dumps([
            {"team": ["ecm", "legal"], "status": "active"},
            {"team": ["ecm"], "status": "n/a"},
        ]),
        encoding="utf-8",
    )
    entities = parser.parse(p).entities
    assert "active" in entities
    assert not any(e.startswith("[") for e in entities)
    assert "n/a" not in entities


# ---- other formats ----------------------------------------------------------


def test_json_record_list(parser, tmp_path):
    p = tmp_path / "vendors.json"
    p.write_text(
        json.dumps([
            {"vendor": "Acme", "region": "EU"},
            {"vendor": "Globex", "region": "US", "tier": "gold"},
        ]),
        encoding="utf-8",
    )
    table = parser.parse(p).tables[0]
    # Keys are unioned across records, in first-seen order.
    assert table.header == ["vendor", "region", "tier"]
    assert table.row_count == 2


def test_json_object_becomes_key_value_table(parser, tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"provider": "qdrant", "dim": 384}), encoding="utf-8")
    table = parser.parse(p).tables[0]
    assert table.header == ["key", "value"]
    assert table.row_count == 2


def test_invalid_json_is_reported_not_raised(parser, tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    doc = parser.parse(p)
    assert doc.tables == []
    assert any("invalid JSON" in n for n in doc.notes)


def test_xml_repeating_elements(parser, tmp_path):
    p = tmp_path / "catalog.xml"
    p.write_text(
        "<catalog>"
        '<item id="1"><name>Alpha</name><owner>Platform</owner></item>'
        '<item id="2"><name>Beta</name><owner>Payments</owner></item>'
        "</catalog>",
        encoding="utf-8",
    )
    doc = parser.parse(p)
    table = doc.tables[0]
    assert table.row_count == 2
    assert set(table.header) == {"id", "name", "owner"}
    assert "Platform" in doc.entities


def test_xlsx_sheets_become_tables(parser, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Vendors"
    ws.append(["vendor", "spend"])
    ws.append(["Acme", 100])
    second = wb.create_sheet("Regions")
    second.append(["region"])
    second.append(["EU"])
    p = tmp_path / "book.xlsx"
    wb.save(p)

    doc = parser.parse(p)
    assert [t.name for t in doc.tables] == ["Vendors", "Regions"]
    assert doc.record_count == 2


def test_supports_matches_the_configured_formats(parser, tmp_path):
    assert parser.supports(tmp_path / "a.csv")
    assert parser.supports(tmp_path / "a.xlsx")
    assert not parser.supports(tmp_path / "a.pdf")


# ---- rendering --------------------------------------------------------------


def test_markdown_note_summarises_schema_and_samples(parser, settings, tmp_path):
    p = tmp_path / "services.csv"
    p.write_text(CSV_BODY, encoding="utf-8")
    doc = parser.parse(p)

    md = render_markdown(doc, "Services", settings.structured)
    assert md.startswith("# Services")
    assert "### Schema" in md
    assert "Sample rows" in md
    assert "| service | owner | region" in md
    assert "## Entities" in md


def test_markdown_escapes_pipes_so_tables_stay_valid(parser, settings, tmp_path):
    p = tmp_path / "piped.csv"
    p.write_text('name\n"a|b"\n', encoding="utf-8")
    md = render_markdown(parser.parse(p), "Piped", settings.structured)
    assert r"a\|b" in md


def test_record_blocks_label_each_field(parser, settings, tmp_path):
    p = tmp_path / "services.csv"
    p.write_text(CSV_BODY, encoding="utf-8")
    doc = parser.parse(p)

    blocks = record_blocks(doc, settings.structured)
    headings = [h for h, _ in blocks]
    assert headings[0][-1] == "Schema"

    body = "\n".join(text for _, text in blocks)
    # Naked CSV rows embed poorly; fields must be restated with their names.
    assert "owner: Platform" in body
    assert "region: eu-west-1" in body


def test_record_blocks_group_by_configured_size(parser, settings, tmp_path):
    settings.structured.records_per_chunk = 2
    p = tmp_path / "rows.csv"
    p.write_text("a\n" + "\n".join(f"v{i}" for i in range(10)) + "\n",
                 encoding="utf-8")
    doc = StructuredParser(settings.structured).parse(p)
    blocks = record_blocks(doc, settings.structured)
    # 1 schema block + ceil(10 / 2) record blocks
    assert len(blocks) == 1 + 5


def test_summarize_is_a_single_line(parser, tmp_path):
    p = tmp_path / "services.csv"
    p.write_text(CSV_BODY, encoding="utf-8")
    line = summarize(parser.parse(p))
    assert "\n" not in line
    assert "csv" in line
