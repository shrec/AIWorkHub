"""Non-blocking disk usage telemetry for the AIWorkHub dashboard.

Directory sizing can take minutes when hundreds of retained worker worktrees
exist.  Dashboard refreshes must therefore never perform that walk inline.
This module returns filesystem capacity immediately and refreshes managed-data
sizes in one daemon thread per repository, with a short cache lifetime.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    repo_policy,
    storage_retention,
    task_retention,
    task_store,
    terminal_log_retention,
    worktree_storage,
)

SCAN_TTL_SECONDS = 60.0

_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}
_running: set[str] = set()


def _tree_size(path: Path) -> tuple[int, int]:
    """Return apparent bytes and file count without following symlinks."""
    total = 0
    files = 0
    if not path.is_dir():
        return total, files
    for root, _dirs, names in os.walk(path, followlinks=False):
        for name in names:
            candidate = Path(root) / name
            try:
                if candidate.is_symlink():
                    continue
                total += candidate.stat().st_size
                files += 1
            except OSError:
                continue
    return total, files


def _storage_bounds(
    *,
    pinned_predecessor_bytes: int,
    worktree_current_bytes: int,
    terminal_unclaimed_count: int,
    terminal_unclaimed_bytes: int,
    terminal_empty_eligible_count: int,
) -> dict[str, Any]:
    """The report every accumulating store must answer: what bounds it, what
    happens when the bound is reached, and what an operator does when the
    automatic path cannot act. A store that cannot answer all three is
    unbounded -- and unbounded is what put 31 GB on this disk.
    """
    return {
        "worktrees": {
            "bounds": "retention age (terminal_runs_days) and the worktree_max_bytes cap",
            "at_bound": (
                "aged, unprotected, provably-owned worktrees enter reversible "
                "quarantine; over the cap, the oldest superseded ones are forced first"
            ),
            "operator_action": (
                "storage_retention.recover_stranded_worktrees() re-registers or "
                "reclaims stranded worktrees the automatic path cannot attribute"
            ),
            # In-flight rework lineage (NF-2026-00286 owns the 989 half-archived
            # rows) is pinned and never evicted; once those pins release the pinned
            # bytes become reclaimable, so the worktree footprint falls to this.
            "pinned_predecessor_bytes": int(pinned_predecessor_bytes),
            "projected_bytes_after_pins_release": max(
                0, int(worktree_current_bytes) - int(pinned_predecessor_bytes)
            ),
        },
        "terminal_log_quarantine": {
            "bounds": "7-day undo window per batch, then purge",
            "at_bound": (
                "an expired batch is purged by enforce; an empty batch is surfaced "
                "purge_eligible for the operator; a record-empty batch still holding "
                "bytes is flagged unclaimed and keeps its window"
            ),
            "operator_action": (
                "terminal_log_retention.purge_empty_batches() collects the empty "
                "batches; terminal_log_retention.reconcile_unclaimed() adopts "
                "directories no record claims into a bounded batch"
            ),
            "unclaimed_batch_count": int(terminal_unclaimed_count),
            "unclaimed_bytes": int(terminal_unclaimed_bytes),
            "empty_purge_eligible_count": int(terminal_empty_eligible_count),
        },
        "process_logs": {
            "bounds": (
                f"{terminal_log_retention.MAX_PROCESS_LOG_FILE_BYTES} bytes per worker "
                "log file, plus age-based quarantine of the per-request set"
            ),
            "at_bound": (
                "a terminal run's oversized log is tail-capped (head released, "
                "diagnostic tail kept); a live run is never touched"
            ),
            "operator_action": (
                "terminal_log_retention.enforce_process_log_bounds() runs the bound "
                "on demand; a still-live oversized log clears once its run ends"
            ),
        },
        "attempt_artifacts": {
            "bounds": "retention age (logs_days) once the owning run is terminal",
            "at_bound": "the aged bundle is moved into a reversible quarantine batch",
            "operator_action": (
                "terminal_log_retention.enforce_process_log_bounds(); a bundle whose "
                "run is not terminal is protected until the run ends"
            ),
        },
        "runtime_generations": {
            # globalStorage-scoped (outside this repository's .aiworkhub), so it is
            # bounded by the VS Code extension's operator-invoked action, not here.
            "bounds": "current + latest three generations kept; older ones are obsolete",
            "at_bound": "obsolete lease-free generations become quarantine-eligible",
            "operator_action": (
                "the dashboard 'Quarantine Runtimes' action (runRuntimeCleanup) moves "
                "them into 7-day quarantine; it is operator-invoked because a live "
                "window lease can still pin a generation and consent proves it is free"
            ),
        },
    }


def _measure(repo_root: Path) -> dict[str, Any]:
    repo_bytes, repo_files = _tree_size(repo_root / ".aiworkhub")
    components: list[dict[str, Any]] = []
    hub = repo_root / ".aiworkhub"
    if hub.is_dir():
        for entry in sorted(hub.iterdir(), key=lambda item: item.name):
            if entry.is_symlink():
                continue
            if entry.is_dir():
                size, files = _tree_size(entry)
            elif entry.is_file():
                try:
                    size, files = entry.stat().st_size, 1
                except OSError:
                    continue
            else:
                continue
            components.append({"id": entry.name, "bytes": size, "files": files})
    footprint = storage_retention.repo_storage_footprint(repo_root)
    worktrees = footprint["scan"]
    observed_total_bytes = int(footprint["observed_total_bytes"])
    registrations = worktree_storage.scan_worktree_registrations(repo_root)
    summary = worktrees.get("summary") or {}
    worker_bytes = int(summary.get("total_bytes") or 0)
    try:
        policy = repo_policy.load_policy(repo_root)
        min_age_days = int(policy["retention"]["terminal_runs_days"])
        worktree_max_bytes = int(policy["retention"]["worktree_max_bytes"])
    except (KeyError, TypeError, ValueError, repo_policy.RepoPolicyError):
        min_age_days = int(repo_policy.DEFAULT_POLICY["retention"]["terminal_runs_days"])
        worktree_max_bytes = int(repo_policy.DEFAULT_POLICY["retention"]["worktree_max_bytes"])
    if isinstance(worktrees.get("worktrees"), list) and worktrees.get("base"):
        cleanup = storage_retention.plan_worktree_reclaim(
            repo_root,
            worktrees,
            min_age_days=min_age_days,
            max_bytes=worktree_max_bytes,
            current_bytes=observed_total_bytes,
        )
        # "Safe" keeps its original clean-and-fully-pushed meaning; the newer,
        # broader lineage-verified reclaim total (which may include dirty/
        # unpushed superseded attempts) is reported separately as eligible_bytes.
        safe_reclaim_bytes = sum(
            int(wt.get("size_bytes") or 0)
            for wt in cleanup.get("would_remove") or []
            if wt.get("class") == worktree_storage.CLASS_REMOVABLE_SAFE
        )
    else:
        cleanup = {
            "would_remove": [],
            "would_keep": [],
            "reclaim_bytes": int(summary.get("removable_safe_bytes") or 0),
        }
        safe_reclaim_bytes = int(summary.get("removable_safe_bytes") or 0)
    try:
        quarantine = storage_retention.list_batches(repo_root)
        quarantine_batches = quarantine.get("batches") or []
    except storage_retention.StorageRetentionError:
        quarantine_batches = []
    quarantine_bytes = sum(int(item.get("bytes") or 0) for item in quarantine_batches)
    pinned_predecessor_bytes = int(cleanup.get("pinned_predecessor_bytes") or 0)
    try:
        terminal_logs = terminal_log_retention.preview(repo_root, include_candidates=False)
        terminal_log_batches = terminal_log_retention.list_batches(repo_root).get("batches") or []
    except terminal_log_retention.TerminalLogRetentionError as exc:
        terminal_logs = {
            "ok": False,
            "error": str(exc)[:160],
            "current_bytes": 0,
            "candidate_count": 0,
            "candidate_bytes": 0,
            "protected_count": 0,
            "projected_bytes": 0,
        }
        terminal_log_batches = []
    try:
        archived_tasks = task_retention.preview(repo_root)
        task_retention_batches = task_retention.list_batches(repo_root).get("batches") or []
    except (task_retention.TaskRetentionError, task_store.TaskStoreError) as exc:
        archived_tasks = {
            "ok": False,
            "error": str(exc)[:160],
            "candidate_count": 0,
            "candidate_total": 0,
            "archived_total": 0,
            "protected_callback_count": 0,
        }
        task_retention_batches = []
    return {
        "scan_status": "ready",
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "repo_data_bytes": repo_bytes,
        "repo_data_files": repo_files,
        "worker_tree_bytes": worker_bytes,
        "worker_tree_count": int(summary.get("count") or 0),
        "safe_reclaimable_bytes": safe_reclaim_bytes,
        "quarantine_bytes": quarantine_bytes,
        "managed_total_bytes": repo_bytes + worker_bytes + quarantine_bytes,
        "components": components,
        "retention_preview": {
            "policy_days": min_age_days,
            "max_bytes": worktree_max_bytes,
            "current_bytes": observed_total_bytes,
            "current_bytes_definition": "observed_total_bytes",
            "projected_bytes": max(0, observed_total_bytes - int(cleanup.get("reclaim_bytes") or 0)),
            "over_limit_bytes": max(0, observed_total_bytes - worktree_max_bytes),
            "eligible_count": len(cleanup.get("would_remove") or []),
            "protected_count": len(cleanup.get("would_keep") or []),
            "eligible_bytes": int(cleanup.get("reclaim_bytes") or 0),
            "dry_run": True,
            "repository_scoped": True,
            "orphaned_excluded": True,
            "registrations": registrations,
            "footprint": {
                key: value for key, value in footprint.items() if key not in ("base", "scan")
            },
            # Surface a material unattributed/foreign footprint prominently so the
            # dashboard cannot read as clean while gigabytes sit stranded outside
            # every reclamation path (see storage_retention.recover_stranded_worktrees).
            "unattributed_alert": storage_retention._unattributed_alert(footprint),
        },
        "quarantine_batches": quarantine_batches,
        "terminal_log_retention": terminal_logs,
        "terminal_log_quarantine_batches": terminal_log_batches,
        "task_retention": archived_tasks,
        "task_retention_batches": task_retention_batches,
        # Every accumulating store's bound, what happens at the bound, and the
        # operator action when the automatic path cannot act (see _storage_bounds).
        "storage_bounds": _storage_bounds(
            pinned_predecessor_bytes=pinned_predecessor_bytes,
            worktree_current_bytes=worker_bytes,
            terminal_unclaimed_count=sum(
                1 for item in terminal_log_batches if item.get("unclaimed")
            ),
            terminal_unclaimed_bytes=sum(
                int(item.get("on_disk_bytes") or 0)
                for item in terminal_log_batches
                if item.get("unclaimed")
            ),
            # Count exactly what ``purge_empty_batches`` -- the trigger named
            # beside this figure -- will actually reap: batches empty in BOTH
            # record and on disk (``reapable_empty``). Summing ``purge_eligible``
            # instead would also count expired-but-still-full batches that the
            # named collector never takes, promising an operator more than it
            # drains -- the false-clean shape this release has been removing.
            terminal_empty_eligible_count=sum(
                1 for item in terminal_log_batches if item.get("reapable_empty")
            ),
        ),
        "errors": [],
    }


def _refresh(key: str, repo_root: Path) -> None:
    try:
        measured = _measure(repo_root)
    except Exception as exc:  # noqa: BLE001 -- telemetry must never break the dashboard
        measured = {
            "scan_status": "error",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "errors": [f"storage_scan_failed:{type(exc).__name__}"],
        }
    measured["_completed_monotonic"] = time.monotonic()
    with _lock:
        _cache[key] = measured
        _running.discard(key)


def snapshot(repo_root: Path | str) -> dict[str, Any]:
    """Return bounded disk telemetry and schedule stale managed-size scans.

    The returned object contains no absolute paths and performs no mutation.
    ``disk_*`` capacity is current; managed byte counts are cached for at most
    :data:`SCAN_TTL_SECONDS` and show ``scan_status=scanning`` until ready.
    """
    root = Path(repo_root).resolve()
    key = str(root)
    try:
        usage = shutil.disk_usage(root)
        disk = {
            "disk_total_bytes": int(usage.total),
            "disk_used_bytes": int(usage.used),
            "disk_free_bytes": int(usage.free),
            "disk_used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0.0,
        }
    except OSError as exc:
        disk = {
            "disk_total_bytes": 0,
            "disk_used_bytes": 0,
            "disk_free_bytes": 0,
            "disk_used_percent": 0.0,
            "disk_error": f"disk_usage_failed:{type(exc).__name__}",
        }

    now = time.monotonic()
    with _lock:
        cached = dict(_cache.get(key) or {})
        completed = float(cached.get("_completed_monotonic") or 0.0)
        stale = not completed or now - completed >= SCAN_TTL_SECONDS
        if stale and key not in _running:
            _running.add(key)
            threading.Thread(
                target=_refresh,
                args=(key, root),
                name=f"aiworkhub-storage-{abs(hash(key)) & 0xffff:x}",
                daemon=True,
            ).start()
        running = key in _running

    cached.pop("_completed_monotonic", None)
    if not cached:
        cached = {
            "scan_status": "scanning" if running else "unavailable",
            "scanned_at": "",
            "repo_data_bytes": 0,
            "repo_data_files": 0,
            "worker_tree_bytes": 0,
            "worker_tree_count": 0,
            "quarantine_bytes": 0,
            "safe_reclaimable_bytes": 0,
            "managed_total_bytes": 0,
            "components": [],
            "retention_preview": {
                "policy_days": 0,
                "max_bytes": 0,
                "current_bytes": 0,
                "projected_bytes": 0,
                "over_limit_bytes": 0,
                "eligible_count": 0,
                "protected_count": 0,
                "eligible_bytes": 0,
                "dry_run": True,
                "repository_scoped": True,
                "orphaned_excluded": True,
            },
            "quarantine_batches": [],
            "terminal_log_retention": {
                "ok": True,
                "dry_run": True,
                "current_bytes": 0,
                "candidate_count": 0,
                "candidate_bytes": 0,
                "protected_count": 0,
                "projected_bytes": 0,
            },
            "terminal_log_quarantine_batches": [],
            "task_retention": {
                "ok": True,
                "dry_run": True,
                "candidate_count": 0,
                "candidate_total": 0,
                "archived_total": 0,
                "protected_callback_count": 0,
            },
            "task_retention_batches": [],
            "errors": [],
        }
    elif running:
        cached["scan_status"] = "refreshing"
    return {"schema_version": 1, "readonly": True, **disk, **cached}


def _reset_cache_for_tests() -> None:
    with _lock:
        _cache.clear()
        _running.clear()


def invalidate(repo_root: Path | str) -> None:
    """Drop one repository's completed size snapshot after a retention write."""
    key = str(Path(repo_root).resolve())
    with _lock:
        _cache.pop(key, None)
