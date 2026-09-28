"""No-op LLM provider. Used when no LLM is configured (the default).

It is always "unavailable" so the enrichment layer skips LLM work entirely,
keeping token consumption at zero.
"""

from __future__ import annotations

from ..config import LLMConfig
from .base import LLMMessage, LLMProvider


class NullLLMProvider(LLMProvider):
    name = "null"

    def __init__(self, cfg: LLMConfig | None = None):
        self._cfg = cfg

    @property
    def available(self) -> bool:
        return False

    def complete(self, messages: list[LLMMessage], **kwargs) -> str:
        raise RuntimeError(
            "No LLM configured (llm.provider='null'). Enrichment should check "
            "provider.available before calling complete()."
        )
