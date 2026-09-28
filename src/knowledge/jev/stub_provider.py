"""Isolated Jev stub / adapter.

This is a clearly isolated placeholder. It implements the JevProvider
interface using a transparent deterministic keyword heuristic so downstream
code has something to call during development and testing. It does NOT
pretend to be the real Jev.

    >>> REAL INTEGRATION POINT <<<
    When the official Jev SDK/API is available, create a new module
    ``jev_provider.py`` with a ``RealJevProvider(JevProvider)`` class that
    calls the real API inside ``classify()``, and register it in
    ``knowledge.jev.build_jev_provider``. Do not modify this stub to call a
    fabricated endpoint.
"""

from __future__ import annotations

import re

from ..config import JevConfig
from .base import JevDecision, JevProvider


class StubJevProvider(JevProvider):
    name = "stub"

    def __init__(self, cfg: JevConfig, disabled: bool = False):
        self._cfg = cfg
        self._disabled = disabled

    @property
    def available(self) -> bool:
        return not self._disabled

    def classify(
        self,
        text: str,
        labels: list[str],
        *,
        task: str = "classification",
        context: dict | None = None,
    ) -> JevDecision:
        if not self.available:
            return JevDecision(
                label="",
                confidence=0.0,
                rationale="Jev is disabled (jev.enabled=false).",
                provider=self.name,
            )
        if not labels:
            return JevDecision(
                label="", confidence=0.0, rationale="no labels supplied",
                provider=self.name,
            )

        # Transparent, deterministic keyword-overlap heuristic. This is a
        # stand-in for a real structured decision engine.
        text_l = text.lower()
        scores: dict[str, int] = {}
        for label in labels:
            tokens = re.findall(r"[a-z0-9]+", label.lower())
            scores[label] = sum(text_l.count(tok) for tok in tokens if tok)

        best = max(labels, key=lambda label_: scores[label_])
        total = sum(scores.values())
        confidence = (scores[best] / total) if total else 0.0
        return JevDecision(
            label=best,
            confidence=round(confidence, 3),
            rationale=f"stub keyword heuristic for task={task}; scores={scores}",
            metadata={"scores": scores},
            provider=self.name,
        )
