"""Enrichment orchestration.

Token minimisation is a first-class requirement. The pipeline runs ALL
deterministic processing (conversion, parsing, headings, metadata, links,
hashing, chunking, embedding) before any LLM is considered. This module
decides *whether* to call the LLM and, if so, sends a compact, bounded
prompt -- never the whole document, never every chunk.

Modes:
    off        -> never call the LLM
    on         -> enrich every document
    selective  -> use heuristics (default)

Jev is consulted for structured classification/decision tasks when enabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import LLMConfig
from ..jev.base import JevProvider
from ..llm.base import LLMMessage, LLMProvider
from ..models import Document


@dataclass
class EnrichmentResult:
    used_llm: bool = False
    used_jev: bool = False
    reason: str = ""
    summary: str | None = None
    entities: list[str] = field(default_factory=list)
    suggested_links: list[str] = field(default_factory=list)
    topic: str | None = None
    topic_confidence: float | None = None
    extra: dict = field(default_factory=dict)


# A compact "document brief" keeps token usage low: title, domain, and the
# heading outline + a short lead, rather than the full body.
def _document_brief(doc: Document, max_chars: int = 1500) -> str:
    headings = []
    for chunk in doc.chunks:
        if chunk.heading_path:
            path = " > ".join(chunk.heading_path)
            if path not in headings:
                headings.append(path)
    outline = "\n".join(f"- {h}" for h in headings[:40])
    lead = doc.body.strip()[:max_chars]
    return (
        f"Title: {doc.title}\n"
        f"Domain: {doc.domain}\nTopic: {doc.topic}\n\n"
        f"Heading outline:\n{outline}\n\n"
        f"Lead:\n{lead}"
    )


class Enricher:
    def __init__(
        self,
        llm: LLMProvider,
        jev: JevProvider,
        cfg: LLMConfig,
        domains: list[str] | None = None,
    ):
        self.llm = llm
        self.jev = jev
        self.cfg = cfg
        self.domains = domains or []

    # ---- gating ----------------------------------------------------------

    def _should_enrich(self, doc: Document, force: bool = False) -> tuple[bool, str]:
        """Decide whether to spend tokens on this document.

        ``force`` comes from ``ingest --force``, which means "reprocess this
        file" -- NOT "ignore my LLM settings". So it overrides only the
        selective heuristics; ``mode=off`` and an unreachable provider remain
        absolute, because "off means zero tokens" is a documented invariant.
        """
        mode = self.cfg.enrichment_mode
        if mode == "off":
            return False, "enrichment disabled (mode=off)"
        if not self.llm.available:
            return False, "no LLM provider available"
        if force:
            return True, "forced (reprocess)"
        if mode == "on":
            return True, "mode=on"

        # selective heuristics
        sel = self.cfg.selective
        reasons = []
        if doc.domain in sel.important_domains:
            reasons.append(f"important domain '{doc.domain}'")
        if not doc.topic:
            reasons.append("no topic assigned")
        if len(doc.body) >= sel.min_document_chars:
            reasons.append("high-value (long) document")
        if reasons:
            return True, "selective: " + ", ".join(reasons)
        return False, "selective: no heuristic triggered"

    # ---- Jev classification ---------------------------------------------

    def classify_domain(self, doc: Document) -> tuple[str | None, float]:
        """Use Jev (if enabled) to pick a domain when deterministic
        classification is weak. Returns (label, confidence)."""
        if not self.jev.available or not self.domains:
            return None, 0.0
        decision = self.jev.classify(
            text=_document_brief(doc, max_chars=800),
            labels=self.domains,
            task="domain_classification",
            context={"title": doc.title},
        )
        return decision.label or None, decision.confidence

    # ---- main entry ------------------------------------------------------

    def enrich(self, doc: Document, force: bool = False) -> EnrichmentResult:
        result = EnrichmentResult()

        # Jev structured classification (cheap, no LLM tokens).
        if self.jev.available and self.domains:
            label, conf = self.classify_domain(doc)
            if label:
                result.used_jev = True
                result.topic = result.topic or None
                result.extra["jev_domain"] = {"label": label, "confidence": conf}

        should, reason = self._should_enrich(doc, force=force)
        result.reason = reason
        if not should:
            return result

        brief = _document_brief(doc)
        system = LLMMessage(
            role="system",
            content=(
                "You are a knowledge-graph curator. Given a compact brief of a "
                "document (NOT the full text), extract structured metadata. "
                "Respond with a single JSON object with keys: summary (<=40 "
                "words), entities (array of key concepts), suggested_links "
                "(array of note titles this should link to), topic (short "
                "string). Be concise to minimise tokens."
            ),
        )
        user = LLMMessage(role="user", content=brief)
        try:
            data = self.llm.complete_json([system, user], json_mode=True)
        except Exception as exc:  # pragma: no cover - network dependent
            result.reason = f"{reason}; llm error: {exc}"
            return result

        result.used_llm = True
        result.summary = data.get("summary")
        result.entities = [str(e) for e in (data.get("entities") or [])][:25]
        result.suggested_links = [
            str(s) for s in (data.get("suggested_links") or [])
        ][:25]
        result.topic = data.get("topic") or result.topic
        return result
