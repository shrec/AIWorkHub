"""Non-blocking disk usage telemetry for the AIWorkHub dashboard.

Directory sizing can take minutes when hundreds of retained worker worktrees
exist.  Dashboard refreshes must therefore never perform that walk inline.
This module returns filesystem capacity immediately and refreshes managed-data
sizes in one daemon thread per repository, with a short cache lifetime.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
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

SCAN_TTL_SECONDS = 300.0
PERSISTED_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
PERSISTED_CACHE_MAX_BYTES = 8 * 1024 * 1024
PERSISTED_CACHE_SCHEMA = "aiworkhub.storage-observability-cache.v1"
PERSISTED_CACHE_RELATIVE_PATH = Path(
    ".aiworkhub/runtime/cache/storage-observability-v1.json"
)
PERSISTED_PAYLOAD_KEYS = frozenset({
    "scan_status", "scanned_at", "repo_data_bytes", "repo_data_files",
    "worker_tree_bytes", "worker_tree_count", "safe_reclaimable_bytes",
    "quarantine_bytes", "managed_total_bytes", "components",
    "retention_preview", "quarantine_batches", "terminal_log_retention",
    "terminal_log_quarantine_batches", "task_retention",
    "task_retention_batches", "storage_bounds", "errors",
})

_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}
_running: set[str] = set()
_loaded: set[str] = set()
_invalidated: set[str] = set()


def _repo_binding(repo_root: Path) -> str:
    return hashlib.sha256(os.fsencode(str(repo_root))).hexdigest()


def _load_persisted(repo_root: Path) -> dict[str, Any]:
    """Load a bounded, repository-bound last-known-good measurement.

    The cache is advisory telemetry, never authority.  Any malformed, oversized,
    symlinked, foreign-repository, or expired file is ignored fail-closed.
    """
    path = repo_root / PERSISTED_CACHE_RELATIVE_PATH
    try:
        if path.is_symlink():
            return {}
        stat = path.stat()
        if stat.st_size <= 0 or stat.st_size > PERSISTED_CACHE_MAX_BYTES:
            return {}
        raw = path.read_bytes()
        envelope = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(envelope, dict):
        return {}
    if envelope.get("schema_id") != PERSISTED_CACHE_SCHEMA:
        return {}
    if envelope.get("repo_binding") != _repo_binding(repo_root):
        return {}
    completed_at = envelope.get("completed_at_epoch")
    payload = envelope.get("payload")
    if not isinstance(completed_at, (int, float)) or isinstance(completed_at, bool):
        return {}
    completed_at = float(completed_at)
    if not math.isfinite(completed_at):
        return {}
    age = max(0.0, time.time() - completed_at)
    if age > PERSISTED_CACHE_MAX_AGE_SECONDS or not isinstance(payload, dict):
        return {}
    if payload.get("scan_status") != "ready" or not isinstance(payload.get("errors"), list):
        return {}
    # Persisted telemetry can never override current disk capacity, readonly,
    # schema, or other response authority fields.
    loaded = {key: value for key, value in payload.items() if key in PERSISTED_PAYLOAD_KEYS}
    loaded["_completed_monotonic"] = time.monotonic() - age
    return loaded


def _persist(repo_root: Path, measured: dict[str, Any]) -> None:
    """Atomically persist a successful scan without exposing a partial cache."""
    # Uninitialised paths used by diagnostics/tests must remain observationally
    # read-only.  Canonical repositories always have this manifest.
    hub = repo_root / ".aiworkhub"
    runtime = hub / "runtime"
    cache_dir = runtime / "cache"
    if hub.is_symlink() or runtime.is_symlink() or cache_dir.is_symlink():
        return
    if not (hub / "project.json").is_file():
        return
    path = repo_root / PERSISTED_CACHE_RELATIVE_PATH
    parent = path.parent
    temp: Path | None = None
    try:
        payload = {
            key: value for key, value in measured.items() if not key.startswith("_")
        }
        envelope = {
            "schema_id": PERSISTED_CACHE_SCHEMA,
            "repo_binding": _repo_binding(repo_root),
            "completed_at_epoch": time.time(),
            "payload": payload,
        }
        encoded = json.dumps(
            envelope, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if len(encoded) > PERSISTED_CACHE_MAX_BYTES:
            return
        parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
        temp = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
        temp = None
    except (OSError, TypeError, ValueError, OverflowError):
        return
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def _tree_size(path: Path, *, exclude: tuple[Path, ...] = ()) -> tuple[int, int]:
    """Return apparent bytes and file count without following symlinks.

    Exact excluded directories are pruned before descent.  The managed
    worktree root lives below ``.aiworkhub/runtime`` and is inventoried by the
    retention scanner, so pruning it here prevents both a second multi-GB walk
    and double-counting its bytes as repository data and worker data.
    """
    total = 0
    files = 0
    if not path.is_dir():
        return total, files
    excluded = {
        os.path.normcase(os.path.abspath(str(item)))
        for item in exclude
    }
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if os.path.normcase(os.path.abspath(entry.path)) in excluded:
                                continue
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
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
                "storage_retention.recover_stranded_worktrees() -- exposed as the "
                "aiworkhub_storage_recover_stranded_worktrees MCP tool -- re-registers "
                "or reclaims stranded worktrees the automatic path cannot attribute"
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


def _measure_components(
    repo_root: Path, worktree_base: Path
) -> tuple[int, int, list[dict[str, Any]]]:
    repo_bytes = 0
    repo_files = 0
    components: list[dict[str, Any]] = []
    hub = repo_root / ".aiworkhub"
    if not hub.is_dir():
        return repo_bytes, repo_files, components
    for entry in sorted(hub.iterdir(), key=lambda item: item.name):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            size, files = _tree_size(entry, exclude=(worktree_base,))
        elif entry.is_file():
            try:
                size, files = entry.stat().st_size, 1
            except OSError:
                continue
        else:
            continue
        repo_bytes += size
        repo_files += files
        components.append({"id": entry.name, "bytes": size, "files": files})
    return repo_bytes, repo_files, components


def _measure(repo_root: Path) -> dict[str, Any]:
    worktree_base = storage_retention.configured_worktree_root(repo_root).resolve()
    # These walks share one physical filesystem. A bounded thread pool was
    # measured on the live 19.9 GB store and regressed 4.63 s -> 5.89 s from
    # metadata-queue/GIL contention, so this path is deliberately sequential.
    repo_bytes, repo_files, components = _measure_components(repo_root, worktree_base)
    footprint = storage_retention.repo_storage_footprint(repo_root)
    registrations = worktree_storage.scan_worktree_registrations(repo_root)
    try:
        quarantine = storage_retention.list_batches(repo_root)
        quarantine_batches = quarantine.get("batches") or []
    except storage_retention.StorageRetentionError:
        quarantine_batches = []
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

    worktrees = footprint["scan"]
    observed_total_bytes = int(footprint["observed_total_bytes"])
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
    quarantine_bytes = sum(int(item.get("bytes") or 0) for item in quarantine_batches)
    pinned_predecessor_bytes = int(cleanup.get("pinned_predecessor_bytes") or 0)
    return {
        "scan_status": "ready",
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "repo_data_bytes": repo_bytes,
        "repo_data_files": repo_files,
        "worker_tree_bytes": worker_bytes,
        "worker_tree_count": int(summary.get("count") or 0),
        "safe_reclaimable_bytes": safe_reclaim_bytes,
        "quarantine_bytes": quarantine_bytes,
        # The worktree base is physically nested under ``.aiworkhub/runtime``.
        # ``repo_bytes`` deliberately prunes it, then the global worktree total
        # is added exactly once.  ``worker_tree_bytes`` remains repository-scoped
        # for ownership reporting, while managed bytes truthfully cover the full
        # on-disk tree (including unattributed/foreign retained worktrees).
        "managed_total_bytes": (
            repo_bytes
            + int(footprint.get("global_worktree_bytes") or worker_bytes)
            + quarantine_bytes
        ),
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
    if measured.get("scan_status") == "ready":
        _persist(repo_root, measured)
    with _lock:
        _cache[key] = measured
        _running.discard(key)
        _invalidated.discard(key)


def snapshot(repo_root: Path | str) -> dict[str, Any]:
    """Return bounded disk telemetry and schedule stale managed-size scans.

    The returned object contains no absolute paths. ``disk_*`` capacity is
    current; managed byte counts are cached for at most
    :data:`SCAN_TTL_SECONDS`. A repository-bound last-known-good telemetry cache
    survives runtime reloads, while its refresh remains off the request thread.
    """
    root = Path(repo_root).resolve()
    key = str(root)
    with _lock:
        should_load = key not in _loaded
        if should_load:
            _loaded.add(key)
    if should_load:
        persisted = _load_persisted(root)
        if persisted:
            with _lock:
                # A refresh that completed while the cache was being read is newer.
                _cache.setdefault(key, persisted)
    disk: dict[str, Any]
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
        stale = (
            not completed
            or now - completed >= SCAN_TTL_SECONDS
            or key in _invalidated
        )
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
        _loaded.clear()
        _invalidated.clear()


def invalidate(repo_root: Path | str) -> None:
    """Refresh one repository after a retention write without blanking the UI."""
    key = str(Path(repo_root).resolve())
    with _lock:
        _invalidated.add(key)
        _loaded.discard(key)
