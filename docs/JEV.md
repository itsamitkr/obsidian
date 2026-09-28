# Jev integration

Jev is treated strictly as an **optional** structured classification /
decision provider. The system never depends on Jev being installed, and we do
**not** fabricate a Jev API.

## Interface

`knowledge/jev/base.py` defines:

```python
class JevProvider(abc.ABC):
    @property
    def available(self) -> bool: ...
    def classify(self, text, labels, *, task="classification", context=None) -> JevDecision: ...
```

`JevDecision` carries `label`, `confidence` (0–1), `rationale`, `metadata`.

## Current state: isolated stub

`knowledge/jev/stub_provider.py` implements the interface with a transparent,
deterministic keyword-overlap heuristic. It is clearly labelled as a stand-in
and is **disabled by default** (`jev.enabled=false`). It exists so downstream
code (the enricher) has something to call during development and tests.

## Where the real integration goes

When the official Jev SDK/API is available in your environment:

1. Create `knowledge/jev/jev_provider.py`:

   ```python
   from .base import JevDecision, JevProvider

   class RealJevProvider(JevProvider):
       name = "jev"
       def __init__(self, cfg):
           import jev              # the real SDK
           self._client = jev.Client(...)   # per the official API
       @property
       def available(self) -> bool:
           return True
       def classify(self, text, labels, *, task="classification", context=None):
           # Call the real Jev API here and map its response to JevDecision.
           resp = self._client.decide(input=text, options=labels, task=task)
           return JevDecision(label=resp.label, confidence=resp.score,
                              rationale=resp.explanation, provider=self.name)
   ```

2. Register it in `knowledge/jev/__init__.py::build_jev_provider`:

   ```python
   if cfg.provider == "jev":
       from .jev_provider import RealJevProvider
       return RealJevProvider(cfg)
   ```

3. Enable it:

   ```bash
   KS_JEV__PROVIDER=jev
   KS_JEV__ENABLED=true
   ```

Do **not** modify the stub to call a fabricated endpoint. The stub stays as a
safe, offline fallback.

## How Jev is used

The enricher (`knowledge/enrichment/enricher.py`) consults Jev for structured
domain classification when it is available and domains are configured. This is
a cheap, token-free decision that complements (and can gate) the more
expensive LLM enrichment.
