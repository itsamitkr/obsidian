# Architecture

## Principles

1. **The Markdown/Git repository is the source of truth.** Everything else is
   either a UI (Obsidian, CLI, web) or a *derived* index (vectors, graph) that
   can be rebuilt from Markdown.
2. **No hard dependency on Obsidian.** The vault is a plain folder of Markdown
   files. Remove Obsidian and all functionality remains.
3. **Everything replaceable is behind an interface.** Converter, embedding
   model, vector store, graph store, LLM and Jev are all pluggable via
   configuration.
4. **Deterministic first, LLM last.** Conversion, parsing, chunking, hashing,
   linking and embedding are deterministic. The LLM is optional and
   token-frugal.

## Component map

```
knowledge/
├── config.py          # layered configuration (YAML < env), single Settings object
├── models.py          # domain models (Document, Chunk, GraphNode/Edge, Manifest)
├── state.py           # SQLite manifest + job checkpoints + chunk-hash tracking
├── system.py          # KnowledgeSystem facade: wires all providers
├── search.py          # retrieval service
├── rebuild.py         # rebuild vector/graph indexes from Markdown
├── vault.py           # scaffold the flat vault (content folder + templates)
│
├── classification/    # TrackClassifier: document | structured | analytical
├── converters/        # DocumentConverter: MarkItDown + builtin plaintext
├── structured/        # StructuredParser + Markdown/record renderers (track 2)
├── embeddings/        # EmbeddingProvider: hash | sentence_transformers | openai
├── vectorstore/       # VectorStore: memory | qdrant
├── graph/             # GraphStore (memory|sqlite) + GraphBuilder (from Markdown)
├── llm/               # LLMProvider: null | openai-compatible (Ollama/LM Studio/vLLM)
├── jev/               # JevProvider: stub (isolated) | real adapter slot
│
├── markdown/          # frontmatter, wiki-link/tag extraction, section parser
├── chunking/          # heading-aware semantic chunker
├── enrichment/        # selective LLM + Jev orchestration (token minimising)
├── ingestion/         # pipeline, manifest, filesystem watcher, lint
├── api/               # FastAPI local API + Control Panel (binds 127.0.0.1)
├── mcp/               # MCP server (stdio default; optional HTTP on 127.0.0.1; SDK 1.x/2.x)
└── cli.py             # `knowledge` CLI
```

## Data flow (ingestion)

Classification runs first and picks one of three tracks. See
[INGESTION.md](INGESTION.md) for the thresholds and rationale.

```
source file
   -> TrackClassifier                          [extension + shape probe, no LLM]
   -> SHA-256 + manifest check (idempotency / dedupe)
   |
   +-- track 1: DOCUMENT
   |      -> preserve original (Sources/)
   |      -> DocumentConverter -> Markdown      [deterministic, no LLM]
   |      -> frontmatter/metadata
   |      -> semantic chunker (heading-aware) + content hashes
   |      -> Enricher (optional, selective LLM + Jev) [compact brief only]
   |      -> write canonical Markdown           [source of truth]
   |      -> EmbeddingProvider -> VectorStore.upsert  [only changed chunks]
   |      -> GraphBuilder -> GraphStore         [links/tags/related]
   |
   +-- track 2: STRUCTURED
   |      -> preserve original (Sources/)
   |      -> StructuredParser -> tables, columns, records, entities
   |      -> record blocks ("column: value")    [not Markdown-derived]
   |      -> write canonical Markdown           [schema + row sample]
   |      -> EmbeddingProvider -> VectorStore.upsert
   |      -> GraphBuilder -> GraphStore         [+ entity nodes, 'mentions']
   |
   +-- track 3: ANALYTICAL
          -> move to staging (90-Indexes/Analytical/)
          -> manifest = completed, nothing indexed
          -> promote() re-enters via track 2 on explicit user opt-in

   (tracks 1 & 2 continue)
   -> Markdown lint
   -> manifest = completed (+ optional git commit)
   -> clear source from the inbox (already archived)
```

Each arrow advances a checkpoint in the manifest (`JobStage`), so a crash
resumes rather than corrupts. The manifest also stores the chosen `track`,
which is what `/stats` aggregates for the panel.

## Derived-index rebuild

Because Markdown is canonical, the vector index and graph are fully
reconstructible:

```
knowledge rebuild-vector-index     # re-chunk + re-embed every note
knowledge rebuild-graph            # re-derive nodes/edges from links
```

Content hashing means an incremental rebuild (`--incremental`) re-embeds only
chunks whose text changed.

## Swapping components

| Change | How |
|--------|-----|
| Use Qdrant instead of memory | `KS_VECTOR__PROVIDER=qdrant_local` (embedded), or `qdrant` + `KS_VECTOR__URL` for your own server |
| Use real embeddings | `KS_EMBEDDING__PROVIDER=sentence_transformers` (or `openai`) |
| Add a local LLM | `ollama pull llama3.2:3b`, then `KS_LLM__PROVIDER=openai` + `KS_LLM__MODEL=llama3.2:3b` (base_url already points at Ollama) |
| Use a hosted LLM instead | same provider, change `KS_LLM__BASE_URL` + `KS_LLM__API_KEY` — note this sends briefs off-machine |
| Add Neo4j | implement `GraphStore`, register in `graph.build_graph_store` |
| Add a converter | implement `DocumentConverter`, register in `converters.build_converter` |
| Retune / disable track routing | `KS_CLASSIFICATION__*` (see [CONFIGURATION.md](CONFIGURATION.md)) |
| Index analytical files automatically | `KS_CLASSIFICATION__ANALYTICAL__REQUIRE_OPTIN=false` |

None of these touch the Markdown repository.
