# MCP server

The system exposes the knowledge base to LLMs/agents via the official MCP
Python SDK. It works with both the 1.x (`FastMCP`) and 2.x (`MCPServer`) SDK
lines automatically.

```bash
pip install -e ".[mcp]"
knowledge mcp                              # stdio (default) -- local clients
knowledge mcp --transport http --port 8765 # streamable-http -- remote clients
```

## Transports: which one for which client

| Client | Transport | How it reaches the server |
|--------|-----------|---------------------------|
| Claude Desktop, Claude Code, Cursor | **stdio** | launches the server as a local child process |
| **claude.ai web / mobile** | **streamable-http** | Anthropic's cloud connects to a **public HTTPS URL** |

The key fact: claude.ai web does **not** run anything on your machine — it
connects from Anthropic's cloud, so it needs a publicly reachable HTTPS
endpoint. `localhost` alone will not work.

## Local clients (Claude Desktop / Code / Cursor)

Point the client's MCP config at the installed `knowledge` command. Use an
absolute `KS_VAULT_PATH` so the right vault (and its co-located state) is used
regardless of the process's working directory:

```json
{
  "mcpServers": {
    "knowledge": {
      "command": "C:/Users/you/project/.venv/Scripts/knowledge.exe",
      "args": ["mcp"],
      "env": {
        "KS_VAULT_PATH": "C:/Users/you/project/knowledge-vault"
      }
    }
  }
}
```

## Remote client: claude.ai web

claude.ai web requires a public HTTPS URL and a paid plan (Pro/Max/Team/
Enterprise; custom connectors are in beta). Steps:

1. Run the server over HTTP (binds to localhost). Because requests arrive
   through a tunnel, the Host header is the public domain, so it must be
   accepted explicitly:

   ```bash
   # Accept any Host (the tunnel is the front door):
   knowledge mcp --transport http --port 8765 --public --read-only

   # Or pin your ngrok domain for a tighter check:
   knowledge mcp --transport http --port 8765 \
     --allow-host your-assigned-name.ngrok-free.dev --read-only
   ```

   Without one of these the SDK's DNS-rebinding protection rejects tunneled
   requests with **HTTP 421 "Invalid Host header"**.

2. Expose it over HTTPS with the local ngrok agent (do NOT bind the server to
   0.0.0.0):

   ```bash
   ngrok http 8765
   #   -> https://<your-domain>.ngrok-free.dev   (stable across restarts)
   ```

3. In claude.ai: **Settings → Connectors → Add custom connector**, paste the
   public URL with the `/mcp` path, e.g.
   `https://<your-domain>.ngrok-free.dev/mcp`, and connect.

Full walkthrough, including authtoken setup and troubleshooting:
[docs/TUNNEL.md](TUNNEL.md).

> Easiest path: `knowledge panel` does steps 1–2 with buttons and shows the
> connector URL with a copy button. It also runs MCP in-process, avoiding the
> embedded-Qdrant single-client lock.

### ⚠️ Security — read before exposing

By default the HTTP transport has **no authentication**: a public tunnel URL
grants anyone who has it full read/write access to your vault via
`create_note`, `ingest_file` and `rebuild_*`.

Important constraint: claude.ai's custom-connector UI **cannot attach static
headers**, and it only supports servers that are fully public or speak
OAuth 2.1. Two consequences:

- Simply adding `--api-key` and pointing claude.ai at the tunnel **will not
  work** (every request would be rejected).
- Auth proxies that require an interactive login don't help either — Anthropic's
  cloud cannot complete one. They remain useful for protecting your *own*
  browser access.

Mitigations, strongest first:

1. **Don't expose it.** Use a local stdio client (Claude Desktop / Claude Code).
   No tunnel, no URL to leak. This is the recommended setup.
2. **`--read-only`** — expose retrieval tools only, so even a leaked URL cannot
   create, ingest, or rebuild anything. Use this whenever you tunnel.
3. **Stop the tunnel when you're done.** A stable hostname is permanently
   guessable once known, so don't leave it running idle.
4. **`--api-key`** — requires an `x-api-key` header. Note that **claude.ai
   cannot send it**, so this only helps for MCP clients (or a local reverse
   proxy) that can set headers. It is not usable for a claude.ai connector.
5. **Full OAuth 2.1** on the transport — the only native auth claude.ai
   supports; significant work, only worth it for a persistent deployment.

```bash
# Recommended when tunnelling: retrieval only, nothing can be modified
knowledge mcp --transport http --port 8765 --public --read-only
```

### Stable URLs

ngrok's free dev domain is **stable across restarts**, so the claude.ai
connector only needs to be configured once. Pin it explicitly with
`KS_TUNNEL__NGROK_DOMAIN` so the panel can show the URL before the agent
reports it. See [docs/TUNNEL.md](TUNNEL.md).

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
| `ingest_file(path, force?)` | run the ingestion pipeline on a file |
| `create_note(title, body, domain?, topic?, tags?, related?)` | create + index a note |
| `rebuild_vector_index(full?)` | rebuild the vector index from Markdown |
| `health()` | provider/config status |

## Resources

| Resource URI | Content |
|--------------|---------|
| `knowledge://note/{markdown_path}` | raw Markdown of a note |

## Notes on token usage

`search_knowledge` truncates snippets and returns metadata (paths, heading
paths, scores) so an agent can decide what to read next with `get_note`,
rather than pulling whole documents. This keeps agent context small.
