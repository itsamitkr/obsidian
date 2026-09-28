"""Background process/thread control for the local Control Panel.

Manages the three long-running helpers the panel can start and stop:

  * the inbox **watcher**      -> in-process thread
  * the **MCP HTTP server**    -> in-process thread, on its own port
  * the **public tunnel**      -> child process (ngrok)

Why the first two are in-process: the embedded Qdrant store
(``vector.provider=qdrant_local``) locks its storage folder to a single client.
Spawning ``knowledge watch`` or ``knowledge mcp`` as separate processes makes
them fail to open the index -- the watcher silently indexes nothing and every
MCP tool call returns "Error executing tool". Sharing the panel's already-open
:class:`KnowledgeSystem` removes the conflict entirely.

The tunnel stays a child process because it is an external binary, and it
points at the MCP port only -- never at the panel, whose control endpoints can
start processes.

This module is local-only by design: it launches things on the machine running
the API, so the API must never be exposed to a network.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# Public URL printed by ngrok. Deliberately TLD-agnostic: accounts get
# `.ngrok-free.dev`, `.ngrok-free.app`, `.ngrok.app` or `.ngrok.io` depending
# on age/plan, and pinning an exact list has already broken once.
_TUNNEL_URL_RE = re.compile(
    r"https://[a-z0-9][a-z0-9-]*\.ngrok[a-z0-9-]*\.[a-z]{2,24}",
    re.IGNORECASE,
)
_MAX_LINES = 400


@dataclass
class ManagedProcess:
    """A child process whose combined output is buffered for the UI."""

    name: str
    command: list[str]
    proc: subprocess.Popen | None = None
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=_MAX_LINES))
    _reader: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, cwd: Path | None = None, env: dict | None = None) -> None:
        if self.running:
            return
        self.lines.clear()
        merged_env = {**os.environ, **(env or {})}
        self.proc = subprocess.Popen(
            self.command,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        if not (self.proc and self.proc.stdout):
            return
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))

    def stop(self) -> bool:
        if not self.proc:
            return True
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        return True

    def tail(self, n: int = 60) -> list[str]:
        return list(self.lines)[-n:]


class ThreadWatcher:
    """Runs the inbox watcher inside this process (see module docstring)."""

    def __init__(self, ks):
        self._ks = ks
        self._watcher = None
        self._thread: threading.Thread | None = None
        self.lines: deque[str] = deque(maxlen=_MAX_LINES)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        from ..ingestion.watcher import InboxWatcher

        self.lines.clear()
        self.lines.append(f"Watching {self._ks.settings.inbox_path} ...")

        def on_event(outcome):
            detail = outcome.document_id or outcome.reason or outcome.error or ""
            self.lines.append(f"[{outcome.status}] {outcome.source_path} -> {detail}")

        self._watcher = InboxWatcher(self._ks, on_event=on_event)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._watcher.run()
        except Exception as exc:  # noqa: BLE001 - surface in the UI log
            self.lines.append(f"[error] watcher stopped: {exc}")

    def stop(self, timeout: float = 15.0) -> bool:
        """Signal the watcher and wait for the thread to exit.

        Returns True only if it actually stopped. The thread may be mid-ingestion
        (conversion + embedding of a large PDF), so we allow a generous wait and
        report honestly rather than claiming success.
        """
        if self._watcher:
            self._watcher.stop()
        thread = self._thread
        if thread:
            thread.join(timeout=timeout)
            if thread.is_alive():
                self.lines.append(
                    "Stop requested, but the watcher is still finishing a file."
                )
                return False
        self._thread = None
        self.lines.append("Watcher stopped.")
        return True

    def tail(self, n: int = 60) -> list[str]:
        return list(self.lines)[-n:]


class ThreadMcpServer:
    """Serves MCP over HTTP from inside this process, on its own port."""

    def __init__(self, ks, port: int, read_only: bool = True):
        self.ks = ks
        self.port = port
        self.read_only = read_only
        self.lines: deque[str] = deque(maxlen=_MAX_LINES)
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        import uvicorn

        from ..mcp.server import _transport_security, build_server

        self.lines.clear()
        mode = "read-only" if self.read_only else "read-write"
        self.lines.append(
            f"Starting in-process MCP ({mode}) on http://127.0.0.1:{self.port}/mcp"
        )
        # Share the panel's KnowledgeSystem -- a second client would fail to
        # open the embedded Qdrant store.
        server = build_server(read_only=self.read_only, ks=self.ks)
        # Requests arrive through a tunnel, so the Host header is the public
        # domain. Without disabling DNS-rebinding protection the SDK rejects
        # them with HTTP 421 "Invalid Host header".
        sec = _transport_security(None, public=True)
        app = (
            server.streamable_http_app(transport_security=sec) if sec
            else server.streamable_http_app()
        )
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port,
            log_level="warning", access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.lines.append("Ready. Point the tunnel at this port.")

    def _run(self) -> None:
        import asyncio

        try:
            # uvicorn skips signal-handler installation off the main thread.
            asyncio.run(self._server.serve())
        except Exception as exc:  # noqa: BLE001 - surface in the UI log
            self.lines.append(f"[error] MCP server stopped: {exc}")

    def stop(self, timeout: float = 10.0) -> bool:
        if self._server is not None:
            self._server.should_exit = True
        thread = self._thread
        if thread:
            thread.join(timeout=timeout)
            if thread.is_alive():
                self.lines.append("Stop requested but the server is still closing.")
                return False
        self._thread = None
        self._server = None
        self.lines.append("MCP server stopped.")
        return True

    def tail(self, n: int = 60) -> list[str]:
        return list(self.lines)[-n:]


class ControlCenter:
    """Owns the managed watcher/MCP/tunnel and derived status for the panel."""

    def __init__(self, project_root: Path, vault_path: Path, ks=None):
        self.project_root = project_root
        self.vault_path = vault_path
        self.ks = ks
        self.watcher: ThreadWatcher | None = None
        self.mcp: ThreadMcpServer | None = None
        self.tunnel: ManagedProcess | None = None
        self.mcp_api_key: str | None = None
        # Set when a named/pinned tunnel supplies a known, stable hostname.
        self._static_hostname: str | None = None

    # ---- executables -----------------------------------------------------

    def _knowledge_cmd(self) -> list[str]:
        """Invoke our own CLI via the current interpreter for portability."""
        return [sys.executable, "-m", "knowledge.cli"]

    def _tool_path(self, name: str) -> str | None:
        """Prefer a binary bundled in ./tools, else fall back to PATH."""
        exe = f"{name}.exe" if os.name == "nt" else name
        local = self.project_root / "tools" / exe
        if local.exists():
            return str(local)
        return shutil.which(name)

    def ngrok_path(self) -> str | None:
        return self._tool_path("ngrok")

    @staticmethod
    def ngrok_authenticated() -> bool:
        """True if an ngrok authtoken has been saved.

        ngrok refuses to start without one, so detecting this up front lets the
        panel show actionable guidance instead of a cryptic agent error.
        """
        if os.environ.get("NGROK_AUTHTOKEN"):
            return True
        candidates: list[Path] = []
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            candidates.append(Path(local_app) / "ngrok" / "ngrok.yml")
        home = Path.home()
        candidates += [
            home / ".ngrok2" / "ngrok.yml",
            home / ".config" / "ngrok" / "ngrok.yml",
            home / "Library" / "Application Support" / "ngrok" / "ngrok.yml",
        ]
        for path in candidates:
            try:
                if path.exists() and "authtoken" in path.read_text(
                    encoding="utf-8", errors="ignore"
                ):
                    return True
            except OSError:
                continue
        return False

    @property
    def tunnel_provider(self) -> str:
        return self.ks.settings.tunnel.provider if self.ks else "ngrok"

    def tunnel_agent_path(self) -> str | None:
        return self.ngrok_path()

    # ---- watcher ---------------------------------------------------------

    def start_watcher(self) -> dict:
        if self.watcher and self.watcher.running:
            return {"running": True, "already": True}
        if self.ks is None:
            return {"running": False, "error": "no KnowledgeSystem bound"}
        self.watcher = ThreadWatcher(self.ks)
        self.watcher.start()
        return {"running": self.watcher.running}

    def stop_watcher(self) -> dict:
        if self.watcher:
            return {"running": not self.watcher.stop()}
        return {"running": False}

    # ---- MCP server ------------------------------------------------------

    def start_mcp(
        self, port: int = 8765, read_only: bool = True, api_key: str | None = None
    ) -> dict:
        """Start the MCP HTTP server in-process on its own port.

        No API key by default: claude.ai cannot send static headers, so
        requiring one would block the connector. ``read_only`` is the practical
        protection.
        """
        if self.mcp and self.mcp.running:
            return {"running": True, "already": True, "port": port}
        if self.ks is None:
            return {"running": False, "error": "no KnowledgeSystem bound"}
        self.mcp_api_key = None
        try:
            self.mcp = ThreadMcpServer(self.ks, port=port, read_only=read_only)
            self.mcp.start()
        except Exception as exc:  # noqa: BLE001
            return {"running": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"running": self.mcp.running, "port": port, "api_key": None}

    def stop_mcp(self) -> dict:
        if self.mcp:
            return {"running": not self.mcp.stop()}
        return {"running": False}

    # ---- tunnel ----------------------------------------------------------

    def start_tunnel(self, port: int = 8765) -> dict:
        """Start the ngrok tunnel in front of the MCP port.

        The free plan's assigned dev domain is stable across restarts, so the
        claude.ai connector URL only has to be set once.
        """
        provider = self.tunnel_provider
        exe = self.tunnel_agent_path()
        if not exe:
            return {
                "running": False,
                "error": (
                    "ngrok not found. Place ngrok.exe in ./tools/ or add it to PATH."
                ),
            }
        if self.tunnel and self.tunnel.running:
            return {"running": True, "already": True, "url": self.tunnel_url()}
        if not self.ngrok_authenticated():
            return {
                "running": False,
                "error": (
                    "ngrok has no authtoken. Sign up free at ngrok.com, copy "
                    "your token from the dashboard, then run: "
                    ".\\tools\\ngrok.exe config add-authtoken <TOKEN>"
                ),
            }

        cfg = self.ks.settings.tunnel if self.ks else None
        domain = cfg.ngrok_domain if cfg else None
        # Pinning the domain means the URL is known before ngrok finishes
        # booting; otherwise it is scraped from the log.
        self._static_hostname = domain
        # logfmt on stdout so the public URL can be parsed reliably.
        cmd = [exe, "http", str(port), "--log", "stdout",
               "--log-format", "logfmt"]
        if domain:
            cmd += ["--url", domain]

        self.tunnel = ManagedProcess("tunnel", cmd)
        self.tunnel.start(cwd=self.project_root)
        return {"running": self.tunnel.running, "named": bool(domain),
                "provider": provider}

    def stop_tunnel(self) -> dict:
        if self.tunnel:
            self.tunnel.stop()
        return {"running": False}

    def tunnel_url(self) -> str | None:
        """Public base URL of the tunnel.

        A pinned domain is known up front; otherwise it is scraped from ngrok's
        output, newest-first so a reconnect's URL wins.
        """
        if not self.tunnel:
            return None
        if self._static_hostname:
            host = self._static_hostname.replace("https://", "").rstrip("/")
            return f"https://{host}"
        for line in reversed(self.tunnel.tail(_MAX_LINES)):
            m = _TUNNEL_URL_RE.search(line)
            if m:
                return m.group(0)
        return None

    # ---- status ----------------------------------------------------------

    @staticmethod
    def _proc_status(p) -> dict:
        """Status for a ManagedProcess or an in-process Thread* helper."""
        return {
            "running": bool(p and p.running),
            "log": p.tail() if p else [],
        }

    def _stable_tunnel(self) -> bool:
        """Whether the public hostname persists across tunnel restarts.

        ngrok free assigns one persistent dev domain per account, so the
        hostname is stable whether or not it is pinned explicitly.
        """
        return bool(self.ks)

    def _configured_hostname(self) -> str | None:
        return self.ks.settings.tunnel.ngrok_domain if self.ks else None

    @property
    def single_process_store(self) -> bool:
        """True when the vector store is limited to a single client.

        Embedded Qdrant locks its storage folder. The panel handles this by
        running the watcher and MCP server in-process, so this is informational:
        it warns against launching a second `knowledge` command (e.g.
        `knowledge watch` in a terminal) while the panel is running.
        """
        if self.ks is None:
            return False
        return self.ks.settings.vector.provider == "qdrant_local"

    def status(self) -> dict:
        url = self.tunnel_url()
        return {
            "watcher": self._proc_status(self.watcher),
            "tunnel": {
                **self._proc_status(self.tunnel),
                "url": url,
                "mcp_url": f"{url}/mcp" if url else None,
                "available": bool(self.tunnel_agent_path()),
                "provider": self.tunnel_provider,
                "stable": self._stable_tunnel(),
                "configured_hostname": self._configured_hostname(),
            },
            "mcp": {
                **self._proc_status(self.mcp),
                "api_key": self.mcp_api_key,
                "single_process_store": self.single_process_store,
            },
        }

    def shutdown(self) -> None:
        for p in (self.watcher, self.mcp, self.tunnel):
            if p:
                try:
                    p.stop()
                except Exception:  # noqa: BLE001 - best effort on exit
                    pass


def generate_key(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
