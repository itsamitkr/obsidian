"""Structural Markdown parsing into a heading hierarchy.

Produces a flat list of sections, each carrying the ordered heading path
that leads to it (e.g. ["AWS Landing Zone", "Security", "SCP"]). This is
what the semantic chunker consumes so chunks respect document structure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*```")


@dataclass
class HeadingSection:
    heading_path: list[str] = field(default_factory=list)
    level: int = 0
    text: str = ""       # body content under this heading (excludes heading line)


def parse_sections(body: str) -> list[HeadingSection]:
    """Parse ``body`` into sections keyed by their heading path.

    Content before the first heading is emitted as a section with an empty
    heading path (the document preamble).
    """
    sections: list[HeadingSection] = []
    stack: list[tuple[int, str]] = []   # (level, title)
    current = HeadingSection(heading_path=[], level=0)
    in_fence = False

    def flush():
        nonlocal current
        if current.text.strip() or current.heading_path:
            current.text = current.text.strip("\n")
            sections.append(current)

    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            current.text += line + "\n"
            continue

        heading = None if in_fence else _HEADING_RE.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            # pop stack to parent level
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current = HeadingSection(
                heading_path=[t for _, t in stack],
                level=level,
                text="",
            )
        else:
            current.text += line + "\n"

    flush()
    # Drop a fully empty leading preamble section.
    return [s for s in sections if s.text.strip() or s.heading_path]
