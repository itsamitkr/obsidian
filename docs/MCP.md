# MCP server

The system exposes the knowledge base to LLMs/agents via the official MCP
Python SDK. It works with both the 1.x (`FastMCP`) and 2.x (`MCPServer`) SDK
lines automatically.

```bash
pip install -e ".[mcp]"
knowledge mcp                              # stdio (default) -- local MCP clients
knowledge mcp --transport http --port 8765 # optional HTTP, binds 127.0.0.1 only
```

Everything runs on your machine. With stdio (the default) nothing listens on
the network: the MCP client starts `knowledge mcp` itself as a child process
and talks to it over stdin/stdout. Clients that connect from the cloud
(claude.ai web/mobile custom connectors) would need a public URL, so this
project deliberately doesn't support them.

## Connect a local MCP client (stdio)

1. Install: run `install.cmd` from the repo folder (macOS: `bash install.sh`).
   It creates `.venv` with everything, including the MCP SDK.

2. Add this server to your client's MCP config:

   - **command**: the absolute path to `.venv\Scripts\knowledge.exe`
   - **args**: `["mcp"]`
   - **env**: at least an absolute `KS_VAULT_PATH`, plus the provider
     settings your vault's index was built with.

   ```json
   {
     "mcpServers": {
       "knowledge": {
         "command": "C:\\path\\to\\obsidian\\.venv\\Scripts\\knowledge.exe",
         "args": ["mcp"],
         "env": {
           "KS_VAULT_PATH": "C:\\path\\to\\obsidian\\knowledge-vault",
           "KS_VECTOR__PROVIDER": "qdrant_local",
           "KS_EMBEDDING__PROVIDER": "sentence_transformers"
         }
       }
     }
   }
   ```

   The same `mcpServers` block works in Claude Desktop's
   `claude_desktop_config.json` and in Kiro's `.kiro/settings/mcp.json`.
   Restart or reload the client after editing.

Why the `env` block: the client doesn't start the server in the repo folder,
so the repo's `.env` and the relative default `./knowledge-vault` are not
picked up. Set the paths and providers there, or point `KS_CONFIG_FILE` at an
absolute YAML config file. `config/default.yaml` is found relative to the
installed package, so it still loads.

Notes:

- stdout carries only the MCP protocol; logs go to stderr.
- Embedded Qdrant (`qdrant_local`) allows one process at a time. Don't run
  `knowledge panel` or `knowledge watch` while the client has the server open,
  or use the `qdrant` server provider.
- The server is **read-only by default** (stdio and HTTP): `create_note`,
  `ingest_file` and `rebuild_vector_index` are not offered, so instructions
  hidden in an ingested document can't get the agent to change the vault. To
  enable them, add `"--allow-writes"` to `args` (or set
  `KS_MCP_ALLOW_WRITES=true` in `env`).

Claude Code one-liner:
`claude mcp add knowledge -e KS_VAULT_PATH=C:\path\to\obsidian\knowledge-vault -- C:\path\to\obsidian\.venv\Scripts\knowledge.exe mcp`

## HTTP transport (local only, advanced)

Off by default. For local HTTP MCP clients on this machine:

```bash
knowledge mcp --transport http --port 8765
#   -> http://127.0.0.1:8765/mcp
```

It binds `127.0.0.1` and has **no authentication**, so never expose it beyond
localhost. Any local process can reach it. It is read-only unless you pass
`--allow-writes`, `--api-key` requires an `x-api-key` header, and a `--host`
other than localhost prints a warning. `--public` / `--allow-host` relax the
Host/Origin checks and are only for use behind your own authenticated reverse
proxy. The Control Panel's "Start MCP (HTTP)" button runs the same server
in-process on 127.0.0.1.

## Tools

| Tool | Purpose |
|------|---------|
| `search_knowledge(query, limit, domain?, topic?)` | semantic search; returns compact snippets |
| `get_note(markdown_path)` | read a note's frontmatter + body |
| `list_tags()` | all tags used across the vault |
| `list_topics()` | distinct `topic` values across notes |
| `graph_neighbors(node_id)` | node + outgoing edges + backlinks |
| `graph_orphans()` | notes with no links |
| `graph_broken_links()` | wiki links with no target note |
| `ingest_file(path, force?)` | run the ingestion pipeline on a file (`--allow-writes` only) |
| `create_note(title, body, domain?, topic?, tags?, related?)` | create + index a note (`--allow-writes` only) |
| `rebuild_vector_index(full?)` | rebuild the vector index from Markdown (`--allow-writes` only) |
| `health()` | provider/config status |

## Resources

| Resource URI | Content |
|--------------|---------|
| `knowledge://note/{markdown_path}` | raw Markdown of a note |

## Notes on token usage

`search_knowledge` truncates snippets and returns metadata (paths, heading
paths, scores) so an agent can decide what to read next with `get_note`,
rather than pulling whole documents. This keeps agent context small.
