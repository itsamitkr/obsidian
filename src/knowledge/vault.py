"""Vault scaffolding for a flat, metadata-driven layout.

Organization is NOT folder-based. All processed notes live in a single
content folder; structure comes from frontmatter (domain/topic), tags and
[[wiki links]], surfaced through Obsidian's graph, tag pane and search.

The scaffolder creates the few structural folders (inbox, content, sources,
templates), the state dirs, optional Obsidian UI defaults, and a couple of
generic seed notes. It is idempotent -- safe to re-run.
"""

from __future__ import annotations

from pathlib import Path

from .config import Settings
from .markdown.frontmatter import dump_frontmatter
from .models import utcnow

_CONCEPT_TEMPLATE = """---
id: {{id}}
title: {{title}}
domain: {{domain}}
topic: {{topic}}
type: concept
status: active
source_type: manual
created_at: {{created_at}}
updated_at: {{updated_at}}
tags: []
related: []
---

# {{title}}

## Summary

## Details

## Related
"""


# Ignore rules for the vault's own git repository. The Markdown is the source
# of truth; everything listed here is derived, machine-specific, or per-user.
_VAULT_GITIGNORE = """\
# Derived, rebuildable state -- NOT the source of truth.
# Recreate with: knowledge rebuild-vector-index && knowledge rebuild-graph
.knowledge/

# Obsidian per-user workspace/cache (settings in .obsidian/*.json ARE tracked)
.obsidian/workspace*
.obsidian/cache
.obsidian/plugins/*/data.json
.trash/

# OS noise
.DS_Store
Thumbs.db
"""


_ANALYTICAL_README = """\
---
id: analytical-staging
title: Analytical staging
type: index
status: active
tags: [staging]
---

# Analytical staging

Files here were classified as **analytical** — spreadsheet models, financial
models, or large datasets. They are working material for analysis, not durable
reference knowledge, so they are deliberately **not** indexed:

- no Markdown conversion
- no chunking or embeddings
- no graph relationships

That keeps the vector index free of thousands of meaningless number-cells,
which would otherwise degrade search for everything else.

To index one anyway, promote it explicitly:

```bash
knowledge staged              # list what's waiting here
knowledge promote <filename>  # parse + index via the structured track
```

Or use **Add to Knowledge Base** in the Control Panel (`knowledge panel`).
"""


