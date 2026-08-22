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
from collections.abc import Mapping, Sequence
from typing import Any

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_GLOB_CHARS = frozenset("*?[")

FINISHED_STATES = frozenset({"finished"})
RETAINED_STATES = frozenset({"processing", "review"})
ACTIVE_STATES = frozenset({"pending", "processing", "review"})
TERMINAL_TARGET_STATUSES = frozenset({"accepted", "finished"})
REOPEN_WITHOUT_SUCCESSOR_STATUSES = frozenset({
    "cancelled",
    "canceled",
    "superseded",
})
REWORK_OR_UNRESOLVED_STATUSES = frozenset({
    "rework",
    "rework_required",
    "unresolved",
})
MAX_TERMINAL_ARTIFACT_ROWS = 200
_MAX_DEPENDS_ON = 64


def _norm_status(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"done", "completed", "stale_already_done"}:
        return "finished"
    return token


def is_terminal_target_status(status: Any) -> bool:
    return _norm_status(status) in TERMINAL_TARGET_STATUSES


def is_rework_or_unresolved_status(status: Any) -> bool:
    return _norm_status(status) in REWORK_OR_UNRESOLVED_STATUSES


def card_status_evidence(card: Mapping[str, Any] | None) -> str:
    if not isinstance(card, Mapping):
        return ""
    archive_op = _norm_status(card.get("archive_operation"))
    if archive_op in REOPEN_WITHOUT_SUCCESSOR_STATUSES:
        return archive_op
    if (
        str(card.get("accepted_request_id") or "").strip()
        and str(card.get("accepted_by") or "").strip()
        and str(card.get("accepted_at") or "").strip()
        and isinstance(card.get("accept_evidence"), Mapping)
    ):
        return "accepted"
    for key in ("status", "worker_status"):
        token = _norm_status(card.get(key))
        if (
            is_terminal_target_status(token)
            or is_rework_or_unresolved_status(token)
            or token in REOPEN_WITHOUT_SUCCESSOR_STATUSES
        ):
            return token
    return _norm_status(card.get("status"))


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def is_valid_task_id(value: Any) -> bool:
    return bool(_TASK_ID_RE.fullmatch(str(value or "").strip()))


def successor_task_id(card: Mapping[str, Any] | None) -> str:
    if not isinstance(card, Mapping):
        return ""
    if str(card.get("archive_operation") or "").strip().lower() != "superseded":
        return ""
    replacement = str(card.get("superseded_by") or "").strip()
    if replacement:
        return replacement if is_valid_task_id(replacement) else ""
    reason = str(card.get("archive_reason") or "")
    if reason.startswith("superseded_by:"):
        candidate = reason.removeprefix("superseded_by:").split(";", 1)[0].strip()
        return candidate if is_valid_task_id(candidate) else ""
    return ""


def enqueue_unknown_task_id(
    task_id: Any,
    *,
    known_ids: Mapping[str, Any],
    seen: set[str],
    pending: list[str],
    limit: int = MAX_TERMINAL_ARTIFACT_ROWS,
) -> bool:
    tid = str(task_id or "").strip()
    if not tid or not is_valid_task_id(tid) or tid in known_ids or tid in seen:
        return True
    if len(seen) >= limit:
        return False
    seen.add(tid)
    pending.append(tid)
    return True


def has_landed_successor(
    card: Mapping[str, Any] | None,
    cards_by_id: Mapping[str, Mapping[str, Any]] | None,
) -> bool:
    if not isinstance(card, Mapping) or cards_by_id is None:
        return False
    seen: set[str] = set()
    current: Mapping[str, Any] = card
    for _ in range(MAX_TERMINAL_ARTIFACT_ROWS):
        successor_id = successor_task_id(current)
        if not successor_id or successor_id in seen:
            return False
        seen.add(successor_id)
        successor = cards_by_id.get(successor_id)
        if not isinstance(successor, Mapping):
            return False
        status = card_status_evidence(successor)
        if is_terminal_target_status(status):
            return True
        if status in REOPEN_WITHOUT_SUCCESSOR_STATUSES:
            current = successor
            continue
        return False
    return False


