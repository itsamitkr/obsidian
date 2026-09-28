"""Local embedding provider backed by sentence-transformers.

Requires the ``embed`` extra. Inference is fully local -- text is embedded on
this machine and never sent anywhere.

Network policy: the model weights have to be fetched **once**. After that the
files live in the Hugging Face cache, and we load with offline mode forced so
there are **zero outbound requests** on subsequent starts. (By default
huggingface_hub re-validates every cached file over the network on each load,
which produced a burst of HTTP traffic and log noise even though nothing was
actually downloaded.)
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager

from ..config import EmbeddingConfig
from .base import EmbeddingProvider

log = logging.getLogger(__name__)

# Env vars huggingface_hub / transformers honour to stay fully offline.
_OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}


@contextmanager
def _offline():
    """Temporarily force Hugging Face offline mode.

    Setting the environment variables is not sufficient: ``huggingface_hub``
    snapshots ``HF_HUB_OFFLINE`` into a module constant at import time, so if
    the library is already imported the env var is ignored. We therefore patch
    the live constants too (and restore them afterwards).
    """
    previous_env = {k: os.environ.get(k) for k in _OFFLINE_ENV}
    os.environ.update(_OFFLINE_ENV)

    patched: list[tuple[object, str, object]] = []
    for mod_name, attr in (
        ("huggingface_hub.constants", "HF_HUB_OFFLINE"),
        ("transformers.utils.hub", "_is_offline_mode"),
    ):
        try:
            import importlib

            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        if hasattr(mod, attr):
            patched.append((mod, attr, getattr(mod, attr)))
            setattr(mod, attr, True)

    try:
        yield
    finally:
        for mod, attr, old in patched:
            setattr(mod, attr, old)
        for key, old in previous_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


class SentenceTransformersProvider(EmbeddingProvider):
    name = "sentence_transformers"

    def __init__(self, cfg: EmbeddingConfig):
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "sentence-transformers not installed. Install with "
                "`pip install 'knowledge-system[embed]'`."
            ) from exc

        self._model = self._load(SentenceTransformer, cfg.model)
        # `get_sentence_embedding_dimension` was renamed to
        # `get_embedding_dimension` in sentence-transformers 6.x.
        get_dim = getattr(
            self._model,
            "get_embedding_dimension",
            getattr(self._model, "get_sentence_embedding_dimension", None),
        )
        self._dim = int(get_dim()) if get_dim else cfg.dim
        self._batch = cfg.batch_size

    @staticmethod
    def _load(SentenceTransformer, model_name: str):
        """Load from the local cache; download only if it isn't cached yet."""
        try:
            with _offline():
                return SentenceTransformer(model_name)
        except Exception:
            # Not cached (or cache incomplete) -> one-time download.
            log.warning(
                "Embedding model %r not in the local cache; downloading once "
                "from Hugging Face. Subsequent starts are fully offline.",
                model_name,
            )
            return SentenceTransformer(model_name)

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed locally. No text leaves this machine."""
        if not texts:
            return []
        vecs = self._model.encode(
            texts,
            batch_size=self._batch,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return [v.tolist() for v in vecs]
