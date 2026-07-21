"""Thin, testable wrapper that provisions the full repository authority set.

``task_store.initialize_repository`` already creates/repairs
``.aiworkhub/project.json`` (via ``repository_state.bootstrap_repository``,
which also creates the ``sessions``/``memory``/``kb``/``config``/``tasking``
durable directories), ``.aiworkhub/config/storage.json``, and a fresh,
schema-only canonical ``.aiworkhub/tasking/task_queue.sqlite``. This module
adds exactly one more authority to that same bounded, idempotent call: the
Source Graph database directory (``source_graph.resolve_db_path`` mkdirs it),
and reports which authorities were created/verified this call so a caller
(``dashboard_mcp_app.initialize_view``) can show a single, honest
provisioning result instead of two separate bootstrap calls.

This module does not reinvent bootstrap: it never writes a manifest, a
storage registry record, or a task_queue schema itself -- it only sequences
``task_store.initialize_repository`` then ``source_graph.resolve_db_path``
and merges their results. It fails closed by re-raising ``task_store``'s own
exceptions (``InitializationRefusedError``, ``TaskStoreError``) unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import source_graph, task_store


def canonicalize_workspace_root(root: str | Path) -> Path:
    """Canonicalize an arbitrary workspace root path.

    Uses only ``Path.resolve()`` (stdlib): no host-specific hardcoded path,
    so this works uniformly for a Windows drive letter, a WSL ``/mnt/c``
    path, a relative path, or a path through a symlink -- resolve() expands
    the symlink and normalizes case/separators per the host platform's own
    rules. This function never touches the filesystem beyond what
    ``resolve()`` itself does (it does not require the path to exist).
    """
    return Path(root).expanduser().resolve()


def initialize_repository_full(
    root: str | Path,
    *,
    expected_repo_id: str | None = None,
) -> dict[str, Any]:
    """Provision task_store's authority set PLUS the Source Graph directory.

    Idempotent and non-destructive: calling this twice against the same
    repository is safe -- the second call reports ``created_canonical_db:
    False`` (from ``task_store.initialize_repository``, unchanged) and
    ``source_graph_ready: True`` with an empty/short ``provisioned`` list for
    whatever was already present.

    ``expected_repo_id``, when given, is forwarded unchanged to
    ``task_store.initialize_repository`` -- an existing manifest with a
    different repo_id refuses the call exactly as that function already
    does; this wrapper adds no separate identity check of its own.

    Re-raises ``task_store.InitializationRefusedError`` /
    ``task_store.TaskStoreError`` unchanged -- this function never swallows a
    fail-closed initialization refusal.
    """
    repo_root = canonicalize_workspace_root(root)

    # task_store.initialize_repository already creates/repairs:
    #   - .aiworkhub/project.json (manifest) + the DURABLE_LAYOUT dirs
    #     (tasking, sessions, memory, kb, config) via bootstrap_repository
    #   - .aiworkhub/config/storage.json (storage registry)
    #   - .aiworkhub/tasking/task_queue.sqlite (canonical task queue, schema-only)
    result = task_store.initialize_repository(repo_root, expected_repo_id=expected_repo_id)

    provisioned: list[str] = []
    if result.get("created_manifest") or result.get("activated_canonical_authority"):
        provisioned.append("manifest")
    provisioned.append("storage_registry")
    if result.get("created_canonical_db"):
        provisioned.append("task_queue_db")
    else:
        provisioned.append("task_queue_db_verified")

    # The one authority this wrapper adds beyond task_store's own call:
    # ensure the Source Graph database directory exists. resolve_db_path
    # mkdirs the parent directory as a side effect and is itself idempotent
    # (a second call is a no-op mkdir with exist_ok=True).
    source_graph_ready = False
    try:
        db_path = source_graph.resolve_db_path(repo_root)
        source_graph_ready = db_path.parent.is_dir()
        if source_graph_ready:
            provisioned.append("source_graph_dir")
    except source_graph.SourceGraphError:
        source_graph_ready = False

    return {
        **result,
        "source_graph_ready": source_graph_ready,
        "provisioned": provisioned,
        "canonicalized_root": str(repo_root),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


__all__ = [
    "canonicalize_workspace_root",
    "initialize_repository_full",
]
