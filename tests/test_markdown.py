from __future__ import annotations

from knowledge.markdown.frontmatter import dump_frontmatter, parse_frontmatter
from knowledge.markdown.links import extract_links
from knowledge.markdown.parser import parse_sections


def test_frontmatter_roundtrip():
    meta = {"id": "x", "title": "Test", "tags": ["a", "b"]}
    body = "# Test\n\nhello"
    text = dump_frontmatter(meta, body)
    parsed_meta, parsed_body = parse_frontmatter(text)
    assert parsed_meta["id"] == "x"
    assert parsed_meta["tags"] == ["a", "b"]
    assert "hello" in parsed_body


def test_parse_frontmatter_none():
    meta, body = parse_frontmatter("# no frontmatter\n\ntext")
    assert meta == {}
    assert body.startswith("# no frontmatter")


def test_extract_links():
    body = (
        "See [[AWS Landing Zone]] and [[SCP|Service Control Policy]].\n"
        "#aws #architecture\n"
        "[external](https://example.com)\n"
    )
    fm = {"related": ["[[Control Tower]]"], "tags": ["governance"]}
    links = extract_links(body, fm)
    assert "AWS Landing Zone" in links.wiki_links
    assert "SCP" in links.wiki_links
    assert "aws" in links.tags
    assert "governance" in links.tags
    assert "Control Tower" in links.related
    assert "https://example.com" in links.markdown_links


def test_parse_sections_hierarchy():
    body = (
        "# Root\n\nlead\n\n"
        "## Security\n\nsec body\n\n"
        "### SCP\n\nscp body\n"
    )
    sections = parse_sections(body)
    paths = [s.heading_path for s in sections]
    assert ["Root"] in paths
    assert ["Root", "Security"] in paths
    assert ["Root", "Security", "SCP"] in paths


def test_parse_sections_ignores_headings_in_code():
    body = "# Real\n\n```\n# not a heading\n```\n"
    sections = parse_sections(body)
    paths = [s.heading_path for s in sections]
    assert ["Real"] in paths
    assert ["# not a heading"] not in paths
