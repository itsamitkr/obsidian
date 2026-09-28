"""MCP server exposing the knowledge base to LLMs / agents.

Built on the official MCP Python SDK (``mcp`` package, FastMCP). Exposes
tools for searching, navigating the graph, reading notes, and updating the
knowledge base, plus resources for direct note access.

Run with:  ``knowledge mcp``  (stdio transport)

Tools are deliberately token-frugal: search returns compact snippets, and
note reads can be scoped. The MCP layer never bypasses the shared services,
so behaviour matches the CLI and API exactly.
"""

from __future__ import annotations

from pathlib import Path

from ..config import load_settings
from ..ingestion.pipeline import IngestionPipeline
from ..markdown.frontmatter import dump_frontmatter, parse_frontmatter
from ..models import Frontmatter, NoteType, slugify, utcnow
from ..rebuild import IndexRebuilder
from ..search import SearchService
from ..system import KnowledgeSystem


def _server_class():
    """Return (ServerClass, major_version), supporting both MCP SDK lines.

    mcp >= 2.x renamed ``FastMCP`` to ``MCPServer`` (``mcp.server.mcpserver``)
    and moved host/port from the constructor to ``run()``. The decorator API
    (``.tool()`` / ``.resource()``) is identical across both, so we only need
    to abstract the import and the run() call.
    """
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2.x

        return MCPServer, 2
    except Exception:
        try:
            from mcp.server.fastmcp import FastMCP  # mcp 1.x

            return FastMCP, 1
        except Exception as exc:  # pragma: no cover
            raise ImportError(
                "The MCP SDK is required. Install with "
                "`pip install 'knowledge-system[mcp]'`."
            ) from exc


def build_server(
    config: str | None = None,
    read_only: bool = False,
    ks: KnowledgeSystem | None = None,
):
    """Build the MCP server.

    read_only: register only retrieval tools. Use when exposing the server
    over a network so a leaked URL cannot mutate the vault.
    ks:        share an already-open KnowledgeSystem. Required when a host
               process (e.g. the Control Panel) also has the vault open:
               embedded Qdrant allows only ONE client per storage folder, so
               creating a second one makes every tool call fail.
    """
    server_class, _ = _server_class()

    ks = ks or KnowledgeSystem(load_settings(config))
    search_service = SearchService(ks)
    pipeline = IngestionPipeline(ks)
    rebuilder = IndexRebuilder(ks)

    mcp = server_class("knowledge-system")

    # ---- Tools: retrieval ------------------------------------------------

    @mcp.tool()
    def search_knowledge(
        query: str, limit: int = 8, domain: str | None = None,
        topic: str | None = None,
    ) -> list[dict]:
        """Semantic search over the knowledge base. Returns compact snippets
        with markdown_path, heading_path and score."""
        results = search_service.search(query, limit, domain, topic)
        return [
            {
                "document_id": r.document_id,
                "markdown_path": r.markdown_path,
                "heading_path": r.heading_path,
                "domain": r.domain,
                "topic": r.topic,
                "score": round(r.score, 4),
                "snippet": r.text[:500],
            }
            for r in results
        ]

    @mcp.tool()
    def get_note(markdown_path: str) -> dict:
        """Read a note (frontmatter + body) by its vault-relative path."""
        path = ks.settings.vault_path / markdown_path
        if not path.exists():
            return {"error": f"not found: {markdown_path}"}
        fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        return {"frontmatter": fm, "body": body, "markdown_path": markdown_path}

    @mcp.tool()
    def list_tags() -> list[str]:
        """List all tags currently used across the vault (flat, metadata-driven
        organization -- tags/topics are the primary grouping, not folders)."""
        tags: set[str] = set()
        for node in ks.graph_store.all().nodes:
            if node.type == "tag":
                tags.add(node.title.lstrip("#"))
            tags.update(node.tags)
        return sorted(tags)

    @mcp.tool()
    def list_topics() -> list[str]:
        """List distinct `topic` values found across notes."""
        topics: set[str] = set()
        for md in ks.settings.vault_path.rglob("*.md"):
            if ".knowledge" in md.parts:
                continue
            fm, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
            if fm.get("topic"):
                topics.add(str(fm["topic"]))
        return sorted(topics)

    # ---- Tools: graph navigation ----------------------------------------

    @mcp.tool()
    def graph_neighbors(node_id: str) -> dict:
        """Get a node, its outgoing edges, target nodes and backlinks."""
        node = ks.graph_store.get_node(node_id)
        if not node:
            return {"error": f"no node '{node_id}'"}
        view = ks.graph_store.neighbors(node_id)
        return {
            "node": node.model_dump(),
            "edges": [e.model_dump() for e in view.edges],
            "backlinks": [b.model_dump() for b in ks.graph_store.backlinks(node_id)],
        }

    @mcp.tool()
    def graph_orphans() -> list[dict]:
        """Notes with no incoming or outgoing links."""
        return [n.model_dump() for n in ks.graph_store.orphans()]

    @mcp.tool()
    def graph_broken_links() -> list[dict]:
        """Wiki links whose target note does not exist."""
        return [e.model_dump() for e in ks.graph_store.broken_links()]

    # ---- Tools: mutation -------------------------------------------------
    # Not registered in read-only mode, so a network-exposed server cannot
    # modify the vault (defence in depth for proxied/tunnelled deployments).
    if not read_only:

        @mcp.tool()
        def ingest_file(path: str, force: bool = False) -> dict:
            """Ingest a source file (any supported format) into the knowledge
            base."""
            outcome = pipeline.process_file(Path(path), force=force)
            d = dict(outcome.__dict__)
            d.pop("enrichment", None)
            return d

        @mcp.tool()
        def create_note(
            title: str, body: str, domain: str | None = None,
            topic: str | None = None, tags: list[str] | None = None,
            related: list[str] | None = None,
        ) -> dict:
            """Create or overwrite a Markdown note, then index it. The Markdown
            repo remains the source of truth; the vector/graph indexes update."""
            note_id = slugify(title)
            now = utcnow()
            fm = Frontmatter(
                id=note_id, title=title, domain=domain, topic=topic,
                type=NoteType.note, created_at=now, updated_at=now,
                tags=tags or [], related=related or [],
            )
            # Flat layout: all notes live in the content folder; domain/topic
            # are metadata, not folder locations.
            folder = ks.settings.content_path
            folder.mkdir(parents=True, exist_ok=True)
            md_path = folder / f"{note_id}.md"
            md_path.write_text(
                dump_frontmatter(fm.model_dump(mode="json"), body),
                encoding="utf-8",
            )
            rel = str(md_path.relative_to(ks.settings.vault_path)).replace(
                "\\", "/"
            )
            # Index the new note deterministically.
            rebuilder.rebuild_graph()
            rebuilder.rebuild_vector_index(full=False)
            return {"markdown_path": rel, "document_id": note_id}

        @mcp.tool()
        def rebuild_vector_index(full: bool = True) -> dict:
            """Rebuild the vector index from the Markdown repository."""
            return rebuilder.rebuild_vector_index(full=full).__dict__

    @mcp.tool()
    def health() -> dict:
        """Report provider/config health."""
        return {**ks.health(), "mode": "read-only" if read_only else "read-write"}

    # ---- Resources -------------------------------------------------------

    @mcp.resource("knowledge://note/{markdown_path}")
    def note_resource(markdown_path: str) -> str:
        """Expose a note as an MCP resource."""
        path = ks.settings.vault_path / markdown_path
        if not path.exists():
            return f"# Not found\n\n{markdown_path}"
        return path.read_text(encoding="utf-8")

    return mcp


