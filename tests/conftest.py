"""Shared pytest fixtures.

Every test runs against the dependency-free fallback stack (hash embeddings,
in-memory vector store, sqlite/memory graph, null LLM, stub Jev) so the suite
needs no external services (Qdrant/Ollama) or optional extras.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge.config import Settings
from knowledge.system import KnowledgeSystem


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    vault = tmp_path / "vault"
    s = Settings(
        vault_path=vault,
        state_dir=vault / ".knowledge",
    )
    # Force the dependency-free stack.
    s.converter.provider = "auto"
    s.embedding.provider = "hash"
    s.embedding.dim = 128
    s.vector.provider = "memory"
    s.graph.provider = "memory"
    s.llm.provider = "null"
    s.jev.provider = "stub"
    s.jev.enabled = False
    s.ingest.auto_git_commit = False
    s.ensure_dirs()
    return s


@pytest.fixture
def ks(settings: Settings) -> KnowledgeSystem:
    return KnowledgeSystem(settings)
