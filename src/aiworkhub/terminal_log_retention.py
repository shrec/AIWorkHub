"""Repository-bound retention for terminal worker output files.

The append-only process event ledger is canonical evidence and is never moved
or rewritten here.  Only the four per-request files owned by a terminal run
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from . import repo_policy, task_store


SCHEMA_ID = "aiworkhub.terminal_log_retention.v1"
AUDIT_SCHEMA_ID = "aiworkhub.terminal_log_retention_audit.v1"
PROCESS_LOG_RELATIVE_PATH = Path(".aiworkhub/runtime/process_logs/process_events.jsonl")
PROCESS_FILES_RELATIVE_PATH = Path(".aiworkhub/runtime/process_logs/processes")
QUARANTINE_RELATIVE_PATH = Path(".aiworkhub/runtime/storage/terminal-log-quarantine")
AUDIT_RELATIVE_PATH = Path(".aiworkhub/runtime/storage/terminal-log-retention.audit.jsonl")
MANIFEST_NAME = "manifest.json"
UNDO_DAYS = 7
KEEP_LAST_PER_TASK = 10
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024

_REQUEST_RE = re.compile(r"^[a-f0-9]{32}$")
_BATCH_RE = re.compile(r"^l[0-9]{8}T[0-9]{6}-[a-f0-9]{12}$")
_OWNED_SUFFIXES = (".request.json", ".stderr.log", ".stdout.log", ".supervisor.json")
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


def _latest_rows(root: Path) -> dict[str, dict[str, Any]]:
    ledger = root / PROCESS_LOG_RELATIVE_PATH
    try:
        info = ledger.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise TerminalLogRetentionError("terminal_log_ledger_unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TerminalLogRetentionError("terminal_log_ledger_invalid")
    if info.st_size > MAX_LEDGER_BYTES:
        raise TerminalLogRetentionError("terminal_log_ledger_too_large")
    latest: dict[str, dict[str, Any]] = {}
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise TerminalLogRetentionError("terminal_log_ledger_unreadable") from exc
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        request_id = str(row.get("request_id") or "")
        if not _REQUEST_RE.fullmatch(request_id):
            continue
        latest[request_id] = {**latest.get(request_id, {}), **row}
    return latest


def _candidate_payload(root: Path) -> dict[str, Any]:
    process_root = root / PROCESS_FILES_RELATIVE_PATH
    if process_root.exists():
        info = process_root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TerminalLogRetentionError("terminal_log_root_invalid")
    cutoff = datetime.now(timezone.utc) - timedelta(days=_logs_days(root))
    protected: list[dict[str, Any]] = []
    eligible_by_task: dict[str, list[dict[str, Any]]] = {}
    current_bytes = 0
    for request_id, row in _latest_rows(root).items():
        task_id = str(row.get("task_id") or "")
        files: list[dict[str, Any]] = []
        for suffix in _OWNED_SUFFIXES:
            name = f"{request_id}{suffix}"
            path = process_root / name
            info = _owned_regular_file(path, process_root)
            if info is None:
                continue
            current_bytes += int(info.st_size)
            files.append({
                "name": name,
                "size_bytes": int(info.st_size),
                "mtime_ns": int(info.st_mtime_ns),
            })
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
        eligible_by_task.setdefault(task_id, []).append(item)

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
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "dry_run": True,
        "repository_scoped": True,
        "logs_days": _logs_days(root),
        "keep_last_per_task": KEEP_LAST_PER_TASK,
        "current_bytes": current_bytes,
        "projected_bytes": max(0, current_bytes - candidate_bytes),
        "candidate_count": len(candidates),
        "candidate_bytes": candidate_bytes,
        "protected_count": len(protected),
        "preview_digest": digest,
        "candidates": candidates,
    }


def preview(repo_root: Path | str) -> dict[str, Any]:
    return _candidate_payload(Path(repo_root).resolve())


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
    if not current["candidates"]:
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


__all__ = ["TerminalLogRetentionError", "list_batches", "preview", "purge", "quarantine", "restore"]
