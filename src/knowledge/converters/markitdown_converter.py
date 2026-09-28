"""Microsoft MarkItDown converter.

MarkItDown converts a wide range of formats (PDF, DOCX, PPTX, XLSX, HTML,
CSV, images, ...) to Markdown deterministically. We never pass an LLM to
MarkItDown; conversion stays purely structural.

If the ``markitdown`` package is not installed, construction raises
ImportError and the factory falls back to the plaintext converter.
"""

from __future__ import annotations

from pathlib import Path

from .base import ConversionResult, DocumentConverter

# Formats MarkItDown handles. We deliberately do not claim formats it
# cannot actually process.
_MARKITDOWN_EXTS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".html", ".htm",
    ".csv", ".txt", ".md", ".json", ".xml", ".png", ".jpg", ".jpeg",
    ".msg", ".epub",
}


class MarkItDownConverter(DocumentConverter):
    name = "markitdown"

    def __init__(self, fallback: DocumentConverter | None = None):
        try:
            from markitdown import MarkItDown  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on env
            raise ImportError(
                "markitdown is not installed. Install with "
                "`pip install 'knowledge-system[convert]'` or use the "
                "'auto'/'plaintext' converter."
            ) from exc
        # Do NOT wire an LLM client into MarkItDown; conversion is structural.
        self._md = MarkItDown(enable_plugins=False)
        self._fallback = fallback

    def supports(self, path: Path) -> bool:
        if path.suffix.lower() in _MARKITDOWN_EXTS:
            return True
        return bool(self._fallback and self._fallback.supports(path))

    def convert(self, path: Path) -> ConversionResult:
        if path.suffix.lower() not in _MARKITDOWN_EXTS and self._fallback:
            return self._fallback.convert(path)
        try:
            result = self._md.convert(str(path))
        except Exception:
            if self._fallback and self._fallback.supports(path):
                return self._fallback.convert(path)
            raise
        title = getattr(result, "title", None)
        markdown = getattr(result, "text_content", "") or ""
        return ConversionResult(
            markdown=markdown,
            title=title,
            metadata={"source_ext": path.suffix.lower()},
            converter=self.name,
        )
