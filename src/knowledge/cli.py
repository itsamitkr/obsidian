"""Command-line interface: ``knowledge <command>``.

Mirrors the API surface so the system is fully usable without Obsidian or
the web UI. Uses Typer + Rich for a friendly local experience.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import load_settings
from .ingestion.pipeline import IngestionPipeline
from .ingestion.watcher import InboxWatcher
from .logging_setup import configure_logging
from .rebuild import IndexRebuilder
from .search import SearchService
from .system import KnowledgeSystem
from .vault import init_vault

app = typer.Typer(
    add_completion=False,
    help="Local-first knowledge management + AI knowledge-graph system.",
)
console = Console()


def _system(config: str | None) -> KnowledgeSystem:
    # Quiet the chatty third-party loggers before any model/provider is built.
    configure_logging()
    settings = load_settings(config)
    return KnowledgeSystem(settings)


ConfigOpt = typer.Option(None, "--config", "-c", help="Path to a YAML config file.")


@app.command()
def init(
    config: str | None = ConfigOpt,
    no_seed: bool = typer.Option(False, "--no-seed", help="Skip seed notes."),
):
    """Scaffold the vault directory structure and templates."""
    ks = _system(config)
    created = init_vault(ks.settings, seed=not no_seed)
    console.print(f"[green]Vault ready at[/green] {ks.settings.vault_path}")
    console.print(f"Created {len(created)} new files/dirs.")


@app.command()
def health(config: str | None = ConfigOpt):
    """Show system configuration and provider health."""
    ks = _system(config)
    console.print_json(json.dumps(ks.health()))


@app.command()
def ingest(
    path: str = typer.Argument(..., help="File to ingest (any supported format)."),
    config: str | None = ConfigOpt,
    force: bool = typer.Option(False, "--force", help="Reprocess even if unchanged."),
):
    """Ingest a single file through the full pipeline."""
    ks = _system(config)
    outcome = IngestionPipeline(ks).process_file(Path(path), force=force)
    console.print_json(json.dumps(_outcome_dict(outcome)))


@app.command("ingest-inbox")
def ingest_inbox(
    config: str | None = ConfigOpt,
    force: bool = typer.Option(False, "--force"),
):
    """Process everything currently in the inbox."""
    ks = _system(config)
    outcomes = IngestionPipeline(ks).process_inbox(force=force)
    table = Table("source", "track", "status", "doc", "chunks", "note")
    for o in outcomes:
        note = o.reason or o.error or (
            "; ".join(o.track_reasons) if o.status == "staged" else ""
        )
        table.add_row(o.source_path, o.track or "-", o.status,
                      o.document_id or "-", str(o.chunks), note)
    console.print(table)
    if any(o.status == "staged" for o in outcomes):
        console.print(
            "\n[yellow]Some files were staged for analysis[/yellow] and are not "
            "indexed. See [cyan]knowledge staged[/cyan]."
        )


@app.command()
def watch(config: str | None = ConfigOpt):
    """Watch the inbox and ingest new files automatically (Ctrl-C to stop)."""
    ks = _system(config)

    def _report(outcome):
        console.print(f"[cyan]{outcome.status}[/cyan] {outcome.source_path}"
                      f" -> {outcome.document_id or outcome.reason or ''}")

    watcher = InboxWatcher(ks, on_event=_report)
    console.print(f"[green]Watching[/green] {ks.settings.inbox_path} ...")
    try:
        watcher.run()
    except KeyboardInterrupt:
        watcher.stop()
        console.print("stopped.")


@app.command()
def staged(config: str | None = ConfigOpt):
    """List analytical files staged for analysis (not in the knowledge base)."""
    ks = _system(config)
    items = IngestionPipeline(ks).list_staged()
    if not items:
        console.print("[yellow]Nothing staged.[/yellow]")
        return
    table = Table("file", "size", "path")
    for it in items:
        table.add_row(it["name"], f"{it['bytes'] / 1_000_000:.2f} MB", it["path"])
    console.print(table)
    console.print(
        "\nIndex one with: [cyan]knowledge promote <file>[/cyan]"
    )


@app.command()
def promote(
    file: str = typer.Argument(..., help="Staged file name or vault-relative path."),
    config: str | None = ConfigOpt,
):
    """Add a staged analytical file to the knowledge base (explicit opt-in)."""
    ks = _system(config)
    pipeline = IngestionPipeline(ks)

    target = Path(file)
    if not target.is_absolute() and not (ks.settings.vault_path / target).exists():
        # Allow a bare filename: resolve it inside the staging area.
        target = Path(ks.settings.classification.analytical.staging_dir) / target.name

    outcome = pipeline.promote(target)
    if outcome.status != "processed":
        console.print(
            f"[red]{outcome.status}[/red]: {outcome.reason or outcome.error}"
        )
        raise typer.Exit(1)
    console.print(
        f"[green]Added to knowledge base[/green] via the "
        f"[cyan]{outcome.track}[/cyan] track\n"
        f"  note   : {outcome.markdown_path}\n"
        f"  chunks : {outcome.chunks} (embedded {outcome.embedded})"
    )


@app.command()
def classify(
    file: str = typer.Argument(..., help="File to classify (no changes made)."),
    config: str | None = ConfigOpt,
):
    """Show which ingestion track a file would take, and why."""
    ks = _system(config)
    c = ks.classifier.classify(Path(file))
    console.print(f"track   : [cyan]{c.track.value}[/cyan]")
    console.print(f"opt-in  : {c.requires_optin}")
    console.print("why     : " + "; ".join(c.reasons))
    if c.signals:
        console.print_json(json.dumps(c.signals))


@app.command()
def search(
    query: str = typer.Argument(...),
    config: str | None = ConfigOpt,
    limit: int = typer.Option(5, "--limit", "-n"),
    domain: str | None = typer.Option(None, "--domain"),
    topic: str | None = typer.Option(None, "--topic"),
):
    """Semantic search over the vector index."""
    ks = _system(config)
    results = SearchService(ks).search(query, limit, domain, topic)
    if not results:
        console.print("[yellow]No results.[/yellow]")
        return
    table = Table("score", "path", "heading", "domain")
    for r in results:
        table.add_row(f"{r.score:.3f}", r.markdown_path,
                      " > ".join(r.heading_path), r.domain or "")
    console.print(table)


@app.command("rebuild-vector-index")
def rebuild_vector_index(
    config: str | None = ConfigOpt,
    incremental: bool = typer.Option(
        False, "--incremental",
        help="Reuse unchanged chunks instead of dropping the collection.",
    ),
):
    """Rebuild the vector index from the Markdown repository."""
    ks = _system(config)
    report = IndexRebuilder(ks).rebuild_vector_index(full=not incremental)
    console.print_json(json.dumps(report.__dict__))


@app.command("rebuild-graph")
def rebuild_graph(config: str | None = ConfigOpt):
    """Rebuild the knowledge graph from Markdown links/properties."""
    ks = _system(config)
    nodes = IndexRebuilder(ks).rebuild_graph()
    console.print(f"[green]Graph rebuilt[/green]: {nodes} nodes.")


@app.command()
def graph(
    config: str | None = ConfigOpt,
    orphans: bool = typer.Option(False, "--orphans"),
    broken: bool = typer.Option(False, "--broken-links"),
    node: str | None = typer.Option(None, "--node", help="Inspect a node id."),
):
    """Inspect the knowledge graph."""
    ks = _system(config)
    store = ks.graph_store
    if node:
        n = store.get_node(node)
        if not n:
            console.print(f"[red]No node[/red] '{node}'")
            raise typer.Exit(1)
        view = store.neighbors(node)
        console.print_json(json.dumps({
            "node": n.model_dump(),
            "edges": [e.model_dump() for e in view.edges],
            "backlinks": [b.model_dump() for b in store.backlinks(node)],
        }))
        return
    if orphans:
        for n in store.orphans():
            console.print(f"orphan: {n.id} ({n.title})")
        return
    if broken:
        for e in store.broken_links():
            console.print(f"broken: {e.source} -> {e.target} ({e.relation})")
        return
    v = store.all()
    console.print(f"nodes={len(v.nodes)} edges={len(v.edges)}")


@app.command()
def serve(
    config: str | None = ConfigOpt,
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
):
    """Run the FastAPI local API server (also serves the control panel at /)."""
    _serve(config, host, port, open_browser=False)


@app.command()
def panel(
    config: str | None = ConfigOpt,
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
    no_open: bool = typer.Option(False, "--no-open", help="Don't open a browser."),
):
    """Open the Control Panel: upload files, run the watcher, rebuild indexes,
    manage the MCP server and tunnel -- all from one page."""
    _serve(config, host, port, open_browser=not no_open)


def _serve(config, host, port, open_browser: bool) -> None:
    try:
        import uvicorn
    except ImportError:
        console.print("[red]Install the API extra:[/red] "
                      "pip install 'knowledge-system[api]'")
        raise typer.Exit(1) from None
    from .api.app import create_app

    ks = _system(config)
    bind_host = host or ks.settings.api.host
    bind_port = port or ks.settings.api.port
    application = create_app(ks)

    # Load the embedding model in the background. It takes ~10s on a CPU, and
    # doing it lazily meant the first request to arrive paid the bill -- which
    # looked like the panel hanging on startup.
    import threading

    threading.Thread(
        target=ks.warm_up,
        kwargs={"on_event": lambda m: console.print(f"[dim]{m}[/dim]")},
        daemon=True,
    ).start()

    url = f"http://{bind_host}:{bind_port}/"
    console.print(f"[green]Control Panel[/green] {url}")
    if bind_host not in ("127.0.0.1", "localhost"):
        console.print(
            "[yellow]WARNING:[/yellow] this API has no authentication and can "
            "start local processes. Bind it to 127.0.0.1 only."
        )
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(application, host=bind_host, port=bind_port)


@app.command()
def mcp(
    config: str | None = ConfigOpt,
    transport: str = typer.Option(
        "stdio", "--transport", "-t",
        help="stdio (local: Claude Desktop) | http | sse (remote: claude.ai web).",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host for http/sse."),
    port: int = typer.Option(8765, "--port", help="Bind port for http/sse."),
    allow_host: list[str] = typer.Option(
        None, "--allow-host",
        help="Accept this Host header (e.g. your tunnel domain). Repeatable.",
    ),
    public: bool = typer.Option(
        False, "--public",
        help="Accept any Host/Origin (needed for quick tunnels whose domain "
             "changes each run). Only use behind an authenticated tunnel.",
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", envvar="KS_MCP_API_KEY",
        help="Require an x-api-key header. Only for clients that can send "
             "headers (claude.ai cannot). Also settable via KS_MCP_API_KEY.",
    ),
    read_only: bool = typer.Option(
        False, "--read-only",
        help="Expose retrieval tools only; disable create/ingest/rebuild.",
    ),
):
    """Run the MCP server.

    stdio  -> local clients (Claude Desktop, Claude Code).
    http   -> remote clients (claude.ai web) via a public HTTPS tunnel.

    WARNING: http/sse expose the vault with NO authentication. Only run them
    behind an authenticated tunnel/reverse proxy. See docs/MCP.md.
    """
    from .mcp.server import run_http, run_stdio

    if transport == "stdio":
        run_stdio(config)
        return
    if transport in ("http", "streamable-http", "sse"):
        resolved = "sse" if transport == "sse" else "streamable-http"
        if host not in ("127.0.0.1", "localhost"):
            console.print(
                "[yellow]WARNING:[/yellow] binding a no-auth MCP server to a "
                f"non-local host ({host}). Put an authenticated tunnel in front."
            )
        console.print(
            f"[green]MCP {resolved}[/green] on http://{host}:{port}"
            f"{'/mcp' if resolved == 'streamable-http' else '/sse'}"
        )
        if public:
            console.print(
                "[yellow]--public:[/yellow] accepting any Host/Origin (no DNS-"
                "rebinding protection). Ensure the tunnel in front is trusted."
            )
        console.print(
            f"auth: {'x-api-key required' if api_key else '[red]NONE[/red]'}"
            f" | mode: {'read-only' if read_only else 'read-write'}"
        )
        if not api_key and not read_only:
            console.print(
                "[yellow]Tip:[/yellow] when exposing this over a tunnel, use "
                "--api-key (via a header-injecting proxy) and/or --read-only."
            )
        run_http(config, host=host, port=port, transport=resolved,
                 allowed_hosts=allow_host or None, public=public,
                 api_key=api_key, read_only=read_only)
        return

    console.print(f"[red]Unknown transport '{transport}'[/red]")
    raise typer.Exit(1)


def _outcome_dict(outcome) -> dict:
    d = dict(outcome.__dict__)
    if d.get("enrichment") is not None:
        d["enrichment"] = outcome.enrichment.__dict__
    return d


if __name__ == "__main__":
    app()
