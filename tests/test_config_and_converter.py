from __future__ import annotations

from pathlib import Path

from knowledge.config import load_settings
from knowledge.converters import build_converter
from knowledge.converters.plaintext_converter import PlainTextConverter
from knowledge.ingestion.lint import lint_markdown


def test_load_default_settings():
    s = load_settings()
    assert s.chunking.max_tokens > 0
    assert s.embedding.dim > 0
    assert s.vector.collection


def test_env_override(monkeypatch):
    monkeypatch.setenv("KS_VECTOR__PROVIDER", "qdrant")
    monkeypatch.setenv("KS_EMBEDDING__DIM", "256")
    s = load_settings()
    assert s.vector.provider == "qdrant"
    assert s.embedding.dim == 256


def test_plaintext_converter_csv(tmp_path: Path):
    p = tmp_path / "data.csv"
    p.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    conv = PlainTextConverter()
    assert conv.supports(p)
    result = conv.convert(p)
    assert "| a | b |" in result.markdown
    assert "| 1 | 2 |" in result.markdown


def test_plaintext_converter_html(tmp_path: Path):
    p = tmp_path / "page.html"
    p.write_text("<html><title>Hi</title><body><p>Hello</p></body></html>",
                 encoding="utf-8")
    result = PlainTextConverter().convert(p)
    assert result.title == "Hi"
    assert "Hello" in result.markdown


def test_converter_factory_auto():
    from knowledge.config import ConverterConfig, IngestConfig
    conv = build_converter(ConverterConfig(provider="auto"), IngestConfig())
    assert conv is not None


def test_lint_flags_issues():
    issues = lint_markdown("body with ``` unbalanced", {"id": "x"})
    assert any("code fence" in i for i in issues)
    assert any("title" in i for i in issues)  # missing title
