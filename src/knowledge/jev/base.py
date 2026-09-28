"""JevProvider interface for structured classification / decision tasks."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class JevDecision:
    """Result of a structured classification/decision.

    ``label`` is the chosen category, ``confidence`` in [0, 1], and
    ``rationale``/``metadata`` carry any explanation the provider offers.
    """

    label: str
    confidence: float
    rationale: str | None = None
    metadata: dict = field(default_factory=dict)
    provider: str = "unknown"


class JevProvider(abc.ABC):
    name: str = "base"

    @property
    @abc.abstractmethod
    def available(self) -> bool:
        ...

    @abc.abstractmethod
    def classify(
        self,
        text: str,
        labels: list[str],
        *,
        task: str = "classification",
        context: dict | None = None,
    ) -> JevDecision:
        """Classify ``text`` into one of ``labels`` (or a free label)."""