def run_stdio(config: str | None = None) -> None:
    """Run over stdio -- for local clients like Claude Desktop / Claude Code."""
    build_server(config).run("stdio")


def _api_key_middleware(app, api_key: str, header: str = "x-api-key"):
    """ASGI middleware requiring a shared secret header on every request.

    Useful for MCP clients (or a local reverse proxy) that can set headers.
    Note that claude.ai's custom-connector UI cannot send static headers, so
    this is NOT usable for a claude.ai connector -- prefer ``--read-only``
    there. Requests without a valid key are rejected with 401.
    """
    header_bytes = header.lower().encode()
    expected = api_key.encode()

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        provided = None
        for k, v in scope.get("headers", []):
            if k.lower() == header_bytes:
                provided = v
                break
        # constant-time comparison
        import hmac

        if provided is None or not hmac.compare_digest(provided, expected):
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"error":"unauthorized: missing or invalid API key"}',
            })
            return
        await app(scope, receive, send)

    return wrapped


def _transport_security(allowed_hosts: list[str] | None, public: bool):
    """Build TransportSecuritySettings for HTTP transports.

    The MCP SDK blocks requests whose Host header isn't localhost (DNS
    rebinding protection), which returns HTTP 421 behind a tunnel. When
    serving through a tunnel the incoming Host is the public domain, so it
    must be allow-listed (``allowed_hosts``) or the check disabled (``public``).
    """
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except Exception:
        return None

    if public:
        # Accept any Host/Origin. Only safe behind an authenticated tunnel.
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if allowed_hosts:
        origins = []
        for h in allowed_hosts:
            origins += [f"https://{h}", f"http://{h}"]
        return TransportSecuritySettings(
            allowed_hosts=list(allowed_hosts), allowed_origins=origins
        )
    return None


def run_http(
    config: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    transport: str = "streamable-http",
    allowed_hosts: list[str] | None = None,
    public: bool = False,
    api_key: str | None = None,
    read_only: bool = False,
) -> None:
    """Run over HTTP -- required for *remote* clients such as claude.ai web,
    which reach the server from Anthropic's cloud over HTTPS.

    SECURITY: this server performs NO authentication. Exposing it publicly
    lets anyone who can reach the URL read and modify your vault. Only expose
    it through an authenticated tunnel / reverse proxy, and never bind to a
    public interface without one. See docs/MCP.md.

    allowed_hosts: Host header values to accept (e.g. the tunnel domain).
    public:        accept any Host/Origin (disables DNS-rebinding protection).
    api_key:       require this shared secret in the x-api-key header. Only
                   works with clients that can set headers -- claude.ai cannot.
    read_only:     expose retrieval tools only (no create/ingest/rebuild).

    transport: "streamable-http" (endpoint /mcp) or "sse" (endpoint /sse).
    """
    _, major = _server_class()
    server = build_server(config, read_only=read_only)
    sec = _transport_security(allowed_hosts, public)

    if api_key:
        # Serve the ASGI app ourselves so we can wrap it with auth.
        try:
            import uvicorn
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "--api-key requires uvicorn. Install with "
                "`pip install 'knowledge-system[api]'`."
            ) from exc
        if transport == "sse":
            app = server.sse_app(transport_security=sec) if sec else server.sse_app()
        else:
            app = (
                server.streamable_http_app(transport_security=sec)
                if sec
                else server.streamable_http_app()
            )
        uvicorn.run(
            _api_key_middleware(app, api_key), host=host, port=port,
            log_level="info",
        )
        return

    kwargs: dict = {"host": host, "port": port}
    if sec is not None:
        kwargs["transport_security"] = sec
    if major >= 2:
        # mcp 2.x: host/port (and transport_security) are passed to run().
        server.run(transport, **kwargs)
    else:
        # mcp 1.x: host/port live on settings.
        server.settings.host = host
        server.settings.port = port
        server.run(transport)
