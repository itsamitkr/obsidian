"""Deterministic extraction of relationships from Markdown.

Sources of relationships (no LLM required):
  * Wiki links:      [[Target]] or [[Target|Alias]] or [[Target#Heading]]
  * Tags:            #tag inline, or `tags:` in frontmatter
  * frontmatter `related:` list (e.g. "[[AWS Control Tower]]")
  * Markdown links:  [text](path.md)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WIKI_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9][\w/-]*)")
_MD_LINK_RE = re.compile(r"(?<!\!)\[([^\]]+)\]\(([^)]+)\)")


@dataclass
class LinkInfo:
    wiki_links: list[str] = field(default_factory=list)      # target note titles
    tags: list[str] = field(default_factory=list)
    markdown_links: list[str] = field(default_factory=list)  # relative paths/urls
    related: list[str] = field(default_factory=list)         # from frontmatter


def _normalize_wiki_target(raw: str) -> str:
    # [[Target|Alias]] -> Target ; [[Target#Heading]] -> Target
    target = raw.split("|", 1)[0]
    target = target.split("#", 1)[0]
    return target.strip()


def _strip_code(text: str) -> str:
    """Remove fenced blocks and inline code spans.

    Documentation frequently *mentions* link syntax -- e.g. ``[[wiki links]]``
    inside backticks -- and treating those as real edges invents relationships
    and pollutes broken-link detection.
    """
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"~~~.*?~~~", " ", text, flags=re.DOTALL)
    return re.sub(r"`[^`\n]*`", " ", text)


def extract_links(body: str, frontmatter: dict | None = None) -> LinkInfo:
    info = LinkInfo()
    seen_wiki: set[str] = set()
    # Links are only read from prose, never from code samples.
    body = _strip_code(body)

    for m in _WIKI_RE.finditer(body):
        target = _normalize_wiki_target(m.group(1))
        if target and target not in seen_wiki:
            seen_wiki.add(target)
            info.wiki_links.append(target)

    seen_tags: set[str] = set()
    # `body` already has code stripped, so '#' inside code won't become a tag.
    for m in _TAG_RE.finditer(body):
        tag = m.group(1)
        if tag not in seen_tags:
            seen_tags.add(tag)
            info.tags.append(tag)

    for m in _MD_LINK_RE.finditer(body):
        info.markdown_links.append(m.group(2).strip())

    if frontmatter:
        for tag in frontmatter.get("tags", []) or []:
            if tag not in seen_tags:
                seen_tags.add(tag)
                info.tags.append(str(tag))
        for rel in frontmatter.get("related", []) or []:
            target = _normalize_wiki_target(str(rel).strip("[]"))
            if target:
                info.related.append(target)

    return info
