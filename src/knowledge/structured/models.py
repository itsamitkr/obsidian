"""Models for parsed structured data."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Column:
    name: str
    dtype: str = "string"          # string | number | bool | date | mixed | empty
    non_null: int = 0
    distinct: int = 0
    samples: list[str] = field(default_factory=list)
    # Distinct values, kept only for low-cardinality columns (entity sources).
    values: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.dtype,
            "non_null": self.non_null,
            "distinct": self.distinct,
            "samples": self.samples[:5],
        }


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0             # total rows seen (may exceed len(rows))
    truncated: bool = False

    @property
    def rows_indexed(self) -> int:
        return len(self.rows)

    @property
    def header(self) -> list[str]:
        return [c.name for c in self.columns]

    def records(self) -> list[dict]:
        """Rows as dicts, which is what renders well for embedding."""
        head = self.header
        return [dict(zip(head, row, strict=False)) for row in self.rows]


@dataclass
class StructuredDoc:
    """Result of parsing one structured file."""

    fmt: str                       # csv | tsv | xlsx | json | xml
    tables: list[Table] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def record_count(self) -> int:
        return sum(t.row_count for t in self.tables)

    @property
    def records_indexed(self) -> int:
        return sum(t.rows_indexed for t in self.tables)

    def to_dict(self) -> dict:
        return {
            "format": self.fmt,
            "tables": [
                {
                    "name": t.name,
                    "rows": t.row_count,
                    "rows_indexed": t.rows_indexed,
                    "truncated": t.truncated,
                    "columns": [c.to_dict() for c in t.columns],
                }
                for t in self.tables
            ],
            "entities": self.entities,
            "record_count": self.record_count,
            "records_indexed": self.records_indexed,
        }
