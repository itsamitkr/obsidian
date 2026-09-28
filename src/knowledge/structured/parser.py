"""Structured-data parser: CSV/TSV, XLSX, JSON, XML -> schema + records.

Deterministic; no LLM. Produces a :class:`StructuredDoc` that the pipeline
renders into a canonical Markdown note, record-level vector chunks, and graph
entities.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime
from pathlib import Path

from ..config import StructuredConfig
from .models import Column, StructuredDoc, Table

_NUM_RE = re.compile(r"^-?\d+(?:[.,]\d+)?%?$")
# Deliberately excludes "0"/"1": a bare 0 or 1 is far more often a number
# (id, count, amount) than a boolean, and mis-typing it made whole columns
# come out as "mixed".
_BOOL = {"true", "false", "yes", "no"}
_DATE_HINTS = ("date", "day", "month", "year", "created", "updated", "time")


def _classify_value(v) -> str:
    if v is None or (isinstance(v, str) and not v.strip()):
        return "empty"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, (datetime, date)):
        return "date"
    s = str(v).strip()
    if s.lower() in _BOOL:
        return "bool"
    if _NUM_RE.match(s.replace(" ", "")):
        return "number"
    return "string"


def _dominant_type(kinds: list[str], column_name: str) -> str:
    real = [k for k in kinds if k != "empty"]
    if not real:
        return "empty"
    uniq = set(real)
    if len(uniq) == 1:
        only = next(iter(uniq))
        if only == "string" and any(h in column_name.lower() for h in _DATE_HINTS):
            return "date"
        return only
    return "mixed"


class StructuredParser:
    """Parses tabular/record files into tables, columns and entities."""

    def __init__(self, cfg: StructuredConfig):
        self.cfg = cfg

    # ---- dispatch --------------------------------------------------------

    def supports(self, path: Path) -> bool:
        return Path(path).suffix.lower() in {
            ".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".json", ".xml",
        }

    def parse(self, path: Path) -> StructuredDoc:
        path = Path(path)
        ext = path.suffix.lower()
        if ext in (".csv", ".tsv"):
            doc = self._parse_delimited(path, ext)
        elif ext in (".xlsx", ".xlsm", ".xls"):
            doc = self._parse_workbook(path)
        elif ext == ".json":
            doc = self._parse_json(path)
        elif ext == ".xml":
            doc = self._parse_xml(path)
        else:  # pragma: no cover - guarded by supports()
            raise ValueError(f"unsupported structured format: {ext}")
        doc.entities = self._extract_entities(doc)
        return doc

    # ---- formats ---------------------------------------------------------

    def _parse_delimited(self, path: Path, ext: str) -> StructuredDoc:
        text = path.read_text(encoding="utf-8", errors="replace")
        if ext == ".tsv":
            delim = "\t"
        else:
            try:
                delim = csv.Sniffer().sniff(
                    text[:4096], delimiters=",;\t|"
                ).delimiter
            except csv.Error:
                delim = ","
        reader = csv.reader(io.StringIO(text), delimiter=delim)
        rows = list(reader)
        if not rows:
            return StructuredDoc(fmt=ext.lstrip("."), notes=["file is empty"])
        header, body = rows[0], rows[1:]
        table = self._build_table(path.stem, header, body)
        return StructuredDoc(fmt=ext.lstrip("."), tables=[table])

    def _parse_workbook(self, path: Path) -> StructuredDoc:
        try:
            import openpyxl  # type: ignore
        except ImportError:
            return StructuredDoc(
                fmt="xlsx",
                notes=["openpyxl not installed; install the 'convert' extra"],
            )
        # data_only=True -> cached values rather than formula strings.
        wb = openpyxl.load_workbook(
            path, read_only=True, data_only=True, keep_links=False
        )
        tables: list[Table] = []
        try:
            for ws in wb.worksheets:
                it = ws.iter_rows(values_only=True)
                header = next(it, None)
                if header is None:
                    continue
                header = [
                    str(h) if h is not None else f"col{i + 1}"
                    for i, h in enumerate(header)
                ]
                body = []
                for count, row in enumerate(it):
                    if count >= self.cfg.max_rows_indexed:
                        break
                    if any(c is not None and str(c).strip() for c in row):
                        body.append(list(row))
                if body or any(header):
                    tables.append(self._build_table(ws.title, header, body))
        finally:
            try:
                wb.close()
            except Exception:
                pass
        return StructuredDoc(fmt="xlsx", tables=tables)

    def _parse_json(self, path: Path) -> StructuredDoc:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            return StructuredDoc(fmt="json", notes=[f"invalid JSON: {exc}"])

        if isinstance(data, list) and data and isinstance(data[0], dict):
            keys: list[str] = []
            for item in data[: self.cfg.max_rows_indexed]:
                for k in item:
                    if k not in keys:
                        keys.append(str(k))
            body = [
                [self._flatten(item.get(k)) for k in keys]
                for item in data[: self.cfg.max_rows_indexed]
            ]
            table = self._build_table(path.stem, keys, body, total=len(data))
            return StructuredDoc(fmt="json", tables=[table])

        if isinstance(data, dict):
            # Object -> a two-column key/value table (config-style documents).
            body = [[str(k), self._flatten(v)] for k, v in data.items()]
            table = self._build_table(path.stem, ["key", "value"], body)
            return StructuredDoc(fmt="json", tables=[table])

        return StructuredDoc(
            fmt="json", notes=["top-level JSON is neither an object nor a record list"]
        )

    def _parse_xml(self, path: Path) -> StructuredDoc:
        try:
            from defusedxml.ElementTree import parse as safe_parse  # type: ignore

            root = safe_parse(str(path)).getroot()
        except Exception:
            try:
                import xml.etree.ElementTree as ET

                root = ET.parse(str(path)).getroot()  # noqa: S314 - local file
            except Exception as exc:
                return StructuredDoc(fmt="xml", notes=[f"invalid XML: {exc}"])

        children = list(root)
        if not children:
            return StructuredDoc(fmt="xml", notes=["no repeating elements found"])

        keys: list[str] = []
        records: list[dict] = []
        for el in children[: self.cfg.max_rows_indexed]:
            rec: dict = dict(el.attrib)
            for sub in el:
                rec[sub.tag] = (sub.text or "").strip()
            if not rec and (el.text or "").strip():
                rec = {el.tag: el.text.strip()}
            for k in rec:
                if k not in keys:
                    keys.append(k)
            records.append(rec)

        body = [[r.get(k, "") for k in keys] for r in records]
        table = self._build_table(
            root.tag or path.stem, keys, body, total=len(children)
        )
        return StructuredDoc(fmt="xml", tables=[table])

    # ---- shared ----------------------------------------------------------

    @staticmethod
    def _flatten(value) -> str:
        """Nested JSON/XML values become compact JSON so nothing is lost."""
        if value is None:
            return ""
        if isinstance(value, (str, int, float, bool)):
            return str(value)
        try:
            return json.dumps(value, ensure_ascii=False)[:500]
        except (TypeError, ValueError):
            return str(value)[:500]

    def _build_table(
        self, name: str, header: list[str], body: list[list], total: int | None = None
    ) -> Table:
        header = [
            (str(h).strip() or f"col{i + 1}") for i, h in enumerate(header)
        ]
        limit = self.cfg.max_rows_indexed
        rows = body[:limit]
        row_total = total if total is not None else len(body)

        columns: list[Column] = []
        for idx, col_name in enumerate(header):
            cells = [r[idx] if idx < len(r) else None for r in rows]
            kinds = [_classify_value(c) for c in cells]
            non_null = sum(1 for k in kinds if k != "empty")
            as_text = [
                str(c).strip() for c, k in zip(cells, kinds, strict=False)
                if k != "empty"
            ]
            distinct_vals = list(dict.fromkeys(as_text))
            col = Column(
                name=col_name,
                dtype=_dominant_type(kinds, col_name),
                non_null=non_null,
                distinct=len(distinct_vals),
                samples=distinct_vals[:5],
            )
            # Keep values only when the column looks categorical.
            if (
                col.dtype in ("string", "bool", "date")
                and 0 < col.distinct <= self.cfg.max_entity_distinct
                and all(len(v) <= self.cfg.max_entity_value_len
                        for v in distinct_vals[:50])
            ):
                col.values = distinct_vals
            columns.append(col)

        return Table(
            name=name or "table",
            columns=columns,
            rows=rows,
            row_count=row_total,
            truncated=row_total > len(rows),
        )

    def _extract_entities(self, doc: StructuredDoc) -> list[str]:
        """Categorical column values become graph entities.

        Numeric and free-text columns are deliberately skipped -- linking every
        distinct amount or comment would create noise, not knowledge.
        """
        seen: dict[str, None] = {}
        for table in doc.tables:
            for col in table.columns:
                for value in col.values:
                    if self._is_entity_like(value):
                        seen.setdefault(value, None)
        return list(seen)[:500]

    @staticmethod
    def _is_entity_like(value: str) -> bool:
        """Filter out placeholders and serialised nested structures.

        A flattened JSON array such as ``["ecm", "legal"]`` is a *container*,
        not an entity -- linking it into the graph would be noise.
        """
        if not value:
            return False
        if value.lower() in ("nan", "none", "null", "n/a", "-", "unknown"):
            return False
        if value[0] in "[{" or value.startswith('"'):
            return False
        return True
