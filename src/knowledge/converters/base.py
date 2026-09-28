"""DocumentConverter abstraction.

    DocumentConverter
        |
        +-- MarkItDownConverter
        +-- PlainTextConverter (builtin fallback)

Future converters (e.g. a specialised PDF or OCR converter) implement the
same interface without touching the rest of the pipeline.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConversionResult:
    markdown: str
    title: str | None = None
    metadata: dict = field(default_factory=dict)
    converter: str = "unknown"


class DocumentConverter(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def supports(self, path: Path) -> bool:
        """Return True if this converter can handle the given file."""

    @abc.abstractmethod
    def convert(self, path: Path) -> ConversionResult:
        """Convert a source file to Markdown. Must be deterministic (no LLM)."""
