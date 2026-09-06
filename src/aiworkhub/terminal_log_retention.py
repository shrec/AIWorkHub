"""Repository-bound retention for terminal worker output files.

The append-only process event ledger is canonical evidence; its bounded active
file and immutable rotations are never cleanup candidates here. Only the four
per-request files owned by a terminal run
may enter quarantine, and only after the task store independently confirms the
task itself is finished or archived.  Active, review, blocked, pending,
unknown and recent runs fail closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import (
    process_event_ledger,
    provider_usage,
    repo_policy,
    runtime_temp,
    task_fsm,
    task_store,
)


SCHEMA_ID = "aiworkhub.terminal_log_retention.v1"
AUDIT_SCHEMA_ID = "aiworkhub.terminal_log_retention_audit.v1"
PROCESS_LOG_RELATIVE_PATH = Path(".aiworkhub/runtime/process_logs/process_events.jsonl")
PROCESS_FILES_RELATIVE_PATH = Path(".aiworkhub/runtime/process_logs/processes")
QUARANTINE_RELATIVE_PATH = Path(".aiworkhub/runtime/storage/terminal-log-quarantine")
AUDIT_RELATIVE_PATH = Path(".aiworkhub/runtime/storage/terminal-log-retention.audit.jsonl")
LEGACY_PROCESS_LOG_RELATIVE_PATH = Path("logs/process_events.jsonl")
LEGACY_PROCESS_FILES_RELATIVE_PATH = Path("logs/processes")
MANIFEST_NAME = "manifest.json"
UNDO_DAYS = 7
# Completed task output is retained by age, not forever by per-task position.
# The canonical append-only event ledger remains available after these bounded
# stdout/stderr/request artifacts enter quarantine.  Active and review tasks
# still fail closed below, so removing this historical keep-last exemption does
# not discard evidence that a manager has not adjudicated yet.
KEEP_LAST_PER_TASK = 0
# Compatibility constant for callers/tests.  Writers rotate at 48 MiB and the
# reader streams every immutable segment, so this is no longer a failure cap.
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
DEFAULT_PREVIEW_LIMIT = 50
MAX_PREVIEW_LIMIT = 200
# Directory a launcher writes one per-request validation/attempt bundle into,
# nested inside the same processes root as the owned per-request log files (see
# process_launcher: ``process_dir / "attempt-artifacts" / request_id``). It had
# no retention in any module; it is bounded here by the same terminal+age gate
# the per-request logs use.
ATTEMPT_ARTIFACTS_DIRNAME = "attempt-artifacts"
# Per-file ceiling for an individual worker stdout/stderr log. The writer
# (process_launcher) applies no size cap, and single runs have been observed at
# 26 MiB, so a directory nothing prunes grows without bound one file at a time.
# This is the retention-side bound: once a run has reached a terminal state, an
# oversized log is tail-capped to its last ``MAX_PROCESS_LOG_FILE_BYTES`` -- the
# exact window the launcher itself reads to diagnose a failure (``_safe_tail``),
# so the tail an operator needs is always kept while the unbounded head is
# released. A live or non-terminal run is never touched (fail closed).
MAX_PROCESS_LOG_FILE_BYTES = 4 * 1024 * 1024
# Written verbatim at the head of a tail-capped file so the truncation is
# explicit and the remaining bytes can never be mistaken for the whole run.
_TAIL_CAP_NOTICE = (
    b"[aiworkhub-retention] earlier output released by the per-file log bound; "
    b"the diagnostic tail is preserved below.\n"
)
_BOUNDABLE_LOG_SUFFIXES = (".stdout.log", ".stderr.log")

_REQUEST_RE = re.compile(r"^[a-f0-9]{32}$")
_BATCH_RE = re.compile(r"^l[0-9]{8}T[0-9]{6}-[a-f0-9]{12}$")
_OWNED_SUFFIXES = (".request.json", ".stderr.log", ".stdout.log", ".supervisor.json")
_enforcement_lock = threading.Lock()
# A terminal provider outcome can later gain a manager disposition in the same
# process ledger.  The latter becomes the latest row, so omitting it made every
# accepted run look live forever and exempted exactly the successful, finished
# tasks from both the 4 MiB stream cap and age retention.  Reuse the canonical
# launcher vocabulary and add only the three post-launch disposition states.
_TERMINAL_STATES = frozenset(
    task_fsm.LAUNCHER_TERMINAL_SUBSTATUSES | {"accepted", "rejected", "archived"}
)


class TerminalLogRetentionError(RuntimeError):
    pass


def _default_now_utc() -> datetime:
    return datetime.now(timezone.utc)


# Injectable reference clock for retention-age/undo-window decisions. Tests
# monkeypatch ``now_utc`` (or reassign ``_now_func``) to express age without
# mutating filesystem mtimes. Production uses the real UTC clock.
_now_func: Callable[[], datetime] = _default_now_utc


def now_utc() -> datetime:
    """Current UTC time via the injectable clock."""
    return _now_func()


def _repo_id(root: Path) -> str:
    readiness = task_store.storage_readiness(root)
    if not readiness.ready or not readiness.repo_id:
        raise TerminalLogRetentionError(f"repository_storage_not_ready:{readiness.reason}")
    return readiness.repo_id


def _logs_days(root: Path) -> int:
    try:
        return int(repo_policy.load_policy(root)["retention"]["logs_days"])
    except (KeyError, TypeError, ValueError, repo_policy.RepoPolicyError):
        return int(repo_policy.DEFAULT_POLICY["retention"]["logs_days"])


def _owned_regular_file(path: Path, parent: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if path.parent != parent or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return info


def _legacy_store_identity(root: Path) -> dict[str, int]:
    """Return a bounded identity for the fixed legacy ``logs/`` tree."""

    legacy_root = root / Path("logs")
    try:
        info = legacy_root.lstat()
    except OSError:
        return {"file_count": 0, "size_bytes": 0, "newest_mtime_ns": 0}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return {"file_count": 0, "size_bytes": 0, "newest_mtime_ns": 0}
    file_count = 0
    size_bytes = 0
    newest_mtime_ns = 0
    for directory, dirnames, filenames in os.walk(legacy_root, followlinks=False):
        parent = Path(directory)
        dirnames[:] = [
            name for name in dirnames if not (parent / name).is_symlink()
        ]
        for name in filenames:
            path = parent / name
            try:
                item = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
                continue
            file_count += 1
            size_bytes += int(item.st_size)
            newest_mtime_ns = max(newest_mtime_ns, int(item.st_mtime_ns))
    return {
        "file_count": file_count,
        "size_bytes": size_bytes,
        "newest_mtime_ns": newest_mtime_ns,
    }


def _latest_rows(root: Path) -> dict[str, dict[str, Any]]:
    ledger = root / PROCESS_LOG_RELATIVE_PATH
    try:
        info = ledger.lstat()
    except FileNotFoundError:
        if not process_event_ledger.ledger_paths(ledger):
            return {}
        info = None
    except OSError as exc:
        raise TerminalLogRetentionError("terminal_log_ledger_unavailable") from exc
    if info is not None and (
        stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
    ):
        raise TerminalLogRetentionError("terminal_log_ledger_invalid")
    latest: dict[str, dict[str, Any]] = {}
    for request_id, row in process_event_ledger.latest_events(ledger).items():
        if not _REQUEST_RE.fullmatch(request_id):
            continue
        latest[request_id] = row
    return latest


def _usage_capture_request_ids(root: Path) -> set[str]:
    result: set[str] = set()
    try:
        events = task_store.list_usage_events(root, limit=50_000)
    except task_store.TaskStoreError:
        # Storage telemetry is also used before a repository has been
        # initialized.  No task store means no durable capture receipts; it is
        # not a dashboard scan failure.
        return result
    for event in events:
        note = str(event.get("note") or "")
        if note.startswith("task_mcp_request:"):
            request_id = note.removeprefix("task_mcp_request:")
            if _REQUEST_RE.fullmatch(request_id):
                result.add(request_id)
    return result


def _ledger_token_counts(usage: Mapping[str, Any], adapter_id: str) -> tuple[int, int]:
    input_tokens = int(usage.get("input_tokens") or 0)
    if adapter_id == "claude_cli":
        input_tokens += int(usage.get("cached_input_tokens") or 0)
        input_tokens += int(usage.get("cache_creation_input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0) + int(
        usage.get("reasoning_output_tokens") or 0
    )
    return input_tokens, output_tokens


def backfill_usage_capture(repo_root: Path | str, *, confirm: bool) -> dict[str, Any]:
    """Idempotently recover usage receipts from retained provider output.

    No token or cost is estimated. A structured provider report is normalized;
    otherwise an explicit ``provider_usage_report_not_observed`` receipt is
    retained so cleanup can proceed without pretending the run was free.
    """

    if confirm is not True:
        raise TerminalLogRetentionError("explicit_confirmation_required")
    root = Path(repo_root).resolve()
    captured = _usage_capture_request_ids(root)
    latest = _latest_rows(root)
    recorded = 0
    already_recorded = 0
    protected = 0
    errors: list[dict[str, str]] = []
    process_root = root / PROCESS_FILES_RELATIVE_PATH
    for request_id, row in sorted(latest.items()):
        if request_id in captured:
            already_recorded += 1
            continue
        task_id = str(row.get("task_id") or "")
        runner = str(row.get("runner") or "")
        if not task_id or not runner or str(row.get("state") or "") not in _TERMINAL_STATES:
            protected += 1
            continue
        stdout_path = process_root / f"{request_id}.stdout.log"
        info = _owned_regular_file(stdout_path, process_root)
        if info is None:
            protected += 1
            errors.append({"request_id": request_id, "reason": "stdout_unavailable"})
            continue
        usage = provider_usage.read_provider_usage(stdout_path, include_samples=True)
        adapter_id = str(row.get("adapter_id") or "")
        input_tokens, output_tokens = _ledger_token_counts(usage, adapter_id)
        observed_model = str(usage.get("observed_model") or "")
        payload = {
            "runner": runner,
            "topic": str(row.get("topic") or ""),
            "model": observed_model or str(row.get("model") or adapter_id),
            "requested_model": str(row.get("model") or adapter_id),
            "observed_model": observed_model,
            "model_observed": bool(usage.get("model_observed")),
            "role": "worker",
            "provider": adapter_id.removesuffix("_cli"),
            "source": "terminal_log_backfill",
            "note": f"task_mcp_request:{request_id}",
            "records": 1,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "visible_output_tokens": int(usage.get("output_tokens") or 0),
            "reasoning_output_tokens": int(usage.get("reasoning_output_tokens") or 0),
            "total_tokens": input_tokens + output_tokens,
            "cached_input_tokens": int(usage.get("cached_input_tokens") or 0),
            "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            "cache_write_input_tokens": int(usage.get("cache_write_input_tokens") or 0),
            "cache_metrics_observed": bool(usage.get("cache_metrics_observed")),
            "usage_observed": bool(usage.get("usage_observed")),
            "telemetry_reason": (
                "" if usage.get("usage_observed") else "provider_usage_report_not_observed"
            ),
            "cost_usd": float(usage.get("cost_usd") or 0.0),
            "cost_observed": bool(usage.get("cost_observed")),
        }
        ok, reason = task_store.append_usage_capture_event(
            root, task_id, runner, payload
        )
        if ok:
            recorded += int(reason == "recorded")
            already_recorded += int(reason == "already_recorded")
        else:
            protected += 1
            errors.append({"request_id": request_id, "reason": reason})
    return {
        "ok": not errors,
        "schema_id": "aiworkhub.provider_usage_backfill.v1",
        "recorded": recorded,
        "already_recorded": already_recorded,
        "protected": protected,
        "errors": errors[:100],
        "tokens_estimated": False,
    }


def _task_status_map(root: Path) -> dict[str, str]:
    """Return ``{task_id: status}`` for every task in one bounded query.

    ``_candidate_payload`` needs only each owning task's status to decide
    authority.  Calling :func:`task_store.get_task` once per terminal request was
    an N+1 query that reopened the (60 MB) task DB thousands of times: on the
    canonical repository that was 2121 lookups at ~26 ms each, ~55 s -- the
    entire cost of :func:`preview`.  :func:`task_store.list_tasks` returns the
    identical ``status`` for every task in a single query (verified byte-for-byte
    against ``get_task`` across 1416 task ids, zero mismatches), so the join runs
    in memory and the task population is read exactly once.

    A task id absent from the map is treated as ``"unknown"`` by the caller,
    matching :func:`task_store.get_task` returning ``None`` for a missing task, so
    a run whose task cannot be proved finished/archived still fails closed.
    """

    try:
        total = sum(task_store.exact_status_counts(root).values())
        # ``exact_status_counts`` and ``list_tasks`` read the same ``tasks`` table,
        # so ``total`` is the exact row count; the buffer absorbs any concurrent
        # insert between the two reads so a growing store never silently truncates
        # the map.
        rows = task_store.list_tasks(root, limit=total + 1024)
    except task_store.TaskStoreError:
        # Storage telemetry runs before a repository has been initialized too (the
        # dashboard measures a not-yet-ready store), and ``StorageNotReadyError``
        # is a ``TaskStoreError``.  No task store means no provable status, so
        # return an empty map: every task id falls to ``"unknown"`` in the caller
        # and its run stays protected (fail closed), exactly as
        # :func:`_usage_capture_request_ids` handles the same not-ready store and
        # as :func:`task_store.get_task` returning ``None`` for a missing task.
        # This must never escape as a bare ``TaskStoreError``: the enclosing
        # measurement classifies only :class:`TerminalLogRetentionError` as a
        # handled telemetry failure, so a raw one here turns a not-ready store into
        # a hard background-scan ``error`` instead of a clean empty result.
        return {}
    return {
        str(row.get("task_id") or ""): str(row.get("status") or "unknown")
        for row in rows
        if row.get("task_id")
    }


# Below this many per-request entries the directory stat runs single-threaded: a
# small walk finishes faster than a thread pool costs to start, so parallelism is
# never added where it would only be contention.  Measured warm on the canonical
# repository the whole 8320-file walk is 24 ms, so the sequential path already
# dominates there; the parallel path exists for the cold-cache / high-load
# operator machine the card describes, where each ``lstat`` blocks on disk I/O and
# the calls overlap across threads.
_PARALLEL_STAT_THRESHOLD = 512


def _scan_worker_count(path_count: int) -> int:
    """Threads for the I/O-bound directory stat, derived from the observed cores.

    ``lstat`` releases the GIL, so overlapping the per-file syscalls across
    threads is the right shape for this walk.  The count is derived from
    :func:`os.cpu_count` -- never a constant -- and never every core: two cores are
    always left free so a scan can never starve the interactive MCP server that
    shares this host or the dashboard's own request thread.  A walk below
    :data:`_PARALLEL_STAT_THRESHOLD` stays single-threaded.
    """
    if path_count < _PARALLEL_STAT_THRESHOLD:
        return 1
    cores = os.cpu_count() or 1
    # ``cores - 2`` leaves the interactive MCP server and the dashboard thread a
    # core each; capped at the path count so a just-over-threshold walk never
    # spawns idle workers.
    return max(1, min(path_count, cores - 2))


def _stat_owned_entries(
    entries: list[Path], process_root: Path
) -> list[tuple[str, dict[str, Any]]]:
    """Stat one contiguous slice of directory entries into ``(request_id, file)``.

    Pure and order-preserving: it reads only the filesystem and returns its slice
    in input order, so concatenating the per-worker results reproduces the exact
    sequential walk regardless of how the entries were partitioned.  Ownership is
    proved by :func:`_owned_regular_file` (same-parent regular file, never a
    symlink) exactly as the sequential walk did.
    """
    owned: list[tuple[str, dict[str, Any]]] = []
    for path in entries:
        info = _owned_regular_file(path, process_root)
        if info is None:
            continue
        request_id = next(
            (
                path.name[: -len(suffix)]
                for suffix in _OWNED_SUFFIXES
                if path.name.endswith(suffix)
            ),
            "",
        )
        if not _REQUEST_RE.fullmatch(request_id):
            continue
        owned.append((request_id, {
            "name": path.name,
            "size_bytes": int(info.st_size),
            "mtime_ns": int(info.st_mtime_ns),
        }))
    return owned


def _build_inventory(
    process_root: Path, *, workers: int | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Group owned per-request files by request id, optionally in parallel.

    The result is byte-for-byte identical to the sequential walk: workers each
    process a contiguous slice and the slices are recombined in order, so both the
    set of request ids and the per-request file order match exactly what one
    thread iterating :func:`Path.iterdir` would produce.  Only *how fast* the stat
    runs changes, never *what* it measures.  ``workers`` overrides the derived
    count (tests pin the parallel and sequential paths against each other); by
    default the count comes from :func:`_scan_worker_count`.
    """
    inventory: dict[str, list[dict[str, Any]]] = {}
    if not process_root.is_dir():
        return inventory
    entries = list(process_root.iterdir())
    total = len(entries)
    count = _scan_worker_count(total) if workers is None else max(1, int(workers))
    if count <= 1 or total <= 1:
        slices_owned = [_stat_owned_entries(entries, process_root)]
    else:
        # Contiguous slices, one per worker: the blocking ``lstat`` calls overlap
        # while each worker's own loop stays cheap, so pool submission is O(workers)
        # not O(files) and the warm walk is never slower than sequential.
        span = (total + count - 1) // count
        chunks = [entries[i:i + span] for i in range(0, total, span)]
        with ThreadPoolExecutor(max_workers=count) as pool:
            slices_owned = list(
                pool.map(lambda slice_: _stat_owned_entries(slice_, process_root), chunks)
            )
    for slice_owned in slices_owned:
        for request_id, entry in slice_owned:
            inventory.setdefault(request_id, []).append(entry)
    return inventory


