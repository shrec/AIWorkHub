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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from . import process_event_ledger, provider_usage, repo_policy, task_store


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

_REQUEST_RE = re.compile(r"^[a-f0-9]{32}$")
_BATCH_RE = re.compile(r"^l[0-9]{8}T[0-9]{6}-[a-f0-9]{12}$")
_OWNED_SUFFIXES = (".request.json", ".stderr.log", ".stdout.log", ".supervisor.json")
_enforcement_lock = threading.Lock()
_TERMINAL_STATES = frozenset({
    "review_ready", "exited", "exited_without_review", "timed_out", "cancelled",
    "launch_failed", "worker_failed", "scope_rejected", "validation_failed",
    "promotion_conflict", "finalize_failed", "monitor_error", "blocked",
})


class TerminalLogRetentionError(RuntimeError):
    pass


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
    for row in process_event_ledger.iter_events(ledger):
        request_id = str(row.get("request_id") or "")
        if not _REQUEST_RE.fullmatch(request_id):
            continue
        latest[request_id] = {**latest.get(request_id, {}), **row}
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


def _candidate_payload(root: Path) -> dict[str, Any]:
    process_root = root / PROCESS_FILES_RELATIVE_PATH
    if process_root.exists():
        info = process_root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TerminalLogRetentionError("terminal_log_root_invalid")
    cutoff = datetime.now(timezone.utc) - timedelta(days=_logs_days(root))
    protected: list[dict[str, Any]] = []
    eligible_by_task: dict[str, list[dict[str, Any]]] = {}
    inventory: dict[str, list[dict[str, Any]]] = {}
    if process_root.is_dir():
        for path in process_root.iterdir():
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
            inventory.setdefault(request_id, []).append({
                "name": path.name,
                "size_bytes": int(info.st_size),
                "mtime_ns": int(info.st_mtime_ns),
            })
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
    for request_id, row in latest_rows.items():
        task_id = str(row.get("task_id") or "")
        files = sorted(inventory.pop(request_id, []), key=lambda item: item["name"])
        if not files:
            continue
        newest_ns = max(item["mtime_ns"] for item in files)
        newest_at = datetime.fromtimestamp(newest_ns / 1_000_000_000, timezone.utc)
        state = str(row.get("state") or "")
        task = task_store.get_task(root, task_id) if task_id else None
        task_status = str((task or {}).get("status") or "unknown")
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
    orphan_candidates: list[dict[str, Any]] = []
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
    candidates.extend(orphan_candidates)
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
    if legacy_candidate is not None:
        digest_source["legacy_candidate"] = legacy_candidate
        digest = hashlib.sha256(
            json.dumps(digest_source, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    observed_current_bytes = canonical_current_bytes + legacy_current_bytes
    total_candidate_bytes = candidate_bytes + (
        int(legacy_candidate["size_bytes"]) if legacy_candidate else 0
    )
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "dry_run": True,
        "repository_scoped": True,
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
        "legacy_candidate": legacy_candidate,
        "projected_bytes": max(0, observed_current_bytes - total_candidate_bytes),
        "candidate_count": len(candidates) + (1 if legacy_candidate else 0),
        "candidate_bytes": total_candidate_bytes,
        "protected_count": len(protected),
        "preview_digest": digest,
        "candidates": candidates,
    }


def preview(
    repo_root: Path | str,
    *,
    cursor: int = 0,
    limit: int = DEFAULT_PREVIEW_LIMIT,
    include_candidates: bool = True,
) -> dict[str, Any]:
    """Return one bounded page while hashing the full eligible population."""

    start = max(0, int(cursor))
    page_limit = max(1, min(MAX_PREVIEW_LIMIT, int(limit)))
    result = _candidate_payload(Path(repo_root).resolve())
    candidates = list(result.pop("candidates", []))
    total = len(candidates)
    end = min(total, start + page_limit)
    result.update({
        "candidate_total": total,
        "cursor": start,
        "limit": page_limit,
        "returned_count": max(0, end - start) if include_candidates else 0,
        "next_cursor": end if include_candidates and end < total else None,
        "response_bounded": True,
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
    now = datetime.now(timezone.utc)
    batch_id = f"l{now.strftime('%Y%m%dT%H%M%S')}-{preview_digest[:12]}"
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
    _append_audit(root, {"schema_id": AUDIT_SCHEMA_ID, "timestamp": datetime.now(timezone.utc).isoformat(), "action": "quarantine_completed", "batch_id": batch_id, "files": moved_files, "bytes": moved_bytes})
    return {"ok": True, "batch_id": batch_id, "quarantined": moved_files, "bytes": moved_bytes, "no_op": False}


def list_batches(repo_root: Path | str) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    qroot = root / QUARANTINE_RELATIVE_PATH
    if not qroot.exists():
        return {"ok": True, "batches": [], "count": 0}
    rows: list[dict[str, Any]] = []
    for entry in sorted(qroot.iterdir(), reverse=True):
        if not entry.is_dir() or not _BATCH_RE.fullmatch(entry.name):
            continue
        try:
            value = _manifest(entry / MANIFEST_NAME, _repo_id(root))
            deadline = datetime.fromisoformat(str(value.get("restore_deadline") or ""))
        except (TerminalLogRetentionError, ValueError):
            continue
        states = [item.get("state") for item in value.get("items") or [] if isinstance(item, dict)]
        legacy = value.get("legacy_store")
        if isinstance(legacy, dict):
            states.append(legacy.get("state"))
        rows.append({
            "batch_id": entry.name,
            "created_at": str(value.get("created_at") or ""),
            "restore_deadline": deadline.isoformat(),
            "status": str(value.get("status") or "unknown"),
            "quarantined_count": states.count("quarantined"),
            "restored_count": states.count("restored"),
            "bytes": int(value.get("quarantined_bytes") or 0),
            "purge_eligible": datetime.now(timezone.utc) >= deadline,
        })
    return {"ok": True, "batches": rows[:100], "count": len(rows[:100])}


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
        if not isinstance(item, dict) or item.get("state") != "quarantined":
            continue
        request_id = str(item.get("request_id") or "")
        source_root = batch / request_id
        expected = item.get("files") or []
        if not _REQUEST_RE.fullmatch(request_id) or any((process_root / str(entry.get("name") or "")).exists() for entry in expected):
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
    _append_audit(root, {"schema_id": AUDIT_SCHEMA_ID, "timestamp": datetime.now(timezone.utc).isoformat(), "action": "restore_completed", "batch_id": batch_id, "files": restored})
    return {"ok": True, "batch_id": batch_id, "restored": restored}


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
    if datetime.now(timezone.utc) < deadline:
        raise TerminalLogRetentionError("retention_undo_window_active")
    released = int(manifest.get("quarantined_bytes") or 0)
    shutil.rmtree(batch)
    _append_audit(root, {"schema_id": AUDIT_SCHEMA_ID, "timestamp": datetime.now(timezone.utc).isoformat(), "action": "purge_completed", "batch_id": batch_id, "bytes": released})
    return {"ok": True, "batch_id": batch_id, "purged": True, "bytes": released}


def enforce(repo_root: Path | str) -> dict[str, Any]:
    """Apply configured log age and quarantine deadlines once.

    Old active output enters reversible quarantine.  Permanent deletion is
    limited to batches whose independent seven-day undo window has expired.
    """

    root = Path(repo_root).resolve()
    if not _enforcement_lock.acquire(blocking=False):
        return {"ok": True, "status": "already_running", "repository_scoped": True}
    try:
        purged_batches = 0
        purged_bytes = 0
        for row in list_batches(root).get("batches") or []:
            if not row.get("purge_eligible"):
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
        _append_audit(root, {
            "schema_id": AUDIT_SCHEMA_ID,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "policy_enforcement_completed",
            "quarantined_files": quarantined_files,
            "quarantined_bytes": quarantined_bytes,
            "purged_batches": purged_batches,
            "purged_bytes": purged_bytes,
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
        }
    finally:
        _enforcement_lock.release()


__all__ = [
    "TerminalLogRetentionError",
    "enforce",
    "list_batches",
    "preview",
    "purge",
    "quarantine",
    "restore",
]
