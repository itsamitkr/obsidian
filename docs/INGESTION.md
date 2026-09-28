# Ingestion

## Drop and walk away

The primary UX: drop a file into `knowledge-vault/00-Inbox/` and do nothing
else.

```bash
knowledge watch          # long-running watcher
```

The watcher debounces rapid filesystem events (`ingest.watch_debounce_seconds`)
and, on startup, sweeps any files that were dropped while it was off — so no
drop is ever missed across restarts.

For one-shot or scripted use:

```bash
knowledge ingest-inbox   # process everything currently in the inbox
knowledge ingest path/to/file.pdf
```

## Three tracks

Not every file is knowledge. A policy PDF, a vendor list, and a financial model
all need different handling, so classification happens **before** any work and
decides which pipeline runs.

| Track | Inputs | What happens | Enters the knowledge base? |
|-------|--------|--------------|----------------------------|
| **Document** | PDF, DOCX, PPTX, HTML, TXT, MD, RTF, EPUB, MSG/EML, images | MarkItDown → Markdown → semantic chunks → embeddings → graph | Yes, automatically |
| **Structured** | CSV, TSV, XLSX/XLSM/XLS, JSON, XML | structured parser → schema note + record chunks + graph entities | Yes, automatically |
| **Analytical** | Excel models, large datasets, anything tripping a size/complexity threshold | staged as-is; nothing converted, chunked or embedded | **No** — only on explicit opt-in |

Classification is deterministic: file extension plus a cheap, bounded probe of
the file's shape (bytes, row count, sheet count, formula density). No LLM is
involved, and every verdict carries human-readable reasons that the Control
Panel and CLI display.

Preview a decision without ingesting anything:

```bash
knowledge classify path/to/file.xlsx
```

### Why analytical files are gated

A 30-sheet financial model is working material, not reference knowledge.
Indexing it would scatter thousands of meaningless number-cells through the
vector store and drown real answers in noise. So these files are parked in
`90-Indexes/Analytical/` where an LLM or data-analysis tool can read them
directly, and they stay out of the index until you say otherwise:

```bash
knowledge staged                      # what's waiting on a decision
knowledge promote telemetry.csv       # "Add to Knowledge Base"
```

Promotion re-runs the file through the **structured** track (that is what makes
a dataset searchable), archives the original into `90-Indexes/Sources/`, and
clears it out of staging.

A file is classified analytical when **any** of these trips
(`classification.analytical`, all tunable):

| Signal | Default | Rationale |
|--------|---------|-----------|
| `max_bytes` | 2 MB | large spreadsheets are datasets, not documents |
| `max_rows` | 5,000 | CSV/TSV rows, or JSON array length |
| `max_sheets` | 5 | many tabs implies a workbook/model |
| `max_formula_cells` | 200 | formula density implies a model, not data |

Set `classification.analytical.require_optin: false` to index them directly, or
`classification.enabled: false` to disable the whole mechanism and send
everything down the document track (the pre-classification behaviour).

## Pipeline stages

Stages 1–3 and 12–14 are shared; the middle differs per track.

**Shared entry**

1. **Classify** — route to a track, or skip an unsupported extension.
2. **Hash** (SHA-256) the bytes.
3. **Manifest lookup** — if the same path+hash already completed, skip. If the
   same hash exists under a different path, skip as a duplicate.

**Track 1 — document knowledge**

4. **Preserve** the original into `90-Indexes/Sources/`.
5. **Convert** to Markdown with MarkItDown (deterministic, never an LLM).
6. **Metadata / frontmatter** generation.
7. **Chunk** the Markdown with the heading-aware semantic chunker; each chunk
   gets a content hash.
8. **Enrich** (optional, selective) — a compact document *brief* only.
9. **Write** the canonical Markdown note.
10. **Embed** only chunks whose content hash changed; upsert to the vector
    store; delete stale chunks.
11. **Graph** — re-derive relationships from links/tags/related.

**Track 2 — structured knowledge**

4. **Preserve** the original into `90-Indexes/Sources/`.
5. **Parse** into tables → columns (inferred type, null/distinct counts,
   examples) → records. Row-capped by `structured.max_rows_indexed`.
6. **Frontmatter** with `type: reference`, `tags: [structured, <fmt>]`,
   `record_count` and table names.
