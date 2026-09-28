#!/usr/bin/env bash
# One-shot setup for the Knowledge System on macOS / Linux.
#
#   ./scripts/setup.sh                 # core + dev (runs on fallbacks)
#   EXTRAS=all ./scripts/setup.sh      # everything
#   EXTRAS=convert,embed,vector ./scripts/setup.sh
#
# 'dev' is always included.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY_BIN="${PYTHON:-python3}"

echo "==> Creating virtual environment (.venv)"
if [ ! -d ".venv" ]; then
  "$PY_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Upgrading pip"
python -m pip install --upgrade pip -q

GROUPS="dev"
if [ -n "${EXTRAS:-}" ]; then
  GROUPS="${GROUPS},${EXTRAS}"
fi
SPEC=".[${GROUPS}]"

echo "==> Installing package: -e ${SPEC}"
python -m pip install -e "${SPEC}"

echo "==> Preparing .env"
if [ -f ".env.example" ] && [ ! -f ".env" ]; then
  cp .env.example .env
fi

echo "==> Scaffolding the knowledge vault"
knowledge init

echo
echo "Setup complete."
echo "Activate with:  source .venv/bin/activate"
echo "Then try:       knowledge health"
