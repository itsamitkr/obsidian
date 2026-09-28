"""FastAPI application exposing the knowledge system over HTTP.

This is the "Local API" node in the architecture. It is intentionally thin:
all logic lives in the shared services so the CLI and MCP server behave
identically. The API binds to localhost by default (see config.api).

Security note: this API has no authentication and is intended for
local-only use. Do NOT expose it to a network without adding auth / a
reverse proxy.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

try:
    from fastapi import FastAPI, File, HTTPException, UploadFile
    from fastapi.responses import FileResponse
    from pydantic import BaseModel
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "FastAPI is required for the API. Install with "
        "`pip install 'knowledge-system[api]'`."
    ) from exc

from ..ingestion.pipeline import IngestionPipeline
from ..markdown.frontmatter import parse_frontmatter
from ..rebuild import IndexRebuilder
from ..search import SearchService
from ..system import KnowledgeSystem
from .control import ControlCenter, generate_key

_STATIC_DIR = Path(__file__).parent / "static"


class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    domain: str | None = None
    topic: str | None = None


class TunnelRequest(BaseModel):
    port: int = 8765


class McpRequest(BaseModel):
    port: int = 8765
    read_only: bool = True
    api_key: str | None = None


class PromoteRequest(BaseModel):
    """Path of a staged analytical file (bare name or vault-relative)."""

    path: str


class ClassifyRequest(BaseModel):
    path: str


def create_app(ks: KnowledgeSystem | None = None) -> FastAPI:
    ks = ks or KnowledgeSystem()
    pipeline = IngestionPipeline(ks)
    search = SearchService(ks)
    rebuilder = IndexRebuilder(ks)
    # Project root = three levels up from this file (src/knowledge/api/app.py).
    control = ControlCenter(
        project_root=Path(__file__).resolve().parents[3],
        vault_path=ks.settings.vault_path,
        ks=ks,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # Stop the watcher / MCP server / tunnel the panel started, so closing
        # the app doesn't leave the vector store locked or a tunnel published.
        control.shutdown()

    app = FastAPI(
        title="Knowledge System API", version="0.1.0", lifespan=lifespan
    )

    # ---- Control Panel UI ------------------------------------------------

    @app.get("/", include_in_schema=False)
    def panel():
        index = _STATIC_DIR / "index.html"
        if not index.exists():
            raise HTTPException(status_code=404, detail="panel not installed")
        # Never cache the panel. The HTML carries the inline JS, so a cached
        # copy silently runs stale frontend code against an updated backend --
        # which looks exactly like "the buttons stopped working".
        return FileResponse(
            index,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/health")
    def health(probe: bool = False):
        """Provider status.

        Defaults to the cheap report so this endpoint stays pollable; building
        the embedding model takes ~10s on first use. Pass ``?probe=true`` to
        actually construct and verify every provider.
        """
        return ks.health(probe=probe)

    @app.get("/stats")
    def stats():
        """Counts for the panel dashboard.

        Vector-store access is optional here: with embedded Qdrant another
        process may hold the storage lock, and the dashboard should still
        render rather than 500.
        """
        notes = [
            p for p in ks.settings.vault_path.rglob("*.md")
            if ".knowledge" not in p.parts
        ]
        inbox = ks.settings.inbox_path
        pending = [
            p for p in inbox.glob("*")
            if p.is_file()
            and p.suffix.lower() in ks.settings.ingest.supported_extensions
        ] if inbox.exists() else []
        graph = ks.graph_store.all()
        manifest = ks.state.all_manifest()

        vector_name, vector_note = ks.settings.vector.provider, None
        try:
            vector_name = ks.vector_store.name
        except Exception as exc:  # noqa: BLE001
            vector_note = (
                "vector store unavailable: another process holds it "
                f"({type(exc).__name__}). Stop the MCP server or switch to a "
                "Qdrant server for concurrent access."
            )
        return {
            "notes": len(notes),
            "pending_inbox": len(pending),
            "pending_files": [p.name for p in pending][:20],
            "sources": len([
                p for p in ks.settings.sources_path.glob("*")
                if p.is_file() and p.name != ".keep"
            ]) if ks.settings.sources_path.exists() else 0,
            "graph_nodes": len(graph.nodes),
            "graph_edges": len(graph.edges),
            "broken_links": len(ks.graph_store.broken_links()),
            "orphans": len(ks.graph_store.orphans()),
            "documents_processed": len(manifest),
            "staged_analytical": len(pipeline.list_staged()),
            "tracks": {
                t: sum(1 for m in manifest if m.track == t)
                for t in ("document", "structured", "analytical")
            },
            "vault_path": str(ks.settings.vault_path.resolve()),
            "embedder": ks.settings.embedding.provider,
            "vector_store": vector_name,
            "converter": ks.converter.name,
            "warning": vector_note,
        }

    @app.get("/notes")
    def list_notes(limit: int = 200):
        out = []
        for p in sorted(ks.settings.vault_path.rglob("*.md")):
            if ".knowledge" in p.parts:
                continue
            fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
            out.append({
                "path": str(p.relative_to(ks.settings.vault_path)).replace("\\", "/"),
                "title": fm.get("title") or p.stem,
                "topic": fm.get("topic"),
                "tags": fm.get("tags") or [],
                "source_type": fm.get("source_type"),
            })
            if len(out) >= limit:
                break
        return out

    # ---- Ingestion -------------------------------------------------------

    @app.post("/ingest/upload")
    async def ingest_upload(files: list[UploadFile] = File(...)):
        """Accept one or many files, drop them in the inbox, and process."""
        inbox = ks.settings.inbox_path
        inbox.mkdir(parents=True, exist_ok=True)
        results = []
        for file in files:
            name = Path(file.filename or "upload").name
            dest = inbox / name
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                shutil.copyfileobj(file.file, tmp)
                tmp_path = Path(tmp.name)
            shutil.move(str(tmp_path), str(dest))
            outcome = pipeline.process_file(dest)
            d = dict(outcome.__dict__)
            d.pop("enrichment", None)
            d["filename"] = name
            results.append(d)
        return results

    @app.post("/ingest/inbox")
    def ingest_inbox(force: bool = False):
        out = []
        for o in pipeline.process_inbox(force=force):
            d = dict(o.__dict__)
            d.pop("enrichment", None)
            out.append(d)
        return out

    # ---- Analytical staging (track 3) ------------------------------------

    @app.get("/staged")
    def staged():
        """Analytical files parked for analysis, awaiting an explicit decision."""
        return pipeline.list_staged()

    @app.post("/ingest/promote")
    def promote(req: PromoteRequest):
        """Add a staged analytical file to the knowledge base (opt-in)."""
        target = Path(req.path)
        if not target.is_absolute() and not (ks.settings.vault_path / target).exists():
            target = Path(
                ks.settings.classification.analytical.staging_dir
            ) / target.name
        outcome = pipeline.promote(target)
        d = dict(outcome.__dict__)
        d.pop("enrichment", None)
        return d

    @app.post("/classify")
    def classify(req: ClassifyRequest):
        """Dry-run: which track would this file take, and why."""
        return ks.classifier.classify(Path(req.path)).to_dict()

    @app.post("/search")
    def do_search(req: SearchRequest):
        results = search.search(req.query, req.limit, req.domain, req.topic)
        return [r.model_dump() for r in results]

    @app.get("/graph")
    def graph():
        return ks.graph_store.all().model_dump()

    @app.get("/graph/node/{node_id}")
    def graph_node(node_id: str):
        node = ks.graph_store.get_node(node_id)
        if not node:
            raise HTTPException(status_code=404, detail="node not found")
        view = ks.graph_store.neighbors(node_id)
        backlinks = ks.graph_store.backlinks(node_id)
        return {
            "node": node.model_dump(),
            "neighbors": view.model_dump(),
            "backlinks": [b.model_dump() for b in backlinks],
        }

    @app.get("/graph/orphans")
    def orphans():
        return [n.model_dump() for n in ks.graph_store.orphans()]

    @app.get("/graph/broken-links")
    def broken_links():
        return [e.model_dump() for e in ks.graph_store.broken_links()]

    @app.post("/rebuild/vector-index")
    def rebuild_vector(full: bool = True):
        return rebuilder.rebuild_vector_index(full=full).__dict__

    @app.post("/rebuild/graph")
    def rebuild_graph():
        return {"nodes": rebuilder.rebuild_graph()}

    # ---- Process control (local only) ------------------------------------

    @app.get("/control/status")
    def control_status():
        return control.status()

    @app.post("/control/watcher/start")
    def watcher_start():
        return control.start_watcher()

    @app.post("/control/watcher/stop")
    def watcher_stop():
        return control.stop_watcher()

    @app.post("/control/mcp/start")
    def mcp_start(req: McpRequest):
        return control.start_mcp(
            port=req.port, read_only=req.read_only, api_key=req.api_key
        )

    @app.post("/control/mcp/stop")
    def mcp_stop():
        return control.stop_mcp()

    @app.post("/control/tunnel/start")
    def tunnel_start(req: TunnelRequest):
        return control.start_tunnel(port=req.port)

    @app.post("/control/tunnel/stop")
    def tunnel_stop():
        return control.stop_tunnel()

    @app.get("/control/api-key/new")
    def new_api_key():
        return {"api_key": generate_key()}

    @app.get("/control/commands")
    def commands():
        """Copy-pasteable CLI equivalents for everything the panel does."""
        root = str(control.project_root)
        exe = ".\\.venv\\Scripts\\knowledge.exe"
        agent = control.tunnel_agent_path() or ".\\tools\\ngrok.exe"
        url = control.tunnel_url()
        return {
            "project_root": root,
            "groups": [
                {
                    "title": "Ingest",
                    "items": [
                        {"label": "Process everything in the inbox",
                         "cmd": f"{exe} ingest-inbox"},
                        {"label": "Ingest one file",
                         "cmd": f"{exe} ingest path\\to\\file.pdf"},
                        {"label": "Watch the inbox (drop & walk away)",
                         "cmd": f"{exe} watch"},
                    ],
                },
                {
                    "title": "Indexes",
                    "items": [
                        {"label": "Rebuild vector index from Markdown",
                         "cmd": f"{exe} rebuild-vector-index"},
                        {"label": "Rebuild graph from links/tags",
                         "cmd": f"{exe} rebuild-graph"},
                        {"label": "Show provider health",
                         "cmd": f"{exe} health"},
                    ],
                },
                {
                    "title": "Local agent (Claude Code / Desktop) — no tunnel",
                    "items": [
                        {"label": "Run MCP over stdio",
                         "cmd": f"{exe} mcp"},
                        {"label": "Register with Claude Code",
                         "cmd": ("claude mcp add knowledge -e "
                                 f"KS_VAULT_PATH={ks.settings.vault_path.resolve()}"
                                 f" -- {root}\\.venv\\Scripts\\knowledge.exe mcp")},
                    ],
                },
                {
                    "title": "Remote (claude.ai web) — optional, on demand",
                    "items": [
                        {"label": "1. MCP over HTTP (read-only)",
                         "cmd": (f"{exe} mcp --transport http --port 8765 "
                                 "--public --read-only")},
                        {"label": "2. Public HTTPS tunnel (runs locally)",
                         "cmd": f"{agent} http 8765"},
                        {"label": "3. Connector URL for claude.ai",
                         "cmd": (f"{url}/mcp" if url
                                 else "(start the tunnel to get the URL)")},
                    ],
                },
            ],
        }

    return app


app = None  # lazily created by the CLI / uvicorn factory


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app
