# Configuration

Configuration is layered, lowest to highest precedence:

1. Defaults in the Pydantic models.
2. `config/default.yaml`.
3. A user config file: `--config path.yaml` or `KS_CONFIG_FILE`.
4. Environment variables (prefix `KS_`, nested with `__`), and `.env`.

The defaults below are the built-in ones. The installer copies `.env.example`
to `.env`, which switches on real search (`sentence_transformers` embeddings,
`qdrant_local` vectors) and, if Ollama was installed, sets
`llm.provider=openai` with the pulled model.

Environment variables always override YAML. Example:

```bash
KS_VECTOR__PROVIDER=qdrant          # -> vector.provider
KS_LLM__ENRICHMENT_MODE=selective   # -> llm.enrichment_mode
```

## Settings reference

| Key | Default | Notes |
|-----|---------|-------|
| `vault_path` | `./knowledge-vault` | root of the Markdown vault (source of truth) |
| `state_dir` | `<vault>/.knowledge` | derived state (git-ignored) |
| `vault_layout.content_dir` | `Notes` | flat folder where all notes are written |
| `vault_layout.sources` | `90-Indexes/Sources` | preserved original files |
| `vault_layout.templates` | `90-Indexes/Templates` | Obsidian templates |
| `vault_layout.domains` | `[]` | optional fixed labels for the Jev classifier; empty = emergent |
| `ingest.inbox_dir` | `00-Inbox` | drop folder |
| `ingest.auto_git_commit` | `false` | commit after each successful ingest |
| `ingest.move_processed_sources` | `true` | clear inbox files after archiving to `Sources/`; `false` leaves them in place |
| `ingest.watch_debounce_seconds` | `2.0` | watcher debounce |
| `ingest.supported_extensions` | pdf/docx/... | processed file types |
| `ingest.markdown_lint` | `true` | run lint after write |
| `converter.provider` | `auto` | `markitdown` \| `auto` |
| `classification.enabled` | `true` | `false` = everything takes the document track |
| `classification.document_extensions` | pdf/docx/pptx/html/md/msg/… | routed to track 1 |
| `classification.structured_extensions` | csv/tsv/xlsx/json/xml | routed to track 2 or 3 |
| `classification.analytical.require_optin` | `true` | `false` = index analytical files directly |
| `classification.analytical.staging_dir` | `90-Indexes/Analytical` | where gated files are parked |
| `classification.analytical.max_bytes` | `2000000` | larger ⇒ analytical |
| `classification.analytical.max_rows` | `5000` | CSV/TSV rows or JSON array length |
| `classification.analytical.max_sheets` | `5` | workbook tabs |
| `classification.analytical.max_formula_cells` | `200` | formula density ⇒ a model, not data |
| `classification.analytical.scan_cell_limit` | `20000` | bounds the workbook probe |
| `structured.max_rows_indexed` | `2000` | rows materialised per table |
| `structured.records_per_chunk` | `20` | records per vector chunk |
| `structured.sample_rows` | `10` | rows shown in the Markdown note |
| `structured.max_entity_distinct` | `50` | ≤ this many distinct values ⇒ graph entities |
| `structured.max_entity_value_len` | `60` | longer values are free text, not entities |
| `chunking.max_tokens` | `512` | per-chunk budget |
| `chunking.min_tokens` | `64` | merge tiny sections up to this |
| `chunking.overlap_tokens` | `32` | overlap between split pieces |
| `chunking.respect_headings` | `true` | chunk around heading structure |
| `embedding.provider` | `hash` | `hash` \| `sentence_transformers` \| `openai` |
| `embedding.model` | MiniLM | model name / id |
| `embedding.dim` | `384` | vector size. **Must match the model** — see below |
| `embedding.base_url` / `api_key` | — | for the `openai` provider |
| `vector.provider` | `memory` | `memory` \| `qdrant_local` (embedded on disk) \| `qdrant` (server) |
| `vector.url` | `http://localhost:6333` | Qdrant endpoint (server mode only) |
| `vector.collection` | `knowledge` | collection name |
| `vector.distance` | `cosine` | `cosine` \| `dot` \| `euclid` |
| `graph.provider` | `sqlite` | `sqlite` \| `memory` |
| `llm.provider` | `null` | `null` \| `openai` (the *protocol*: Ollama, LM Studio, vLLM, OpenAI) |
| `llm.base_url` | `http://localhost:11434/v1` | OpenAI-compatible endpoint (this default is Ollama) |
| `llm.model` | `llama3.2:3b` | any model the endpoint serves |
| `llm.api_key` | `null` | only needed by hosted endpoints |
| `llm.enrichment_mode` | `selective` | `off` \| `on` \| `selective` |
| `llm.max_tokens` | `1024` | cap on the completion |
| `llm.temperature` | `0.1` | low, because the output is parsed as JSON |
| `llm.timeout_seconds` | `180.0` | generous: local CPU inference is slow and the first call also loads the model |
| `llm.keep_alive` | `30m` | Ollama hint; stops the model being evicted between notes |
| `llm.num_ctx` | `null` | Ollama context override; leave unset unless briefs get truncated |
| `llm.health_check` | `true` | probe the endpoint so a stopped server degrades to "unavailable" |
| `llm.health_cache_seconds` | `60.0` | how long a probe result is reused |
| `llm.selective.important_domains` | `[]` | domains that trigger enrichment (empty in flat mode) |
| `llm.selective.min_document_chars` | `800` | "high-value" threshold |
| `jev.provider` | `stub` | `stub` \| real adapter |
| `jev.enabled` | `false` | turn Jev on |
| `api.host` / `api.port` | `127.0.0.1:8000` | local API bind |

