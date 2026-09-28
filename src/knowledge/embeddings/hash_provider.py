"""Deterministic hashing embedding provider.

This is a dependency-free fallback so the whole system (ingestion, vector
search, MCP) is runnable and testable without downloading a model. It uses
a hashed bag-of-words projection into a fixed dimension and L2-normalises.

It is NOT semantically strong -- it is a functional stand-in. For real
semantic search, configure the sentence_transformers or openai provider.
"""

from __future__ import annotations

import hashlib
import math
import re

from ..config import EmbeddingConfig
from .base import EmbeddingProvider

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class HashEmbeddingProvider(EmbeddingProvider):
    name = "hash"

    def __init__(self, cfg: EmbeddingConfig):
        self._dim = cfg.dim or 384

    @property
    def dim(self) -> int:
        return self._dim

    def _embed_text(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        tokens = _TOKEN_RE.findall(text.lower())
        for tok in tokens:
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "little") % self._dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_text(t) for t in texts]
