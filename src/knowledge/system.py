"""KnowledgeSystem facade.

Wires together configuration and all pluggable providers into a single
object that the pipeline, API, CLI and MCP server share. Every component is
swappable via configuration without touching call sites.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from .chunking import MarkdownChunker
from .classification import TrackClassifier
from .config import Settings, get_settings
from .converters import build_converter
from .embeddings import build_embedding_provider
from .enrichment import Enricher
from .graph import GraphBuilder, build_graph_store
from .graph.base import GraphStore
from .jev import build_jev_provider
from .llm import build_llm_provider
from .state import StateStore
from .structured import StructuredParser
from .vectorstore import build_vector_store

T = TypeVar("T")


class IndexMismatchError(RuntimeError):
    """The vector index was built with a different embedding setup."""


def _describe(fp: dict) -> str:
    model = f" ({fp['model']})" if fp.get("model") else ""
    return f"{fp.get('provider')}{model}, dim {fp.get('dim')}"


class KnowledgeSystem:
    """Shared, thread-safe container for the configured providers.

    Providers are built lazily on first use and then reused. Construction is
    guarded by a lock: ``functools.cached_property`` does NOT serialise
    concurrent first access, and because the API serves sync endpoints from a
    threadpool (and shares this object with the in-process MCP server), several
    requests arriving together each built their own provider. For the embedding
    provider that meant loading a full SentenceTransformer model per request --
    wasted memory and a burst of Hugging Face cache-validation traffic.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self._providers: dict[str, Any] = {}
        # Re-entrant: building the vector store reads `self.embedder`.
        self._lock = threading.RLock()

    def _provider(self, key: str, factory: Callable[[], T]) -> T:
        # Fast path: already built, no locking.
        try:
            return self._providers[key]
        except KeyError:
            pass
        with self._lock:
            # Double-check now that we hold the lock.
            if key not in self._providers:
                self._providers[key] = factory()
            return self._providers[key]

    # ---- providers (lazily constructed, cached, thread-safe) -------------

    @property
    def converter(self):
        return self._provider(
            "converter",
            lambda: build_converter(self.settings.converter, self.settings.ingest),
        )

    @property
    def embedder(self):
        def _build():
            provider = build_embedding_provider(self.settings.embedding)
            # The vector store may already have been opened using the
            # *configured* dimension (see `vector_store`). If the real model
            # disagrees, fail loudly now rather than writing mis-sized vectors.
            configured = self.settings.embedding.dim
            if provider.dim != configured:
                raise ValueError(
                    f"embedding.dim is {configured} but model "
                    f"{self.settings.embedding.model!r} produces {provider.dim}"
                    f"-dimensional vectors. Set KS_EMBEDDING__DIM={provider.dim} "
                    "and run `knowledge rebuild-vector-index`."
                )
            return provider

        return self._provider("embedder", _build)

    @property
    def vector_store(self):
        def _build():
            persist = self.settings.state_dir / "state" / "vectors.json"
            local_dir = self.settings.state_dir / "state" / "qdrant"
            # Use the CONFIGURED dimension, not `self.embedder.dim`. Asking the
            # embedder would load the full model (torch + weights, ~11s on a
            # laptop CPU) just to read a number we already have -- which made
            # every read-only page load of the Control Panel wait for it. The
            # embedder validates the two agree when it is eventually built.
            return build_vector_store(
                self.settings.vector,
                self.settings.embedding.dim,
                persist_path=persist,
                local_dir=local_dir,
            )

        return self._provider("vector_store", _build)

    # ---- index fingerprint -----------------------------------------------
    # Vectors from different models (or hash vs. a model) share a width of
    # 384, so mixing them raises no error -- search just gets worse. The index
    # records what built it, and search/ingest refuse to run on a mismatch.

    @property
    def _index_meta_path(self):
        return self.settings.state_dir / "state" / "index_meta.json"

    def index_fingerprint(self) -> dict:
        """The embedding setup the current config would write vectors with."""
        cfg = self.settings.embedding
        return {
            "provider": cfg.provider,
            "model": cfg.model if cfg.provider != "hash" else None,
            "dim": cfg.dim,
        }

    def index_built_with(self) -> dict | None:
        try:
            return json.loads(self._index_meta_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def index_mismatch(self) -> str | None:
        """A readable message if the index was built with another setup."""
        stored = self.index_built_with()
        current = self.index_fingerprint()
        if stored is None or stored == current:
            return None
        return (
            f"The vector index was built with {_describe(stored)}, but the "
            f"config now uses {_describe(current)}. Run "
            "`knowledge rebuild-vector-index` to rebuild it with the current "
            "setup, or change the config back."
        )

    def ensure_index_matches(self) -> None:
        msg = self.index_mismatch()
        if msg:
            raise IndexMismatchError(msg)

    def record_index_fingerprint(self) -> None:
        path = self._index_meta_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.index_fingerprint()), encoding="utf-8")
        os.replace(tmp, path)

    # ---- warm-up ---------------------------------------------------------

    def warm_up(self, on_event: Callable[[str], None] | None = None) -> None:
        """Build the slow providers up front.

        Loading the embedding model takes ~10s on a CPU. Doing it lazily means
        whichever request arrives first pays the cost, which looks like a hang.
        Long-running services (the panel, the watcher) call this on a background
        thread so the first real search or ingest is already warm.
        """
        def note(msg: str) -> None:
            if on_event:
                on_event(msg)

        for label, get in (
            ("vector store", lambda: self.vector_store),
            ("embedding model", lambda: self.embedder),
            ("graph index", lambda: self.graph_store),
        ):
            try:
                get()
                note(f"ready: {label}")
            except Exception as exc:  # noqa: BLE001 - advisory only
                note(f"warm-up skipped for {label}: {type(exc).__name__}: {exc}")

    @property
    def llm(self):
        return self._provider("llm", lambda: build_llm_provider(self.settings.llm))

    @property
    def jev(self):
        return self._provider("jev", lambda: build_jev_provider(self.settings.jev))

    @property
    def chunker(self) -> MarkdownChunker:
        return self._provider(
            "chunker", lambda: MarkdownChunker(self.settings.chunking)
        )

    @property
    def classifier(self) -> TrackClassifier:
        """Routes each upload to the document/structured/analytical track."""
        return self._provider(
            "classifier", lambda: TrackClassifier(self.settings.classification)
        )

    @property
    def structured_parser(self) -> StructuredParser:
        return self._provider(
            "structured_parser", lambda: StructuredParser(self.settings.structured)
        )

    @property
    def state(self) -> StateStore:
        return self._provider(
            "state", lambda: StateStore(self.settings.state_db_path)
        )

    @property
    def graph_store(self) -> GraphStore:
        return self._provider(
            "graph_store",
            lambda: build_graph_store(
                self.settings.graph,
                self.settings.state_dir / "state" / "graph.sqlite",
            ),
        )

    @property
    def enricher(self) -> Enricher:
        return self._provider(
            "enricher",
            lambda: Enricher(
                llm=self.llm,
                jev=self.jev,
                cfg=self.settings.llm,
                domains=self.settings.vault_layout.domains,
            ),
        )

    # ---- helpers ---------------------------------------------------------

    def rebuild_graph(self) -> GraphStore:
        GraphBuilder(self.settings.vault_path, self.graph_store).rebuild()
        return self.graph_store

    def health(self, probe: bool = True) -> dict:
        """Report provider status.

        ``probe=True`` (the CLI default) actually builds every provider, which
        verifies they work but costs ~10s the first time because the embedding
        model has to load. ``probe=False`` reports the *configured* stack plus
        whether each provider is already loaded -- fast enough to poll, and
        honest about what it did not check.
        """
        if not probe:
            return {
                "vault_path": str(self.settings.vault_path),
                "probed": False,
                "converter": self.settings.converter.provider,
                "embedder": self.settings.embedding.provider,
                "embedding_dim": self.settings.embedding.dim,
                "embedder_loaded": "embedder" in self._providers,
                "vector_store": self.settings.vector.provider,
                "vector_loaded": "vector_store" in self._providers,
                "index_built_with": self.index_built_with(),
                "index_mismatch": self.index_mismatch(),
                "graph_store": self.settings.graph.provider,
                "llm": self.settings.llm.provider,
                "llm_model": (
                    self.settings.llm.model
                    if self.settings.llm.provider != "null" else None
                ),
                "enrichment_mode": self.settings.llm.enrichment_mode,
                "jev": self.settings.jev.provider,
            }
        return {
            "vault_path": str(self.settings.vault_path),
            "probed": True,
            "converter": self.converter.name,
            "embedder": self.embedder.name,
            "embedding_dim": self.embedder.dim,
            "vector_store": self.vector_store.name,
            "vector_healthy": self.vector_store.health_check(),
            "index_built_with": self.index_built_with(),
            "index_mismatch": self.index_mismatch(),
            "graph_store": self.graph_store.name,
            "llm": self.llm.name,
            "llm_available": self.llm.available,
            "llm_model": (
                self.settings.llm.model
                if self.settings.llm.provider != "null" else None
            ),
            "llm_endpoint": (
                self.settings.llm.base_url
                if self.settings.llm.provider != "null" else None
            ),
            "enrichment_mode": self.settings.llm.enrichment_mode,
            "jev": self.jev.name,
            "jev_available": self.jev.available,
        }
