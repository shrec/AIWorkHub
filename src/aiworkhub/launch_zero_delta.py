"""In-run zero-delta observation for isolated worker launches."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Iterable

from .worker_workspace import WorkerWorkspace


ZERO_DELTA_NOTICE = "zero_required_output_delta_warning"
RUNTIME_NOTICE_EVENT_KIND = "runtime_notice"
ZERO_DELTA_ELAPSED_SHARE_ENV = "AIWORKHUB_ZERO_DELTA_ELAPSED_SHARE"
ZERO_DELTA_DEFAULT_ELAPSED_SHARE = 0.5
ZERO_DELTA_MIN_SECONDS = 60.0
ZERO_DELTA_MAX_SECONDS = 600.0
ZERO_DELTA_POLL_SECONDS = 15.0


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


def evaluate_zero_delta_tripwire(
    *,
    workspace: WorkerWorkspace,
    elapsed_seconds: float,
    timeout_seconds: Any,
    required_outputs: Iterable[str] = (),
    read_only: bool = False,
    allow_unchanged_required_outputs: Iterable[str] = (),
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
    try:
        ceiling = float(int(timeout_seconds))
    except (TypeError, ValueError):
        ceiling = 0.0
    return ZeroDeltaTripwire(
        settled=True,
        notice={
            "notice": ZERO_DELTA_NOTICE,
            "elapsed_seconds": round(elapsed, 3),
            "elapsed_share": round(elapsed / ceiling, 4) if ceiling > 0 else None,
            "notice_after_seconds": round(notice_after, 3),
            "timeout_seconds": int(ceiling),
            "required_outputs": [str(value) for value in required_outputs],
            "allowed_writes": allowed_writes,
            "changed_allowed_writes": [],
            "enforced": False,
        },
    )
