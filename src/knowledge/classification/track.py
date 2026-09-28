"""Ingestion tracks: how an upload is treated.

Not every file belongs in a knowledge base. A policy PDF is durable reference
knowledge; a 40 MB financial model is working material you want to *analyse*,
not permanently embed. Conflating the two pollutes the vector index with
thousands of meaningless number-cells and makes retrieval worse.

So each upload is routed to exactly one track:

    document    reference prose -> Markdown -> chunks -> embeddings -> graph
    structured  tabular/record data -> schema + records -> graph + vector
    analytical  models/large datasets -> staged; indexed ONLY on explicit
                opt-in ("Add to Knowledge Base")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Track(str, Enum):
    document = "document"
    structured = "structured"
    analytical = "analytical"
    unsupported = "unsupported"


@dataclass
class Classification:
    """Why a file landed on a given track.

    ``reasons`` is human-readable and surfaced in the UI/CLI so the routing
    decision is never a black box. ``signals`` carries the raw measurements.
    """

    track: Track
    reasons: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    requires_optin: bool = False

    @property
    def indexed_now(self) -> bool:
        """True if this file should be indexed as part of this ingest run."""
        return self.track in (Track.document, Track.structured)

    def to_dict(self) -> dict:
        return {
            "track": self.track.value,
            "reasons": self.reasons,
            "signals": self.signals,
            "requires_optin": self.requires_optin,
        }
