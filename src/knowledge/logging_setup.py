"""Logging defaults for the CLI/servers.

Third-party libraries are extremely chatty at INFO level. Loading a
SentenceTransformer emits dozens of Hugging Face cache-validation requests, and
``httpx`` logs every one of them, which buries our own output and makes the
terminal unusable while a model warms up.

We only quiet *library* loggers, and only when running our own entry points --
importing ``knowledge`` as a library leaves logging untouched.
"""

from __future__ import annotations

import logging
import os

# Loggers that flood the console during model load / HTTP work.
_NOISY = (
    "httpx",
    "httpcore",
    "huggingface_hub",
    "transformers",
    "sentence_transformers",
    "filelock",
    "urllib3",
)


def configure_logging(verbose: bool = False) -> None:
    """Apply sane console logging defaults.

    verbose=True keeps library INFO logs (useful when debugging a model or
    network problem).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )
    if verbose:
        return

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    # Silence the HF progress bars and the "unauthenticated requests" notice;
    # neither is actionable for local, cache-backed model loads.
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
