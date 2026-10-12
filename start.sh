#!/usr/bin/env bash
# Start the Control Panel (upload files, search, watcher) at http://127.0.0.1:8000
# Stop it with Ctrl+C.   Usage: bash start.sh
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/knowledge" ]; then
  echo "Not installed yet. Run: bash install.sh"
  exit 1
fi
exec .venv/bin/knowledge panel "$@"
