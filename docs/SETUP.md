# Setup on a fresh clone

## TL;DR

```powershell
# Windows
git clone <your-repo-url> knowledge-system
cd knowledge-system
.\install.cmd          # once: Python, dependencies, models, Obsidian, Ollama
.\start.cmd            # the Control Panel at http://127.0.0.1:8000
```

```bash
# macOS
git clone <your-repo-url> knowledge-system
cd knowledge-system
bash install.sh        # once
bash start.sh
```

The installer needs no admin rights and nothing preinstalled. What it sets up
is listed in the [README](../README.md#quick-start). Options: `-NoObsidian`,
`-NoLlm`, `-LlmModel <name>` on Windows; `--no-obsidian`, `--no-llm`,
`LLM_MODEL=<name>` on macOS.

To uninstall: delete the repo folder (that removes `.tools/`, `.venv/` and the
vault, so move the vault first if you want to keep it). Obsidian and Ollama are
normal apps: remove them from Windows *Installed apps* or `~/Applications`.

## What gets installed

Dependencies are declared in `pyproject.toml`; the installer runs
`uv pip install -e ".[all,dev]"`. To install a smaller set by hand (any Python
3.12+), pick from the table below.

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

The core install has **no heavy or paid dependencies** and always works with
the default providers. If you configure an embedding model or Qdrant, its extra
must be installed: the system stops with an install hint rather than quietly
switching to the weaker defaults.

A reasoning LLM is **not** a Python dependency: it is an external
OpenAI-compatible server you point at. The installer sets up Ollama for you
(skip with `-NoLlm` / `--no-llm`); see
[README](../README.md#optional-a-local-llm-for-enrichment) for other servers.
Without one, ingestion is fully deterministic and spends no tokens.

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
pytest -q            # test suite, default providers, no network
ruff check src tests # lint
knowledge health     # shows which providers are active
```

To connect a local MCP client (Claude Desktop, Kiro, …) over stdio, see
[MCP.md](MCP.md#connect-a-local-mcp-client-stdio).

## Using the same vault on several machines

Derived indexes (vector + graph) are **per-machine and rebuildable**; only the
Markdown vault needs to travel. So different machines can use different
backends without any code changes — just a different `.env`.

| Machine | `.env` | Notes |
|---------|--------|-------|
| Any laptop (recommended) | `KS_VECTOR__PROVIDER=qdrant_local` | real Qdrant, embedded on disk; one process at a time |
| Running your own Qdrant server | `KS_VECTOR__PROVIDER=qdrant`<br>`KS_VECTOR__URL=http://localhost:6333` | start the server first; several processes can share it |

Workflow when moving to another machine:

```bash
git clone <your-vault-repo> knowledge-vault   # the source of truth
.\install.cmd                                 # macOS: bash install.sh
# edit .env if THIS machine needs a different vector provider
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
