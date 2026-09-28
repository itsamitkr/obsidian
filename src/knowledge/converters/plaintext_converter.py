"""Builtin deterministic converter used as a fallback when MarkItDown is
not installed. Handles text-ish formats without any third-party deps so the
pipeline is always functional.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
from pathlib import Path

from .base import ConversionResult, DocumentConverter

_TEXT_EXTS = {".txt", ".md", ".markdown"}
_HTML_EXTS = {".html", ".htm"}
_CSV_EXTS = {".csv"}
_JSON_EXTS = {".json"}


def _strip_html(raw: str) -> str:
    # Remove script/style blocks, then tags, then unescape entities.
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</p>", "\n\n", raw)
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class PlainTextConverter(DocumentConverter):
    name = "plaintext"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in (
            _TEXT_EXTS | _HTML_EXTS | _CSV_EXTS | _JSON_EXTS
        )

    def convert(self, path: Path) -> ConversionResult:
        ext = path.suffix.lower()
        raw = path.read_text(encoding="utf-8", errors="replace")
        title = path.stem

        if ext in _TEXT_EXTS:
            markdown = raw
        elif ext in _HTML_EXTS:
            title_match = re.search(r"(?is)<title>(.*?)</title>", raw)
            if title_match:
                title = html.unescape(title_match.group(1).strip())
            markdown = _strip_html(raw)
        elif ext in _CSV_EXTS:
            markdown = self._csv_to_markdown(raw)
        elif ext in _JSON_EXTS:
            markdown = "```json\n" + json.dumps(
                json.loads(raw), indent=2, ensure_ascii=False
            ) + "\n```"
        else:  # pragma: no cover - guarded by supports()
            markdown = raw

        return ConversionResult(
            markdown=markdown,
            title=title,
            metadata={"source_ext": ext},
            converter=self.name,
        )

    @staticmethod
    def _csv_to_markdown(raw: str) -> str:
        reader = csv.reader(io.StringIO(raw))
        rows = list(reader)
        if not rows:
            return ""
        header = rows[0]
        lines = ["| " + " | ".join(header) + " |",
                 "| " + " | ".join("---" for _ in header) + " |"]
        for row in rows[1:]:
            # pad short rows
            row = row + [""] * (len(header) - len(row))
            lines.append("| " + " | ".join(row[: len(header)]) + " |")
        return "\n".join(lines)
