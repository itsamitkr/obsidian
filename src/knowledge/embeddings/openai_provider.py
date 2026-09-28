"""OpenAI-compatible embedding provider.

Works against any OpenAI-compatible endpoint (OpenAI, Ollama, LM Studio,
vLLM, ...). Configure ``base_url``, ``api_key`` and ``model``.
"""

from __future__ import annotations

import httpx

from ..config import EmbeddingConfig
from .base import EmbeddingProvider


class OpenAIEmbeddingProvider(EmbeddingProvider):
    name = "openai"

    def __init__(self, cfg: EmbeddingConfig):
        if not cfg.base_url:
            raise ValueError("embedding.base_url is required for the openai provider")
        self._url = cfg.base_url.rstrip("/") + "/embeddings"
        self._model = cfg.model
        self._dim = cfg.dim
        self._headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            self._headers["Authorization"] = f"Bearer {cfg.api_key}"
        self._batch = cfg.batch_size

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        with httpx.Client(timeout=60.0) as client:
            for i in range(0, len(texts), self._batch):
                batch = texts[i : i + self._batch]
                resp = client.post(
                    self._url,
                    headers=self._headers,
                    json={"model": self._model, "input": batch},
                )
                resp.raise_for_status()
                data = resp.json()["data"]
                data.sort(key=lambda d: d.get("index", 0))
                out.extend(d["embedding"] for d in data)
        # Keep configured dim in sync with reality on first call.
        if out and self._dim != len(out[0]):
            self._dim = len(out[0])
        return out
