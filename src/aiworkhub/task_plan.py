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
        my_writes = set(c.get("allowed_writes") or [])
        overlap = {p for p in my_writes if _paths_conflict_any(p, claimed_paths)}
        if overlap:
            write_scope_overlaps[tid] = sorted(overlap)
            continue
        ready.append(tid)
        claimed_paths |= my_writes

    return {
        "task_ids": sorted(by_id),
        "lifecycle": lifecycle,
        "dependencies": dependencies,
        "dependents": dependents,
        "blockers": {tid: deps for tid, deps in blockers.items() if deps},
        "invalid_depends_on": sorted(invalid_depends_on),
        "write_scope_overlaps": write_scope_overlaps,
        "ready": ready,
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
