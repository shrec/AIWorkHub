from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Mapping

from . import task_store

_OID_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def base_drift_candidate_evidence(card: Mapping[str, Any]) -> tuple[str | None, list[str]]:
    """The card's recorded base oid and the repo paths its candidate changed."""
    for name in ("terminal_review", "terminal_failure"):
        event = card.get(name)
        evidence = event.get("evidence") if isinstance(event, dict) else None
        workspace = evidence.get("workspace") if isinstance(evidence, dict) else None
        changed = evidence.get("changed_paths") if isinstance(evidence, dict) else None
        base_oid = workspace.get("base_oid") if isinstance(workspace, dict) else None
        paths = [item for item in changed if isinstance(item, str)] if isinstance(changed, list) else []
        if isinstance(base_oid, str) and paths:
            return base_oid, paths
    predecessor = card.get("rework_predecessor")
    workspace = predecessor.get("workspace") if isinstance(predecessor, dict) else None
    hashes = predecessor.get("changed_path_hashes") if isinstance(predecessor, dict) else None
    base_oid = workspace.get("base_oid") if isinstance(workspace, dict) else None
    if isinstance(base_oid, str) and isinstance(hashes, dict):
        return base_oid, [str(item) for item in hashes if isinstance(item, str)]
    return None, []


def base_drift_proof(
    card: Mapping[str, Any], repo_root: Callable[[], Path]
) -> tuple[dict[str, Any] | None, str | None]:
    """Prove with git that canonical HEAD moved under a candidate-touched path.

    Returns ``(proof, None)`` or ``(None, reason)``. The proof is only the
    recorded base oid being an ancestor of HEAD and at least one path the
    candidate changed differing between the two; anything unprovable refuses.
    """
    base_oid, candidate_paths = base_drift_candidate_evidence(card)
    if base_oid is None or _OID_PATTERN.fullmatch(base_oid) is None:
        return None, "base_oid_unrecorded"
    paths = list(dict.fromkeys(
        path for path in candidate_paths
        if path
        and not path.startswith(("/", "-"))
        and "\x00" not in path
        and ".." not in path.replace("\\", "/").split("/")
    ))
    if not paths:
        return None, "candidate_paths_unrecorded"
    try:
        repo = repo_root().resolve(strict=True)
        head = task_store._committed_drift_git(repo, "rev-parse", "--verify", "HEAD")
        head_oid = str(head.stdout or "").strip()
        if head.returncode != 0 or _OID_PATTERN.fullmatch(head_oid) is None:
            return None, "head_unresolvable"
        ancestry = task_store._committed_drift_git(
            repo, "merge-base", "--is-ancestor", base_oid, "HEAD"
        )
        if ancestry.returncode != 0:
            return None, "base_not_ancestor_of_head"
        diff = task_store._committed_drift_git(
            repo, "diff", "--name-only", "-z", base_oid, "HEAD", "--", *paths
        )
    except Exception:
        return None, "git_probe_failed"
    if diff.returncode != 0:
        return None, "git_probe_failed"
    drifted = sorted(set(item for item in str(diff.stdout or "").split("\x00") if item))
    if not drifted:
        return None, "no_candidate_path_drift"
    return {
        "base_oid": base_oid,
        "head_oid": head_oid,
        "drifted_paths": drifted[:256],
        "drifted_path_count": len(drifted),
    }, None
