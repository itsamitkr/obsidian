"""Jev: optional structured classification / decision provider.

Jev is treated strictly as an optional provider behind the
:class:`JevProvider` interface. We do NOT fabricate a Jev API. If a real
Jev SDK becomes available in the environment, implement a concrete adapter
(see ``stub_provider.py`` for the exact integration point) and register it
in :func:`build_jev_provider`. The system never depends on Jev being
installed.
"""

from __future__ import annotations

from ..config import JevConfig
from .base import JevDecision, JevProvider
from .stub_provider import StubJevProvider


def build_jev_provider(cfg: JevConfig) -> JevProvider:
    if not cfg.enabled:
        return StubJevProvider(cfg, disabled=True)

    if cfg.provider == "stub":
        return StubJevProvider(cfg)

    # Real adapter registration point. Example (once the SDK exists):
    #     if cfg.provider == "jev":
    #         from .jev_provider import RealJevProvider
    #         return RealJevProvider(cfg)
    # We fall back to the stub rather than crashing if an unknown provider
    # name is configured but no adapter is present.
    return StubJevProvider(cfg)


__all__ = ["JevProvider", "JevDecision", "StubJevProvider", "build_jev_provider"]
