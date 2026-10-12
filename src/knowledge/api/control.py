"""Background process/thread control for the local Control Panel.

Manages the two long-running helpers the panel can start and stop:

  * the inbox **watcher**      -> in-process thread
  * the **MCP HTTP server**    -> in-process thread, on its own port (127.0.0.1)

Why they are in-process: the embedded Qdrant store
(``vector.provider=qdrant_local``) locks its storage folder to a single client.
Spawning ``knowledge watch`` or ``knowledge mcp`` as separate processes makes
them fail to open the index -- the watcher silently indexes nothing and every
MCP tool call returns "Error executing tool". Sharing the panel's already-open
:class:`KnowledgeSystem` removes the conflict entirely.

This module is local-only by design: it launches things on the machine running
the API, so the API must never be exposed to a network.
"""

from __future__ import annotations

import sys
import threading
from collections import deque
from pathlib import Path

_MAX_LINES = 400


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
        # Keep the SDK's Host/Origin (DNS-rebinding) checks on: the server has
        # no auth, so a web page must not be able to reach it via a rebound
        # hostname. Local clients send Host: 127.0.0.1 and are unaffected.
        sec = _transport_security(None, public=False)
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
        self.lines.append(
            f"Ready: http://127.0.0.1:{self.port}/mcp (this machine only)."
        )

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
    """Owns the managed watcher/MCP server and derived status for the panel."""

    def __init__(self, project_root: Path, vault_path: Path, ks=None):
        self.project_root = project_root
        self.vault_path = vault_path
        self.ks = ks
        self.watcher: ThreadWatcher | None = None
        self.mcp: ThreadMcpServer | None = None

    # ---- executables -----------------------------------------------------

    def _knowledge_cmd(self) -> list[str]:
        """Invoke our own CLI via the current interpreter for portability."""
        return [sys.executable, "-m", "knowledge.cli"]

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

    def start_mcp(self, port: int = 8765, read_only: bool = True) -> dict:
        """Start the MCP HTTP server in-process on its own port.

        Binds 127.0.0.1 and has no authentication; ``read_only`` (default
        True) is the practical protection.
        """
        if self.mcp and self.mcp.running:
            return {"running": True, "already": True, "port": port}
        if self.ks is None:
            return {"running": False, "error": "no KnowledgeSystem bound"}
        try:
            self.mcp = ThreadMcpServer(self.ks, port=port, read_only=read_only)
            self.mcp.start()
        except Exception as exc:  # noqa: BLE001
            return {"running": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"running": self.mcp.running, "port": port}

    def stop_mcp(self) -> dict:
        if self.mcp:
            return {"running": not self.mcp.stop()}
        return {"running": False}

    # ---- status ----------------------------------------------------------

    @staticmethod
    def _proc_status(p) -> dict:
        """Status for an in-process Thread* helper."""
        return {
            "running": bool(p and p.running),
            "log": p.tail() if p else [],
        }

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
        running = bool(self.mcp and self.mcp.running)
        return {
            "watcher": self._proc_status(self.watcher),
            "mcp": {
                **self._proc_status(self.mcp),
                "single_process_store": self.single_process_store,
                "url": f"http://127.0.0.1:{self.mcp.port}/mcp" if running else None,
            },
        }

    def shutdown(self) -> None:
        for p in (self.watcher, self.mcp):
            if p:
                try:
                    p.stop()
                except Exception:  # noqa: BLE001 - best effort on exit
                    pass
