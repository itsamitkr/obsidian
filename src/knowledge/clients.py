"""Register the MCP server with desktop MCP clients (Claude Desktop, Cursor, Kiro).

Each client reads an ``mcpServers`` JSON map and starts the server itself over
stdio. ``connect`` merges one entry into that file (other servers and settings
are kept, and the previous file is saved as ``.bak``); ``disconnect`` removes it.

The entry uses absolute paths and passes the vault path and search providers as
environment variables, because a client doesn't start the server in the repo
folder, so the repo's ``.env`` and the relative ``./knowledge-vault`` default
wouldn't be found.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

SERVER_NAME = "knowledge"


@dataclass(frozen=True)
class Client:
    key: str
    label: str

    def config_path(self) -> Path:
        home = Path.home()
        if self.key == "claude-desktop":
            if sys.platform == "darwin":
                return home / "Library" / "Application Support" / "Claude" / \
                    "claude_desktop_config.json"
            appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
            return Path(appdata) / "Claude" / "claude_desktop_config.json"
        if self.key == "cursor":
            return home / ".cursor" / "mcp.json"
        if self.key == "kiro":
            return home / ".kiro" / "settings" / "mcp.json"
        raise ValueError(self.key)


CLIENTS: dict[str, Client] = {
    c.key: c for c in (
        Client("claude-desktop", "Claude Desktop"),
        Client("cursor", "Cursor"),
        Client("kiro", "Kiro"),
    )
}


def get_client(name: str) -> Client:
    key = name.strip().lower().replace("_", "-").replace(" ", "-")
    key = {"claude": "claude-desktop", "claudedesktop": "claude-desktop"}.get(key, key)
    if key not in CLIENTS:
        raise ValueError(
            f"Unknown app '{name}'. Choose from: {', '.join(CLIENTS)}"
        )
    return CLIENTS[key]


def _server_command() -> tuple[str, list[str]]:
    """The installed ``knowledge`` executable next to this interpreter."""
    scripts = Path(sys.executable).parent
    exe = scripts / ("knowledge.exe" if os.name == "nt" else "knowledge")
    if exe.exists():
        return str(exe), ["mcp"]
    return sys.executable, ["-m", "knowledge.cli", "mcp"]


def server_entry(settings: Settings, allow_writes: bool = False) -> dict:
    """The ``mcpServers`` entry for this install, with absolute paths."""
    command, args = _server_command()
    if allow_writes:
        args = [*args, "--allow-writes"]
    env = {
        "KS_VAULT_PATH": str(settings.vault_path.resolve()),
        # Must match what built the index, or search refuses to run.
        "KS_EMBEDDING__PROVIDER": settings.embedding.provider,
        "KS_EMBEDDING__MODEL": settings.embedding.model,
        "KS_EMBEDDING__DIM": str(settings.embedding.dim),
        "KS_VECTOR__PROVIDER": settings.vector.provider,
        "KS_VECTOR__COLLECTION": settings.vector.collection,
    }
    if settings.vector.provider == "qdrant":
        env["KS_VECTOR__URL"] = settings.vector.url
    return {"command": command, "args": args, "env": env}


def _load(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # Never overwrite a file we can't parse: it may hold the user's settings.
        raise ValueError(
            f"{path} is not valid JSON ({exc}); fix or remove it, then retry."
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def connect(
    client: Client, settings: Settings, allow_writes: bool = False,
    dry_run: bool = False,
) -> tuple[Path, dict]:
    """Add or update this server in the client's config. Returns (path, entry)."""
    path = client.config_path()
    entry = server_entry(settings, allow_writes)
    data = _load(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[SERVER_NAME] = entry
    data["mcpServers"] = servers
    if not dry_run:
        _save(path, data)
    return path, entry


def disconnect(client: Client) -> tuple[Path, bool]:
    """Remove this server from the client's config. Returns (path, removed)."""
    path = client.config_path()
    data = _load(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return path, False
    del servers[SERVER_NAME]
    _save(path, data)
    return path, True
