from __future__ import annotations

"""Pure, read-time Plan-DAG helpers for AIWorkHub task cards.

Nothing here performs IO or claims/mutates a card. Callers (``core.py``)
supply the exact card dicts already loaded from the canonical task store and
this module only computes: dependency validation, a DAG snapshot
(dependencies / dependents / blockers / write-scope overlaps), and the
deterministic set of task_ids that are ready to be claimed right now.

Queue order for tie-breaking (which of several ready, write-overlapping
cards may claim first) is ``(created_at, task_id)`` ascending -- the same
FIFO order the rest of AIWorkHub's queue already implies.
"""

import fnmatch
import re
from typing import Any

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_GLOB_CHARS = frozenset("*?[")

FINISHED_STATES = frozenset({"finished"})
RETAINED_STATES = frozenset({"processing", "review"})
ACTIVE_STATES = frozenset({"pending", "processing", "review"})
_MAX_DEPENDS_ON = 64


class TaskPlanError(ValueError):
    """Raised for an invalid depends_on list or an illegal dependency edge."""


def normalize_depends_on(depends_on: list[str] | None) -> list[str]:
    """Bounded, de-duplicated, order-preserving validation of a depends_on list."""
    if depends_on is None:
        return []
    if not isinstance(depends_on, list) or len(depends_on) > _MAX_DEPENDS_ON:
        raise TaskPlanError("invalid_depends_on")
    out: list[str] = []
    seen: set[str] = set()
    for item in depends_on:
        text = str(item or "").strip()
        if not _TASK_ID_RE.fullmatch(text):
            raise TaskPlanError(f"invalid_dependency_id:{text}")
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def existing_edges_from_cards(
    cards: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[str]], set[str]]:
    """Build the ``existing_edges`` map ``validate_new_dependency_edge`` needs
    from raw (already-loaded) legacy/existing cards, keyed by task_id.

    A card whose stored ``depends_on`` fails validation is never silently
    treated as ``[]``/edge-free: its task_id is instead returned in the
    second element (``invalid_ids``) so callers can refuse to build new
    edges through it (fail closed) rather than pretending it has no
    dependencies.
    """
    edges: dict[str, list[str]] = {}
    invalid_ids: set[str] = set()
    for tid, card in cards.items():
        try:
            edges[tid] = normalize_depends_on(card.get("depends_on"))
        except TaskPlanError:
            edges[tid] = []
            invalid_ids.add(tid)
    return edges, invalid_ids


def validate_new_dependency_edge(
    task_id: str,
    depends_on: list[str],
    existing_edges: dict[str, list[str]],
    *,
    invalid_ids: set[str] | None = None,
) -> None:
    """Validate that ``task_id -> depends_on`` is a legal edge to add.

    ``existing_edges`` maps every already-existing task_id in the repo to its
    own (already-validated) depends_on list. Raises ``TaskPlanError`` on:
    self-dependency, a dependency that does not already exist in the repo, an
    edge that would introduce a cycle, or an edge that passes through a card
    whose own ``depends_on`` is malformed (``invalid_ids``) -- such a card's
    true dependency set is unknown, so we fail closed rather than assume it
    has none.
    """
    invalid_ids = invalid_ids or set()
    for dep in depends_on:
        if dep == task_id:
            raise TaskPlanError("self_dependency_forbidden")
        if dep not in existing_edges:
            raise TaskPlanError(f"dependency_not_found:{dep}")
        if dep in invalid_ids:
            raise TaskPlanError(f"dependency_has_invalid_depends_on:{dep}")
    visited: set[str] = set()
    stack = list(depends_on)
    while stack:
        node = stack.pop()
        if node == task_id:
            raise TaskPlanError("dependency_cycle_detected")
        if node in visited:
            continue
        visited.add(node)
        if node in invalid_ids:
            raise TaskPlanError(f"dependency_has_invalid_depends_on:{node}")
        stack.extend(existing_edges.get(node, []))


def paths_conflict(a: str, b: str) -> bool:
    """Conservative, bounded write-scope conflict check between two
    ``allowed_writes`` entries.

    Detects exact matches, parent/child paths (``src`` or ``src/`` conflicts
    with ``src/x.py``), and glob patterns (``src/**`` or ``out/*.json``
    conflict with any literal path they match). Deliberately over-approximate
    (a glob is treated as matching any path it could possibly match) rather
    than under-approximate, since a missed overlap risks two workers writing
    the same file concurrently.
    """
    a = str(a or "").strip()
    b = str(b or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    a_dir = a if a.endswith("/") else a + "/"
    b_dir = b if b.endswith("/") else b + "/"
    if b.startswith(a_dir) or a.startswith(b_dir):
        return True
    a_is_glob = any(ch in _GLOB_CHARS for ch in a)
    b_is_glob = any(ch in _GLOB_CHARS for ch in b)
    if a_is_glob and fnmatch.fnmatchcase(b, a):
        return True
    if b_is_glob and fnmatch.fnmatchcase(a, b):
        return True
    if a_is_glob and b_is_glob:
        a_prefix = a.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0]
        b_prefix = b.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0]
        if a_prefix and b_prefix and (a_prefix.startswith(b_prefix) or b_prefix.startswith(a_prefix)):
            return True
    return False