The MCP server has no config key: stdio is the default, and
`knowledge mcp --transport http` binds `127.0.0.1` unless `--host` says
otherwise. The server is read-only unless started with `--allow-writes` (or
`KS_MCP_ALLOW_WRITES=true`).

## `embedding.dim` must match the model

The vector store is opened using the **configured** dimension, not one read off
the model. That is deliberate: asking the model would load it (torch plus the
weights, ~10s on a laptop CPU) just to learn a number already in config, and it
made every read-only page load of the Control Panel wait for it.

The trade-off is that a wrong `embedding.dim` is now an error rather than being
silently absorbed. When the model is eventually loaded it is checked against the
configured value and refuses to run on a mismatch:

```
embedding.dim is 384 but model 'BAAI/bge-base-en-v1.5' produces 768-dimensional
vectors. Set KS_EMBEDDING__DIM=768 and run `knowledge rebuild-vector-index`.
```

So when you change `embedding.model`, set `embedding.dim` to match and rebuild.
Common pairings:

| Model | `dim` |
|-------|-------|
| `sentence-transformers/all-MiniLM-L6-v2` (default) | 384 |
| `BAAI/bge-small-en-v1.5` | 384 |
| `intfloat/e5-small-v2` | 384 |
| `BAAI/bge-base-en-v1.5` | 768 |
| `intfloat/e5-large` | 1024 |
| OpenAI `text-embedding-3-small` | 1536 |

## Startup cost and warm-up

Loading a real embedding model takes roughly ten seconds on a CPU. It is
deferred until something actually needs to embed — a search or an ingest — so
the panel binds in about a second and its dashboard, notes, staged list and
control endpoints all respond immediately.

Long-running services (`knowledge panel`, `knowledge watch`) additionally call
`KnowledgeSystem.warm_up()` on a background thread, so the model is usually
ready before you click anything. Progress is printed as `ready: embedding model`.

`GET /health` is the cheap report by default (it states `probed: false` and
`embedder_loaded`), which keeps it safe to poll. Use `GET /health?probe=true`, or
the `knowledge health` CLI command, to actually construct and verify every
provider — that is the call which pays the model-load cost.

## Tuning upload classification

Every upload is routed to one of three tracks before any work happens — see
[INGESTION.md](INGESTION.md) for the full behaviour. Classification is
deterministic (extension + a bounded shape probe), so it costs no tokens.

Check a decision before changing thresholds:

```bash
knowledge classify reports/Q3-model.xlsx
```

Common adjustments:

```bash
# Treat bigger datasets as knowledge rather than staging them.
KS_CLASSIFICATION__ANALYTICAL__MAX_ROWS=50000

# Index analytical files immediately instead of requiring "Add to Knowledge Base".
KS_CLASSIFICATION__ANALYTICAL__REQUIRE_OPTIN=false

# Disable the whole mechanism (pre-classification behaviour).
KS_CLASSIFICATION__ENABLED=false

# Bigger record groups = fewer, broader chunks.
KS_STRUCTURED__RECORDS_PER_CHUNK=50
```

Note the interaction with `ingest.supported_extensions`: that list gates which
files the **watcher and inbox sweep pick up**, while
`classification.*_extensions` decide **which track** a picked-up file takes. To
ingest a new format you generally need it in both.

Spreadsheet probing and parsing need the optional `structured` extra
(`pip install -e ".[structured]"`, included in `.[all]`). Without it, `.xlsx`
files produce a note explaining the extra is missing and XML falls back to the
standard-library parser.

## Token minimisation

`llm.enrichment_mode` controls LLM usage:

- **`off`** — the LLM is never called (zero tokens).
- **`on`** — every document is enriched.
- **`selective`** (default) — enrich only when a heuristic fires: no topic yet,
  a high-value (long) document, or (if you set `important_domains`) an
  important domain. Even then, only a compact *brief* (title, heading outline,
  short lead) is sent — never the full document, never every chunk.

Two gates are **absolute** and cannot be overridden: `mode=off`, and an
unreachable provider. In particular `knowledge ingest --force` means "reprocess
this file", not "ignore my LLM settings" — it bypasses only the selective
heuristics. (This previously leaked: `--force` would enrich even with
`mode=off`, breaking the zero-token guarantee.)

### Running a local model

See the [README](../README.md#optional-a-local-llm-for-enrichment) for the
Ollama walkthrough. Points that matter once it's running:

```bash
KS_LLM__PROVIDER=openai
KS_LLM__MODEL=llama3.2:3b
KS_LLM__TIMEOUT_SECONDS=300      # raise if a big note times out on a slow CPU
KS_LLM__KEEP_ALIVE=1h            # avoid reloading the model between notes
KS_LLM__NUM_CTX=8192             # raise only if briefs are being truncated
KS_LLM__HEALTH_CHECK=false       # skip probing (hosted endpoints that bill per call)
```

The provider probes `GET {base_url}/models` at most once per
`health_cache_seconds`. If that fails, the enricher reports
`no LLM provider available` and ingestion proceeds without enrichment — a
stopped Ollama slows nothing down and fails nothing.

`<think>...</think>` blocks from reasoning models are stripped before the
response is parsed as JSON, so those models won't corrupt the output. They still
spend tokens on reasoning that this task doesn't need, so a plain instruct model
is the better fit.

### Adding folders back (optional)

The flat layout is the default. If you prefer folder-per-domain organization,
set `vault_layout.domains` to your labels and route notes yourself — but note
that metadata (`domain`/`topic`/`tags`) remains the source of truth either
way, and the graph/search work identically regardless of folders.

The built-in LLM provider is `null`, which consumes **no** LLM tokens at all.
The installer's Ollama setup runs the model on your machine, so enrichment
still costs nothing and sends nothing off-machine.
