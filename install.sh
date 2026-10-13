#!/usr/bin/env bash
# One-shot install of the Knowledge System on macOS, for the current user only
# (no sudo). Safe to re-run.
#   Usage: bash install.sh [--apps=cursor,claude-desktop,kiro] [--no-obsidian] [--no-llm]
#   Without --apps, it asks which MCP apps to connect.
#
#   * uv + Python 3.12   into ./.tools/ (nothing global; delete it to remove)
#   * .venv + all Python dependencies: document conversion (MarkItDown),
#     spreadsheets, local embedding model, embedded Qdrant vector DB,
#     Control Panel, MCP server, dev tools
#   * the embedding model (~90 MB, downloaded once; later starts are offline)
#   * Obsidian           (optional UI)        into ~/Applications
#   * Ollama + a model   (optional local LLM)  into ~/Applications
#
# Set LLM_MODEL to pull a different model (default llama3.2:3b, ~2 GB).
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

NO_OBSIDIAN=0
NO_LLM=0
MCP_APPS=""
MCP_APPS_SET=0
for arg in "$@"; do
  case "$arg" in
    --no-obsidian) NO_OBSIDIAN=1 ;;
    --no-llm) NO_LLM=1 ;;
    --apps=*) MCP_APPS="${arg#--apps=}"; MCP_APPS_SET=1 ;;
    *) echo "Unknown option: $arg"; exit 2 ;;
  esac
done

# Ask up front so the rest of the install can run unattended.
if [ "$MCP_APPS_SET" = 0 ] && [ -t 0 ]; then
  printf '\n\033[36mWhich AI apps should use your knowledge base?\033[0m\n'
  echo "  claude-desktop, cursor, kiro (comma-separated), or press Enter to skip"
  read -r -p "Apps: " MCP_APPS || MCP_APPS=""
fi
MCP_APPS="$(printf '%s' "$MCP_APPS" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
[ "$MCP_APPS" = none ] && MCP_APPS=""
LLM_MODEL="${LLM_MODEL:-llama3.2:3b}"
UV_VERSION="0.13.0"
PYTHON_VERSION="3.12"
TOOLS="$ROOT/.tools"
DL="$(mktemp -d)"
APPS="$HOME/Applications"
mkdir -p "$TOOLS" "$APPS"
trap 'rm -rf "$DL"' EXIT

SUMMARY=""
note() { SUMMARY="${SUMMARY}$(printf '  %-24s %s' "$1" "$2")"$'\n'; }
step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
warn() { printf '    \033[33m%s\033[0m\n' "$1"; }
fetch() { echo "    downloading $1"; curl -fL --retry 3 -o "$2" "$1"; }
sha256_ok() { [ "$(shasum -a 256 "$1" | cut -d' ' -f1)" = "$2" ]; }

# These functions run as `if` conditions, where bash ignores `set -e`, so every
# step checks its own result and returns 1 on failure.

# Copy the .app out of a .dmg into ~/Applications and check its signature.
install_dmg_app() {  # <dmg> <App name>
  local mnt="$DL/mnt-$2"
  mkdir -p "$mnt" || return 1
  hdiutil attach "$1" -nobrowse -quiet -mountpoint "$mnt" || return 1
  rm -rf "$APPS/$2.app"
  if ! cp -R "$mnt/$2.app" "$APPS/"; then
    hdiutil detach "$mnt" -quiet; return 1
  fi
  hdiutil detach "$mnt" -quiet
  if ! codesign --verify --deep --strict "$APPS/$2.app"; then
    warn "$2.app failed its signature check; removed."
    rm -rf "$APPS/$2.app"; return 1
  fi
}

install_obsidian() {
  local url digest
  read -r url digest < <("$PY" - <<'PYEOF'
import json, re, urllib.request
rel = json.load(urllib.request.urlopen(
    "https://api.github.com/repos/obsidianmd/obsidian-releases/releases/latest"))
a = next(a for a in rel["assets"] if re.fullmatch(r"Obsidian-[\d.]+\.dmg", a["name"]))
print(a["browser_download_url"], (a.get("digest") or "sha256:").split(":", 1)[1] or "-")
PYEOF
  ) || return 1
  [ -n "$url" ] || return 1
  fetch "$url" "$DL/Obsidian.dmg" || return 1
  if [ "$digest" != "-" ] && ! sha256_ok "$DL/Obsidian.dmg" "$digest"; then
    warn "Checksum mismatch for Obsidian.dmg"; return 1
  fi
  install_dmg_app "$DL/Obsidian.dmg" "Obsidian"
}

# --------------------------------------------------------------------------
# 1. uv (Python + package manager), kept inside .tools/
# --------------------------------------------------------------------------
UV="$(find "$TOOLS/uv" -name uv -type f 2>/dev/null | head -n1 || true)"
if [ -z "$UV" ]; then
  step "Installing uv $UV_VERSION (into .tools/uv)"
  case "$(uname -m)" in arm64|aarch64) ARCH=aarch64 ;; *) ARCH=x86_64 ;; esac
  ASSET="uv-$ARCH-apple-darwin.tar.gz"
  BASE="https://github.com/astral-sh/uv/releases/download/$UV_VERSION"
  fetch "$BASE/$ASSET" "$DL/$ASSET"
  EXPECTED="$(curl -fsSL "$BASE/$ASSET.sha256" | cut -d' ' -f1)"
  sha256_ok "$DL/$ASSET" "$EXPECTED" || { echo "Checksum mismatch for $ASSET"; exit 1; }
  mkdir -p "$TOOLS/uv"
  tar -xzf "$DL/$ASSET" -C "$TOOLS/uv"
  UV="$(find "$TOOLS/uv" -name uv -type f | head -n1)"