def _paths_conflict_any(candidate: str, others: set[str]) -> bool:
    return any(paths_conflict(candidate, other) for other in others)


def lifecycle_state(card: dict[str, Any]) -> str:
    """Compact lifecycle classifier -- mirrors ``core._lifecycle_state``.

    Kept as a local pure copy (no import of ``core``) so this module stays
    dependency-free and independently testable.
    """
    status = str(card.get("status") or "").strip().lower()
    worker_status = str(card.get("worker_status") or "").strip().lower()
    if status == "archived":
        return "archived"
    if status in {"finished", "completed", "stale_already_done"} or worker_status == "done":
        return "finished"
    if status.startswith("blocked") or worker_status.startswith(("blocked", "deferred")):
        return "blocked"
    if status in {"review", "ready_for_review", "codex_review", "awaiting_review"} or worker_status in {
        "review",
        "ready_for_review",
        "codex_review",
        "awaiting_review",
    }:
        return "review"
    if status in {"processing", "in_progress"} or worker_status in {"claimed", "in_progress"}:
        return "processing"
    return "pending"


def build_snapshot(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a read-time DAG snapshot from a list of full canonical cards.

    Each card is expected to carry (at least): ``task_id``, ``status``,
    ``worker_status``, ``allowed_writes`` and an optional ``depends_on``.
    Pure function: no IO, no mutation, deterministic given the same input.
    """
    # Archived cards are audit history, not active Plan-DAG nodes.  Filter
    # them before constructing any derived structure so they cannot reappear
    # as pending/ready, block dependencies, or reserve write scope.
    by_id = {
        str(c["task_id"]): c
        for c in cards
        if lifecycle_state(c) != "archived"
    }
    lifecycle = {tid: lifecycle_state(c) for tid, c in by_id.items()}

    dependencies: dict[str, list[str]] = {}
    dependents: dict[str, list[str]] = {tid: [] for tid in by_id}
    blockers: dict[str, list[str]] = {}
    invalid_depends_on: set[str] = set()
    for tid, c in by_id.items():
        try:
            deps = normalize_depends_on(c.get("depends_on"))
        except TaskPlanError:
            deps = []
            invalid_depends_on.add(tid)
        dependencies[tid] = deps
        for dep in deps:
            dependents.setdefault(dep, []).append(tid)
        if tid in invalid_depends_on:
            # Malformed depends_on: true dependency set is unknown, so the
            # card is reported invalid and permanently blocked rather than
            # silently normalized to an edge-free, immediately-ready card.
            blockers[tid] = ["__invalid_depends_on__"]
        else:
            blockers[tid] = [d for d in deps if lifecycle.get(d) != "finished"]

    retained_paths: set[str] = set()
    for tid, c in by_id.items():
        if lifecycle[tid] in RETAINED_STATES:
            retained_paths |= set(c.get("allowed_writes") or [])

    ordered = sorted(
        by_id.values(),
        key=lambda c: (str(c.get("created_at") or ""), str(c.get("task_id"))),
    )

    ready: list[str] = []
    write_scope_overlaps: dict[str, list[str]] = {}
    claimed_paths: set[str] = set(retained_paths)
    for c in ordered:
        tid = str(c["task_id"])
        if lifecycle[tid] != "pending":
            continue
        if blockers[tid]:
            continue
        if isinstance(c.get("operational_blocker"), dict):
            continue
        my_writes = set(c.get("allowed_writes") or [])
        overlap = {p for p in my_writes if _paths_conflict_any(p, claimed_paths)}
        if overlap:
            write_scope_overlaps[tid] = sorted(overlap)
            continue
        ready.append(tid)
        claimed_paths |= my_writes

    # Deterministic topological presentation metadata.  This never changes
    # claim authority: it projects the already-validated dependency map for
    # the dashboard.  Legacy cycles fail visibly instead of fabricating a
    # critical path through an invalid graph.
    present_dependencies = {
        tid: [dep for dep in deps if dep in by_id]
        for tid, deps in dependencies.items()
    }
    indegree = {tid: len(deps) for tid, deps in present_dependencies.items()}
    layer_by_id = {tid: 0 for tid in by_id}
    queue = sorted(tid for tid, degree in indegree.items() if degree == 0)
    topological: list[str] = []
    while queue:
        tid = queue.pop(0)
        topological.append(tid)
        for child in sorted(dependents.get(tid, [])):
            if child not in indegree:
                continue
            layer_by_id[child] = max(layer_by_id[child], layer_by_id[tid] + 1)
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
                queue.sort()
    cycle_nodes = sorted(tid for tid, degree in indegree.items() if degree > 0)
    layers: list[dict[str, Any]] = []
    for layer_index in sorted(set(layer_by_id.values())):
        task_ids = sorted(
            tid for tid, value in layer_by_id.items()
            if value == layer_index and tid not in cycle_nodes
        )
        if task_ids:
            layers.append({"index": layer_index, "task_ids": task_ids})

    critical_path: list[str] = []
    if not cycle_nodes:
        longest_to: dict[str, list[str]] = {}
        for tid in topological:
            candidates = [longest_to[dep] for dep in present_dependencies[tid]]
            prefix = max(candidates, key=lambda value: (len(value), value)) if candidates else []
            longest_to[tid] = [*prefix, tid]
        active_candidates = [
            path for tid, path in longest_to.items()
            if lifecycle.get(tid) in ACTIVE_STATES
        ]
        if active_candidates:
            critical_path = max(active_candidates, key=lambda value: (len(value), value))

    orphaned_processing_task_ids = sorted(
        tid
        for tid, value in lifecycle.items()
        if value == "processing"
        and not str(by_id[tid].get("launch_request_id") or "").strip()
    )
    operational_blockers = {
        tid: "processing_without_launch_request"
        for tid in orphaned_processing_task_ids
    }
    for tid, card in by_id.items():
        blocker = card.get("operational_blocker")
        if lifecycle[tid] != "pending" or not isinstance(blocker, dict):
            continue
        reason = str(blocker.get("reason") or blocker.get("kind") or "launch_blocked")
        operational_blockers[tid] = reason[:500]
    operational_blocked_task_ids = sorted(operational_blockers)
    dependency_blocked_ids = sorted(tid for tid, value in blockers.items() if value)
    lifecycle_blocked_ids = sorted(
        tid for tid, value in lifecycle.items() if value == "blocked"
    )
    blocked_task_ids = sorted(
        set(dependency_blocked_ids)
        | set(lifecycle_blocked_ids)
        | set(operational_blocked_task_ids)
    )

    return {
        "task_ids": sorted(by_id),
        "lifecycle": lifecycle,
        "dependencies": dependencies,
        "dependents": dependents,
        "blockers": {tid: deps for tid, deps in blockers.items() if deps},
        "invalid_depends_on": sorted(invalid_depends_on),
        "write_scope_overlaps": write_scope_overlaps,
        "ready": ready,
        "ready_capacity": len(ready),
        "active_count": sum(1 for value in lifecycle.values() if value in ACTIVE_STATES),
        # Backward-compatible total: callers that historically consumed
        # ``blocked_count`` now see every blocked task, not only DAG edges.
        "blocked_count": len(blocked_task_ids),
        "blocked_task_ids": blocked_task_ids,
        "dependency_blocked_count": len(dependency_blocked_ids),
        "dependency_blocked_task_ids": dependency_blocked_ids,
        "lifecycle_blocked_count": len(lifecycle_blocked_ids),
        "lifecycle_blocked_task_ids": lifecycle_blocked_ids,
        "operational_blockers": operational_blockers,
        "operational_blocked_task_ids": operational_blocked_task_ids,
        "operational_blocked_count": len(operational_blocked_task_ids),
        "orphaned_processing": orphaned_processing_task_ids,
        "orphaned_processing_count": len(orphaned_processing_task_ids),
        "edge_count": sum(len(value) for value in dependencies.values()),
        "layers": layers,
        "critical_path": critical_path,
        "critical_path_length": len(critical_path),
        "dag_valid": not cycle_nodes and not invalid_depends_on,
        "cycle_nodes": cycle_nodes,
    }


def filter_claimable(
    snapshot: dict[str, Any],
    cards: list[dict[str, Any]],
    *,
    runner: str,
    topic: str | None = None,
) -> list[dict[str, Any]]:
    """Ordered subset of ``cards`` that are DAG-ready (per ``snapshot``) AND
    match ``runner``/``topic``. Preserves the snapshot's deterministic queue
    order; callers claim ``result[0]``."""
    by_id = {str(c["task_id"]): c for c in cards}
    out: list[dict[str, Any]] = []
    for tid in snapshot.get("ready") or []:
        c = by_id.get(tid)
        if c is None:
            continue
        if c.get("runner") != runner:
            continue
        if topic is not None and c.get("topic") != topic:
            continue
        out.append(c)
    return out
