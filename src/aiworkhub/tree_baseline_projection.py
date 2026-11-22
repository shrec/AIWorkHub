"""Persisted-card form of a worktree's ``tree_baseline`` (NF-2026-02350).

``WorkerWorkspace.as_metadata()`` carries ``tree_baseline``: the complete
worktree manifest (24,279 entries / 3.6 MB measured on a Linux checkout).
That map is an authority only while an attempt is active, and it already
lives in the request's own on-disk metadata
(``<process_dir>/<request_id>.request.json``), which ``as_metadata`` writes and
this module never changes.  Copying it into ``terminal_review.evidence``,
``rework_predecessor`` and the ``terminal_review``/``accept_review`` event
payloads only grew the task store (4.96 GB) and every card read.

A card or task event therefore carries the stand-in
``{summarized: true, entry_count, sha256}`` -- the same canonical digest the
summary render (``core.summarize_card_baselines``) has always used, defined
once here -- and a reader that genuinely needs the full map resolves it from
the on-disk metadata and accepts it only when its digest equals the stand-in.
``workspace_baseline``/``parent_baseline`` are scoped to ``allowed_writes`` and
stay exact.  A legacy card with an inline map keeps working unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

TREE_BASELINE_KEY = "tree_baseline"
STAND_IN_KEYS = frozenset({"summarized", "entry_count", "sha256"})
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

# Where workspace metadata sits inside a stored card / task event payload.
CARD_WORKSPACE_PATHS: tuple[tuple[str, ...], ...] = (
    ("terminal_review", "evidence", "workspace"),
    ("rework_predecessor", "workspace"),
)
EVENT_WORKSPACE_PATHS: tuple[tuple[str, ...], ...] = (
    ("evidence", "workspace"),
    ("terminal_review", "evidence", "workspace"),
    ("rework_predecessor", "workspace"),
    ("workspace",),
)


def baseline_map_digest(mapping: Mapping[str, Any]) -> str:
    """Canonical sha256 of one baseline hash map."""

    canonical = json.dumps(mapping, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def baseline_stand_in(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """``mapping`` as its ``{summarized, entry_count, sha256}`` identity."""

    return {
        "summarized": True,
        "entry_count": len(mapping),
        "sha256": baseline_map_digest(mapping),
    }


def is_stand_in(value: Any) -> bool:
    """True only for an exact, well-formed baseline stand-in."""

    return (
        type(value) is dict
        and set(value) == STAND_IN_KEYS
        and value["summarized"] is True
        and type(value["entry_count"]) is int
        and value["entry_count"] >= 0
        and type(value["sha256"]) is str
        and _SHA256_HEX.fullmatch(value["sha256"]) is not None
    )


def _needs_fold(workspace: Any) -> bool:
    if not isinstance(workspace, dict):
        return False
    tree = workspace.get(TREE_BASELINE_KEY)
    return isinstance(tree, dict) and not is_stand_in(tree)


def project_workspace_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """``metadata`` in its persisted-card form; the input is never mutated."""

    projected = dict(metadata)
    if _needs_fold(projected):
        projected[TREE_BASELINE_KEY] = baseline_stand_in(projected[TREE_BASELINE_KEY])
    return projected


def card_workspace_metadata(workspace: Any) -> dict[str, Any]:
    """The ``workspace`` block a card or task event stores for ``workspace``."""

    return project_workspace_metadata(workspace.as_metadata())


def inline_tree_baseline(value: Any) -> dict[Any, Any]:
    """The inline map ``value`` carries; ``{}`` for a stand-in or no map.

    A stand-in is an identity, never a manifest: decoding it as path->digest
    rows would invent three bogus paths, so it decodes as "no tree manifest"
    and every fallback consumer keeps its existing ``baseline_missing`` refusal.
    """

    return {} if is_stand_in(value) else dict(value or {})


def _fold_at(container: Mapping[str, Any], path: Sequence[str]) -> dict[str, Any] | None:
    """A copy of ``container[path[0]]`` with the tree baseline under ``path`` folded."""

    head = container.get(path[0])
    if not isinstance(head, dict):
        return None
    if len(path) == 1:
        return project_workspace_metadata(head) if _needs_fold(head) else None
    inner = _fold_at(head, path[1:])
    return None if inner is None else {**head, path[1]: inner}


def fold_tree_baselines(doc: dict[str, Any], paths: Sequence[Sequence[str]]) -> bool:
    """Fold every inline ``tree_baseline`` found at ``paths`` of ``doc``.

    Copy-on-write: only ``doc``'s own top-level keys are reassigned, so a
    nested block shared with a caller is never mutated.  Every other key --
    including ``workspace_baseline``/``parent_baseline`` and the rest of the
    workspace block -- stays byte-exact.  Idempotent.
    """

    changed = False
    for path in paths:
        folded = _fold_at(doc, path)
        if folded is not None:
            doc[path[0]] = folded
            changed = True
    return changed


# The coordinator's process directory, as ``ProcessManager.process_dir`` derives
# it.  Restated, not imported: this module sits in worker_workspace's import
# closure, and importing process_launcher (even lazily) would seed its whole
# closure into every sparse validation workspace.  The NF-2026-02350 test pins
# both values to process_launcher's.
PROCESS_DIR_ENV = "AIWORKHUB_PROCESS_DIR"
PROCESS_DIR_DEFAULT_REL = Path(".aiworkhub/runtime/process_logs/processes")


def default_process_dir(repo: Path) -> Path:
    """The process directory for ``repo``: env override, else the repo default."""

    return Path(os.environ.get(PROCESS_DIR_ENV) or str(Path(repo) / PROCESS_DIR_DEFAULT_REL))


def request_metadata_path(process_dir: Path, request_id: str) -> Path | None:
    """``<process_dir>/<request_id>.request.json``; None for an unnameable id."""

    if not request_id or request_id in {".", ".."} or Path(request_id).name != request_id:
        return None
    return Path(process_dir) / f"{request_id}.request.json"


def resolve_tree_baseline(
    workspace: Mapping[str, Any], *, metadata_path: Path | None
) -> dict[Any, Any] | None:
    """The full ``tree_baseline`` behind a stored ``workspace`` block, or None.

    A legacy inline map is returned as stored.  A stand-in resolves only from
    the request's own on-disk metadata whose workspace names the same
    ``request_id`` and ``path`` and whose map has exactly the stand-in's entry
    count and sha256.  Anything missing, unreadable, oversized or different is
    ``None``: the caller keeps its existing fail-closed refusal.
    """

    tree = workspace.get(TREE_BASELINE_KEY)
    if type(tree) is dict and not is_stand_in(tree):
        return tree
    if not is_stand_in(tree) or metadata_path is None:
        return None
    from .request_metadata_limits import REQUEST_METADATA_MAX_BYTES

    try:
        if (
            metadata_path.is_symlink()
            or not metadata_path.is_file()
            or metadata_path.stat().st_size > REQUEST_METADATA_MAX_BYTES
        ):
            return None
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    on_disk = payload.get("workspace") if isinstance(payload, dict) else None
    if not isinstance(on_disk, dict):
        return None
    for key in ("request_id", "path"):
        expected = workspace.get(key)
        if type(expected) is not str or not expected or on_disk.get(key) != expected:
            return None
    full = on_disk.get(TREE_BASELINE_KEY)
    if type(full) is not dict or is_stand_in(full):
        return None
    if len(full) != tree["entry_count"] or baseline_map_digest(full) != tree["sha256"]:
        return None
    return full


def payload_tree_baseline(payload: Mapping[str, Any]) -> dict[Any, Any]:
    """The full ``tree_baseline`` a workspace metadata ``payload`` stands for.

    The one decoder ``WorkerWorkspace.from_metadata`` uses.  An inline map (the
    on-disk request metadata, or a legacy card) decodes exactly as stored.  A
    card stand-in resolves from ``<process_dir>/<request_id>.request.json`` --
    the authority the launcher writes -- and only when that map's digest equals
    the stand-in, so a card-built workspace keeps the same ``as_metadata()`` as
    the launcher's (the reviewer-accept identity check).  Missing, mismatched,
    symlinked or oversized metadata is ``{}``: "no tree manifest", and every
    fallback consumer keeps its existing ``baseline_missing`` refusal.
    """

    raw = payload.get(TREE_BASELINE_KEY)
    if not is_stand_in(raw):
        return inline_tree_baseline(raw)
    try:
        process_dir = default_process_dir(Path(payload["repo"]))
        metadata_path = request_metadata_path(process_dir, str(payload["request_id"]))
    except (KeyError, TypeError, ValueError):
        return {}
    return dict(resolve_tree_baseline(payload, metadata_path=metadata_path) or {})
