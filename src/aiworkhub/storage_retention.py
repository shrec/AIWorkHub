"""Repository-scoped, preview-first retained-worktree quarantine lifecycle.

The dashboard never deletes a worktree directly. A read-only preview identifies
policy-aged worktrees owned by the current repository that no live task
attempt still holds: either clean and fully pushed, or a superseded rework
attempt whose local commits were deliberately never pushed (see
:func:`plan_worktree_reclaim`). An explicit user confirmation may atomically
move those exact entries into a same-volume quarantine. Restore is supported
during the bounded undo window; purge is a separate explicit action after
that deadline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
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
LEGACY_LOG_RELATIVE_PATH = Path("logs")
CANONICAL_RUNTIME_RELATIVE_PATH = Path(".aiworkhub/runtime")
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


def _protected_attempt_ids(repo_root: Path) -> tuple[set[str], bool]:
    """Worktree ids a live task attempt still holds: never quarantine candidates.

    A worktree is protected only while it is the *current* claimed attempt of
    a task that is actively ``processing`` or ``review`` (the newest attempt
    under evaluation). Everything else -- a superseded rework predecessor, or
    an old attempt of a finished/archived/blocked/pending/superseded card --
    is no longer live and may be reclaimed regardless of its git dirty/
    unpushed state, since quarantine only moves it into a same-volume,
    restorable holding area; nothing is deleted until a separate purge.

    The claim path writes the live worktree's request id into
    ``launch_request_id``. ``accepted_request_id`` is populated only once a
    review is ACCEPTED and the card has already flipped to ``finished``, so a
    card genuinely ``processing`` or ``review`` always has it empty -- keying
    protection on it left every live attempt unprotected.

    Liveness is resolved with one unbounded read of the canonical ``tasks``
    table's lifecycle columns, not through ``task_store.list_tasks``: that
    reader's SQL ``LIMIT`` is capped at 5000 rows no matter what value is
    passed, so on a repository whose live-task count exceeds it, rows would
    silently fall outside the window and lose protection -- and any other
    fixed row cap would carry the identical defect under a different number.
    Reading every row's (status, worker_status, archived_at, card_json) is a
    single query whose bound is the exact size of the ``tasks`` table, which
    can never itself exclude a live card.

    Returns ``(protected_ids, verified)``. ``verified`` is False when task
    lineage could not be read at all, so the caller fails closed rather than
    guessing that an unreadable attempt is safe to reclaim.
    """
    protected: set[str] = set()
    try:
        db_path = task_store.canonical_db_path(repo_root)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            rows = conn.execute(
                "SELECT status, worker_status, archived_at, card_json FROM tasks"
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            if task_store.canonical_status(dict(row)) not in ("processing", "review"):
                continue
            try:
                card_json = json.loads(row["card_json"] or "{}")
            except json.JSONDecodeError:
                card_json = {}
            if not isinstance(card_json, dict):
                continue
            request_id = str(card_json.get("launch_request_id") or "").strip()
            if request_id:
                protected.add(request_id)
    except (task_store.TaskStoreError, sqlite3.Error, OSError):
        return set(), False
    return protected, True


def plan_worktree_reclaim(
    repo_root: Path | str,
    scan: Mapping[str, Any],
    *,
    min_age_days: int,
    max_bytes: int,
    current_bytes: int,
    now: float | None = None,
) -> dict[str, Any]:
    """Lineage-aware split of a worktree scan into reclaim vs. keep.

    Mirrors :func:`worktree_storage.plan_cleanup`'s shape, but a worktree is
    eligible once no live task attempt still holds it -- not merely once it is
    git-clean and fully pushed. A superseded rework attempt commonly carries
    local commits that were deliberately never pushed (the rework replaced
    them), which made the pure git-state check protect it forever regardless
    of rejection count. Orphaned worktrees (broken git metadata) are still
    never included here; that remains a separate, opt-in cleanup path.

    When ``current_bytes`` exceeds ``max_bytes``, the oldest eligible-but-
    under-age superseded worktrees are pulled forward (oldest modified first)
    until the projection clears the cap or the pool is exhausted, so a policy
    breach is never reported as nothing to reclaim.

    ``now`` is an explicit as-of Unix timestamp used to compute each
    worktree's age from its ``modified_at_epoch``. It defaults to the real
    current time; callers (tests included) may inject a synthetic value so
    age can be exercised deterministically without mutating filesystem
    mtimes.
    """
    root = Path(repo_root).resolve()
    protected_ids, lineage_verified = _protected_attempt_ids(root)
    minimum_age_seconds = max(0, min(int(min_age_days), 3650)) * 86400
    effective_now = time.time() if now is None else float(now)

    would_keep: list[dict[str, Any]] = []
    reclaimable: list[dict[str, Any]] = []
    for wt in scan.get("worktrees") or []:
        wt_id = str(wt.get("id") or "")
        if wt.get("class") == worktree_storage.CLASS_ORPHANED or wt_id in protected_ids:
            would_keep.append(wt)
            continue
        if wt.get("class") == worktree_storage.CLASS_REMOVABLE_SAFE or lineage_verified:
            reclaimable.append(wt)
        else:
            # Dirty/unpushed and card lineage could not be verified: fail closed.
            would_keep.append(wt)

    would_remove = [
        wt for wt in reclaimable
        if (effective_now - float(wt.get("modified_at_epoch") or 0.0)) >= minimum_age_seconds
    ]
    under_age = [wt for wt in reclaimable if wt not in would_remove]
    would_keep.extend(under_age)

    if current_bytes > max_bytes:
        projected = current_bytes - sum(int(wt.get("size_bytes") or 0) for wt in would_remove)
        for wt in sorted(under_age, key=lambda item: float(item.get("modified_at_epoch") or 0.0)):
            if projected <= max_bytes:
                break
            would_remove.append(wt)
            would_keep.remove(wt)
            projected -= int(wt.get("size_bytes") or 0)

    return {
        "base": scan.get("base"),
        "would_remove": would_remove,
        "would_keep": would_keep,
        "reclaim_bytes": sum(int(wt.get("size_bytes") or 0) for wt in would_remove),
        "kept_bytes": sum(int(wt.get("size_bytes") or 0) for wt in would_keep),
        "lineage_verified": lineage_verified,
    }


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


def repo_storage_footprint(repo_root: Path | str, *, base: Path | None = None) -> dict[str, Any]:
    """Single, shared definition of on-disk footprint bytes for every
    ``worktree_max_bytes`` cap comparison across this subsystem's surfaces
    (the retention preview and the dashboard telemetry), so they can never
    silently disagree about what "current bytes" means.

    Repo-scoped worktree bytes alone under-report usage on a repository whose
    legacy ``logs/`` tree or ``.aiworkhub/runtime`` canonical data are large;
    ``observed_total_bytes`` -- global worktree bytes plus both of those -- is
    the authoritative figure for cap comparisons.
    """
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root(root)).resolve()
    scan = worktree_storage.scan_worktrees(worktree_base, with_sizes=True, repo_root=root)
    global_scan = worktree_storage.scan_worktrees(worktree_base, with_sizes=True)
    repository_worktree_bytes = int(scan.get("summary", {}).get("total_bytes") or 0)
    global_worktree_bytes = int(global_scan.get("summary", {}).get("total_bytes") or 0)
    legacy_log_root = root / LEGACY_LOG_RELATIVE_PATH
    canonical_runtime_root = root / CANONICAL_RUNTIME_RELATIVE_PATH
    legacy_log_bytes = (
        worktree_storage.directory_size_bytes(legacy_log_root)
        if legacy_log_root.is_dir() and not legacy_log_root.is_symlink()
        else 0
    )
    canonical_runtime_bytes = (
        worktree_storage.directory_size_bytes(canonical_runtime_root)
        if canonical_runtime_root.is_dir() and not canonical_runtime_root.is_symlink()
        else 0
    )
    observed_total_bytes = global_worktree_bytes + legacy_log_bytes + canonical_runtime_bytes
    return {
        "base": worktree_base,
        "scan": scan,
        "observed_total_bytes": observed_total_bytes,
        "canonical_runtime_bytes": canonical_runtime_bytes,
        "legacy_log_bytes": legacy_log_bytes,
        "global_worktree_bytes": global_worktree_bytes,
        "repository_worktree_bytes": repository_worktree_bytes,
        "unattributed_or_foreign_worktree_bytes": max(
            0, global_worktree_bytes - repository_worktree_bytes
        ),
        "legacy_log_status": (
            "present_unmanaged" if legacy_log_bytes else "absent_or_empty"
        ),
    }


def _preview_payload(repo_root: Path, base: Path, *, now: float | None = None) -> dict[str, Any]:
    policy_days, max_bytes = _policy(repo_root)
    footprint = repo_storage_footprint(repo_root, base=base)
    scan = footprint["scan"]
    registrations = worktree_storage.scan_worktree_registrations(repo_root, base)
    observed_total_bytes = footprint["observed_total_bytes"]
    repository_worktree_bytes = footprint["repository_worktree_bytes"]
    plan = plan_worktree_reclaim(
        repo_root,
        scan,
        min_age_days=policy_days,
        max_bytes=max_bytes,
        current_bytes=observed_total_bytes,
        now=now,
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
        "registration_preview_digest": str(registrations.get("preview_digest") or ""),
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
        "current_bytes": observed_total_bytes,
        "worktree_current_bytes": repository_worktree_bytes,
        "footprint": {
            key: value for key, value in footprint.items() if key not in ("base", "scan")
        },
        "projected_bytes": max(
            0,
            observed_total_bytes
            - sum(item["size_bytes"] for item in candidates),
        ),
        "candidate_count": len(candidates),
        "candidate_bytes": sum(item["size_bytes"] for item in candidates),
        "protected_count": len(plan.get("would_keep") or []),
        "preview_digest": digest,
        "candidates": candidates,
        "registration_health": registrations,
        "base": base,
    }


def _public_preview(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "base"}


def preview(
    repo_root: Path | str, *, base: Path | None = None, now: float | None = None
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root(root)).resolve()
    return _public_preview(_preview_payload(root, worktree_base, now=now))


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
    now: float | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise StorageRetentionError("explicit_confirmation_required")
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root(root)).resolve()
    current = _preview_payload(root, worktree_base, now=now)
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
        # A candidate need not be CLASS_REMOVABLE_SAFE: plan_worktree_reclaim()
        # also admits superseded-lineage worktrees carrying unpushed local
        # commits that were deliberately never going to be pushed. Orphaned
        # (broken git metadata) worktrees are still refused here, matching
        # preview's own exclusion.
        git_state = worktree_storage._worktree_git_state(source / "worktree")
        if (
            worktree_storage._classify(git_state) == worktree_storage.CLASS_ORPHANED
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


def prune_stale_registrations(
    repo_root: Path | str,
    *,
    preview_digest: str,
    confirm: bool,
    base: Path | None = None,
) -> dict[str, Any]:
    """Prune only exact stale AIWorkHub registrations after fresh consent.

    ``git worktree prune`` operates at repository scope. The operation therefore
    refuses to run when *any* stale registration is outside AIWorkHub's exact
    configured worktree layout, so a foreign developer worktree is never
    silently affected.
    """

    if not confirm:
        raise StorageRetentionError("explicit_confirmation_required")
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root(root)).resolve()
    current = worktree_storage.scan_worktree_registrations(root, worktree_base)
    if not current.get("ok"):
        raise StorageRetentionError(str(current.get("error") or "registration_preview_failed"))
    if preview_digest != current.get("preview_digest"):
        raise StorageRetentionError("registration_preview_stale")
    if int(current.get("foreign_stale_count") or 0) > 0:
        raise StorageRetentionError("foreign_stale_registration_present")
    if int(current.get("candidate_overflow_count") or 0) > 0:
        raise StorageRetentionError("registration_candidate_limit_exceeded")
    candidates = [
        str(item.get("id") or "")
        for item in current.get("stale_candidates") or []
        if isinstance(item, dict)
    ]
    if not candidates:
        return {"ok": True, "pruned": 0, "ids": [], "no_op": True}

    rc, _output = worktree_storage._git(root, "worktree", "prune", "--expire", "now", "--verbose")
    if rc != 0:
        raise StorageRetentionError("worktree_registration_prune_failed")
    after = worktree_storage.scan_worktree_registrations(root, worktree_base)
    if not after.get("ok"):
        raise StorageRetentionError(str(after.get("error") or "registration_rescan_failed"))
    remaining = {
        str(item.get("id") or "")
        for item in after.get("stale_candidates") or []
        if isinstance(item, dict)
    }
    pruned_ids = sorted(set(candidates) - remaining)
    if remaining.intersection(candidates):
        raise StorageRetentionError("worktree_registration_prune_incomplete")
    _append_audit(root, {
        "schema_id": "aiworkhub.storage_retention_audit.v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "stale_registration_prune_completed",
        "count": len(pruned_ids),
        "ids": pruned_ids,
    })
    return {"ok": True, "pruned": len(pruned_ids), "ids": pruned_ids, "no_op": False}


def list_batches(repo_root: Path | str, *, base: Path | None = None) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root(root)).resolve()
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
    worktree_base = (base or configured_worktree_root(root)).resolve()
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
    worktree_base = (base or configured_worktree_root(root)).resolve()
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
    "plan_worktree_reclaim",
    "preview",
    "prune_stale_registrations",
    "purge",
    "quarantine",
    "restore",
]
