"""Lightweight deterministic Markdown validation.

Not a full markdownlint replacement -- a set of cheap checks that flag
common issues in generated notes. Returns a list of human-readable issues
(empty list == clean).
"""

from __future__ import annotations

import re

_REQUIRED_FM = ("id", "title")


def lint_markdown(body: str, frontmatter: dict | None = None) -> list[str]:
    issues: list[str] = []
    frontmatter = frontmatter or {}

    for key in _REQUIRED_FM:
        if not frontmatter.get(key):
            issues.append(f"frontmatter missing required key '{key}'")

    if not body.strip():
        issues.append("document body is empty")

    # Unbalanced code fences
    if body.count("```") % 2 != 0:
        issues.append("unbalanced code fence (```) count")

    # Unclosed wiki links
    if body.count("[[") != body.count("]]"):
        issues.append("unbalanced wiki-link brackets")

    # Multiple H1 headings (Obsidian prefers one document title)
    h1s = re.findall(r"^#\s+", body, flags=re.MULTILINE)
    if len(h1s) > 1:
        issues.append(f"multiple H1 headings ({len(h1s)})")

    # Trailing whitespace lines (informational)
    if re.search(r"[ \t]+$", body, flags=re.MULTILINE):
        issues.append("trailing whitespace present")

    return issues
