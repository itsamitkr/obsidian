# Public tunnel (ngrok)

claude.ai web connects to MCP servers **from Anthropic's cloud**, so it needs a
public HTTPS URL — `localhost` alone won't work. The tunnel bridges that gap.

Everything still runs on your machine: ngrok is a local agent, and nothing is
deployed to a cloud.

**Why ngrok:** its free plan includes one auto-assigned dev domain that is
**stable across restarts** and needs no domain of your own, so you add the
claude.ai connector once. See
[ngrok's pricing limits](https://ngrok.com/docs/pricing-limits) and their
[static dev domains post](https://ngrok.com/blog/free-static-domains-ngrok-users).
*(Rephrased for licensing compliance.)*

---

## Setup (one time)

### 1. Get a free authtoken

Sign up at [ngrok.com](https://ngrok.com) — when asked what you're doing, pick
**Share localhost**. Copy the token from **Your Authtoken**, then:

```powershell
cd path\to\knowledge-system
.\tools\ngrok.exe config add-authtoken <YOUR_TOKEN>
```

`ngrok.exe` already lives in `tools/` (git-ignored). If it's missing, download
the Windows AMD64 zip from ngrok and extract `ngrok.exe` into `tools/`.

### 2. Confirm the provider

`.env` should contain:

```ini
KS_TUNNEL__PROVIDER=ngrok
KS_TUNNEL__MCP_PORT=8765
```

### 3. (Optional) pin your domain

Find it in the ngrok dashboard under **Universal Gateway → Domains**. Newer
accounts get a `.ngrok-free.dev` name, older ones `.ngrok-free.app` — both work
(URL detection is TLD-agnostic).

```ini
KS_TUNNEL__NGROK_DOMAIN=your-assigned-name.ngrok-free.dev
```

Pinning means the panel can show the connector URL immediately instead of
waiting for the agent to report it.

---

## Daily use

```powershell
.\.venv\Scripts\knowledge.exe panel
```

In the panel: **Start MCP** → **Start tunnel** → copy the URL (it ends in
`/mcp`) → paste into claude.ai → **Settings → Connectors → Add custom
connector**.

Because the hostname is stable, that's a one-time step.

Equivalent from the CLI:

```powershell
# terminal 1 - MCP over HTTP, retrieval only
.\.venv\Scripts\knowledge.exe mcp --transport http --port 8765 --public --read-only
# terminal 2 - the tunnel
.\tools\ngrok.exe http 8765
```

> Prefer the panel: it runs the MCP server **in-process**, which avoids the
> embedded-Qdrant single-client lock. Running `knowledge mcp` in a terminal
> while the panel is open will make tool calls fail.

### The interstitial page (not a problem for MCP)

Free ngrok endpoints show a browser warning page for browser-style requests.
MCP traffic sends `Accept: application/json, text/event-stream`, which bypasses
it. **Verified:** an `initialize` handshake through a free `ngrok-free.dev`
endpoint returned `200 text/event-stream` with no interstitial HTML, and a
`search_knowledge` call returned real results.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ngrok has no authtoken` | Run step 1 above. |
| `ngrok not found` | Put `ngrok.exe` in `tools/` or on `PATH`. |
| Panel shows no URL | Give the agent a few seconds, or pin `KS_TUNNEL__NGROK_DOMAIN`. Check the tunnel log in the panel. |
| MCP returns **421 Invalid Host header** | The server needs `--public` (the panel passes it automatically). |
| `Error executing tool search_knowledge` | Something else holds the embedded Qdrant store. Don't run `knowledge watch` / `knowledge mcp` in a terminal while the panel is open — use the panel buttons. |
| Buttons seem dead / stale UI | Hard-refresh the browser (Ctrl+Shift+R) or click **↻ Reload UI**. |
| ngrok reports a bandwidth/session limit | Free-plan quota. Stop the tunnel when not in use. |

---

## Security

The tunnel makes this machine reachable from the internet for as long as it
runs, and a stable hostname is permanently guessable once known.

- Keep **`--read-only`** on (the panel default) — no MCP tool can modify the
  vault; only search/read/graph tools are exposed.
- Treat the URL as a secret and **stop the tunnel when you're done**.
- claude.ai cannot send custom headers, so an API key is not usable for a
  connector; read-only is the practical protection. (`--api-key` exists for
  clients that *can* set headers.)

---

## Using a different tunnel agent

`provider` in `TunnelConfig` is the extension point. To add another agent, add a
literal to `Literal["ngrok"]` in `src/knowledge/config.py` and a matching branch
in `ControlCenter.start_tunnel` (`src/knowledge/api/control.py`), then extend
`_TUNNEL_URL_RE` so the public URL can be parsed from its output.
