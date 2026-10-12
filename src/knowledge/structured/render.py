"""Render parsed structured data into Markdown and embeddable text.

Two different audiences:

* **Markdown note** -- the canonical, human-readable artifact stored in the
  vault (schema summary + a sample of rows). It is the source of truth.
* **Record blocks** -- ``key: value`` text used for embedding. Raw CSV rows
  embed badly: the column names are lost and digits dominate the tokens, so
  retrieval degrades. Restating each row with its field names keeps the
  semantics that make a vector search useful.
"""

from __future__ import annotations

from ..config import StructuredConfig
from .models import StructuredDoc, Table


def _md_escape(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _md_table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(_md_escape(h) for h in header) + " |",
           "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows:
        padded = list(row) + [""] * (len(header) - len(row))
        out.append(
            "| " + " | ".join(
                _md_escape("" if c is None else c) for c in padded[: len(header)]
            ) + " |"
        )
    return "\n".join(out)


def render_markdown(doc: StructuredDoc, title: str, cfg: StructuredConfig) -> str:
    """The canonical note for a structured file."""
    parts: list[str] = [f"# {title}", ""]
    parts.append(
        f"Structured **{doc.fmt.upper()}** source — "
        f"{len(doc.tables)} table(s), {doc.record_count} record(s)."
    )
    for note in doc.notes:
        parts += ["", f"> {note}"]

    for table in doc.tables:
        parts += ["", f"## {table.name}", ""]
        parts.append(f"{table.row_count} row(s), {len(table.columns)} column(s)")
        if table.truncated:
            parts += ["", _truncation_note(table)]
        parts += ["", "### Schema", ""]
        parts.append(
            _md_table(
                ["column", "type", "non-null", "distinct", "examples"],
                [
                    [c.name, c.dtype, c.non_null, c.distinct, ", ".join(c.samples[:3])]
                    for c in table.columns
                ],
            )
        )
        if table.rows:
            shown = table.rows[: cfg.sample_rows]
            parts += ["", f"### Sample rows (first {len(shown)})", ""]
            parts.append(_md_table(table.header, shown))

    if doc.entities:
        parts += ["", "## Entities", ""]
        parts.append(", ".join(f"`{e}`" for e in doc.entities[:100]))

    return "\n".join(parts) + "\n"


def _truncation_note(table: Table) -> str:
    """Says plainly that search only covers part of the table."""
    excluded = table.row_count - table.rows_indexed
    return (
        f"> Partial index: only the first {table.rows_indexed} of "
        f"{table.row_count} rows are searchable ({excluded} not indexed). "
        "Use the original file for questions about the whole table."
    )


def _record_text(record: dict) -> str:
    """One record as labelled fields, skipping empties."""
    lines = []
    for key, value in record.items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            lines.append(f"{key}: {text}")
    return "\n".join(lines)


def record_blocks(
    doc: StructuredDoc, cfg: StructuredConfig
) -> list[tuple[list[str], str]]:
    """Text blocks for embedding, as ``(heading_path, text)`` pairs.

    The first block is a schema overview (so "what columns does X have?"
    retrieves well); the rest are groups of records.
    """
    blocks: list[tuple[list[str], str]] = []

    for table in doc.tables:
        schema_lines = [f"Table: {table.name}", f"Rows: {table.row_count}"]
        if table.truncated:
            schema_lines.append(_truncation_note(table).removeprefix("> "))
        schema_lines.append("Columns:")
        schema_lines += [
            f"  - {c.name} ({c.dtype}"
            + (f", examples: {', '.join(c.samples[:3])}" if c.samples else "")
            + ")"
            for c in table.columns
        ]
        blocks.append(([table.name, "Schema"], "\n".join(schema_lines)))

        records = table.records()
        size = max(1, cfg.records_per_chunk)
        for start in range(0, len(records), size):
            group = records[start : start + size]
            texts = [_record_text(r) for r in group]
            texts = [t for t in texts if t]
            if not texts:
                continue
            first, last = start + 1, start + len(group)
            body = f"Records {first}-{last} of {table.name}\n\n" + "\n\n".join(texts)
            blocks.append(([table.name, f"Records {first}-{last}"], body))

    return blocks


def summarize(doc: StructuredDoc) -> str:
    """Short one-line description for logs/UI."""
    tables = ", ".join(f"{t.name}({t.row_count})" for t in doc.tables[:5])
    return f"{doc.fmt}: {tables}" if tables else f"{doc.fmt}: no tables"


__all__ = ["render_markdown", "record_blocks", "summarize", "Table"]
