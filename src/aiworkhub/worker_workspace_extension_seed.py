"""AIWorkHub extension repository-support seeding for the sparse worktree.

NF-2026-01011: the AIWorkHub VS Code extension suite (extension-static.test.js
and package checks) asserts directly on a few committed repository-root files.
``worker_workspace`` seeds exactly these as immutable support for any card that
touches the AIWorkHub extension package; unrelated repositories and packages
receive nothing extra.

The error type and the beneath-root guard are owned by ``worker_workspace``;
they are imported lazily to avoid an import cycle.
"""

from __future__ import annotations

import json
from pathlib import Path

# Repository-root files the AIWorkHub extension suite asserts on directly
# (extension-static.test.js and package checks).  Exact closure: unrelated
# repositories and packages receive nothing extra.
AIWORKHUB_EXTENSION_REPOSITORY_SUPPORT: tuple[str, ...] = (
    "README.md",
    "scripts/aiworkhub-app-server-mux",
    "scripts/aiworkhub-app-server-mux.cmd",
    # extension-static.test.js asserts these committed raster
    # fixtures exist at the repository root.
    "docs/assets/aiworkhub-block-diagram.png",
    "docs/assets/aiworkhub-source-graph-architecture.png",
    "docs/assets/demo/aiworkhub-task-review-loop.gif",
    "docs/assets/screenshots/aiworkhub-self-hosted-dashboard.png",
)


def aiworkhub_extension_repository_support(repo: Path) -> set[str]:
    """Return the committed repository-root files the extension suite needs.

    Refuses (fail closed) when any listed file is missing or a symlink so a
    sparse worktree can never silently run the extension gate without them.
    """
    from .worker_workspace import WorkspaceError

    rows: set[str] = set()
    for relative in AIWORKHUB_EXTENSION_REPOSITORY_SUPPORT:
        candidate = repo / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise WorkspaceError(f"validation_npm_support_missing:{relative}")
        rows.add(relative)
    return rows


def is_aiworkhub_extension_package(repo: Path) -> bool:
    """True when ``vscode-extension/package.json`` names the AIWorkHub package.

    An absent manifest is simply not the AIWorkHub extension; an unreadable or
    malformed one is refused rather than treated as "not ours".
    """
    from .worker_workspace import WorkspaceError, _require_beneath

    package_path = repo / "vscode-extension" / "package.json"
    if not package_path.exists() and not package_path.is_symlink():
        return False
    _require_beneath(repo, package_path)
    if package_path.is_symlink() or not package_path.is_file():
        raise WorkspaceError("validation_npm_manifest_invalid:vscode-extension")
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("validation_npm_manifest_invalid:vscode-extension") from exc
    return isinstance(package, dict) and package.get("name") == "aiworkhub"