def exact_artifact_target(card: Mapping[str, Any]) -> tuple[str, str, str]:
    quality = _as_mapping(card.get("quality_review"))
    terminal = _as_mapping(card.get("terminal_review"))
    terminal_evidence = _as_mapping(terminal.get("evidence") if terminal else None)
    terminal_quality = _as_mapping(
        terminal_evidence.get("quality_review") if terminal_evidence else None
    )
    implementation = _as_mapping(card.get("implementation"))
    retry_target = _as_mapping(card.get("retry_target"))
    if quality:
        target_id = str(quality.get("target_task_id") or "").strip()
        if target_id:
            return target_id, _norm_status(quality.get("target_status")), "reviewer"
    if terminal_quality:
        target_id = str(terminal_quality.get("target_task_id") or "").strip()
        if target_id:
            return (
                target_id,
                _norm_status(terminal_quality.get("target_status")),
                "reviewer",
            )
    if implementation:
        target_id = str(implementation.get("target_task_id") or "").strip()
        if target_id:
            return (
                target_id,
                _norm_status(implementation.get("target_status")),
                "implementation",
            )
    if retry_target:
        target_id = str(
            retry_target.get("target_task_id") or retry_target.get("task_id") or ""
        ).strip()
        if target_id:
            kind = _norm_status(retry_target.get("kind"))
            if kind not in {"reviewer", "implementation"}:
                kind = "implementation"
            return target_id, _norm_status(retry_target.get("target_status")), kind
    return "", "", ""


def prefetch_unknown_terminal_cards(
    cards: Sequence[Mapping[str, Any]],
    cards_by_id: dict[str, Any],
    load_one,
    *,
    limit: int = MAX_TERMINAL_ARTIFACT_ROWS,
) -> bool:
    pending: list[str] = []
    seen: set[str] = set()
    complete = True

    def enqueue(task_id: Any) -> None:
        nonlocal complete
        if not enqueue_unknown_task_id(
            task_id,
            known_ids=cards_by_id,
            seen=seen,
            pending=pending,
            limit=limit,
        ):
            complete = False

    seeds: list[Mapping[str, Any]] = [
        card for card in cards if isinstance(card, Mapping)
    ]
    for known in list(cards_by_id.values()):
        if isinstance(known, Mapping):
            seeds.append(known)
    for card in seeds:
        target_id, _recorded, _kind = exact_artifact_target(card)
        enqueue(target_id)
        enqueue(successor_task_id(card))
        known = cards_by_id.get(target_id) if target_id else None
        if isinstance(known, Mapping):
            enqueue(successor_task_id(known))

    while pending:
        pending.sort()
        batch = pending
        pending = []
        for target_id in batch:
            if target_id in cards_by_id:
                continue
            found = load_one(target_id)
            if not isinstance(found, Mapping):
                cards_by_id[target_id] = None
                continue
            cards_by_id[target_id] = found
            enqueue(successor_task_id(found))
    return complete