def _write_if_absent(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


# --------------------------------------------------------------------------
# Obsidian configuration (optional UI defaults). Written on init but never
# required -- removing Obsidian or this folder leaves the system fully
# functional. Per-user files (.obsidian/workspace*, cache) stay git-ignored.
# --------------------------------------------------------------------------


def _obsidian_files(settings: Settings) -> dict[str, str]:
    import json

    app = {
        "alwaysUpdateLinks": True,
        "newLinkFormat": "shortest",
        "useMarkdownLinks": False,
        # Route files dragged/pasted into Obsidian to the inbox so the watcher
        # auto-ingests them (Obsidian as a one-stop upload UI).
        "attachmentFolderPath": settings.ingest.inbox_dir,
        "newFileLocation": "folder",
        "newFileFolderPath": settings.vault_layout.content_dir,
        "showFrontmatter": True,
        "readableLineLength": True,
        "foldHeading": True,
        "foldIndent": True,
        "spellcheck": True,
        "promptDelete": False,
    }
    appearance = {"baseFontSize": 16, "theme": "obsidian", "showViewHeader": True}
    core_plugins = {
        "file-explorer": True, "global-search": True, "switcher": True,
        "graph": True, "backlink": True, "canvas": True, "outgoing-link": True,
        "tag-pane": True, "properties": True, "page-preview": True,
        "templates": True, "note-composer": True, "command-palette": True,
        "editor-status": True, "bookmarks": True, "outline": True,
        "word-count": True, "file-recovery": True, "daily-notes": False,
        "sync": False,
    }
    # Flat layout: colour the graph by metadata, not folders. These are
    # example tag groups; they simply do nothing until such tags exist.
    graph = {
        "collapse-filter": True, "search": "", "showTags": True,
        "showAttachments": False, "hideUnresolved": False, "showOrphans": True,
        "collapse-color-groups": False,
        "colorGroups": [
            {"query": "tag:#moc", "color": {"a": 1, "rgb": 16750848}},
            {"query": "path:00-Inbox", "color": {"a": 1, "rgb": 9342606}},
        ],
        "collapse-display": True, "showArrow": True, "textFadeMultiplier": -0.3,
        "nodeSizeMultiplier": 1.1, "lineSizeMultiplier": 1,
        "collapse-forces": True, "centerStrength": 0.518, "repelStrength": 12,
        "linkStrength": 1, "linkDistance": 220, "scale": 1, "close": False,
    }
    templates = {"folder": settings.vault_layout.templates}
    return {
        "app.json": json.dumps(app, indent=2),
        "appearance.json": json.dumps(appearance, indent=2),
        "core-plugins.json": json.dumps(core_plugins, indent=2),
        "community-plugins.json": "[]",
        "graph.json": json.dumps(graph, indent=2),
        "templates.json": json.dumps(templates, indent=2),
    }


def _write_obsidian_config(settings: Settings) -> list[str]:
    created: list[str] = []
    obs_dir = settings.vault_path / ".obsidian"
    for name, content in _obsidian_files(settings).items():
        if _write_if_absent(obs_dir / name, content + "\n"):
            created.append(str(obs_dir / name))
    # Home / Map-of-Content note for navigation (lives in the content folder).
    home = settings.content_path / "Home.md"
    if _write_if_absent(home, _home_note(settings)):
        created.append(str(home))
    return created


def _home_note(settings: Settings) -> str:
    now = utcnow().isoformat()
    fm = {
        "id": "home",
        "title": "Home",
        "type": "index",
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "tags": ["moc", "home"],
    }
    body = (
        "# Home\n\n"
        "Map of Content for the knowledge vault. This is a plain note -- open "
        "it in Obsidian or read it anywhere.\n\n"
        "## How this vault is organized\n\n"
        "There is **no fixed folder taxonomy**. Every note carries metadata in "
        "its frontmatter (`domain`, `topic`, `tags`, `related`) and links to "
        "other notes with `[[wiki links]]`. Browse by:\n\n"
        "- **Graph view** (left ribbon) -- see how notes connect.\n"
        "- **Tag pane** -- group notes by `#tag`.\n"
        "- **Search / quick switcher** (Ctrl+O) -- jump to any note.\n\n"
        "## Workflow\n\n"
        f"1. Drop files into `{settings.ingest.inbox_dir}/` (the watcher ingests them).\n"
        f"2. Converted notes land in `{settings.vault_layout.content_dir}/`, "
        "auto-chunked, embedded and linked.\n"
        "3. Add `tags:` / `topic:` / `[[links]]` to weave them into the graph.\n\n"
        "## Tips\n\n"
        "- Folders are just storage; **metadata is the organization**.\n"
        "- Use `related:` in frontmatter for explicit relationships.\n"
        "- Everything (search index, graph) rebuilds from these Markdown files.\n"
    )
    return dump_frontmatter(fm, body)


def init_vault(settings: Settings, seed: bool = True) -> list[str]:
    """Create the (flat) vault structure. Returns the list of created paths."""
    created: list[str] = []
    vault = settings.vault_path
    vault.mkdir(parents=True, exist_ok=True)

    # Structural folders: inbox (+ processed marker), content, sources, templates.
    inbox = settings.inbox_path
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / settings.ingest.processed_marker).mkdir(exist_ok=True)
    settings.content_path.mkdir(parents=True, exist_ok=True)
    settings.sources_path.mkdir(parents=True, exist_ok=True)
    settings.templates_path.mkdir(parents=True, exist_ok=True)
    # Staging area for analytical files (models/large datasets). Deliberately
    # NOT part of the knowledge base until explicitly promoted.
    settings.analytical_path.mkdir(parents=True, exist_ok=True)

    # .keep files so structural folders survive git while empty.
    for folder in (settings.content_path, settings.sources_path):
        keep = folder / ".keep"
        if _write_if_absent(keep, ""):
            created.append(str(keep))

    # Explain the staging area in place, so it isn't mistaken for content.
    readme = settings.analytical_path / "README.md"
    if _write_if_absent(readme, _ANALYTICAL_README):
        created.append(str(readme))

    # State dirs
    settings.ensure_dirs()

    # Templates
    tpl_path = settings.templates_path / "Concept.md"
    if _write_if_absent(tpl_path, _CONCEPT_TEMPLATE):
        created.append(str(tpl_path))

    # The vault is versioned as its own git repository, so it ships an ignore
    # file: derived state and per-user Obsidian cruft must never be committed.
    if _write_if_absent(vault / ".gitignore", _VAULT_GITIGNORE):
        created.append(str(vault / ".gitignore"))

    # Obsidian: note that the vault is a plain Markdown folder.
    obs_readme = vault / ".obsidian-README.md"
    if _write_if_absent(
        obs_readme,
        "# Opening this vault in Obsidian\n\n"
        "This directory is a plain Markdown vault. Open it in Obsidian via "
        "'Open folder as vault'. Obsidian is optional -- all functionality "
        "works without it. The Markdown files are the source of truth.\n",
    ):
        created.append(str(obs_readme))

    # Preconfigured (but optional) Obsidian UI defaults + a Home MOC.
    created.extend(_write_obsidian_config(settings))

    if seed:
        created.extend(_seed_notes(settings))

    return created


def _seed_notes(settings: Settings) -> list[str]:
    """A couple of generic, domain-agnostic notes that demonstrate the
    metadata-driven pattern (frontmatter + tags + [[links]]). They also give
    the graph something to show on first open."""
    created: list[str] = []
    now = utcnow().isoformat()

    welcome = {
        "id": "welcome",
        "title": "Welcome",
        "topic": "Getting Started",
        "type": "note",
        "status": "active",
        "source_type": "manual",
        "created_at": now,
        "updated_at": now,
        "tags": ["moc", "getting-started"],
        "related": ["[[Getting Started]]"],
    }
    welcome_body = (
        "# Welcome\n\n"
        "This is a flat, metadata-driven knowledge vault. Start with "
        "[[Getting Started]].\n\n"
        "Notes are organized by frontmatter (`topic`, `tags`, `related`) and "
        "`[[wiki links]]`, not by folders.\n"
    )
    p = settings.content_path / "welcome.md"
    if _write_if_absent(p, dump_frontmatter(welcome, welcome_body)):
        created.append(str(p))

    getting_started = {
        "id": "getting-started",
        "title": "Getting Started",
        "topic": "Getting Started",
        "type": "note",
        "status": "active",
        "source_type": "manual",
        "created_at": now,
        "updated_at": now,
        "tags": ["getting-started"],
        "related": ["[[Welcome]]"],
    }
    gs_body = (
        "# Getting Started\n\n"
        "Drop a document into the inbox and it becomes a note here. Link it to "
        "[[Welcome]] or any other note.\n\n"
        "## Next steps\n\n"
        "See [[Advanced Usage]] once it exists -- this deliberately dangling "
        "link demonstrates broken-link detection in the graph.\n"
    )
    p2 = settings.content_path / "getting-started.md"
    if _write_if_absent(p2, dump_frontmatter(getting_started, gs_body)):
        created.append(str(p2))

    return created
