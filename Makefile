# Convenience targets (macOS/Linux; on Windows use scripts/setup.ps1).
# Usage: make setup | make install-all | make test | make lint | make init

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: help setup install install-all test lint fmt init clean

help:
	@echo "setup        create venv + install core & dev deps + scaffold vault"
	@echo "install      install core + dev into current environment"
	@echo "install-all  install ALL extras (markitdown, embeddings, qdrant, mcp, api) + dev"
	@echo "test         run the test suite"
	@echo "lint         run ruff checks"
	@echo "fmt          auto-fix lint issues"
	@echo "init         scaffold the knowledge vault"
	@echo "clean        remove venv and caches"

setup:
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e ".[dev]"
	[ -f .env ] || cp .env.example .env
	$(BIN)/knowledge init

install:
	$(BIN)/python -m pip install -e ".[dev]"

install-all:
	$(BIN)/python -m pip install -e ".[all,dev]"

test:
	$(BIN)/python -m pytest -q

lint:
	$(BIN)/ruff check src tests

fmt:
	$(BIN)/ruff check src tests --fix

init:
	$(BIN)/knowledge init

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache **/__pycache__
