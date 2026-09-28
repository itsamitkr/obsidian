"""Deterministic track classification.

No LLM is involved: routing is decided from the extension plus a cheap,
bounded probe of the file's shape (size, row count, sheet count, formula
density). Every decision carries human-readable reasons.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from ..config import ClassificationConfig
from .track import Classification, Track

_SPREADSHEET = {".xlsx", ".xlsm", ".xls"}
_DELIMITED = {".csv", ".tsv"}


class TrackClassifier:
    def __init__(self, cfg: ClassificationConfig):
        self.cfg = cfg

    # ---- public API ------------------------------------------------------

    def classify(self, path: Path, force_track: Track | None = None) -> Classification:
        path = Path(path)
        ext = path.suffix.lower()

        if force_track is not None:
            return Classification(
                track=force_track,
                reasons=[f"explicitly routed to '{force_track.value}'"],
                signals={"extension": ext},
            )

        if not self.cfg.enabled:
            # Classification disabled -> everything takes the document path,
            # which is the historical behaviour.
            return Classification(
                track=Track.document,
                reasons=["classification disabled; using document track"],
                signals={"extension": ext},
            )

        if ext in self.cfg.document_extensions:
            return Classification(
                track=Track.document,
                reasons=[f"'{ext}' is a document format"],
                signals={"extension": ext},
            )

        if ext in self.cfg.structured_extensions:
            return self._classify_structured(path, ext)

        return Classification(
            track=Track.unsupported,
            reasons=[f"'{ext}' is not a supported format"],
            signals={"extension": ext},
        )

    # ---- structured vs analytical ----------------------------------------

    def _classify_structured(self, path: Path, ext: str) -> Classification:
        a = self.cfg.analytical
        signals: dict = {"extension": ext}
        reasons: list[str] = []

        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        signals["bytes"] = size
        if size > a.max_bytes:
            reasons.append(
                f"file is {size / 1_000_000:.1f} MB "
                f"(> {a.max_bytes / 1_000_000:.1f} MB)"
            )

        if ext in _SPREADSHEET:
            signals.update(self._probe_workbook(path))
            sheets = signals.get("sheets")
            formulas = signals.get("formula_cells")
            if sheets and sheets > a.max_sheets:
                reasons.append(f"{sheets} sheets (> {a.max_sheets})")
            if formulas and formulas > a.max_formula_cells:
                reasons.append(
                    f"{formulas}+ formula cells (> {a.max_formula_cells}) "
                    "suggests a model, not reference data"
                )
        elif ext in _DELIMITED:
            rows = self._probe_rows(path, a.max_rows)
            signals["rows_at_least"] = rows
            if rows > a.max_rows:
                reasons.append(f"{rows}+ rows (> {a.max_rows})")
        elif ext == ".json":
            count = self._probe_json(path, a.max_rows)
            if count is not None:
                signals["records_at_least"] = count
                if count > a.max_rows:
                    reasons.append(f"{count}+ records (> {a.max_rows})")

        if reasons:
            return Classification(
                track=Track.analytical,
                reasons=reasons,
                signals=signals,
                requires_optin=a.require_optin,
            )

        return Classification(
            track=Track.structured,
            reasons=[f"'{ext}' is tabular/record data within knowledge-base limits"],
            signals=signals,
        )

    # ---- cheap, bounded probes -------------------------------------------

    def _probe_workbook(self, path: Path) -> dict:
        """Count sheets and (approximately) formula cells without loading all
        values into memory. Returns {} if openpyxl isn't available."""
        try:
            import openpyxl  # type: ignore
        except ImportError:
            return {}
        try:
            wb = openpyxl.load_workbook(
                path, read_only=True, data_only=False, keep_links=False
            )
        except Exception:
            return {}
        try:
            out: dict = {"sheets": len(wb.sheetnames)}
            scanned = 0
            formulas = 0
            limit = self.cfg.analytical.scan_cell_limit
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    for value in row:
                        scanned += 1
                        if isinstance(value, str) and value.startswith("="):
                            formulas += 1
                    if scanned >= limit:
                        break
                if scanned >= limit:
                    break
            out["formula_cells"] = formulas
            out["cells_scanned"] = scanned
            return out
        finally:
            try:
                wb.close()
            except Exception:
                pass

    @staticmethod
    def _probe_rows(path: Path, cap: int) -> int:
        """Count rows, stopping shortly after the cap (cheap for huge files)."""
        rows = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
                for _ in fh:
                    rows += 1
                    if rows > cap:
                        break
        except OSError:
            return 0
        return rows

    @staticmethod
    def _probe_json(path: Path, cap: int) -> int | None:
        """Length of a top-level JSON array, if that's what the file holds."""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(1024).lstrip()
                if not head.startswith("["):
                    return None
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        return len(data) if isinstance(data, list) else None

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def sniff_delimiter(sample: str) -> str:
        try:
            return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            return ","