7. **Chunk** from *records*, not from the Markdown: each chunk is a group of
   `column: value` blocks (`structured.records_per_chunk`), preceded by one
   schema-overview chunk so "what columns does X have?" retrieves well.
8. *(no enrichment — the schema is already structured)*
9. **Write** the canonical note: a schema summary plus a row sample
   (`structured.sample_rows`), not a dump of every cell.
10. **Embed** the record chunks (same incremental hashing as track 1).
11. **Graph** — low-cardinality column values become `entity` nodes linked from
    the note by a `mentions` edge, so `Platform` or `eu-west-1` become real,
    resolvable nodes rather than dangling wiki links.

**Track 3 — analytical**

4. **Stage** the file into `classification.analytical.staging_dir` and stop.
   Nothing is converted, chunked, embedded or linked. The manifest records the
   track and reaches `completed`, because staging is a finished outcome rather
   than an interrupted job to retry.

**Shared exit** (tracks 1 and 2)

12. **Lint** the Markdown.
13. **Manifest = completed**, optional `git add/commit`.
14. **Finalise source** — clear the file from the inbox. The original is already
    archived under `90-Indexes/Sources/`, so it is *not* duplicated into
    `.processed/`. (If that archive copy is missing or size-mismatched, the file
    is moved to `00-Inbox/.processed/` instead, so nothing is ever lost.) A
    promoted file is cleared from the staging area the same way.

### Why structured data is not just converted to Markdown

MarkItDown can render a spreadsheet as a Markdown table, and embedding that
table is tempting — but it retrieves badly. Chunk boundaries fall mid-table, so
column headers end up in one chunk and rows in another; and a naked row like
`api,Platform,eu-west-1` carries no hint of what those values *mean*. The
structured track keeps the Markdown table for humans and embeds
`owner: Platform` / `region: eu-west-1` for the retriever.

## Idempotency & recovery

State lives in SQLite under `.knowledge/state/state.sqlite`:

- **`manifest`** — one row per source file: hash, resolved document id,
  markdown path, current `stage`, chunk hashes, last error.
- **`document_chunks`** — the content hash of every indexed chunk, enabling
  "re-embed only what changed".

Because every stage advances `stage`, a crash mid-pipeline leaves a partial
record; re-running resumes safely. Re-processing an unchanged file is a no-op.

## Event handling

| Event | Behaviour |
|-------|-----------|
| new file | full pipeline |
| modified file | re-hash; re-embed only changed chunks |
| deleted file | remove document from vector + graph, drop manifest |
| renamed/moved | treat old path as delete, new path as create |
| duplicate content | skipped (hash already known) |
| analytical file | `staged`; moved to the staging area, nothing indexed |
| promoted staged file | forced down the structured track, then cleared from staging |
| unsupported type | skipped with a reason |
| failed processing | recorded as `failed` with the error; safe to retry |
| app restart | startup sweep re-scans the inbox |

Every outcome reports `track` and `track_reasons`, so the CLI table and the
Control Panel can always explain *why* a file was handled the way it was.

## Commands and endpoints

| Action | CLI | HTTP |
|--------|-----|------|
| Preview the track for a file | `knowledge classify <file>` | `POST /classify` |
| Process the inbox (shows a `track` column) | `knowledge ingest-inbox` | `POST /ingest/inbox` |
| List files awaiting a decision | `knowledge staged` | `GET /staged` |
| Add a staged file to the knowledge base | `knowledge promote <file>` | `POST /ingest/promote` |

`GET /stats` reports `staged_analytical` plus a `tracks` breakdown, which is
what the Control Panel's Overview cards display.

## Organization (flat, metadata-driven)

There is no folder taxonomy to classify into. Every ingested note is written
to a single content folder (`vault_layout.content_dir`, default `Notes/`).
Organization comes entirely from metadata:

- `domain` / `topic` frontmatter (unset unless authored or assigned by
  optional enrichment)
- `tags`
- `[[wiki links]]` and `related:`

The folder a file happens to sit in has no semantic meaning, so you can move
notes freely. When the optional LLM/Jev providers are enabled, the enricher
may assign a `topic` (and, if you configured `vault_layout.domains`, a
`domain`) as metadata — never as a folder move.
