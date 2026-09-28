# Setup on a fresh clone

## TL;DR

```powershell
# Windows (PowerShell)
git clone <your-repo-url> knowledge-system
cd knowledge-system
./scripts/setup.ps1                 # core + dev, scaffolds the vault
# or everything (PDF/DOCX conversion, real embeddings, Qdrant, MCP, API):
./scripts/setup.ps1 -Extras all
```

```bash
# macOS / Linux
git clone <your-repo-url> knowledge-system
cd knowledge-system
./scripts/setup.sh                  # core + dev
EXTRAS=all ./scripts/setup.sh       # everything
# or:  make setup   /   make install-all
```

Both scripts: create `.venv`, install the package, copy `.env.example` to
`.env`, and run `knowledge init` to scaffold the vault. Then:

```bash
knowledge health
```

## What gets installed

Dependencies are declared in `pyproject.toml`, so `pip` resolves everything —
there is no `requirements.txt` to drift out of sync.

| Install command | What you get |
|-----------------|--------------|
| `pip install -e ".[dev]"` | core + tests; **runs with zero external services** (hash embeddings, in-memory vectors, no LLM) |
| `pip install -e ".[convert]"` | + MarkItDown (PDF, DOCX, PPTX, XLSX, HTML, images) |
| `pip install -e ".[structured]"` | + openpyxl/defusedxml — spreadsheet probing and parsing for the structured/analytical tracks |
| `pip install -e ".[embed]"` | + sentence-transformers (local semantic embeddings) |
| `pip install -e ".[vector]"` | + Qdrant client |
| `pip install -e ".[api]"` | + FastAPI/uvicorn |
| `pip install -e ".[mcp]"` | + MCP SDK |
| `pip install -e ".[all,dev]"` | everything above |

The core install has **no heavy or paid dependencies** and always works;
optional integrations degrade gracefully to built-in fallbacks if missing.

A reasoning LLM is **not** a Python dependency: it is an external
OpenAI-compatible server you point at. There is nothing to install unless you
want enrichment — see
[README](../README.md#optional-a-local-llm-for-enrichment) for the Ollama
walkthrough. Without it, ingestion is fully deterministic and spends no tokens.

## Repository model: code vs. vault

This repo contains the **code**. Your **knowledge vault is separate** — it is
your source of truth and personal data, versioned in its own git repository.

- `knowledge-vault/` is git-ignored in this code repo (see `.gitignore`), so
  cloning the code never drags in personal documents, and there is no nested
  git repo to trip over.
- `knowledge init` recreates the vault's structure on any machine.
- To version your vault, initialise git *inside* it:

  ```bash
  cd knowledge-vault
  git init && git add -A && git commit -m "init vault"
  git remote add origin <your-vault-repo-url>
  git push -u origin main
  ```

  A `knowledge-vault/.gitignore` already excludes derived state
  (`.knowledge/state`, logs), keeping only the Markdown source of truth.

If you *prefer* a single repo containing both code and a starter vault, remove
the `/knowledge-vault/` line from `.gitignore` and make sure the vault has no
nested `.git` directory.

## Verify the clone works

```bash
pytest -q            # test suite, fallback stack, no network
ruff check src tests # lint
knowledge health     # shows which providers are active
```

## Using the same vault on several machines

Derived indexes (vector + graph) are **per-machine and rebuildable**; only the
Markdown vault needs to travel. So different machines can use different
backends without any code changes — just a different `.env`.

| Machine | `.env` | Notes |
|---------|--------|-------|
| No Docker (locked-down laptop) | `KS_VECTOR__PROVIDER=qdrant_local` | real Qdrant, embedded on disk |
| Docker available | `KS_VECTOR__PROVIDER=qdrant`<br>`KS_VECTOR__URL=http://localhost:6333` | run `docker compose up -d qdrant` first |

Workflow when moving to another machine:

```bash
git clone <your-vault-repo> knowledge-vault   # the source of truth
./scripts/setup.ps1 -Extras embed,vector      # or: EXTRAS=embed,vector ./scripts/setup.sh
# edit .env to pick the vector provider for THIS machine
knowledge rebuild-vector-index                # rebuild the index locally
knowledge rebuild-graph
```

Do **not** copy `.knowledge/state/` between machines — it is derived data
(and the embedded-Qdrant files are platform/version specific). Rebuilding is
fast and always authoritative because it reads the Markdown.

Keep `KS_EMBEDDING__PROVIDER` and `KS_EMBEDDING__MODEL` **identical** across
machines. Changing the embedding model changes vector dimensions/semantics, so
you must run `knowledge rebuild-vector-index` after any such change.

## Reproducible / pinned installs (optional)

For byte-for-byte reproducibility across machines, generate a lock after
installing the set you want:

```bash
pip freeze > requirements.lock.txt      # snapshot exact versions
# on another machine, after creating the venv:
pip install -r requirements.lock.txt
pip install -e . --no-deps
```

Keep `pyproject.toml` as the source of truth for *ranges*; use the lock only
when you need identical versions everywhere.

## Docker (no local Python needed)

```bash
docker compose up -d     # Qdrant + the app, vault mounted from ./knowledge-vault
```