fi
note "uv" "ok"
export UV_PYTHON_INSTALL_DIR="$TOOLS/python"

# --------------------------------------------------------------------------
# 2. Python + virtual environment + all Python dependencies
# --------------------------------------------------------------------------
PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  step "Creating .venv with Python $PYTHON_VERSION (downloaded by uv if needed)"
  "$UV" venv .venv --python "$PYTHON_VERSION" --seed
fi
step "Installing the Knowledge System and all Python dependencies"
echo "    (first run downloads ~1 GB, mostly PyTorch for the embedding model)"
"$UV" pip install --python "$PY" -e ".[all,dev]"
note "Python + dependencies" "ok"

if [ ! -f .env ]; then
  step "Creating .env from .env.example"
  cp .env.example .env
fi
step "Creating the vault (./knowledge-vault)"
.venv/bin/knowledge init

# --------------------------------------------------------------------------
# 3. Obsidian (optional UI)
# --------------------------------------------------------------------------
if [ "$NO_OBSIDIAN" = 1 ]; then
  note "Obsidian" "skipped (--no-obsidian)"
elif [ -d "$APPS/Obsidian.app" ] || [ -d "/Applications/Obsidian.app" ]; then
  note "Obsidian" "already installed"
else
  step "Installing Obsidian (into ~/Applications)"
  if install_obsidian; then
    note "Obsidian" "installed"
  else
    warn "Obsidian install failed. It's optional: https://obsidian.md/download"
    note "Obsidian" "FAILED (optional)"
  fi
fi

# --------------------------------------------------------------------------
# 4. Ollama + model (optional local LLM for enrichment)
# --------------------------------------------------------------------------
ollama_up() { curl -fs --max-time 2 http://127.0.0.1:11434/api/version >/dev/null; }
find_ollama() {
  command -v ollama 2>/dev/null && return 0
  for p in "$APPS/Ollama.app/Contents/Resources/ollama" "/Applications/Ollama.app/Contents/Resources/ollama"; do
    [ -x "$p" ] && { echo "$p"; return 0; }
  done
  return 1
}

setup_ollama() {
  local ollama
  if ! ollama="$(find_ollama)"; then
    step "Installing Ollama (into ~/Applications)"
    fetch "https://ollama.com/download/Ollama.dmg" "$DL/Ollama.dmg" || return 1
    install_dmg_app "$DL/Ollama.dmg" "Ollama" || return 1
    ollama="$APPS/Ollama.app/Contents/Resources/ollama"
  fi
  if ! ollama_up; then
    open -g -a "Ollama" 2>/dev/null || ("$ollama" serve >/dev/null 2>&1 &)
    for _ in $(seq 60); do ollama_up && break; sleep 1; done
    ollama_up || { warn "The Ollama server didn't start"; return 1; }
  fi
  step "Pulling LLM model $LLM_MODEL (one time)"
  "$ollama" pull "$LLM_MODEL"
}

if [ "$NO_LLM" = 1 ]; then
  note "Ollama + model" "skipped (--no-llm)"
elif setup_ollama; then
  # Turn enrichment on, unless .env already points at another LLM.
  if grep -qE '^\s*KS_LLM__PROVIDER=(null)?\s*$' .env || ! grep -q '^\s*KS_LLM__PROVIDER=' .env; then
    "$PY" - "$LLM_MODEL" <<'PYEOF'
import re, sys
from pathlib import Path
env = Path(".env")
text = env.read_text(encoding="utf-8")
for key, value in (("KS_LLM__PROVIDER", "openai"),
                   ("KS_LLM__BASE_URL", "http://localhost:11434/v1"),
                   ("KS_LLM__MODEL", sys.argv[1])):
    line = f"{key}={value}"
    text, n = re.subn(rf"(?m)^\s*{key}=.*$", line, text)
    if not n:
        text += f"\n{line}\n"
env.write_text(text, encoding="utf-8")
PYEOF
  fi
  note "Ollama + model" "ok ($LLM_MODEL)"
else
  warn "Local LLM setup failed. It's optional: https://ollama.com/download"
  note "Ollama + model" "FAILED (optional)"
fi

# --------------------------------------------------------------------------
# 5. Load every provider once (downloads the embedding model the first time)
# --------------------------------------------------------------------------
step "Checking the install (loads the embedding model, vector DB and LLM)"
if .venv/bin/knowledge health; then HEALTH=ok; else HEALTH=FAILED; fi
note "Health check" "$HEALTH"

# --------------------------------------------------------------------------
# 6. Connect MCP apps (Claude Desktop, Cursor, Kiro)
# --------------------------------------------------------------------------
if [ -z "$MCP_APPS" ]; then
  note "MCP apps" "none (later: knowledge connect <app>)"
else
  step "Connecting MCP apps: $MCP_APPS"
  if .venv/bin/knowledge connect "$MCP_APPS"; then
    note "MCP apps" "connected ($MCP_APPS)"
  else
    note "MCP apps" "FAILED"
  fi
fi

printf '\n\033[36mSummary\033[0m\n%s\n' "$SUMMARY"
if [ "$HEALTH" != ok ]; then
  echo "The health check failed; see the output above. Re-run: bash install.sh"
  exit 1
fi
printf '\033[32mInstall complete.\033[0m Start the Control Panel with:  bash start.sh\n'
