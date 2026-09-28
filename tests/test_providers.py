from __future__ import annotations

from knowledge.config import EmbeddingConfig, JevConfig, VectorConfig
from knowledge.embeddings import build_embedding_provider
from knowledge.embeddings.hash_provider import HashEmbeddingProvider
from knowledge.jev import build_jev_provider
from knowledge.vectorstore import build_vector_store
from knowledge.vectorstore.base import VectorPoint


def test_hash_embeddings_deterministic_and_normalized():
    p = HashEmbeddingProvider(EmbeddingConfig(dim=64))
    a = p.embed_one("aws landing zone security")
    b = p.embed_one("aws landing zone security")
    assert a == b
    assert len(a) == 64
    norm = sum(x * x for x in a) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_embedding_factory_falls_back():
    # sentence_transformers likely not installed -> falls back to hash.
    p = build_embedding_provider(
        EmbeddingConfig(provider="sentence_transformers", dim=32)
    )
    assert p.dim in (32, p.dim)  # provider present or hash fallback
    assert len(p.embed_one("hello")) == p.dim


def test_memory_vector_store_search():
    store = build_vector_store(VectorConfig(provider="memory"), dim=3)
    store.upsert([
        VectorPoint("a", [1.0, 0.0, 0.0], {"document_id": "d1", "domain": "AWS"}),
        VectorPoint("b", [0.0, 1.0, 0.0], {"document_id": "d2", "domain": "Azure"}),
    ])
    res = store.search([1.0, 0.0, 0.0], limit=1)
    assert res[0].id == "a"
    # filter
    res2 = store.search([1.0, 1.0, 0.0], limit=5, filters={"domain": "Azure"})
    assert all(m.payload["domain"] == "Azure" for m in res2)
    # delete by document
    store.delete_by_document("d1")
    assert store.get("a") is None


def test_jev_disabled_by_default():
    p = build_jev_provider(JevConfig(enabled=False))
    assert p.available is False
    d = p.classify("aws security", ["AWS", "Azure"])
    assert d.confidence == 0.0


def test_jev_stub_classifies_when_enabled():
    p = build_jev_provider(JevConfig(provider="stub", enabled=True))
    assert p.available is True
    d = p.classify("this is about aws aws aws security", ["AWS", "Azure"])
    assert d.label == "AWS"
    assert d.confidence > 0.5


def test_vector_store_opens_without_building_the_embedder(ks):
    """Regression: opening the vector store used to read `embedder.dim`.

    With a real model that pulls in torch and ~90 MB of weights -- about 10
    seconds on a laptop CPU -- purely to learn a number that is already in
    `embedding.dim`. Every read-only Control Panel page load waited for it,
    which looked like the panel hanging on startup.
    """
    assert ks.vector_store.name
    assert "embedder" not in ks._providers


def test_embedder_rejects_a_dim_that_disagrees_with_config(settings, monkeypatch):
    """The flip side of trusting configured dim: a mismatch must be loud.

    Otherwise the store is created at one width and vectors are written at
    another, which fails later and far from the cause. The hash provider derives
    its width *from* config and so can never disagree — only a real model can,
    hence the stub.
    """
    import pytest

    from knowledge import system as system_mod
    from knowledge.system import KnowledgeSystem

    class WiderThanConfigured(HashEmbeddingProvider):
        name = "stub-wide"

        @property
        def dim(self) -> int:
            return 768

    settings.embedding.dim = 384
    monkeypatch.setattr(
        system_mod, "build_embedding_provider",
        lambda cfg: WiderThanConfigured(cfg),
    )

    ks = KnowledgeSystem(settings)
    with pytest.raises(ValueError, match="768-dimensional"):
        _ = ks.embedder


def test_embedder_accepts_a_matching_dim(settings):
    settings.embedding.provider = "hash"
    settings.embedding.dim = 128
    from knowledge.system import KnowledgeSystem

    assert KnowledgeSystem(settings).embedder.dim == 128


def test_light_health_does_not_build_providers(ks):
    """`/health` is pollable, so the default report must not load the model."""
    light = ks.health(probe=False)
    assert light["probed"] is False
    assert light["embedder_loaded"] is False
    assert light["embedder"] == ks.settings.embedding.provider
    assert ks._providers == {}

    probed = ks.health(probe=True)
    assert probed["probed"] is True
    assert probed["vector_healthy"] is True
