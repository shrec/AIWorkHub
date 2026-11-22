"""In-run zero-delta observation for isolated worker launches."""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from typing import Any, Iterable

from .worker_workspace import WorkerWorkspace


ZERO_DELTA_NOTICE = "zero_required_output_delta_warning"
RUNTIME_NOTICE_EVENT_KIND = "runtime_notice"
ORCHESTRATOR_ADVISORY_EVENT_TYPES = frozenset({
    "review_orchestrator_wait",
    "review_orchestrator_mechanical_rework",
})
ZERO_DELTA_ELAPSED_SHARE_ENV = "AIWORKHUB_ZERO_DELTA_ELAPSED_SHARE"
ZERO_DELTA_DEFAULT_ELAPSED_SHARE = 0.5
ZERO_DELTA_MIN_SECONDS = 60.0
ZERO_DELTA_MAX_SECONDS = 600.0
ZERO_DELTA_POLL_SECONDS = 15.0
ZERO_DELTA_ENFORCE_SECONDS_ENV = "AIWORKHUB_ZERO_DELTA_ENFORCE_SECONDS"
ZERO_DELTA_DEFAULT_ENFORCE_SECONDS = 2700.0
ZERO_DELTA_ENFORCE_REASON = "stalled_no_edit"


def split_lifecycle_tail(
    events: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Split an event list into its last lifecycle row and latest advisory row.

    An advisory ``runtime_notice`` row (see ``RUNTIME_NOTICE_EVENT_KIND``) has
    no ``state`` and carries ``pid`` without ``pid_start_ticks``. Every reader
    that took lifecycle state from ``events[-1]`` must read the last LIFECYCLE
    row instead, or a quiet worker's own notice stops reconciliation forever.
    Stateless review-orchestrator rows (``ORCHESTRATOR_ADVISORY_EVENT_TYPES``)
    are skipped entirely: they are neither the lifecycle tail nor the notice.
    Returns ``({}, None)`` for an empty or notice-only list so every caller's
    ``.get(...)`` stays safe without its own None-guard.
    """

    lifecycle: dict[str, Any] = {}
    advisory: dict[str, Any] | None = None
    for event in events:
        kind = _tail_row_kind(event)
        if kind == "advisory":
            advisory = event
        elif kind == "lifecycle":
            lifecycle = event
    return lifecycle, advisory


def _tail_row_kind(event: dict[str, Any]) -> str:
    """``skip`` (orchestrator advisory), ``advisory`` (runtime notice) or ``lifecycle``."""

    if str(event.get("event_type") or "") in ORCHESTRATOR_ADVISORY_EVENT_TYPES:
        return "skip"
    if str(event.get("event_kind") or "") == RUNTIME_NOTICE_EVENT_KIND:
        return "advisory"
    return "lifecycle"


def stated_lifecycle_tail(
    events: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """``split_lifecycle_tail``'s lifecycle row, with a state whenever one exists.

    NF-2026-02358: a lifecycle tail can end in a row that carries no ``state``
    (a small delta row), and a status reader that took ``state`` from it
    reported ``null``. The tail row stays authoritative for every other field;
    only a missing/blank ``state`` is filled from the LAST lifecycle row that
    has one. Returns ``(row, source)`` where source is ``lifecycle_tail``,
    ``last_stated_lifecycle_row`` or ``absent`` (no lifecycle row has a state;
    the caller must derive one from elsewhere, never report null).
    """

    rows = list(events)
    tail, _advisory = split_lifecycle_tail(rows)
    if str(tail.get("state") or "").strip():
        return tail, "lifecycle_tail"
    for event in reversed(rows):
        if _tail_row_kind(event) != "lifecycle":
            continue
        state = str(event.get("state") or "").strip()
        if state:
            return {**tail, "state": state}, "last_stated_lifecycle_row"
    return tail, "absent"


def zero_delta_elapsed_share() -> float:
    raw = os.environ.get(ZERO_DELTA_ELAPSED_SHARE_ENV)
    if raw is None or not str(raw).strip():
        return ZERO_DELTA_DEFAULT_ELAPSED_SHARE
    try:
        share = float(str(raw).strip())
    except ValueError:
        return ZERO_DELTA_DEFAULT_ELAPSED_SHARE
    if not 0.0 < share <= 1.0:
        return ZERO_DELTA_DEFAULT_ELAPSED_SHARE
    return share


def zero_delta_notice_after_seconds(timeout_seconds: Any) -> float:
    try:
        ceiling = float(int(timeout_seconds))
    except (TypeError, ValueError):
        ceiling = 0.0
    scaled = max(0.0, ceiling) * zero_delta_elapsed_share()
    return min(ZERO_DELTA_MAX_SECONDS, max(ZERO_DELTA_MIN_SECONDS, scaled))


def zero_delta_enforce_after_seconds(timeout_seconds: Any) -> float:
    notice_after = zero_delta_notice_after_seconds(timeout_seconds)
    raw = os.environ.get(ZERO_DELTA_ENFORCE_SECONDS_ENV)
    configured = ZERO_DELTA_DEFAULT_ENFORCE_SECONDS
    if raw is not None and str(raw).strip():
        try:
            value = float(str(raw).strip())
        except ValueError:
            value = 0.0
        if math.isfinite(value) and value > 0.0:
            configured = value
    return max(notice_after, configured)


def changed_allowed_write_paths(workspace: WorkerWorkspace) -> list[str]:
    """Name allowed-write paths already differing from their baseline."""

    changed: list[str] = []
    for raw in workspace.allowed_writes:
        pattern = str(raw or "").strip().replace("\\", "/")
        if not pattern:
            continue
        if any(ch in pattern for ch in "*?["):
            matches = sorted(workspace.path.glob(pattern))
        else:
            candidate = workspace.path / pattern
            matches = [candidate] if candidate.exists() else []
        if not matches:
            if workspace.workspace_baseline.get(pattern) is not None:
                changed.append(pattern)
            continue
        for path in matches:
            try:
                relative = path.relative_to(workspace.path).as_posix()
            except ValueError:
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    changed.append(relative)
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                current = f"file:{path.stat().st_mode & 0o777:o}:{digest}"
            except OSError:
                changed.append(relative)
                continue
            baseline = workspace.workspace_baseline.get(relative)
            if baseline is None or baseline not in {digest, current}:
                changed.append(relative)
    return sorted(set(changed))


@dataclass(frozen=True)
class ZeroDeltaTripwire:
    settled: bool
    notice: dict[str, Any] | None = None
    enforce: bool = False


def evaluate_zero_delta_tripwire(
    *,
    workspace: WorkerWorkspace,
    elapsed_seconds: float,
    timeout_seconds: Any,
    required_outputs: Iterable[str] = (),
    read_only: bool = False,
    allow_unchanged_required_outputs: Iterable[str] = (),
    notice_emitted: bool = False,
) -> ZeroDeltaTripwire:
    allowed_writes = [
        str(value) for value in workspace.allowed_writes if str(value or "").strip()
    ]
    if read_only or not allowed_writes:
        return ZeroDeltaTripwire(settled=True)
    if [str(value) for value in allow_unchanged_required_outputs]:
        return ZeroDeltaTripwire(settled=True)
    if changed_allowed_write_paths(workspace):
        return ZeroDeltaTripwire(settled=True)
    notice_after = zero_delta_notice_after_seconds(timeout_seconds)
    elapsed = float(elapsed_seconds)
    if elapsed < notice_after:
        return ZeroDeltaTripwire(settled=False)
    enforce_after = zero_delta_enforce_after_seconds(timeout_seconds)
    if notice_emitted and elapsed < enforce_after:
        return ZeroDeltaTripwire(settled=False)
    enforce = notice_emitted
    try:
        ceiling = float(int(timeout_seconds))
    except (TypeError, ValueError):
        ceiling = 0.0
    notice: dict[str, Any] = {
        "notice": ZERO_DELTA_NOTICE,
        "elapsed_seconds": round(elapsed, 3),
        "elapsed_share": round(elapsed / ceiling, 4) if ceiling > 0 else None,
        "notice_after_seconds": round(notice_after, 3),
        "enforce_after_seconds": round(enforce_after, 3),
        "timeout_seconds": int(ceiling),
        "required_outputs": [str(value) for value in required_outputs],
        "allowed_writes": allowed_writes,
        "changed_allowed_writes": [],
        "enforced": enforce,
    }
    if enforce:
        notice["reason"] = ZERO_DELTA_ENFORCE_REASON
    return ZeroDeltaTripwire(settled=enforce, notice=notice, enforce=enforce)
