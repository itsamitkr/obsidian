"""Document conversion providers (source file -> Markdown).

Conversion is deterministic and MUST NOT use an LLM.
"""

from __future__ import annotations

from ..config import ConverterConfig, IngestConfig
from .base import ConversionResult, DocumentConverter
from .markitdown_converter import MarkItDownConverter
from .plaintext_converter import PlainTextConverter


def build_converter(
    converter_cfg: ConverterConfig, ingest_cfg: IngestConfig
) -> DocumentConverter:
    """Factory selecting a converter, with graceful fallback.

    ``auto`` uses MarkItDown when it is importable, otherwise a builtin
    plaintext/HTML/CSV converter so the system still works with no extras.
    """
    if converter_cfg.provider in ("markitdown", "auto"):
        try:
            return MarkItDownConverter(fallback=PlainTextConverter())
        except ImportError:
            if converter_cfg.provider == "markitdown":
                raise
            return PlainTextConverter()
    return PlainTextConverter()


__all__ = [
    "DocumentConverter",
    "ConversionResult",
    "MarkItDownConverter",
    "PlainTextConverter",
    "build_converter",
]
