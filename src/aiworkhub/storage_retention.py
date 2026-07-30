"""Repository-scoped, preview-first retained-worktree quarantine lifecycle.

The dashboard never deletes a worktree directly. A read-only preview identifies
only clean, fully-pushed, policy-aged worktrees owned by the current repository.
An explicit user confirmation may atomically move those exact entries into a
same-volume quarantine. Restore is supported during the bounded undo window;
purge is a separate explicit action after that deadline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from . import repo_policy, task_store, worktree_storage
from .worker_workspace import configured_worktree_root


SCHEMA_ID = "aiworkhub.storage_retention.v1"
MANIFEST_NAME = "manifest.json"
QUARANTINE_DIRNAME = ".aiworkhub-quarantine"
AUDIT_RELATIVE_PATH = Path(".aiworkhub/runtime/storage/retention.audit.jsonl")
UNDO_DAYS = 7
MAX_MANIFEST_BYTES = 512 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class StorageRetentionError(RuntimeError):
    pass


def _repo_id(repo_root: Path) -> str:
    readiness = task_store.storage_readiness(repo_root)
    if not readiness.ready or not readiness.repo_id:
        raise StorageRetentionError(f"repository_storage_not_ready:{readiness.reason}")
    return readiness.repo_id


def _policy(repo_root: Path) -> tuple[int, int]:
    try:
        retention = repo_policy.load_policy(repo_root)["retention"]
        return int(retention["terminal_runs_days"]), int(retention["worktree_max_bytes"])
    except (KeyError, TypeError, ValueError, repo_policy.RepoPolicyError):
        defaults = repo_policy.DEFAULT_POLICY["retention"]
        return int(defaults["terminal_runs_days"]), int(defaults["worktree_max_bytes"])


def _quarantine_root(repo_root: Path, base: Path) -> Path:
    return base / QUARANTINE_DIRNAME / _repo_id(repo_root)


def _ensure_quarantine_root(repo_root: Path, base: Path) -> Path:
    parent = base / QUARANTINE_DIRNAME
    parent.mkdir(mode=0o700, exist_ok=True)
    root = _quarantine_root(repo_root, base)
    root.mkdir(mode=0o700, exist_ok=True)
    for candidate in (parent, root):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise StorageRetentionError("retention_quarantine_root_invalid")
    if root.resolve().parent != parent.resolve() or parent.resolve().parent != base:
        raise StorageRetentionError("retention_quarantine_root_escape")
    return root


def _read_quarantine_root(repo_root: Path, base: Path) -> Path | None:
    root = _quarantine_root(repo_root, base)
    try:
        info = root.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StorageRetentionError("retention_quarantine_root_invalid") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise StorageRetentionError("retention_quarantine_root_invalid")
    if root.resolve().parent.parent != base:
        raise StorageRetentionError("retention_quarantine_root_escape")
    return root


def _verified_batch(repo_root: Path, base: Path, batch_id: str) -> Path:
    if not _ID_RE.fullmatch(batch_id):
        raise StorageRetentionError("retention_batch_id_invalid")
    qroot = _ensure_quarantine_root(repo_root, base)
    batch = qroot / batch_id
    try:
        info = batch.lstat()
    except OSError as exc:
        raise StorageRetentionError("retention_batch_not_found") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise StorageRetentionError("retention_batch_invalid")
    if batch.resolve().parent != qroot.resolve():
        raise StorageRetentionError("retention_batch_escape")
    return batch


def _preview_payload(repo_root: Path, base: Path) -> dict[str, Any]:
    policy_days, max_bytes = _policy(repo_root)
    scan = worktree_storage.scan_worktrees(
        base,
        with_sizes=True,
        repo_root=repo_root,
    )
    plan = worktree_storage.plan_cleanup(
        scan,
        include_orphaned=False,
        min_age_days=policy_days,
    )
    candidates = [
        {
            "id": str(item.get("id") or ""),
            "head": str(item.get("head") or ""),
            "size_bytes": int(item.get("size_bytes") or 0),
            "modified_at_epoch": int(float(item.get("modified_at_epoch") or 0.0)),
        }
        for item in plan.get("would_remove") or []
    ]
    candidates.sort(key=lambda item: item["id"])
    digest_input = {
        "schema_id": SCHEMA_ID,
        "repo_id": _repo_id(repo_root),
        "policy_days": policy_days,
        "max_bytes": max_bytes,
        "candidates": candidates,
    }
    digest = hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "dry_run": True,
        "repository_scoped": True,
        "policy_days": policy_days,
        "max_bytes": max_bytes,
        "current_bytes": int(scan.get("summary", {}).get("total_bytes") or 0),
        "projected_bytes": max(
            0,
            int(scan.get("summary", {}).get("total_bytes") or 0)
            - sum(item["size_bytes"] for item in candidates),
        ),
        "candidate_count": len(candidates),
        "candidate_bytes": sum(item["size_bytes"] for item in candidates),
        "protected_count": len(plan.get("would_keep") or []),
        "preview_digest": digest,
        "candidates": candidates,
        "base": base,
    }


def _public_preview(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "base"}


def preview(repo_root: Path | str, *, base: Path | None = None) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root()).resolve()
    return _public_preview(_preview_payload(root, worktree_base))


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_MANIFEST_BYTES:
        raise StorageRetentionError("retention_manifest_too_large")
    fd, name = tempfile.mkstemp(prefix=".retention-", suffix=".tmp", dir=path.parent)
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


def _append_audit(repo_root: Path, event: Mapping[str, Any]) -> None:
    path = repo_root / AUDIT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(event), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _load_manifest(path: Path, repo_id: str) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise StorageRetentionError("retention_batch_not_found") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
        raise StorageRetentionError("retention_manifest_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageRetentionError("retention_manifest_invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_id") != SCHEMA_ID
        or value.get("repo_id") != repo_id
        or not _ID_RE.fullmatch(str(value.get("batch_id") or ""))
        or not isinstance(value.get("items"), list)
    ):
        raise StorageRetentionError("retention_manifest_identity_mismatch")
    return value


def quarantine(
    repo_root: Path | str,
    *,
    preview_digest: str,
    confirm: bool,
    base: Path | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise StorageRetentionError("explicit_confirmation_required")
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root()).resolve()
    current = _preview_payload(root, worktree_base)
    if preview_digest != current["preview_digest"]:
        raise StorageRetentionError("retention_preview_stale")
    if not current["candidates"]:
        return {"ok": True, "quarantined": 0, "bytes": 0, "batch_id": "", "no_op": True}

    now = datetime.now(timezone.utc)
    batch_id = f"q{now.strftime('%Y%m%dT%H%M%S')}-{preview_digest[:12]}"
    qroot = _ensure_quarantine_root(root, worktree_base)
    batch = qroot / batch_id
    batch.mkdir(parents=True, exist_ok=False, mode=0o700)
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
    moved = 0
    moved_bytes = 0
    repo_common_dir = worktree_storage._git_common_dir(root)
    for item in manifest["items"]:
        item_id = item["id"]
        if not _ID_RE.fullmatch(item_id):
            item["state"] = "skipped_invalid_id"
            _atomic_json(manifest_path, manifest)
            continue
        source = (worktree_base / item_id).resolve()
        destination = batch / item_id
        if source.parent != worktree_base or source.is_symlink() or destination.exists():
            item["state"] = "skipped_identity_changed"
            _atomic_json(manifest_path, manifest)
            continue
        # The digest proves the immediately preceding scan; lstat facts are
        # checked again directly before the same-volume atomic move.
        try:
            source_info = source.lstat()
        except OSError:
            item["state"] = "skipped_missing"
            _atomic_json(manifest_path, manifest)
            continue
        if not stat.S_ISDIR(source_info.st_mode) or int(source_info.st_mtime) != item["modified_at_epoch"]:
            item["state"] = "skipped_identity_changed"
            _atomic_json(manifest_path, manifest)
            continue
        git_state = worktree_storage._worktree_git_state(source / "worktree")
        if (
            worktree_storage._classify(git_state) != worktree_storage.CLASS_REMOVABLE_SAFE
            or git_state.get("head") != item["head"]
            or not repo_common_dir
            or git_state.get("parent_git_dir") != repo_common_dir
        ):
            item["state"] = "skipped_git_state_changed"
            _atomic_json(manifest_path, manifest)
            continue
        os.replace(source, destination)
        item["state"] = "quarantined"
        moved += 1
        moved_bytes += int(item["size_bytes"])
        _atomic_json(manifest_path, manifest)
    manifest["status"] = "quarantined" if moved else "empty"
    manifest["quarantined_count"] = moved
    manifest["quarantined_bytes"] = moved_bytes
    _atomic_json(manifest_path, manifest)
    _append_audit(root, {
        "schema_id": "aiworkhub.storage_retention_audit.v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "quarantine_completed",
        "batch_id": batch_id,
        "count": moved,
        "bytes": moved_bytes,
    })
    return {"ok": True, "batch_id": batch_id, "quarantined": moved, "bytes": moved_bytes, "no_op": False}


def list_batches(repo_root: Path | str, *, base: Path | None = None) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root()).resolve()
    repo_id = _repo_id(root)
    qroot = _read_quarantine_root(root, worktree_base)
    rows: list[dict[str, Any]] = []
    if qroot is not None:
        for entry in sorted(qroot.iterdir(), reverse=True):
            if not entry.is_dir() or not _ID_RE.fullmatch(entry.name):
                continue
            try:
                value = _load_manifest(entry / MANIFEST_NAME, repo_id)
            except StorageRetentionError:
                continue
            states = [str(item.get("state") or "") for item in value["items"] if isinstance(item, dict)]
            quarantined_bytes = sum(
                int(item.get("size_bytes") or 0)
                for item in value["items"]
                if isinstance(item, dict) and item.get("state") == "quarantined"
            )
            deadline = str(value.get("restore_deadline") or "")
            try:
                purge_eligible = datetime.now(timezone.utc) >= datetime.fromisoformat(deadline)
            except ValueError:
                purge_eligible = False
            rows.append({
                "batch_id": value["batch_id"],
                "created_at": str(value.get("created_at") or ""),
                "restore_deadline": deadline,
                "status": str(value.get("status") or "unknown"),
                "quarantined_count": states.count("quarantined"),
                "restored_count": states.count("restored"),
                "bytes": quarantined_bytes,
                "purge_eligible": purge_eligible,
            })
    return {"ok": True, "batches": rows[:100], "count": len(rows[:100])}


def restore(
    repo_root: Path | str,
    *,
    batch_id: str,
    confirm: bool,
    base: Path | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise StorageRetentionError("explicit_confirmation_required")
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root()).resolve()
    batch = _verified_batch(root, worktree_base, batch_id)
    manifest_path = batch / MANIFEST_NAME
    manifest = _load_manifest(manifest_path, _repo_id(root))
    restored = 0
    for item in manifest["items"]:
        if not isinstance(item, dict) or item.get("state") != "quarantined":
            continue
        item_id = str(item.get("id") or "")
        if not _ID_RE.fullmatch(item_id):
            continue
        source = batch / item_id
        destination = worktree_base / item_id
        if not source.is_dir() or source.is_symlink() or destination.exists():
            item["state"] = "restore_conflict"
            _atomic_json(manifest_path, manifest)
            continue
        os.replace(source, destination)
        item["state"] = "restored"
        restored += 1
        _atomic_json(manifest_path, manifest)
    manifest["status"] = "restored" if restored else manifest.get("status", "quarantined")
    _atomic_json(manifest_path, manifest)
    _append_audit(root, {
        "schema_id": "aiworkhub.storage_retention_audit.v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "restore_completed",
        "batch_id": batch_id,
        "count": restored,
    })
    return {"ok": True, "batch_id": batch_id, "restored": restored}


def purge(
    repo_root: Path | str,
    *,
    batch_id: str,
    confirm: bool,
    base: Path | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise StorageRetentionError("explicit_confirmation_required")
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root()).resolve()
    batch = _verified_batch(root, worktree_base, batch_id)
    manifest = _load_manifest(batch / MANIFEST_NAME, _repo_id(root))
    try:
        deadline = datetime.fromisoformat(str(manifest.get("restore_deadline") or ""))
    except ValueError as exc:
        raise StorageRetentionError("retention_deadline_invalid") from exc
    if datetime.now(timezone.utc) < deadline:
        raise StorageRetentionError("retention_undo_window_active")
    shutil.rmtree(batch)
    # The worktree registrations deliberately remain intact during the undo
    # window so restore is lossless. Only after permanent purge do we prune
    # missing registrations from this exact repository.
    worktree_storage._git(root, "worktree", "prune")
    _append_audit(root, {
        "schema_id": "aiworkhub.storage_retention_audit.v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "purge_completed",
        "batch_id": batch_id,
        "bytes": int(manifest.get("quarantined_bytes") or 0),
    })
    return {"ok": True, "batch_id": batch_id, "purged": True, "bytes": int(manifest.get("quarantined_bytes") or 0)}


__all__ = [
    "AUDIT_RELATIVE_PATH",
    "QUARANTINE_DIRNAME",
    "SCHEMA_ID",
    "StorageRetentionError",
    "list_batches",
    "preview",
    "purge",
    "quarantine",
    "restore",
]
