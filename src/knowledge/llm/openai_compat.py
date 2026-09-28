"""OpenAI-compatible chat completions provider.

Works with any OpenAI-compatible endpoint: Ollama, LM Studio, vLLM, OpenAI.
No vendor is hard-coded -- everything is configuration driven. "openai" here
names the *wire protocol*, not the company.

Local-inference realities this provider accounts for:

* **Cold starts.** A local server loads weights into RAM on first use, so the
  first call can take tens of seconds. Hence a generous, configurable timeout
  and an Ollama ``keep_alive`` hint so the model isn't evicted between notes.
* **The server may simply be off.** ``available`` performs a cached probe, so a
  stopped Ollama makes the enricher skip cleanly instead of raising once per
  document.
* **Reasoning models leak their scratchpad.** Models such as Qwen3 emit
  ``<think>...</think>`` before the answer, which breaks naive JSON parsing.
  That block is stripped before parsing.
"""

from __future__ import annotations

import json
import re
import time

import httpx

from ..config import LLMConfig
from .base import LLMMessage, LLMProvider

# Reasoning scratchpads. Non-greedy, and tolerant of an unclosed tag when the
# response is cut short by max_tokens.
_THINK_RE = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)


class OpenAICompatProvider(LLMProvider):
    name = "openai"

    def __init__(self, cfg: LLMConfig):
        self._cfg = cfg
        self._base = cfg.base_url.rstrip("/")
        self._url = self._base + "/chat/completions"
        self._model = cfg.model
        self._max_tokens = cfg.max_tokens
        self._temperature = cfg.temperature
        self._timeout = cfg.timeout_seconds
        self._headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            self._headers["Authorization"] = f"Bearer {cfg.api_key}"
        self._health: bool | None = None
        self._health_at: float = 0.0

    # ---- availability ----------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether the endpoint is reachable.

        Cached briefly: the enricher asks once per document, and a TCP probe per
        note would be wasteful. When ``health_check`` is off we optimistically
        report True (useful for hosted endpoints that bill per request).
        """
        if not self._cfg.health_check:
            return True
        now = time.monotonic()
        if (
            self._health is not None
            and (now - self._health_at) < self._cfg.health_cache_seconds
        ):
            return self._health
        self._health = self._probe()
        self._health_at = now
        return self._health

    def _probe(self) -> bool:
        """GET /models -- the one endpoint every OpenAI-compatible server has."""
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(self._base + "/models", headers=self._headers)
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    def models(self) -> list[str]:
        """Model ids the endpoint is serving (used by `knowledge health`)."""
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(self._base + "/models", headers=self._headers)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            return []
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]

    # ---- completion ------------------------------------------------------

    def complete(self, messages: list[LLMMessage], **kwargs) -> str:
        payload: dict = {
            "model": kwargs.get("model", self._model),
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": kwargs.get("temperature", self._temperature),
            "max_tokens": kwargs.get("max_tokens", self._max_tokens),
            "stream": False,
        }
        if kwargs.get("json_mode"):
            payload["response_format"] = {"type": "json_object"}
        # Extra hints Ollama understands and other servers ignore.
        if self._cfg.keep_alive:
            payload["keep_alive"] = self._cfg.keep_alive
        if self._cfg.num_ctx:
            payload["options"] = {"num_ctx": self._cfg.num_ctx}

        timeout = kwargs.get("timeout", self._timeout)
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(self._url, headers=self._headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"unexpected response shape from {self._url}: "
                f"{json.dumps(data)[:300]}"
            ) from exc
        return _THINK_RE.sub("", content).strip()
