"""LLMProvider interface."""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass
class LLMMessage:
    role: str   # system | user | assistant
    content: str


class LLMProvider(abc.ABC):
    name: str = "base"

    @property
    def available(self) -> bool:
        """Whether the provider can actually service requests."""
        return True

    @abc.abstractmethod
    def complete(self, messages: list[LLMMessage], **kwargs) -> str:
        """Return a single completion string."""

    def complete_json(self, messages: list[LLMMessage], **kwargs) -> dict:
        """Return a parsed JSON object from the completion.

        Providers may override for native JSON modes. The default parses the
        text response and is resilient to code fences.
        """
        import json
        import re

        raw = self.complete(messages, **kwargs).strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group(0)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
