"""LLM providers behind a common interface. The LLM is optional and only
used for reasoning/enrichment tasks, never for document conversion."""

from __future__ import annotations

from ..config import LLMConfig
from .base import LLMProvider
from .null_provider import NullLLMProvider


def build_llm_provider(cfg: LLMConfig) -> LLMProvider:
    if cfg.provider == "openai":
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(cfg)
    return NullLLMProvider(cfg)


__all__ = ["LLMProvider", "NullLLMProvider", "build_llm_provider"]
