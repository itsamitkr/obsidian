"""YAML frontmatter read/write for Markdown notes.

Uses python-frontmatter when present, otherwise a small builtin parser so
the system has no hard dependency for this core capability.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

_DELIM = "---"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a Markdown string into (frontmatter dict, body)."""
    if not text.startswith(_DELIM):
        return {}, text
    lines = text.splitlines()
    # find closing delimiter
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _DELIM:
            end = i
            break
    if end is None:
        return {}, text
    raw_fm = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    try:
        data = yaml.safe_load(raw_fm) or {}
        if not isinstance(data, dict):
            data = {}
    except yaml.YAMLError:
        data = {}
    return data, body


def _yaml_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def dump_frontmatter(metadata: dict[str, Any], body: str) -> str:
    """Serialise frontmatter + body back into a Markdown string."""
    clean = {k: _yaml_default(v) for k, v in metadata.items() if v is not None}
    fm = yaml.safe_dump(clean, sort_keys=False, allow_unicode=True).strip()
    return f"{_DELIM}\n{fm}\n{_DELIM}\n\n{body.strip()}\n"


def load_note(path: Path) -> tuple[dict[str, Any], str]:
    return parse_frontmatter(path.read_text(encoding="utf-8"))


def dump_note(path: Path, metadata: dict[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(metadata, body), encoding="utf-8")
