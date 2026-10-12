"""Configuration loading and validation.

Layering (lowest to highest precedence):
    1. Hard-coded defaults in the Pydantic models below.
    2. config/default.yaml (shipped with the project).
    3. A user config file (``--config`` / ``KS_CONFIG_FILE``).
    4. Environment variables (prefix ``KS_``, nested with ``__``).

The result is a single immutable :class:`Settings` object that the rest of
the system depends on. No module reads environment variables directly.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# --------------------------------------------------------------------------
# Sub-models
# --------------------------------------------------------------------------


class VaultLayout(BaseModel):
    inbox: str = "00-Inbox"
    content_dir: str = "Notes"          # flat content folder for processed notes
    sources: str = "90-Indexes/Sources"
    templates: str = "90-Indexes/Templates"
    # Optional fixed domain labels for the (optional) Jev classifier. Empty
    # means organization is fully emergent from frontmatter/tags/links.
    domains: list[str] = Field(default_factory=list)


class IngestConfig(BaseModel):
    inbox_dir: str = "00-Inbox"
    auto_git_commit: bool = False
    move_processed_sources: bool = True
    processed_marker: str = ".processed"
    watch_debounce_seconds: float = 2.0
    supported_extensions: list[str] = Field(
        default_factory=lambda: [
            ".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".html", ".htm",
            ".csv", ".txt", ".md", ".json", ".xml", ".png", ".jpg", ".jpeg",
        ]
    )
    markdown_lint: bool = True


class ConverterConfig(BaseModel):
    provider: Literal["markitdown", "auto"] = "auto"


class AnalyticalConfig(BaseModel):
    """When a structured file looks like a *model/dataset* rather than
    reference knowledge, it is staged instead of indexed.

    The thresholds are deliberately cheap and deterministic -- no LLM is used
    to decide. Any single trigger is enough to classify a file as analytical.
    """

    require_optin: bool = True          # never auto-index analytical files
    staging_dir: str = "90-Indexes/Analytical"
    max_bytes: int = 2_000_000          # >2 MB spreadsheet/dataset
    max_rows: int = 5_000               # CSV/TSV row count
    max_sheets: int = 5                 # workbook tab count
    max_formula_cells: int = 200        # formula density => a model, not data
    scan_cell_limit: int = 20_000       # bound the probe for large workbooks


class StructuredConfig(BaseModel):
    """How tabular/record data is turned into knowledge.

    Records are rendered as ``key: value`` blocks and grouped into chunks --
    embedding raw CSV rows produces poor vectors, because the column context is
    lost and numbers dominate the token space.
    """

    max_rows_indexed: int = 2_000     # rows materialised per table
    records_per_chunk: int = 20        # records grouped into one vector chunk
    sample_rows: int = 10              # rows shown in the Markdown note
    max_entity_distinct: int = 50      # <= this many distinct values => entities
    max_entity_value_len: int = 60     # skip long free-text as entities


class ClassificationConfig(BaseModel):
    """Routes each upload to one of three ingestion tracks.

    document   -> MarkItDown -> Markdown -> chunks -> embeddings -> graph
    structured -> structured parser -> schema/records/entities -> graph+vector
    analytical -> staged for temporary analysis; enters the KB only on an
                  explicit "Add to Knowledge Base" action
    """

    enabled: bool = True
    document_extensions: list[str] = Field(
        default_factory=lambda: [
            ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".html", ".htm",
            ".txt", ".md", ".markdown", ".rtf", ".epub",
            ".msg", ".eml",                      # emails
            ".png", ".jpg", ".jpeg",             # images MarkItDown can read
        ]
    )
    structured_extensions: list[str] = Field(
        default_factory=lambda: [
            ".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".json", ".xml",
        ]
    )
    analytical: AnalyticalConfig = Field(default_factory=AnalyticalConfig)


class ChunkingConfig(BaseModel):
    max_tokens: int = 512
    min_tokens: int = 64
    overlap_tokens: int = 32
    chars_per_token: int = 4
    respect_headings: bool = True


class EmbeddingConfig(BaseModel):
    provider: Literal["sentence_transformers", "openai", "hash"] = "hash"
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    dim: int = 384
    base_url: str | None = None
    api_key: str | None = None
    batch_size: int = 32


class VectorConfig(BaseModel):
    # qdrant       -> a Qdrant server at `url`
    # qdrant_local -> embedded Qdrant persisted on disk (no server needed)
    # memory       -> dependency-free fallback
    provider: Literal["qdrant", "qdrant_local", "memory"] = "memory"
    url: str = "http://localhost:6333"
    collection: str = "knowledge"
    api_key: str | None = None
    distance: Literal["cosine", "dot", "euclid"] = "cosine"


class GraphConfig(BaseModel):
    provider: Literal["sqlite", "memory"] = "sqlite"


class LLMSelectiveConfig(BaseModel):
    important_domains: list[str] = Field(default_factory=list)
    min_document_chars: int = 800
    classification_confidence_threshold: float = 0.6


class LLMConfig(BaseModel):
    """Optional reasoning/enrichment LLM.

    ``openai`` means the OpenAI *wire protocol*, not the vendor: it covers
    Ollama, LM Studio, vLLM and OpenAI itself. The default is ``null`` so the
    system consumes no tokens and needs no model until you opt in.
    """

    provider: Literal["openai", "null"] = "null"
    base_url: str = "http://localhost:11434/v1"
    api_key: str | None = None
    model: str = "llama3.2:3b"
    enrichment_mode: Literal["off", "on", "selective"] = "selective"
    max_tokens: int = 1024
    temperature: float = 0.1
    # Local CPU inference is slow; a whole-document brief can take a while on
    # the first call because the model has to be loaded into RAM.
    timeout_seconds: float = 180.0
    # Ollama-specific niceties, ignored by endpoints that don't understand them.
    # Without keep_alive the model is evicted after ~5 minutes, so every note in
    # a slow ingest run pays the load cost again.
    keep_alive: str | None = "30m"
    num_ctx: int | None = None
    # Probe the endpoint before enriching, so a stopped Ollama degrades to
    # "no LLM available" instead of failing once per document.
    health_check: bool = True
    health_cache_seconds: float = 60.0
    selective: LLMSelectiveConfig = Field(default_factory=LLMSelectiveConfig)


class JevConfig(BaseModel):
    provider: str = "stub"
    enabled: bool = False


class APIConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


# --------------------------------------------------------------------------
# Root settings
# --------------------------------------------------------------------------


# Holds the merged YAML defaults for the active load. Populated by
# load_settings() immediately before constructing Settings so the custom
# YAML source can inject it at the correct (lowest) precedence.
_YAML_DATA: dict[str, Any] = {}


class _YamlConfigSource(PydanticBaseSettingsSource):
    """Feeds the merged YAML config as a settings source ranked *below*
    environment variables, so env vars always win over YAML."""

    def __init__(self, settings_cls, data: dict[str, Any]):
        super().__init__(settings_cls)
        self._data = data

    def get_field_value(self, field, field_name):  # pragma: no cover - iface
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


class Settings(BaseSettings):
    """Root configuration object."""

    model_config = SettingsConfigDict(
        env_prefix="KS_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Precedence (highest first): init kwargs > env > .env > YAML defaults.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlConfigSource(settings_cls, dict(_YAML_DATA)),
            file_secret_settings,
        )

    vault_path: Path = Path("./knowledge-vault")
    # state_dir defaults to <vault_path>/.knowledge. It is intentionally NOT a
    # fixed independent path: co-locating derived state with the vault prevents
    # "split-brain" (notes written to one vault, index tracked elsewhere) when
    # only vault_path is overridden or the process is launched from another cwd.
    state_dir: Path | None = None

    @model_validator(mode="after")
    def _derive_state_dir(self) -> Settings:
        if self.state_dir is None:
            self.state_dir = self.vault_path / ".knowledge"
        return self

    vault_layout: VaultLayout = Field(default_factory=VaultLayout)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    classification: ClassificationConfig = Field(
        default_factory=ClassificationConfig
    )
    structured: StructuredConfig = Field(default_factory=StructuredConfig)
    converter: ConverterConfig = Field(default_factory=ConverterConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    jev: JevConfig = Field(default_factory=JevConfig)
    api: APIConfig = Field(default_factory=APIConfig)

    # ---- Derived convenience paths ----------------------------------------

    @property
    def inbox_path(self) -> Path:
        return self.vault_path / self.ingest.inbox_dir

    @property
    def content_path(self) -> Path:
        return self.vault_path / self.vault_layout.content_dir

    @property
    def sources_path(self) -> Path:
        return self.vault_path / self.vault_layout.sources

    @property
    def analytical_path(self) -> Path:
        """Where analytical files are staged (not part of the knowledge base
        until explicitly promoted)."""
        return self.vault_path / self.classification.analytical.staging_dir

    @property
    def templates_path(self) -> Path:
        return self.vault_path / self.vault_layout.templates

    @property
    def manifests_dir(self) -> Path:
        return self.state_dir / "manifests"

    @property
    def state_db_path(self) -> Path:
        return self.state_dir / "state" / "state.sqlite"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    def ensure_dirs(self) -> None:
        """Create the state directories required for operation."""
        for p in (
            self.state_dir,
            self.manifests_dir,
            self.state_db_path.parent,
            self.logs_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must contain a mapping at the top level")
    return data


def _default_config_path() -> Path:
    # config/default.yaml relative to the project root (two levels up from here)
    return Path(__file__).resolve().parents[2] / "config" / "default.yaml"


def load_settings(config_file: str | os.PathLike[str] | None = None) -> Settings:
    """Load :class:`Settings` from YAML + user config + environment.

    Pydantic's BaseSettings applies environment variables automatically; we
    feed the merged YAML dict as the initialisation kwargs so env vars still
    win (they are read during ``Settings(**data)`` construction).
    """
    global _YAML_DATA
    data = _load_yaml(_default_config_path())

    user_cfg = config_file or os.environ.get("KS_CONFIG_FILE")
    if user_cfg:
        data = _deep_merge(data, _load_yaml(Path(user_cfg)))

    _YAML_DATA = data
    try:
        return Settings()
    finally:
        _YAML_DATA = {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor used across the app (call ``get_settings.cache_clear()``
    in tests to reset)."""
    return load_settings()