def evaluate_terminal_artifact(
    card: Mapping[str, Any],
    cards_by_id: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    target_id, _recorded_status, kind = exact_artifact_target(card)
    if not target_id or cards_by_id is None:
        return None
    candidate = cards_by_id.get(target_id)
    if not isinstance(candidate, Mapping):
        return None
    target_status = card_status_evidence(candidate)
    if is_rework_or_unresolved_status(target_status):
        return None
    if target_status in REOPEN_WITHOUT_SUCCESSOR_STATUSES:
        if not has_landed_successor(candidate, cards_by_id):
            return None
    elif not is_terminal_target_status(target_status):
        return None
    return {
        "task_id": str(card.get("task_id") or "").strip(),
        "target_task_id": target_id,
        "target_status": target_status,
        "artifact_kind": kind,
        "reason": "exact_target_terminal",
    }


def _terminal_artifact_state(
    cards: Sequence[Mapping[str, Any]],
    cards_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    by_id = cards_by_id
    if by_id is None:
        by_id = {
            str(card.get("task_id") or ""): card
            for card in cards
            if isinstance(card, Mapping) and card.get("task_id")
        }
    rows: list[dict[str, Any]] = []
    excluded: set[str] = set()
    seen: set[str] = set()
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        row = evaluate_terminal_artifact(card, by_id)
        if row is None:
            continue
        task_id = str(row.get("task_id") or "").strip()
        excluded.add(task_id)
        key = task_id or f"{row['target_task_id']}:{row['artifact_kind']}"
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    rows.sort(
        key=lambda item: (
            str(item.get("task_id") or ""),
            str(item.get("target_task_id") or ""),
        )
    )
    return rows, excluded


def collect_terminal_artifacts(
    cards: Sequence[Mapping[str, Any]],
    cards_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows, _excluded = _terminal_artifact_state(cards, cards_by_id)
    return rows[:MAX_TERMINAL_ARTIFACT_ROWS]


def terminal_artifact_exclusion_ids(
    cards: Sequence[Mapping[str, Any]],
    cards_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> set[str]:
    _rows, excluded = _terminal_artifact_state(cards, cards_by_id)
    return excluded


def terminal_artifact_projection(
    cards: Sequence[Mapping[str, Any]],
    cards_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    rows, excluded = _terminal_artifact_state(cards, cards_by_id)
    return rows[:MAX_TERMINAL_ARTIFACT_ROWS], excluded


class TaskPlanError(ValueError):
    """Raised for an invalid depends_on list or an illegal dependency edge."""


def _superseded_replacement(card: dict[str, Any]) -> str:
    return successor_task_id(card)


def resolve_superseded_dependency(
    dependency_id: str,
    cards: dict[str, dict[str, Any]],
) -> tuple[str | None, list[str], str | None]:
    """Resolve an archived predecessor through explicit replacement edges.

    Returns ``(resolved_id, chain, error)``.  Ordinary active/finished or
    unknown dependencies are returned unchanged so the existing missing-task
    validation/blocker remains authoritative.  Only archived predecessors are
    rewritten, and every malformed, missing, non-superseded, or cyclic chain
    fails closed with a deterministic blocker.
    """
    current = dependency_id
    chain: list[str] = []
    seen: set[str] = set()
    while True:
        card = cards.get(current)
        if card is None:
            if chain:
                return None, chain, f"__superseded_replacement_not_found__:{current}"
            return current, chain, None
        if lifecycle_state(card) != "archived":
            return current, chain, None
        if current in seen:
            return None, chain, f"__superseded_replacement_cycle__:{current}"
        seen.add(current)
        chain.append(current)
        if str(card.get("archive_operation") or "").strip().lower() != "superseded":
            return None, chain, f"__archived_dependency_not_superseded__:{current}"
        replacement = successor_task_id(card)
        if not replacement:
            raw = str(card.get("superseded_by") or "").strip()
            if not raw:
                reason = str(card.get("archive_reason") or "")
                if reason.startswith("superseded_by:"):
                    raw = reason.removeprefix("superseded_by:").split(";", 1)[0].strip()
            if raw:
                return None, chain, f"__invalid_superseded_replacement__:{current}"
            return None, chain, f"__superseded_dependency_without_replacement__:{current}"
        current = replacement


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
        if not is_valid_task_id(text):
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
        if lifecycle_state(card) == "archived":
            resolved, _chain, error = resolve_superseded_dependency(tid, cards)
            if error is not None or resolved is None or resolved == tid:
                edges[tid] = []
                invalid_ids.add(tid)
            else:
                edges[tid] = [resolved]
            continue
        try:
            raw_dependencies = normalize_depends_on(card.get("depends_on"))
        except TaskPlanError:
            edges[tid] = []
            invalid_ids.add(tid)
            continue
        resolved_dependencies: list[str] = []
        for dependency_id in raw_dependencies:
            resolved, _chain, error = resolve_superseded_dependency(dependency_id, cards)
            if error is not None or resolved is None:
                invalid_ids.add(tid)
                continue
            if resolved not in resolved_dependencies:
                resolved_dependencies.append(resolved)
        edges[tid] = resolved_dependencies
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


def _active_write_collisions(
    by_id: dict[str, dict[str, Any]],
    lifecycle: dict[str, str],
) -> list[dict[str, Any]]:
    """Pairwise write-scope collisions among ACTIVE cards.

    Mirrors the canonical collision guard's active-card population: only
    ``pending``, ``processing`` and ``review`` cards still own or can acquire
    write authority, so finished/blocked/archived cards are excluded here and
    can never hold the plan in a phantom collision.  Dependency-blocked
    pending cards are still scanned because a write-scope overlap is a truth
    about the active plan, not a launch-eligibility verdict.

    Returns one record per unordered colliding card pair, each carrying the
    pair's ``task_ids`` (sorted) and the ``paths`` (sorted union of both
    cards' overlapping allowed_writes).
    """
    active_ids = sorted(
        tid for tid, state in lifecycle.items() if state in ACTIVE_STATES
    )
    records: list[dict[str, Any]] = []
    for index, tid_a in enumerate(active_ids):
        writes_a = [str(p) for p in (by_id[tid_a].get("allowed_writes") or [])]
        for tid_b in active_ids[index + 1:]:
            writes_b = [str(p) for p in (by_id[tid_b].get("allowed_writes") or [])]
            a_conflicts = [p for p in writes_a if _paths_conflict_any(p, set(writes_b))]
            if not a_conflicts:
                continue
            b_conflicts = [p for p in writes_b if _paths_conflict_any(p, set(writes_a))]
            paths = sorted(set(a_conflicts) | set(b_conflicts))
            records.append({"task_ids": sorted([tid_a, tid_b]), "paths": paths})
    return records


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
    all_by_id = {str(c["task_id"]): c for c in cards}
    terminal_artifacts_excluded, excluded_ids = terminal_artifact_projection(
        cards, all_by_id
    )
    by_id = {
        str(c["task_id"]): c
        for c in cards
        if lifecycle_state(c) != "archived" and str(c["task_id"]) not in excluded_ids
    }
    lifecycle = {tid: lifecycle_state(c) for tid, c in by_id.items()}

    dependencies: dict[str, list[str]] = {}
    original_dependencies: dict[str, list[str]] = {}
    dependency_replacements: dict[str, dict[str, Any]] = {}
    dependency_resolution_errors: dict[str, list[str]] = {}
    dependents: dict[str, list[str]] = {tid: [] for tid in by_id}
    blockers: dict[str, list[str]] = {}
    invalid_depends_on: set[str] = set()
    for tid, c in by_id.items():
        try:
            raw_dependencies = normalize_depends_on(c.get("depends_on"))
        except TaskPlanError:
            raw_dependencies = []
            invalid_depends_on.add(tid)
        original_dependencies[tid] = raw_dependencies
        deps: list[str] = []
        resolution_errors: list[str] = []
        for dependency_id in raw_dependencies:
            if dependency_id in excluded_ids:
                continue
            resolved, chain, error = resolve_superseded_dependency(
                dependency_id, all_by_id
            )
            if error is not None or resolved is None:
                resolution_errors.append(error or "__dependency_resolution_failed__")
                continue
            if resolved in excluded_ids:
                continue
            if chain:
                dependency_replacements.setdefault(tid, {})[dependency_id] = {
                    "chain": chain,
                    "resolved_to": resolved,
                }
            if resolved not in deps:
                deps.append(resolved)
        if resolution_errors:
            dependency_resolution_errors[tid] = resolution_errors
        dependencies[tid] = deps
        for dep in deps:
            dependents.setdefault(dep, []).append(tid)
        if tid in invalid_depends_on:
            # Malformed depends_on: true dependency set is unknown, so the
            # card is reported invalid and permanently blocked rather than
            # silently normalized to an edge-free, immediately-ready card.
            blockers[tid] = ["__invalid_depends_on__"]
        elif resolution_errors:
            blockers[tid] = resolution_errors
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

    def has_persisted_noncollision_blocker(card: dict[str, Any]) -> bool:
        blocker = card.get("operational_blocker")
        if not isinstance(blocker, dict):
            return False
        # collision_guard_failed records a point-in-time pre-claim result.
        # Re-project collision truth from the current canonical cards below;
        # otherwise an archived/finished contender leaves ready_capacity at
        # zero forever even though an exact launch would now pass its live
        # guard. Other operational blockers remain fail-closed until an exact
        # claim or explicit repair clears them.
        return str(blocker.get("reason") or "") != "collision_guard_failed"

    ready: list[str] = []
    write_scope_overlaps: dict[str, list[str]] = {}
    claimed_paths: set[str] = set(retained_paths)
    for c in ordered:
        tid = str(c["task_id"])
        if lifecycle[tid] != "pending":
            continue
        if blockers[tid]:
            continue
        if has_persisted_noncollision_blocker(c):
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
        if reason == "collision_guard_failed":
            # Current write_scope_overlaps/ready projection above supersedes
            # this historical point-in-time collision result.
            continue
        operational_blockers[tid] = reason[:500]
    operational_blocked_task_ids = sorted(operational_blockers)
    # A pending pre-claim operational blocker is intentionally excluded from
    # automatic ``ready`` selection so a broken adapter cannot create a retry
    # loop. It is nevertheless eligible for an explicit exact launch, whose
    # claim path clears the historical blocker and re-runs current preflight.
    # Surface that recovery path separately instead of presenting the task as
    # an opaque permanent blocker.
    explicit_retry_task_ids = sorted(
        tid
        for tid in operational_blocked_task_ids
        if lifecycle.get(tid) == "pending"
    )
    dependency_blocked_ids = sorted(tid for tid, value in blockers.items() if value)
    lifecycle_blocked_ids = sorted(
        tid for tid, value in lifecycle.items() if value == "blocked"
    )
    blocked_task_ids = sorted(
        set(dependency_blocked_ids)
        | set(lifecycle_blocked_ids)
        | set(operational_blocked_task_ids)
    )

    # Global collision health is an observational truth about every active
    # card's write scope, independent of claim order and launch eligibility.
    # ``write_scope_overlaps`` above stays a claim-eligibility projection
    # (which pending card is blocked by an already-owned scope), so an empty
    # ``write_scope_overlaps`` must never be read as "collision free" -- e.g.
    # two processing cards can overlap while no pending card is affected.
    # ``global_collision_free`` below is the authoritative signal.
    collision_records = _active_write_collisions(by_id, lifecycle)
    global_collision_pairs = [rec["task_ids"] for rec in collision_records]
    global_collision_paths = sorted(
        {path for rec in collision_records for path in rec["paths"]}
    )
    global_collision_task_ids = sorted(
        {tid for rec in collision_records for tid in rec["task_ids"]}
    )
    card_collision_free = {tid: True for tid in by_id}
    card_collision_task_ids: dict[str, list[str]] = {}
    card_collision_paths: dict[str, list[str]] = {}
    for rec in collision_records:
        for tid in rec["task_ids"]:
            card_collision_free[tid] = False
            peers = card_collision_task_ids.setdefault(tid, [])
            for peer in rec["task_ids"]:
                if peer != tid and peer not in peers:
                    peers.append(peer)
            paths = card_collision_paths.setdefault(tid, [])
            for path in rec["paths"]:
                if path not in paths:
                    paths.append(path)
    card_collision_task_ids = {
        tid: sorted(peers) for tid, peers in card_collision_task_ids.items()
    }
    card_collision_paths = {
        tid: sorted(paths) for tid, paths in card_collision_paths.items()
    }

    return {
        "task_ids": sorted(by_id),
        "lifecycle": lifecycle,
        "dependencies": dependencies,
        "original_dependencies": original_dependencies,
        "dependency_replacements": dependency_replacements,
        "dependency_resolution_errors": dependency_resolution_errors,
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
        "explicit_retry_task_ids": explicit_retry_task_ids,
        "explicit_retry_count": len(explicit_retry_task_ids),
        "orphaned_processing": orphaned_processing_task_ids,
        "orphaned_processing_count": len(orphaned_processing_task_ids),
        "edge_count": sum(len(value) for value in dependencies.values()),
        "layers": layers,
        "critical_path": critical_path,
        "critical_path_length": len(critical_path),
        "dag_valid": (
            not cycle_nodes
            and not invalid_depends_on
            and not dependency_resolution_errors
        ),
        "cycle_nodes": cycle_nodes,
        "global_collision_free": not collision_records,
        "global_collision_count": len(collision_records),
        "global_collision_paths": global_collision_paths,
        "global_collision_task_ids": global_collision_task_ids,
        "global_collision_pairs": global_collision_pairs,
        "card_collision_free": card_collision_free,
        "card_collision_task_ids": card_collision_task_ids,
        "card_collision_paths": card_collision_paths,
        "terminal_artifacts_excluded": terminal_artifacts_excluded,
        "terminal_artifacts_excluded_count": len(excluded_ids),
    }


# Bounded current-state fields the Plan-DAG summary projection forwards
# verbatim.  The historical DAG (dependencies/dependents/layers/lifecycle/
# task_ids) is intentionally excluded -- those stay behind ``full=True`` at
# the MCP boundary.  Collision truth is current-state, not graph history, so
# it belongs here.  Kept at the pure plan boundary so the summary projection
# and any future caller share one authoritative list.
PLAN_SUMMARY_FIELDS = (
    "ready",
    "ready_capacity",
    "active_count",
    "terminal_artifacts_excluded",
    "terminal_artifacts_excluded_count",
    "blocked_count",
    "blocked_task_ids",
    "dependency_blocked_count",
    "dependency_blocked_task_ids",
    "lifecycle_blocked_count",
    "lifecycle_blocked_task_ids",
    "operational_blockers",
    "operational_blocked_task_ids",
    "operational_blocked_count",
    "explicit_retry_task_ids",
    "explicit_retry_count",
    "orphaned_processing",
    "orphaned_processing_count",
    "invalid_depends_on",
    "write_scope_overlaps",
    "global_collision_free",
    "global_collision_count",
    "global_collision_paths",
    "global_collision_task_ids",
    "global_collision_pairs",
    "card_collision_free",
    "card_collision_task_ids",
    "card_collision_paths",
    "critical_path",
    "critical_path_length",
    "edge_count",
    "dag_valid",
    "cycle_nodes",
)


def summarize_task_plan_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded summary projection of a full Plan-DAG snapshot.

    Retains current planning truth -- ready work, live blockers, collision
    health and malformed/orphaned state -- without replaying the historical
    DAG (dependencies/dependents/layers/lifecycle/task_ids), which stays
    behind ``full=True`` at the MCP boundary.  The global and exact per-card
    collision fields are included because they are bounded current-state
    facts, not historical graph structure.
    """
    lifecycle = snapshot.get("lifecycle")
    lifecycle_map = dict(lifecycle) if isinstance(lifecycle, Mapping) else {}
    actionable_lifecycle = {
        str(task_id): str(state)
        for task_id, state in lifecycle_map.items()
        if str(state).strip().lower() not in {"finished", "archived"}
    }
    task_ids = snapshot.get("task_ids")
    task_count = len(task_ids) if isinstance(task_ids, list) else len(lifecycle_map)
    terminal_task_count = max(0, task_count - len(actionable_lifecycle))
    layers = snapshot.get("layers")

    result: dict[str, Any] = {
        "ok": bool(snapshot.get("ok", True)),
        "schema_id": snapshot.get("schema_id", "aiworkhub.task_plan_snapshot.v1"),
        "snapshot_mode": "summary",
        "full_snapshot_available": True,
        "task_count": task_count,
        "actionable_task_count": len(actionable_lifecycle),
        "terminal_task_count": terminal_task_count,
        "actionable_lifecycle": actionable_lifecycle,
        "layer_count": len(layers) if isinstance(layers, list) else 0,
    }
    for field in PLAN_SUMMARY_FIELDS:
        if field in snapshot:
            result[field] = snapshot[field]
    result["omitted_fields"] = [
        "dependencies",
        "dependents",
        "layers",
        "lifecycle",
        "task_ids",
    ]
    return result


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