def _candidate_payload(root: Path, *, deadline: float | None = None) -> dict[str, Any]:
    process_root = root / PROCESS_FILES_RELATIVE_PATH
    if process_root.exists():
        info = process_root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TerminalLogRetentionError("terminal_log_root_invalid")
    cutoff = now_utc() - timedelta(days=_logs_days(root))
    protected: list[dict[str, Any]] = []
    eligible_by_task: dict[str, list[dict[str, Any]]] = {}
    # One directory stat of every owned per-request file.  IO-bound (``lstat``
    # releases the GIL), so it overlaps across a core-derived worker count on a
    # large store while a small one stays sequential -- identical result either
    # way (same request ids, same per-request file order; see _build_inventory).
    inventory = _build_inventory(process_root)
    process_file_bytes = sum(
        int(item["size_bytes"])
        for files in inventory.values()
        for item in files
    )
    ledger_path = root / PROCESS_LOG_RELATIVE_PATH
    ledger_bytes = sum(
        int(path.stat().st_size)
        for path in process_event_ledger.ledger_paths(ledger_path)
    )
    latest_rows = _latest_rows(root)
    usage_capture_ids = _usage_capture_request_ids(root)
    # One bulk read of every task status, replacing a per-request ``get_task``
    # N+1 that was the whole 55 s cost of this measurement (see _task_status_map).
    task_status_by_id = _task_status_map(root)
    # True only when a supplied ``deadline`` cut the classification short.  A
    # completed measurement always reports ``partial`` False; a cut-short one
    # reports True so a caller can never read an incomplete scan as a whole one.
    partial = False
    for request_id, row in latest_rows.items():
        if deadline is not None and time.monotonic() >= deadline:
            partial = True
            break
        task_id = str(row.get("task_id") or "")
        files = sorted(inventory.pop(request_id, []), key=lambda item: item["name"])
        if not files:
            continue
        newest_ns = max(item["mtime_ns"] for item in files)
        newest_at = datetime.fromtimestamp(newest_ns / 1_000_000_000, timezone.utc)
        state = str(row.get("state") or "")
        task_status = task_status_by_id.get(task_id, "unknown") if task_id else "unknown"
        item = {
            "request_id": request_id,
            "task_id": task_id,
            "state": state,
            "task_status": task_status,
            "modified_at": newest_at.isoformat(),
            "modified_at_ns": newest_ns,
            "size_bytes": sum(entry["size_bytes"] for entry in files),
            "files": files,
        }
        if state not in _TERMINAL_STATES or task_status not in {"finished", "archived"}:
            protected.append({**item, "reason": "active_or_unverified_authority"})
            continue
        if request_id not in usage_capture_ids:
            protected.append({**item, "reason": "usage_capture_receipt_missing"})
            continue
        eligible_by_task.setdefault(task_id, []).append(item)

    orphan_file_bytes = 0
    for request_id, files in sorted(inventory.items()):
        if not files:
            continue
        orphan_file_bytes += sum(int(item["size_bytes"]) for item in files)
        orphan_item = {
            "request_id": request_id,
            "task_id": "",
            "state": "orphaned",
            "task_status": "unknown",
            "modified_at": datetime.fromtimestamp(
                max(item["mtime_ns"] for item in files) / 1_000_000_000,
                timezone.utc,
            ).isoformat(),
            "modified_at_ns": max(item["mtime_ns"] for item in files),
            "size_bytes": sum(int(item["size_bytes"]) for item in files),
            "files": files,
        }
        orphan_modified = datetime.fromisoformat(orphan_item["modified_at"])
        protected.append({
            **orphan_item,
            "reason": (
                "usage_capture_receipt_missing"
                if orphan_modified <= cutoff
                else "orphan_retention_age_not_met"
            ),
        })

    candidates: list[dict[str, Any]] = []
    for rows in eligible_by_task.values():
        rows.sort(key=lambda item: (item["modified_at_ns"], item["request_id"]), reverse=True)
        for index, item in enumerate(rows):
            modified = datetime.fromisoformat(item["modified_at"])
            if index < KEEP_LAST_PER_TASK:
                protected.append({**item, "reason": "last_runs_protected"})
            elif modified > cutoff:
                protected.append({**item, "reason": "retention_age_not_met"})
            else:
                candidates.append(item)
    # NF-2026-00286: an orphan (a per-request file set with no ledger row) can
    # never be proved to belong to a finished/archived task, so it is always
    # protected above and fails closed -- it is never a quarantine candidate.
    # The former ``candidates.extend(orphan_candidates)`` consumed a list that
    # nothing ever appended to; wiring it would sweep unattributable files and
    # break that fail-closed guarantee, so the dead path is removed rather than
    # wired.
    candidates.sort(key=lambda item: item["request_id"])
    digest_source = {
        "schema_id": SCHEMA_ID,
        "repo_id": _repo_id(root),
        "logs_days": _logs_days(root),
        "keep_last_per_task": KEEP_LAST_PER_TASK,
        "candidates": candidates,
    }
    digest = hashlib.sha256(
        json.dumps(digest_source, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    candidate_bytes = sum(int(item["size_bytes"]) for item in candidates)
    canonical_current_bytes = process_file_bytes + ledger_bytes
    legacy_identity = _legacy_store_identity(root)
    legacy_current_bytes = int(legacy_identity["size_bytes"])
    legacy_modified = (
        datetime.fromtimestamp(
            int(legacy_identity["newest_mtime_ns"]) / 1_000_000_000,
            timezone.utc,
        )
        if legacy_identity["newest_mtime_ns"]
        else None
    )
    # NF-2026-00286: the legacy ``logs/`` tree is always protected (fail closed),
    # reported by age -- it is never a quarantine candidate. ``legacy_candidate``
    # was a dead producer: it only ever stayed ``None``, yet a downstream
    # ``if legacy_candidate is not None`` digest recompute and two
    # ``... if legacy_candidate else ...`` accumulators consumed it, reading as a
    # legacy-quarantine path that could never run -- coverage that was not there.
    # Wiring it would contradict ``test_aged_legacy_store_is_protected_without_
    # usage_receipts`` (which pins ``legacy_candidate is None``) and would widen
    # the eventual-deletion path, so the dead consumers are removed and
    # ``legacy_candidate`` is a fixed ``None``.
    legacy_candidate = None
    if legacy_current_bytes and legacy_modified is not None and legacy_modified <= cutoff:
        protected.append({
            "request_id": "legacy_logs",
            "task_id": "",
            "state": "legacy",
            "task_status": "unknown",
            "modified_at": legacy_modified.isoformat(),
            "modified_at_ns": int(legacy_identity["newest_mtime_ns"]),
            "size_bytes": legacy_current_bytes,
            "files": [],
            "reason": "usage_capture_receipt_missing",
        })
    elif legacy_current_bytes:
        protected.append({
            "request_id": "legacy_logs",
            "task_id": "",
            "state": "legacy",
            "task_status": "unknown",
            "modified_at": legacy_modified.isoformat() if legacy_modified else "",
            "modified_at_ns": int(legacy_identity["newest_mtime_ns"]),
            "size_bytes": legacy_current_bytes,
            "files": [],
            "reason": "legacy_retention_age_not_met",
        })
    observed_current_bytes = canonical_current_bytes + legacy_current_bytes
    total_candidate_bytes = candidate_bytes
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "dry_run": True,
        "repository_scoped": True,
        # False for a complete population; True when a ``deadline`` cut the walk
        # short, so a partial measurement never reads as a whole (or clean) one.
        "partial": partial,
        "logs_days": _logs_days(root),
        "keep_last_per_task": KEEP_LAST_PER_TASK,
        "current_bytes": observed_current_bytes,
        "canonical_current_bytes": canonical_current_bytes,
        "legacy_current_bytes": legacy_current_bytes,
        "ledger_bytes": ledger_bytes,
        "process_file_bytes": process_file_bytes,
        "orphan_file_count": sum(len(files) for files in inventory.values()),
        "orphan_file_bytes": orphan_file_bytes,
        "legacy_status": "present_unmanaged" if legacy_current_bytes else "absent_or_empty",
        # Always ``None``: the legacy store is never a candidate (see above).
        "legacy_candidate": legacy_candidate,
        "projected_bytes": max(0, observed_current_bytes - total_candidate_bytes),
        "candidate_count": len(candidates),
        "candidate_bytes": total_candidate_bytes,
        "protected_count": len(protected),
        "protected": protected,
        "preview_digest": digest,
        "candidates": candidates,
    }


def preview(
    repo_root: Path | str,
    *,
    cursor: int = 0,
    limit: int = DEFAULT_PREVIEW_LIMIT,
    include_candidates: bool = True,
    deadline_seconds: float | None = None,
) -> dict[str, Any]:
    """Return one bounded page while hashing the full eligible population.

    ``deadline_seconds`` bounds the wall-clock of the classification: when it is
    exceeded the result is returned with ``partial=True`` rather than blocking.
    ``None`` (the default) measures the whole population and always reports
    ``partial=False``.  The measurement itself is fast -- the per-task authority
    lookup is a single bulk query, not a per-request one -- so the dashboard runs
    it without a deadline and gets a complete result in about a second.
    """

    start = max(0, int(cursor))
    page_limit = max(1, min(MAX_PREVIEW_LIMIT, int(limit)))
    deadline = (
        None
        if deadline_seconds is None
        else time.monotonic() + max(0.0, float(deadline_seconds))
    )
    result = _candidate_payload(Path(repo_root).resolve(), deadline=deadline)
    candidates = list(result.pop("candidates", []))
    total = len(candidates)
    end = min(total, start + page_limit)
    # Bottleneck audit T6 (2026-09-01): the protected list was returned whole
    # (3,417 entries, 2.5MB on a limit=10 call) while candidates were paged.
    # The same page window bounds it; ``protected_count`` keeps the total.
    protected = list(result.pop("protected", []))
    result.update({
        "candidate_total": total,
        "cursor": start,
        "limit": page_limit,
        "returned_count": max(0, end - start) if include_candidates else 0,
        "next_cursor": end if include_candidates and end < total else None,
        "response_bounded": True,
        "protected": protected[start : start + page_limit],
        "protected_returned_count": len(protected[start : start + page_limit]),
    })
    if include_candidates:
        result["candidates"] = candidates[start:end]
    return result


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_MANIFEST_BYTES:
        raise TerminalLogRetentionError("terminal_log_manifest_too_large")
    fd, name = tempfile.mkstemp(prefix=".terminal-log-", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)


def _append_audit(root: Path, event: Mapping[str, Any]) -> None:
    path = root / AUDIT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(event), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _quarantine_root(root: Path) -> Path:
    result = root / QUARANTINE_RELATIVE_PATH
    result.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = result.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TerminalLogRetentionError("terminal_log_quarantine_root_invalid")
    return result


def _batch(root: Path, batch_id: str) -> Path:
    if not _BATCH_RE.fullmatch(batch_id):
        raise TerminalLogRetentionError("terminal_log_batch_id_invalid")
    result = _quarantine_root(root) / batch_id
    try:
        info = result.lstat()
    except OSError as exc:
        raise TerminalLogRetentionError("terminal_log_batch_not_found") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TerminalLogRetentionError("terminal_log_batch_invalid")
    return result


def _manifest(path: Path, repo_id: str) -> dict[str, Any]:
    try:
        info = path.lstat()
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalLogRetentionError("terminal_log_manifest_invalid") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
        raise TerminalLogRetentionError("terminal_log_manifest_invalid")
    if not isinstance(value, dict) or value.get("schema_id") != SCHEMA_ID or value.get("repo_id") != repo_id:
        raise TerminalLogRetentionError("terminal_log_manifest_identity_mismatch")
    return value


def quarantine(repo_root: Path | str, *, preview_digest: str, confirm: bool) -> dict[str, Any]:
    if confirm is not True:
        raise TerminalLogRetentionError("explicit_confirmation_required")
    root = Path(repo_root).resolve()
    current = _candidate_payload(root)
    if preview_digest != current["preview_digest"]:
        raise TerminalLogRetentionError("terminal_log_preview_stale")
    if not current["candidates"] and not current.get("legacy_candidate"):
        return {"ok": True, "quarantined": 0, "bytes": 0, "batch_id": "", "no_op": True}
    now = now_utc()
    # NF-2026-00296: a per-batch unique suffix, never ``preview_digest[:12]``. Two
    # enforce passes in separate processes compute the SAME digest for the SAME
    # eligible population within the SAME second, so a digest-derived id collided
    # on ``batch.mkdir`` (FileExistsError) and one pass crashed. A random 12-hex
    # suffix (still matching ``_BATCH_RE``) makes each pass stage into its own
    # directory; the preview digest is retained in the manifest, not the id.
    batch_id = f"l{now.strftime('%Y%m%dT%H%M%S')}-{os.urandom(6).hex()}"
    batch = _quarantine_root(root) / batch_id
    batch.mkdir(mode=0o700)
    manifest = {
        "schema_id": SCHEMA_ID,
        "repo_id": _repo_id(root),
        "batch_id": batch_id,
        "created_at": now.isoformat(),
        "restore_deadline": (now + timedelta(days=UNDO_DAYS)).isoformat(),
        "preview_digest": preview_digest,
        "status": "quarantining",
        "items": [dict(item, state="planned") for item in current["candidates"]],
        "legacy_store": (
            dict(current["legacy_candidate"], state="planned")
            if current.get("legacy_candidate")
            else None
        ),
    }
    manifest_path = batch / MANIFEST_NAME
    _atomic_json(manifest_path, manifest)
    process_root = root / PROCESS_FILES_RELATIVE_PATH
    moved_files = 0
    moved_bytes = 0
    for item in manifest["items"]:
        request_id = str(item.get("request_id") or "")
        if not _REQUEST_RE.fullmatch(request_id):
            item["state"] = "skipped_identity_changed"
            continue
        destination_root = batch / request_id
        destination_root.mkdir(mode=0o700)
        complete = True
        for expected in item.get("files") or []:
            name = str(expected.get("name") or "")
            if not any(name == f"{request_id}{suffix}" for suffix in _OWNED_SUFFIXES):
                complete = False
                break
            source = process_root / name
            info = _owned_regular_file(source, process_root)
            if info is None or int(info.st_size) != int(expected.get("size_bytes") or -1) or int(info.st_mtime_ns) != int(expected.get("mtime_ns") or -1):
                complete = False
                break
        if not complete:
            item["state"] = "skipped_identity_changed"
            _atomic_json(manifest_path, manifest)
            continue
        for expected in item["files"]:
            source = process_root / expected["name"]
            os.replace(source, destination_root / expected["name"])
            moved_files += 1
            moved_bytes += int(expected["size_bytes"])
        item["state"] = "quarantined"
        _atomic_json(manifest_path, manifest)
    legacy = manifest.get("legacy_store")
    if isinstance(legacy, dict) and legacy.get("state") == "planned":
        source = root / "logs"
        destination = batch / "legacy-logs"
        current_identity = _legacy_store_identity(root)
        expected_identity = {
            key: int(legacy.get(key) or 0)
            for key in ("file_count", "size_bytes", "newest_mtime_ns")
        }
        try:
            source_info = source.lstat()
        except OSError:
            source_info = None
        if (
            source_info is None
            or stat.S_ISLNK(source_info.st_mode)
            or not stat.S_ISDIR(source_info.st_mode)
            or destination.exists()
            or current_identity != expected_identity
        ):
            legacy["state"] = "skipped_identity_changed"
        else:
            os.replace(source, destination)
            legacy["state"] = "quarantined"
            moved_files += int(legacy["file_count"])
            moved_bytes += int(legacy["size_bytes"])
        _atomic_json(manifest_path, manifest)
    manifest["status"] = "quarantined" if moved_files else "empty"
    manifest["quarantined_files"] = moved_files
    manifest["quarantined_bytes"] = moved_bytes
    _atomic_json(manifest_path, manifest)
    if _batch_reapable_empty(manifest, batch):
        # The eligible population changed between the digest snapshot and this
        # apply (typically a concurrent sweep in another process moved the
        # files first).  An empty batch holds nothing to restore, so its
        # seven-day undo window protects nothing while the entry sits on the
        # storage panel forever, shape-identical to a real batch.  Reap it now
        # instead of accumulating operator-visible noise -- but only when the
        # directory is also physically empty, so a batch that unexpectedly holds
        # files is never rmtree'd here on a record that says it is empty.
        shutil.rmtree(batch, ignore_errors=True)
        _append_audit(root, {"schema_id": AUDIT_SCHEMA_ID, "timestamp": now_utc().isoformat(), "action": "quarantine_empty_reaped", "batch_id": batch_id, "files": 0, "bytes": 0})
        return {"ok": True, "batch_id": "", "quarantined": 0, "bytes": 0, "no_op": True}
    _append_audit(root, {"schema_id": AUDIT_SCHEMA_ID, "timestamp": now_utc().isoformat(), "action": "quarantine_completed", "batch_id": batch_id, "files": moved_files, "bytes": moved_bytes})
    return {"ok": True, "batch_id": batch_id, "quarantined": moved_files, "bytes": moved_bytes, "no_op": False}


def list_batches(repo_root: Path | str) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    qroot = root / QUARANTINE_RELATIVE_PATH
    if not qroot.exists():
        return {"ok": True, "batches": [], "count": 0}
    # Repository identity is invariant for the whole listing.  Resolving it in
    # the loop turned one dashboard refresh into an N+1 storage-readiness query
    # (740 SQLite opens on the observed repository), accounting for ~19 seconds
    # of a 26-second storage scan.  Resolve once, then validate every manifest
    # against that same canonical identity.
    repo_id = _repo_id(root)
    rows: list[dict[str, Any]] = []
    for entry in sorted(qroot.iterdir(), reverse=True):
        # The public contract returns at most the newest 100 valid batches. Do
        # not inventory payloads for hundreds of older directories only to
        # slice them away after the expensive filesystem walk.
        if len(rows) >= 100:
            break
        if not entry.is_dir() or not _BATCH_RE.fullmatch(entry.name):
            continue
        try:
            value = _manifest(entry / MANIFEST_NAME, repo_id)
            deadline = datetime.fromisoformat(str(value.get("restore_deadline") or ""))
        except (TerminalLogRetentionError, ValueError):
            continue
        states = [item.get("state") for item in value.get("items") or [] if isinstance(item, dict)]
        legacy = value.get("legacy_store")
        if isinstance(legacy, dict):
            states.append(legacy.get("state"))
        # The truthful on-disk size, so a batch whose manifest under-records what
        # it physically holds (the stranded-3.68 GB shape) reports its real bytes
        # and can never read as "0 bytes, nothing here" while gigabytes sit in it.
        # One walk yields both the byte total and the presence flag.
        payload_bytes, has_payload = _batch_payload_summary(entry)
        recorded_bytes = int(value.get("quarantined_bytes") or 0)
        record_empty = _batch_is_empty(value)
        unclaimed = record_empty and has_payload
        rows.append({
            "batch_id": entry.name,
            "created_at": str(value.get("created_at") or ""),
            "restore_deadline": deadline.isoformat(),
            "status": str(value.get("status") or "unknown"),
            "quarantined_count": states.count("quarantined"),
            "restored_count": states.count("restored"),
            "bytes": max(recorded_bytes, payload_bytes),
            "recorded_bytes": recorded_bytes,
            "on_disk_bytes": payload_bytes,
            # A batch whose record says empty while it still physically holds
            # files is not reclaimable data an operator can drop -- it is an
            # unreconciled disagreement between record and disk. Surface it so the
            # panel names it instead of hiding it as "0 bytes".
            "unclaimed": unclaimed,
            # A batch is reapable now only if its undo window has expired *or* it
            # is empty in BOTH its record and on disk.  This mirrors exactly what
            # ``purge`` accepts, so the storage panel is truthful about the empty
            # batches an operator may release and never marks an unreconciled
            # batch (record-empty but holding bytes) as a harmless purge.
            "purge_eligible": now_utc() >= deadline or (record_empty and not has_payload),
            # Exactly the batches ``purge_empty_batches`` collects: empty in BOTH
            # record and on disk (``_batch_reapable_empty``). ``purge_eligible``
            # also covers expired-but-still-full batches, which that collector
            # never takes, so an operator count of what it drains must read this
            # field -- never ``purge_eligible`` -- to avoid promising more than the
            # named trigger delivers.
            "reapable_empty": record_empty and not has_payload,
        })
    return {"ok": True, "batches": rows, "count": len(rows)}


def restore(repo_root: Path | str, *, batch_id: str, confirm: bool) -> dict[str, Any]:
    if confirm is not True:
        raise TerminalLogRetentionError("explicit_confirmation_required")
    root = Path(repo_root).resolve()
    batch = _batch(root, batch_id)
    manifest_path = batch / MANIFEST_NAME
    manifest = _manifest(manifest_path, _repo_id(root))
    process_root = root / PROCESS_FILES_RELATIVE_PATH
    process_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    restored = 0
    for item in manifest.get("items") or []:
        if not isinstance(item, dict):
            continue
        state = item.get("state")
        # NF-2026-00296: recover from the manifest, not from a state flag alone.
        # ``quarantine`` moves an item's files, then commits ``state="quarantined"``
        # in a following manifest write; a crash between the move and that commit
        # leaves the files physically in the batch while the item still reads
        # ``planned``. Restoring only ``quarantined`` items stranded those files
        # with no way back. An item is restorable when its recorded files sit in
        # the batch: a committed ``quarantined`` item, or a ``planned`` item whose
        # files are physically present (the interrupted case).
        if state not in ("quarantined", "planned"):
            continue
        request_id = str(item.get("request_id") or "")
        source_root = batch / request_id
        expected = item.get("files") or []
        if not _REQUEST_RE.fullmatch(request_id):
            continue
        present = any(
            (source_root / str(entry.get("name") or "")).is_file() for entry in expected
        )
        if state == "planned" and not present:
            # An interrupted item whose files never moved: they are still in
            # ``processes`` where they started, so there is nothing in the batch to
            # restore. Leave it untouched (the identity re-check on a later apply
            # handles it) rather than marking it acted on.
            continue
        if any((process_root / str(entry.get("name") or "")).exists() for entry in expected):
            item["state"] = "restore_conflict"
            _atomic_json(manifest_path, manifest)
            continue
        for entry in expected:
            source = source_root / str(entry["name"])
            if source.is_file() and not source.is_symlink():
                os.replace(source, process_root / source.name)
                restored += 1
        item["state"] = "restored"
        _atomic_json(manifest_path, manifest)
    legacy = manifest.get("legacy_store")
    if isinstance(legacy, dict) and legacy.get("state") == "quarantined":
        source = batch / "legacy-logs"
        destination = root / "logs"
        if source.is_dir() and not source.is_symlink() and not destination.exists():
            os.replace(source, destination)
            legacy["state"] = "restored"
            restored += int(legacy.get("file_count") or 0)
        else:
            legacy["state"] = "restore_conflict"
        _atomic_json(manifest_path, manifest)
    manifest["status"] = "restored" if restored else manifest.get("status", "quarantined")
    _atomic_json(manifest_path, manifest)
    _append_audit(root, {"schema_id": AUDIT_SCHEMA_ID, "timestamp": now_utc().isoformat(), "action": "restore_completed", "batch_id": batch_id, "files": restored})
    return {"ok": True, "batch_id": batch_id, "restored": restored}


_EMPTY_BATCH_ITEM_STATES = frozenset({"skipped_identity_changed"})


def _batch_is_empty(manifest: Mapping[str, Any]) -> bool:
    """Single source of truth for "this batch holds nothing to restore".

    A batch is empty only when nothing was ever moved into it: every recorded
    item was skipped because its on-disk identity changed before the move
    (``skipped_identity_changed``), and no legacy store was captured.  Such a
    batch has no files under its directory, so its seven-day undo window
    protects nothing and it is reapable immediately.

    Any item still ``quarantined`` (files sit in the batch), already
    ``restored`` (the operator relied on that outcome), or in
    ``restore_conflict`` (files sit in the batch because the restore could not
    place them) keeps the batch's full restore deadline.  ``restore_conflict``
    in particular means the file is still physically present and is *more*
    likely to be wanted, so it must never be treated as absent.
    """
    for item in manifest.get("items") or []:
        if not isinstance(item, dict):
            return False
        if item.get("state") not in _EMPTY_BATCH_ITEM_STATES:
            return False
    legacy = manifest.get("legacy_store")
    if isinstance(legacy, dict) and legacy.get("state") not in _EMPTY_BATCH_ITEM_STATES:
        return False
    return True


def _batch_payload_summary(batch_path: Path) -> tuple[int, bool]:
    """One filesystem walk: ``(non-manifest bytes, any non-manifest file present)``.

    The single source of truth for what a batch actually holds on disk, read
    from the filesystem rather than trusted from the manifest's item states.
    ``_batch_is_empty`` answers "does the *record* claim anything to restore";
    this answers "does the *directory* hold anything at all". They can disagree:
    a batch whose manifest records every item as ``skipped_identity_changed``
    (record says empty) can still physically hold gigabytes -- exactly the shape
    that stranded 3.68 GB behind a "status: empty, bytes: 0" record that no purge
    would ever act on. The presence flag (not the byte total) is authoritative
    for emptiness, so a zero-byte file still counts as content a restore could
    return. Symlinks are never followed and never counted. Both facts come from
    one walk so ``list_batches`` does not re-walk a large batch three times.
    """
    total = 0
    present = False
    for directory, dirnames, filenames in os.walk(batch_path, followlinks=False):
        parent = Path(directory)
        dirnames[:] = [name for name in dirnames if not (parent / name).is_symlink()]
        for name in filenames:
            if name == MANIFEST_NAME and parent == batch_path:
                continue
            try:
                info = (parent / name).lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                continue
            present = True
            total += int(info.st_size)
    return total, present


def _batch_payload_bytes(batch_path: Path) -> int:
    """Bytes of real files physically present under a batch, excluding its manifest."""
    return _batch_payload_summary(batch_path)[0]


def _dir_total_bytes(path: Path) -> int:
    """Total bytes of every regular file under ``path`` (no manifest exclusion).

    Unlike :func:`_batch_payload_bytes`, this makes no assumption that ``path`` is
    a batch root, so it correctly measures a moved directory whose own content
    happens to include a file named ``manifest.json``. Symlinks are never
    followed or counted.
    """
    total = 0
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        parent = Path(directory)
        dirnames[:] = [name for name in dirnames if not (parent / name).is_symlink()]
        for name in filenames:
            try:
                info = (parent / name).lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                continue
            total += int(info.st_size)
    return total


def _batch_dir_has_payload(batch_path: Path) -> bool:
    """True when the batch directory physically holds any non-manifest file.

    A batch is safe to treat as empty (reapable before its deadline) only when
    both its record says empty AND its directory is physically empty; either one
    holding content keeps the full undo window. This never follows symlinks.
    """
    return _batch_payload_summary(batch_path)[1]


def _batch_reapable_empty(manifest: Mapping[str, Any], batch_path: Path) -> bool:
    """A batch is early-reapable only when its record AND its disk are both empty.

    ``_batch_is_empty`` trusts the manifest item states; on their own they let a
    batch that physically holds files (whose states drifted to
    ``skipped_identity_changed``) be purged inside its live undo window or be
    surfaced as ``purge_eligible`` while holding bytes. Requiring the directory
    to also be physically empty makes the record-versus-disk disagreement
    impossible to act on destructively: a batch that still holds files keeps its
    full deadline and is routed to :func:`reconcile_unclaimed` instead.
    """
    return _batch_is_empty(manifest) and not _batch_dir_has_payload(batch_path)


def purge(repo_root: Path | str, *, batch_id: str, confirm: bool) -> dict[str, Any]:
    if confirm is not True:
        raise TerminalLogRetentionError("explicit_confirmation_required")
    root = Path(repo_root).resolve()
    batch = _batch(root, batch_id)
    manifest = _manifest(batch / MANIFEST_NAME, _repo_id(root))
    try:
        deadline = datetime.fromisoformat(str(manifest.get("restore_deadline") or ""))
    except ValueError as exc:
        raise TerminalLogRetentionError("terminal_log_deadline_invalid") from exc
    # The undo window protects every batch that still holds files a restore
    # could return -- items in any state other than ``skipped_identity_changed``
    # (including ``restore_conflict``, whose files are still physically present
    # and are more likely to be wanted).  Only a batch that is empty in BOTH its
    # record and on disk (``_batch_reapable_empty``) holds nothing to restore and
    # is reapable before its deadline; a batch whose record reads empty while its
    # directory still holds bytes keeps its full window and is reconciled, never
    # purged out from under an operator on a stale record.
    if now_utc() < deadline and not _batch_reapable_empty(manifest, batch):
        raise TerminalLogRetentionError("retention_undo_window_active")
    # NF-2026-00287: report the bytes actually reclaimed from disk, not the
    # record's ``quarantined_bytes``. A batch whose files exist on disk but are
    # unclaimed in the record (every item ``skipped_identity_changed`` ->
    # ``quarantined_bytes`` 0) still physically holds gigabytes; trusting the
    # record reported "0 bytes reclaimed" while ``rmtree`` freed them all. Measure
    # the physical non-manifest payload before removal and report the larger of the
    # two, so a truthful batch is unchanged while the stranded/unclaimed shape
    # reports what it truly reclaims.
    released = max(int(manifest.get("quarantined_bytes") or 0), _batch_payload_bytes(batch))
    shutil.rmtree(batch)
    _append_audit(root, {"schema_id": AUDIT_SCHEMA_ID, "timestamp": now_utc().isoformat(), "action": "purge_completed", "batch_id": batch_id, "bytes": released})
    return {"ok": True, "batch_id": batch_id, "purged": True, "bytes": released}


RECONCILE_SCHEMA_ID = "aiworkhub.terminal_log_reconcile.v1"


def _scan_unclaimed(root: Path) -> list[dict[str, Any]]:
    """Quarantine directories that physically hold files no record truthfully claims.

    Ownership is proved by LOCATION, never by name or age: every entry examined
    is a direct child of this repository's ``_repo_id``-scoped quarantine root, so
    a directory found here provably belongs to this repository's own store. A
    directory is unclaimed when it holds bytes on disk yet either has no readable
    manifest, or has one that records nothing to restore (``_batch_is_empty``) --
    the exact record-versus-disk disagreement that stranded 3.68 GB behind a
    "status: empty, bytes: 0" batch. A healthy in-flight or content-bearing batch
    (its record claims items) is never listed.
    """
    qroot = root / QUARANTINE_RELATIVE_PATH
    results: list[dict[str, Any]] = []
    try:
        info = qroot.lstat()
    except FileNotFoundError:
        return results
    except OSError as exc:
        raise TerminalLogRetentionError("terminal_log_quarantine_root_invalid") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TerminalLogRetentionError("terminal_log_quarantine_root_invalid")
    repo_id = _repo_id(root)
    for entry in sorted(qroot.iterdir()):
        try:
            e_info = entry.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(e_info.st_mode) or not stat.S_ISDIR(e_info.st_mode):
            continue
        manifest_path = entry / MANIFEST_NAME
        reason = ""
        if not _BATCH_RE.fullmatch(entry.name):
            reason = "not_a_batch_directory"
        else:
            try:
                manifest = _manifest(manifest_path, repo_id)
            except TerminalLogRetentionError:
                reason = "manifest_present_unreadable" if manifest_path.exists() else "manifest_absent"
            else:
                if not _batch_is_empty(manifest):
                    continue  # the record claims content: a healthy batch, left alone
                reason = "record_empty_disk_nonempty"
        payload = _batch_payload_bytes(entry)
        if payload <= 0:
            # An empty directory with no bytes is the empty-batch collector's job
            # (see ``purge_empty_batches``); reconcile only ever adopts real bytes.
            continue
        results.append({"name": entry.name, "path": entry, "bytes": payload, "reason": reason})
    return results


# Test seam: called with the batch directory after each source subtree is moved
# into a reconcile batch, so a test can observe the batch mid-construction and
# prove ``_scan_unclaimed`` never adopts a batch that is still being built. None
# in production; retention never sets it.
_RECONCILE_MOVE_OBSERVER: Callable[[Path], None] | None = None


def _stage_reconcile_batch(
    root: Path, sources: list[dict[str, Any]], *, action: str
) -> dict[str, Any]:
    """Move a set of provably-owned directories into one fresh, bounded batch.

    Each source directory is moved (same-volume ``os.replace``) into a new batch
    under this repository's quarantine root, and a reconciliation manifest is
    written that claims exactly what now sits on disk with a fresh
    :data:`UNDO_DAYS` window. The bytes then flow through the ordinary
    ``purge``/``enforce`` expiry like any other batch -- nothing is deleted here.
    Every item is recorded ``state="reconciled"`` so a restore never tries to
    replay it into ``processes`` (its origin is unknown) yet the batch is never
    mistaken for empty. Shared by :func:`reconcile_unclaimed` and the
    attempt-artifacts bound so both adopt strays through one audited path.

    The manifest is written FIRST, exactly as :func:`quarantine` does before its
    own move loop: every destination subdir is planned, a manifest whose items
    already claim content (``state="planned"``, so ``_batch_is_empty`` reads
    False) is written, and only then is the payload moved in. So from the instant
    the directory can hold bytes it already carries a record that claims them, and
    a concurrent :func:`reconcile_unclaimed` or :func:`purge_empty_batches` never
    observes the batch-shaped-directory-with-payload-but-no-claiming-record
    signature (``manifest_absent`` / ``record_empty_disk_nonempty``) that
    ``_scan_unclaimed`` adopts. This ordering is safe on its own and does not rely
    on any caller holding ``_enforcement_lock``.
    """
    now = now_utc()
    # NF-2026-00296: a per-batch unique suffix, never derived from ``action`` +
    # the source names. Two enforce passes in separate processes that observed the
    # same strays within the same second computed an identical id and collided on
    # ``batch.mkdir`` (FileExistsError), crashing one pass. A random 12-hex suffix
    # (still matching ``_BATCH_RE``) gives each pass its own staging directory.
    batch_id = f"l{now.strftime('%Y%m%dT%H%M%S')}-{os.urandom(6).hex()}"
    qroot = _quarantine_root(root)
    batch = qroot / batch_id
    batch.mkdir(mode=0o700)
    # Plan every destination subdir BEFORE any move so the manifest can claim real
    # content up front. Collisions are deduplicated against the names already
    # planned (never against the half-built directory on disk), so planning never
    # depends on partial filesystem state.
    used: set[str] = set()
    items: list[dict[str, Any]] = []
    plan: list[tuple[Path, dict[str, Any]]] = []
    for index, source in enumerate(sources):
        name = str(source["name"])
        subname = name if (
            _REQUEST_RE.fullmatch(name) or _BATCH_RE.fullmatch(name)
        ) else f"reconciled-{index:04d}"
        if subname in used:
            subname = f"reconciled-{index:04d}"
        used.add(subname)
        item = {
            "request_id": name if _REQUEST_RE.fullmatch(name) else "",
            # "planned" keeps ``_batch_is_empty`` False the moment the manifest
            # lands, so the batch is never adoptable while payload is arriving.
            "state": "planned",
            "source_name": name,
            "subdir": subname,
            "size_bytes": 0,
            "reason": str(source.get("reason") or action),
        }
        items.append(item)
        plan.append((Path(source["path"]), item))
    manifest = {
        "schema_id": SCHEMA_ID,
        "repo_id": _repo_id(root),
        "batch_id": batch_id,
        "created_at": now.isoformat(),
        "restore_deadline": (now + timedelta(days=UNDO_DAYS)).isoformat(),
        "preview_digest": "reconciled",
        "status": "reconciling",
        "reconciled": True,
        "reconcile_action": action,
        "items": items,
        "quarantined_files": 0,
        "quarantined_bytes": 0,
    }
    manifest_path = batch / MANIFEST_NAME
    # Write the claiming manifest before the first move, mirroring ``quarantine``:
    # the record is non-empty from here on, so no concurrent scan can classify the
    # batch as an unclaimed directory while its bytes are still being moved in.
    _atomic_json(manifest_path, manifest)
    reconciled_bytes = 0
    moved = 0
    for src, item in plan:
        try:
            s_info = src.lstat()
        except OSError:
            item["state"] = "skipped_source_gone"
            continue
        # Re-prove identity immediately before the move: still a real directory,
        # never a symlink.
        if stat.S_ISLNK(s_info.st_mode) or not stat.S_ISDIR(s_info.st_mode):
            item["state"] = "skipped_source_gone"
            continue
        destination = batch / str(item["subdir"])
        os.replace(src, destination)
        moved_bytes = _dir_total_bytes(destination)
        reconciled_bytes += moved_bytes
        moved += 1
        item["state"] = "reconciled"
        item["size_bytes"] = moved_bytes
        if _RECONCILE_MOVE_OBSERVER is not None:
            _RECONCILE_MOVE_OBSERVER(batch)
    manifest["status"] = "reconciled"
    manifest["quarantined_files"] = moved
    manifest["quarantined_bytes"] = reconciled_bytes
    _atomic_json(manifest_path, manifest)
    if not moved:
        # Nothing actually moved (every source vanished under a race). Do not
        # leave a phantom batch: it is empty in record and on disk, so reap it.
        shutil.rmtree(batch, ignore_errors=True)
        return {"batch_id": "", "count": 0, "bytes": 0}
    return {"batch_id": batch_id, "count": moved, "bytes": reconciled_bytes}


def reconcile_unclaimed(
    repo_root: Path | str, *, confirm: bool = False, reason: str = ""
) -> dict[str, Any]:
    """Operator action: bring quarantine directories no record claims back under a bound.

    A batch directory can physically hold files while its manifest records none
    of them -- every item skipped, or the manifest missing/corrupt. Record and
    disk disagree, so no purge ever acts on those bytes and they strand forever
    (3.68 GB in the canonical store). This scans the repository's own terminal-log
    quarantine root (ownership proved by location, never inferred from a name or
    an age), and for every directory holding bytes that no record truthfully
    claims, moves it into a fresh batch with a 7-day undo window so it flows
    through the ordinary ``purge``/``enforce`` expiry. Nothing is deleted here.

    ``confirm=False`` (default) is a read-only report of what would be
    reconciled. ``confirm=True`` requires a non-empty ``reason``, performs the
    reconciliation, and records the reason in the audit log. It is invoked ONLY
    by an explicit operator call; it never runs on import, on a read, or as a
    side effect of any preview/snapshot.
    """
    root = Path(repo_root).resolve()
    unclaimed = _scan_unclaimed(root)
    report: dict[str, Any] = {
        "ok": True,
        "schema_id": RECONCILE_SCHEMA_ID,
        "dry_run": not confirm,
        "invoked_from": (
            "terminal_log_retention.reconcile_unclaimed "
            "(explicit operator action; never on import or as a read side effect)"
        ),
        "reason": str(reason or ""),
        "unclaimed": [
            {"batch_id": item["name"], "bytes": item["bytes"], "reason": item["reason"]}
            for item in unclaimed
        ],
        "unclaimed_count": len(unclaimed),
        "unclaimed_bytes": sum(int(item["bytes"]) for item in unclaimed),
    }
    if not confirm:
        return report
    if not str(reason or "").strip():
        raise TerminalLogRetentionError("terminal_log_reconcile_reason_required")
    staged = (
        _stage_reconcile_batch(root, unclaimed, action="reconcile_unclaimed")
        if unclaimed
        else {"batch_id": "", "count": 0, "bytes": 0}
    )
    _append_audit(root, {
        "schema_id": AUDIT_SCHEMA_ID,
        "timestamp": now_utc().isoformat(),
        "action": "reconcile_unclaimed_completed",
        "reason": str(reason or ""),
        "batch_id": staged["batch_id"],
        "reconciled_count": staged["count"],
        "reconciled_bytes": staged["bytes"],
    })
    report.update({
        "batch_id": staged["batch_id"],
        "reconciled_count": staged["count"],
        "reconciled_bytes": staged["bytes"],
    })
    report["ok"] = staged["count"] == len(unclaimed)
    return report


def purge_empty_batches(repo_root: Path | str, *, confirm: bool) -> dict[str, Any]:
    """Operator-invoked collector for empty terminal-log quarantine batches.

    NF-2026-00273 stopped new empty batches being created; the ones already on
    disk (100 in the canonical store, every one ``purge_eligible`` yet never
    collected, because ``enforce`` deliberately never sweeps a batch an operator
    has not asked to release) had no consumer -- an eligible queue with nothing
    draining it. This is that consumer: one named, operator-reachable trigger
    that purges every batch empty in BOTH its record and on disk, and only those.
    A batch holding any file is never touched here -- including an unreconciled
    record-empty batch that still holds bytes, which is routed to
    :func:`reconcile_unclaimed` instead of being dropped on a stale record.
    """
    if confirm is not True:
        raise TerminalLogRetentionError("explicit_confirmation_required")
    root = Path(repo_root).resolve()
    qroot = root / QUARANTINE_RELATIVE_PATH
    if not qroot.exists():
        return {"ok": True, "purged": 0, "batch_ids": [], "bytes": 0}
    repo_id = _repo_id(root)
    purged: list[str] = []
    freed = 0
    for entry in sorted(qroot.iterdir()):
        try:
            e_info = entry.lstat()
        except OSError:
            continue
        if (
            stat.S_ISLNK(e_info.st_mode)
            or not stat.S_ISDIR(e_info.st_mode)
            or not _BATCH_RE.fullmatch(entry.name)
        ):
            continue
        try:
            manifest = _manifest(entry / MANIFEST_NAME, repo_id)
        except TerminalLogRetentionError:
            continue
        if _batch_reapable_empty(manifest, entry):
            freed += int(manifest.get("quarantined_bytes") or 0)
            shutil.rmtree(entry, ignore_errors=True)
            purged.append(entry.name)
    if purged:
        _append_audit(root, {
            "schema_id": AUDIT_SCHEMA_ID,
            "timestamp": now_utc().isoformat(),
            "action": "empty_batches_collected",
            "count": len(purged),
            "batch_ids": sorted(purged),
            "bytes": freed,
        })
    return {"ok": True, "purged": len(purged), "batch_ids": sorted(purged), "bytes": freed}


def _terminal_request_ids(root: Path) -> set[str]:
    """Request ids whose owning run reached a terminal ledger state.

    The only runs whose per-file logs or attempt bundles may be bounded: a
    terminal state means the launcher's writer has stopped, so the file is no
    longer being appended and its tail is stable. A live/unknown run
    (non-terminal) fails closed and is never bounded, so the tail an operator is
    still watching is never disturbed.
    """
    return {
        request_id
        for request_id, row in _latest_rows(root).items()
        if str(row.get("state") or "") in _TERMINAL_STATES
    }


def _tail_cap_file(path: Path, size: int) -> int:
    """Rewrite ``path`` to a truncation notice plus its last bytes; return bytes freed.

    Keeps the tail that fits within :data:`MAX_PROCESS_LOG_FILE_BYTES` once the
    notice is accounted for -- the exact window the launcher itself reads to
    diagnose a failure -- discarding the unbounded head.
    The kept tail is trimmed to the next line boundary so it starts cleanly, and
    the notice makes the truncation explicit. Written atomically at 0o600.
    """
    # Reserve room for the notice so the written payload (notice + tail) never
    # exceeds the bound; otherwise a just-capped file stays oversized forever and
    # every enforce pass rewrites the whole 4 MiB again (a permanent fixed point).
    keep = MAX_PROCESS_LOG_FILE_BYTES - len(_TAIL_CAP_NOTICE)
    with open(path, "rb") as handle:
        handle.seek(max(0, size - keep))
        tail = handle.read()
    if size > keep:
        newline = tail.find(b"\n")
        if 0 <= newline < len(tail) - 1:
            tail = tail[newline + 1:]
    payload = _TAIL_CAP_NOTICE + tail
    fd, name = tempfile.mkstemp(prefix=".process-log-", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)
    return max(0, size - len(payload))


def _plan_process_log_bounds(root: Path) -> dict[str, Any]:
    """Read-only split of per-file logs and attempt bundles into boundable vs protected."""
    process_root = root / PROCESS_FILES_RELATIVE_PATH
    terminal = _terminal_request_ids(root)
    cutoff = now_utc() - timedelta(days=_logs_days(root))
    oversized: list[dict[str, Any]] = []
    protected_files: list[dict[str, Any]] = []
    if process_root.is_dir():
        for path in sorted(process_root.iterdir()):
            info = _owned_regular_file(path, process_root)
            if info is None:
                continue
            suffix = next(
                (s for s in _BOUNDABLE_LOG_SUFFIXES if path.name.endswith(s)), ""
            )
            if not suffix or int(info.st_size) <= MAX_PROCESS_LOG_FILE_BYTES:
                continue
            request_id = path.name[: -len(suffix)]
            entry = {
                "name": path.name,
                "request_id": request_id,
                "size_bytes": int(info.st_size),
            }
            if _REQUEST_RE.fullmatch(request_id) and request_id in terminal:
                oversized.append(entry)
            else:
                # A live or unattributed run: never truncated, so a tail an
                # operator is still watching is never lost.
                protected_files.append({**entry, "reason": "run_not_terminal"})
    bundles_root = process_root / ATTEMPT_ARTIFACTS_DIRNAME
    aged_bundles: list[dict[str, Any]] = []
    protected_bundles: list[dict[str, Any]] = []
    if bundles_root.is_dir() and not bundles_root.is_symlink():
        for entry in sorted(bundles_root.iterdir()):
            try:
                b_info = entry.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(b_info.st_mode) or not stat.S_ISDIR(b_info.st_mode):
                continue
            request_id = entry.name
            newest_ns = 0
            size_bytes = 0
            for directory, dirnames, filenames in os.walk(entry, followlinks=False):
                parent = Path(directory)
                dirnames[:] = [n for n in dirnames if not (parent / n).is_symlink()]
                for name in filenames:
                    try:
                        item = (parent / name).lstat()
                    except OSError:
                        continue
                    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
                        continue
                    size_bytes += int(item.st_size)
                    newest_ns = max(newest_ns, int(item.st_mtime_ns))
            newest = datetime.fromtimestamp(newest_ns / 1_000_000_000, timezone.utc) if newest_ns else None
            record = {"request_id": request_id, "path": entry, "size_bytes": size_bytes}
            terminal_ok = _REQUEST_RE.fullmatch(request_id) and request_id in terminal
            aged_ok = newest is not None and newest <= cutoff
            if terminal_ok and aged_ok:
                aged_bundles.append({**record, "name": request_id, "reason": "attempt_artifacts_aged"})
            else:
                protected_bundles.append({
                    **record,
                    "reason": "run_not_terminal" if not terminal_ok else "retention_age_not_met",
                })
    return {
        "oversized_logs": oversized,
        "protected_logs": protected_files,
        "aged_bundles": aged_bundles,
        "protected_bundles": protected_bundles,
        "max_process_log_file_bytes": MAX_PROCESS_LOG_FILE_BYTES,
        "logs_days": _logs_days(root),
    }


def process_log_bounds_preview(repo_root: Path | str) -> dict[str, Any]:
    """Read-only report of what the per-file log bound and attempt-artifacts bound would act on."""
    root = Path(repo_root).resolve()
    plan = _plan_process_log_bounds(root)
    return {
        "ok": True,
        "schema_id": "aiworkhub.terminal_log_process_bounds.v1",
        "dry_run": True,
        "max_process_log_file_bytes": plan["max_process_log_file_bytes"],
        "logs_days": plan["logs_days"],
        "oversized_log_count": len(plan["oversized_logs"]),
        "oversized_log_bytes": sum(int(item["size_bytes"]) for item in plan["oversized_logs"]),
        "protected_log_count": len(plan["protected_logs"]),
        "aged_bundle_count": len(plan["aged_bundles"]),
        "aged_bundle_bytes": sum(int(item["size_bytes"]) for item in plan["aged_bundles"]),
        "protected_bundle_count": len(plan["protected_bundles"]),
        "oversized_logs": [
            {"name": item["name"], "size_bytes": item["size_bytes"]}
            for item in plan["oversized_logs"]
        ],
        "aged_bundles": [
            {"request_id": item["request_id"], "size_bytes": item["size_bytes"]}
            for item in plan["aged_bundles"]
        ],
    }


def enforce_process_log_bounds(repo_root: Path | str, *, confirm: bool = True) -> dict[str, Any]:
    """Apply the per-file log bound and the attempt-artifacts bound once.

    Oversized logs of terminal runs are tail-capped in place (head released, tail
    kept). Attempt-artifacts bundles of terminal runs aged past ``logs_days`` are
    moved into a reversible quarantine batch (7-day undo, then ordinary purge).
    A live/non-terminal run is never touched. ``confirm=False`` returns the
    read-only preview unchanged.
    """
    root = Path(repo_root).resolve()
    if not confirm:
        return process_log_bounds_preview(root)
    plan = _plan_process_log_bounds(root)
    process_root = root / PROCESS_FILES_RELATIVE_PATH
    capped = 0
    freed = 0
    for item in plan["oversized_logs"]:
        path = process_root / str(item["name"])
        info = _owned_regular_file(path, process_root)
        if info is None or int(info.st_size) <= MAX_PROCESS_LOG_FILE_BYTES:
            continue  # raced away or shrank since the plan; skip
        freed += _tail_cap_file(path, int(info.st_size))
        capped += 1
    staged = (
        _stage_reconcile_batch(root, plan["aged_bundles"], action="attempt_artifacts_bound")
        if plan["aged_bundles"]
        else {"batch_id": "", "count": 0, "bytes": 0}
    )
    if capped or staged["count"]:
        _append_audit(root, {
            "schema_id": AUDIT_SCHEMA_ID,
            "timestamp": now_utc().isoformat(),
            "action": "process_log_bounds_enforced",
            "logs_capped": capped,
            "log_bytes_freed": freed,
            "bundles_quarantined": staged["count"],
            "bundle_batch_id": staged["batch_id"],
            "bundle_bytes": staged["bytes"],
        })
    return {
        "ok": True,
        "repository_scoped": True,
        "logs_capped": capped,
        "log_bytes_freed": freed,
        "bundles_quarantined": staged["count"],
        "bundle_batch_id": staged["batch_id"],
        "bundle_bytes": staged["bytes"],
    }


def _dead_owner_temp_gc(root: Path) -> tuple[int, int]:
    """Remove only exact dead-owner request dirs under ``<repo>/.aiworkhub/temp``.

    Identification (PID/start-time owner identity) is delegated to the
    repository-local temp authority; this module performs the deletion as the
    sole cleanup authority.  A live or unknown owner, a symlink, or any path
    that escapes the temp root fails closed and is never touched.
    """
    count = 0
    released = 0
    try:
        temp_root = runtime_temp.temp_root(root).resolve(strict=False)
    except RuntimeError:
        return 0, 0
    for entry in runtime_temp.identify_dead_owner_dirs(root):
        path = Path(entry.get("path") or "")
        if not path.is_absolute():
            continue
        try:
            resolved = path.resolve(strict=False)
            if resolved.is_symlink() or not resolved.is_dir():
                continue
            resolved.relative_to(temp_root)
        except (OSError, ValueError):
            continue
        try:
            shutil.rmtree(resolved)
        except OSError:
            continue
        count += 1
        released += int(entry.get("bytes") or 0)
    return count, released


def enforce(repo_root: Path | str) -> dict[str, Any]:
    """Apply configured log age and quarantine deadlines once.

    Old active output enters reversible quarantine.  Permanent deletion is
    limited to batches whose independent seven-day undo window has expired.
    The repository-local temp authority is reaped here too: this is the sole
    cleanup authority, and only exact dead-owner request directories (never a
    live or unknown owner) are removed.
    """

    root = Path(repo_root).resolve()
    if not _enforcement_lock.acquire(blocking=False):
        return {"ok": True, "status": "already_running", "repository_scoped": True}
    try:
        purged_batches = 0
        purged_bytes = 0
        for row in list_batches(root).get("batches") or []:
            # Reap only batches whose independent seven-day undo window has
            # actually expired.  A pre-existing empty batch is reapable on an
            # explicit operator purge (see ``purge``/``_batch_is_empty``), but
            # enforce never deletes a batch the operator has not asked to
            # release: this repository has spent enough of its history letting
            # retention destroy things that were still wanted, so an empty batch
            # is made eligible and surfaced, not silently swept.
            try:
                deadline = datetime.fromisoformat(str(row.get("restore_deadline") or ""))
            except ValueError:
                continue
            if now_utc() < deadline:
                continue
            result = purge(root, batch_id=str(row["batch_id"]), confirm=True)
            purged_batches += 1
            purged_bytes += int(result.get("bytes") or 0)

        current = _candidate_payload(root)
        quarantined_files = 0
        quarantined_bytes = 0
        batch_id = ""
        if current.get("candidate_count"):
            result = quarantine(
                root,
                preview_digest=str(current["preview_digest"]),
                confirm=True,
            )
            quarantined_files = int(result.get("quarantined") or 0)
            quarantined_bytes = int(result.get("bytes") or 0)
            batch_id = str(result.get("batch_id") or "")

        temp_gc_count, temp_gc_bytes = _dead_owner_temp_gc(root)
        # The per-file worker-log bound and the attempt-artifacts bound: an
        # oversized terminal-run log is tail-capped (head released, diagnostic
        # tail kept) and an aged terminal-run bundle enters reversible quarantine.
        # A live/non-terminal run is never touched, so this self-bounds the two
        # stores that previously grew without limit without ever cutting a tail an
        # operator still needs.
        process_bounds = enforce_process_log_bounds(root, confirm=True)
        _append_audit(root, {
            "schema_id": AUDIT_SCHEMA_ID,
            "timestamp": now_utc().isoformat(),
            "action": "policy_enforcement_completed",
            "quarantined_files": quarantined_files,
            "quarantined_bytes": quarantined_bytes,
            "purged_batches": purged_batches,
            "purged_bytes": purged_bytes,
            "temp_gc_count": temp_gc_count,
            "temp_gc_bytes": temp_gc_bytes,
            "logs_capped": int(process_bounds.get("logs_capped") or 0),
            "bundles_quarantined": int(process_bounds.get("bundles_quarantined") or 0),
        })
        return {
            "ok": True,
            "status": "completed",
            "repository_scoped": True,
            "batch_id": batch_id,
            "quarantined_files": quarantined_files,
            "quarantined_bytes": quarantined_bytes,
            "purged_batches": purged_batches,
            "purged_bytes": purged_bytes,
            "temp_gc_count": temp_gc_count,
            "temp_gc_bytes": temp_gc_bytes,
            "logs_capped": int(process_bounds.get("logs_capped") or 0),
            "log_bytes_freed": int(process_bounds.get("log_bytes_freed") or 0),
            "bundles_quarantined": int(process_bounds.get("bundles_quarantined") or 0),
            "bundle_bytes": int(process_bounds.get("bundle_bytes") or 0),
        }
    finally:
        _enforcement_lock.release()


__all__ = [
    "MAX_PROCESS_LOG_FILE_BYTES",
    "TerminalLogRetentionError",
    "enforce",
    "enforce_process_log_bounds",
    "list_batches",
    "now_utc",
    "preview",
    "process_log_bounds_preview",
    "purge",
    "purge_empty_batches",
    "quarantine",
    "reconcile_unclaimed",
    "restore",
]
