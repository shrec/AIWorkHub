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
import math
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
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
# Wall-clock ceiling for the read-only preview's on-disk measurement. The
# dashboard snapshot is already bounded because it measures in a background
# thread and serves a cached result; the preview acquires the identical bound
# (see :func:`preview`) so a per-file walk over hundreds of worktrees can never
# run to the caller's request timeout. Past this deadline the preview returns a
# result explicitly labelled incomplete rather than blocking.
PREVIEW_DEADLINE_SECONDS = 90.0
# Upper bound on any accepted wall-clock deadline. A preview measurement can
# never sanely need more than a day, and -- more importantly -- a finiteness
# check alone is not enough: an absurd-but-finite value such as ``1e300`` passes
# ``math.isfinite`` yet overflows the C timeout ``threading.Event.wait`` derives
# from it (seconds -> int64 nanoseconds), so the wait raises ``OverflowError``
# instead of honouring a bound. Anything above this ceiling is rejected, not
# clamped, for the same reason ``inf``/NaN are: a bound a caller can push past
# what the wait can represent is not a bound.
MAX_DEADLINE_SECONDS = 86_400.0
# Canonical lifecycle states in which a card can never launch again, so any
# ``rework_predecessor`` it once referenced is genuinely superseded and stays
# reclaimable. Every other state (pending/processing/review/blocked) keeps its
# predecessor attempt protected. Keyed on lifecycle only -- never mtime/age.
_FINISHED_STATUSES = frozenset({"finished", "archived", "superseded"})
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


def _protected_attempt_ids(
    repo_root: Path,
) -> tuple[dict[str, str], bool, dict[str, list[str]]]:
    """Worktree ids a live attempt or in-flight rework lineage still holds.

    A worktree is protected -- and therefore never a quarantine candidate --
    while any of these hold, keyed strictly on the canonical card lifecycle
    (never on file mtime, age, or recency):

    * ``live_worker`` -- it is the *current* claimed attempt of a card that is
      actively ``processing`` or ``review`` (the newest attempt under
      evaluation), recorded by the claim path in ``card_json`` as
      ``launch_request_id``.
    * ``rework_predecessor_retained`` -- it is referenced as the
      ``rework_predecessor`` of a card that has NOT yet reached a finished
      lifecycle state. A card rejected back to ``pending`` (or otherwise still
      ``processing``/``review``/``blocked``) can relaunch, and the launcher
      overlays this predecessor's changed files into the successor attempt
      (see ``worker_workspace._materialize_rework_predecessor``); reclaiming it
      would strand the card with ``rework_predecessor_workspace_missing`` and
      silently destroy work no successor has replaced. Protection ends only
      once the referencing card itself reaches a finished state
      (``finished``/``archived``/``superseded``) -- i.e. once a successor has
      genuinely been sealed -- so genuinely superseded lineage stays
      reclaimable and an over-cap repository can still force reclamation.

    ``accepted_request_id`` is populated only once a review is ACCEPTED and the
    card has already flipped to ``finished``, so a card genuinely
    ``processing``/``review`` always has it empty -- keying protection on it
    left every live attempt unprotected.

    Liveness is resolved with one unbounded read of the canonical ``tasks``
    table's lifecycle columns, not through ``task_store.list_tasks``: that
    reader's SQL ``LIMIT`` is capped at 5000 rows no matter what value is
    passed, so on a repository whose live-task count exceeds it, rows would
    silently fall outside the window and lose protection -- and any other
    fixed row cap would carry the identical defect under a different number.
    Reading every row's (status, worker_status, archived_at, card_json) is a
    single query whose bound is the exact size of the ``tasks`` table, which
    can never itself exclude a live card.

    Returns ``(protected, verified, pinned_by)``. ``protected`` maps each
    protected request id to its reason. ``pinned_by`` maps each
    ``rework_predecessor_retained`` worktree id to the sorted list of card ids
    still pinning it, so the preview can report how much storage is held by
    in-flight rework lineage AND which cards hold it -- an operator can then see
    a bounded, named standoff (see :func:`plan_worktree_reclaim`) rather than a
    silent one. ``verified`` is False when task lineage could not be read at
    all, so the caller fails closed rather than guessing that an unreadable
    attempt is safe to reclaim.
    """
    protected: dict[str, str] = {}
    pinned_by: dict[str, list[str]] = {}
    try:
        db_path = task_store.canonical_db_path(repo_root)
        # Path().resolve().as_uri() percent-encodes '#' as %23; the raw
        # f"file:{db_path}?mode=ro" form lets a '#' in the path start a URI
        # fragment, silently dropping ?mode=ro so SQLite opens the connection
        # read-write and create-if-missing at a truncated path.
        ro_uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(ro_uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout=5000")
            rows = conn.execute(
                "SELECT task_id, status, worker_status, archived_at, card_json FROM tasks"
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            status = task_store.canonical_status(dict(row))
            try:
                card_json = json.loads(row["card_json"] or "{}")
            except json.JSONDecodeError:
                card_json = {}
            if not isinstance(card_json, dict):
                continue
            if status in ("processing", "review"):
                request_id = str(card_json.get("launch_request_id") or "").strip()
                if request_id:
                    protected[request_id] = "live_worker"
            if status not in _FINISHED_STATUSES:
                predecessor = card_json.get("rework_predecessor")
                if isinstance(predecessor, dict):
                    predecessor_id = str(predecessor.get("request_id") or "").strip()
                    if predecessor_id:
                        protected.setdefault(predecessor_id, "rework_predecessor_retained")
                        card_id = str(row["task_id"] or "").strip()
                        if card_id:
                            holders = pinned_by.setdefault(predecessor_id, [])
                            if card_id not in holders:
                                holders.append(card_id)
    except (task_store.TaskStoreError, sqlite3.Error, OSError):
        return {}, False, {}
    for holders in pinned_by.values():
        holders.sort()
    return protected, True, pinned_by


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
    protected_ids, lineage_verified, pinned_by = _protected_attempt_ids(root)
    minimum_age_seconds = max(0, min(int(min_age_days), 3650)) * 86400
    validated_now = _resolve_now(now)
    effective_now = time.time() if validated_now is None else validated_now

    would_keep: list[dict[str, Any]] = []
    reclaimable: list[dict[str, Any]] = []
    # Why each kept worktree is being retained, so the preview can distinguish a
    # live worker from a retained rework predecessor without reading the code.
    protection_reasons: dict[str, str] = {}
    for wt in scan.get("worktrees") or []:
        wt_id = str(wt.get("id") or "")
        if wt.get("class") == worktree_storage.CLASS_ORPHANED:
            would_keep.append(wt)
            protection_reasons[wt_id] = "orphaned"
            continue
        if wt_id in protected_ids:
            would_keep.append(wt)
            protection_reasons[wt_id] = protected_ids[wt_id]
            continue
        if wt.get("class") == worktree_storage.CLASS_REMOVABLE_SAFE or lineage_verified:
            reclaimable.append(wt)
        else:
            # Dirty/unpushed and card lineage could not be verified: fail closed.
            would_keep.append(wt)
            protection_reasons[wt_id] = "lineage_unverified"

    would_remove = [
        wt for wt in reclaimable
        if (effective_now - float(wt.get("modified_at_epoch") or 0.0)) >= minimum_age_seconds
    ]
    under_age = [wt for wt in reclaimable if wt not in would_remove]
    would_keep.extend(under_age)
    for wt in under_age:
        protection_reasons.setdefault(str(wt.get("id") or ""), "under_age")

    if current_bytes > max_bytes:
        projected = current_bytes - sum(int(wt.get("size_bytes") or 0) for wt in would_remove)
        for wt in sorted(under_age, key=lambda item: float(item.get("modified_at_epoch") or 0.0)):
            if projected <= max_bytes:
                break
            would_remove.append(wt)
            would_keep.remove(wt)
            protection_reasons.pop(str(wt.get("id") or ""), None)
            projected -= int(wt.get("size_bytes") or 0)

    # Rework-predecessor lineage is pinned while its card is not finished, and
    # the over-cap forcing path above deliberately cannot evict it -- reclaiming
    # an in-flight predecessor is the exact data loss this planner exists to
    # prevent. That protection has no upper bound, so a card abandoned in
    # ``pending`` pins its predecessor's bytes indefinitely. Rather than let that
    # be a silent standoff between two correct rules, surface it: report the
    # pinned bytes and the exact cards holding them as their own preview line. If
    # ``pinned_predecessor_bytes`` alone exceeds the cap, the correct escalation
    # is human, not a planner override -- resolve or finish the naming cards (so
    # their pin lifts and the lineage becomes reclaimable normally), never quietly
    # evict a predecessor a live card still needs.
    #
    # Membership is keyed on ``pinned_by`` -- every worktree a non-finished card
    # references as its ``rework_predecessor`` -- NOT on the single displayed
    # ``protection_reason``. A worktree can be pinned for two reasons at once: if
    # it is ALSO the live worker of another card its reason reads ``live_worker``,
    # yet it is still a pinned predecessor holding storage a live card needs.
    # Keying on the reason string dropped exactly that overlap from the bytes an
    # operator reads, under-reporting the case most likely to matter; count it
    # under both instead.
    pinned_predecessors: list[dict[str, Any]] = []
    for wt in would_keep:
        wt_id = str(wt.get("id") or "")
        if wt_id not in pinned_by:
            continue
        pinned_predecessors.append({
            "id": wt_id,
            "size_bytes": int(wt.get("size_bytes") or 0),
            "pinned_by": list(pinned_by.get(wt_id, [])),
        })
    pinned_predecessors.sort(key=lambda item: item["id"])
    pinned_predecessor_bytes = sum(item["size_bytes"] for item in pinned_predecessors)

    return {
        "base": scan.get("base"),
        "would_remove": would_remove,
        "would_keep": would_keep,
        "reclaim_bytes": sum(int(wt.get("size_bytes") or 0) for wt in would_remove),
        "kept_bytes": sum(int(wt.get("size_bytes") or 0) for wt in would_keep),
        "lineage_verified": lineage_verified,
        "protection_reasons": protection_reasons,
        "pinned_predecessors": pinned_predecessors,
        "pinned_predecessor_bytes": pinned_predecessor_bytes,
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
    protection_reasons = plan.get("protection_reasons") or {}
    protected = sorted(
        (
            {
                "id": str(wt.get("id") or ""),
                "reason": protection_reasons.get(str(wt.get("id") or ""), "protected"),
            }
            for wt in plan.get("would_keep") or []
        ),
        key=lambda item: item["id"],
    )
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
        "complete": True,
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
        "protected": protected,
        # In-flight rework lineage held off-limits, reported as its own line so an
        # operator can see how much storage is pinned and by exactly which cards
        # (never evicted by the over-cap path; see plan_worktree_reclaim).
        "pinned_predecessor_bytes": int(plan.get("pinned_predecessor_bytes") or 0),
        "pinned_predecessors": plan.get("pinned_predecessors") or [],
        "preview_digest": digest,
        "candidates": candidates,
        "registration_health": registrations,
        "base": base,
    }


def _public_preview(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "base"}


# Single-flight coordinator for the expensive footprint walk. EVERY entry
# point that runs the measurement (``preview`` and ``quarantine``) goes through
# here, so no call path can run the walk without a finite deadline and repeated
# timed-out previews can never stack N concurrent filesystem walks -- each
# holding its own SQLite connection -- and drive unbounded disk I/O. At most one
# measurement runs per (repository, base, as-of) key: a second caller ATTACHES
# to the running walk instead of starting another, which bounds the work itself
# rather than only the caller's wait. The per-file walk lives in
# ``worktree_storage`` and is measured off the request thread exactly as the
# already-bounded ``storage_observability`` snapshot does.
_measure_lock = threading.Lock()
_measurements: dict[Any, "_Measurement"] = {}


class _Measurement:
    __slots__ = ("done", "value", "error", "succeeded")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.error: Exception | None = None
        # Set True ONLY when ``fn()`` returns a value. Completion (``done``) plus
        # a falsy ``error`` is NOT proof of success: a control-flow BaseException
        # propagates out of the worker leaving ``error`` None, and the finally
        # still sets ``done``. Keying success on this explicit flag -- never on
        # ``error is None`` -- makes a failed measurement impossible to represent
        # as a ``(value, True)`` success by any path (see _measure_within_deadline).
        self.succeeded = False


def _require_finite(
    value: Any,
    error_code: str,
    *,
    minimum: float | None = None,
    minimum_exclusive: bool = False,
    maximum: float | None = None,
) -> float:
    """The single gate every externally-supplied numeric input to this module
    passes through -- the preview/quarantine ``deadline_seconds`` and the as-of
    ``now`` alike -- so no rejection path can leak a raw ``ValueError`` or
    ``OverflowError`` and no bad value can silently degenerate a bound or the
    single-flight dedup key.

    ``value`` is rejected with ``StorageRetentionError(error_code)`` when it is:

    * non-numeric or otherwise unconvertible -- ``float()`` raises ``TypeError``
      or ``ValueError``, or ``OverflowError`` for an integer too large to
      represent as a float;
    * NaN or infinite -- a NaN in particular compares unequal to itself, so a
      NaN ``now`` would hand every caller a distinct ``_measure_key`` and defeat
      the single-flight dedup, letting concurrent previews each start their own
      footprint walk (the exact availability failure this module prevents);
    * outside its permitted range -- below ``minimum`` (or equal to it when
      ``minimum_exclusive``), or above ``maximum`` -- because a bound a caller
      can push past what the wait's C timeout can represent is not a bound.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise StorageRetentionError(error_code) from None
    if not math.isfinite(number):
        raise StorageRetentionError(error_code)
    if minimum is not None and (
        number < minimum or (minimum_exclusive and number == minimum)
    ):
        raise StorageRetentionError(error_code)
    if maximum is not None and number > maximum:
        raise StorageRetentionError(error_code)
    return number


def _resolve_deadline(deadline_seconds: float | None) -> float:
    """Return the validated wall-clock bound, which no argument can switch off.

    ``None`` selects :data:`PREVIEW_DEADLINE_SECONDS`. Any explicit value goes
    through :func:`_require_finite`, so it must be finite, strictly positive,
    and no greater than :data:`MAX_DEADLINE_SECONDS`: ``float('inf')`` would make
    ``threading.Event.wait`` block forever; NaN or a non-positive value
    degenerates the bound; and an absurd-but-finite value like ``1e300`` passes
    ``math.isfinite`` yet overflows the wait's C timeout and raises
    ``OverflowError`` -- each silently defeating the documented ceiling. A bound
    an argument can disable (or crash the wait with) is not a bound, so those
    are rejected rather than clamped.
    """
    if deadline_seconds is None:
        return PREVIEW_DEADLINE_SECONDS
    return _require_finite(
        deadline_seconds,
        "retention_deadline_invalid",
        minimum=0.0,
        minimum_exclusive=True,
        maximum=MAX_DEADLINE_SECONDS,
    )


def _resolve_now(now: float | None) -> float | None:
    """Validate a caller-supplied as-of ``now`` through the same gate as the
    deadline. ``None`` means "measure against the live clock" and passes through
    untouched; any explicit value must be finite and numeric. NaN is rejected
    here rather than deeper in :func:`_measure_key`, where it would compare
    unequal to itself and give every caller a distinct single-flight key.
    """
    if now is None:
        return None
    return _require_finite(now, "retention_now_invalid")


def _measure_worker(key: Any, measurement: "_Measurement", fn: Any) -> None:
    try:
        measurement.value = fn()
        # Reached only on a genuine return: this is the ONE place success is
        # recorded, so no failure path can leave the measurement looking complete
        # and successful at once.
        measurement.succeeded = True
    except Exception as exc:  # noqa: BLE001 -- re-raised on the caller below
        # Ordinary errors are captured for precise re-raise. SystemExit,
        # KeyboardInterrupt and GeneratorExit derive from BaseException, not
        # Exception, so they propagate out of this worker thread instead of being
        # stored -- but ``succeeded`` stays False, so the caller still surfaces a
        # measurement-failed error rather than a ``(None, True)`` partial success.
        measurement.error = exc
    finally:
        # Set the completion event BEFORE evicting the single-flight key. Popping
        # first would leave a window in which the entry is gone but ``done`` is
        # not yet set; a concurrent caller arriving there would find no entry,
        # miss this just-finished result, and launch a duplicate filesystem walk
        # -- the exact availability failure this single-flight exists to prevent,
        # arriving through a race. With ``done`` set first, any caller that still
        # sees the entry attaches to the finished measurement instead.
        measurement.done.set()
        with _measure_lock:
            if _measurements.get(key) is measurement:
                _measurements.pop(key, None)


def _measure_within_deadline(
    key: Any, fn: Any, deadline_seconds: float
) -> tuple[Any, bool]:
    """Run ``fn`` under a shared single-flight walk; return ``(value, True)`` if
    it finishes within ``deadline_seconds``, else ``(None, False)``.

    Concurrent or repeated callers for the same ``key`` attach to ONE running
    daemon walk instead of each starting their own, so a stalled measurement can
    never be amplified into stacked threads and stacked disk I/O. The caller is
    released at the deadline even while the walk is still running (matching the
    bounded snapshot path). An ``(None, False)`` result is returned ONLY once the
    walk is confirmed still in flight -- an honest "still measuring" partial, not
    a failure. If the walk has already raised by the time the caller is released,
    that exception is re-raised here rather than being silently discarded and
    presented as an incomplete ``ok: True`` success. A walk that finished without
    producing a value (``succeeded`` False -- e.g. a control-flow BaseException
    tore the worker down, leaving ``error`` None) is likewise raised as a
    measurement failure, never returned as ``(None, True)``.
    """
    with _measure_lock:
        measurement = _measurements.get(key)
        start = measurement is None
        if start:
            measurement = _Measurement()
            _measurements[key] = measurement

    if start:
        threading.Thread(
            target=_measure_worker,
            args=(key, measurement, fn),
            daemon=True,
            name="aiworkhub-retention-preview",
        ).start()

    finished = measurement.done.wait(deadline_seconds)
    if not finished and not measurement.done.is_set():
        # Genuinely still walking: an honest incomplete result, never a failure
        # dressed as one -- no error has occurred yet at this point.
        return None, False
    # Finished within the deadline, or completed in the instant between the wait
    # timing out and this check. Either way surface a real error so a genuine
    # failure is never reported as a partial success.
    if measurement.error is not None:
        raise measurement.error
    if not measurement.succeeded:
        # Completed (``done`` set) but produced no value: a control-flow
        # BaseException propagated out of the worker, leaving ``error`` None. That
        # is a FAILED measurement, not a slow one -- raise rather than hand back a
        # ``(None, True)`` success that would crash preview with an AttributeError.
        raise StorageRetentionError("retention_measurement_failed")
    return measurement.value, True


def _measure_key(root: Path, base: Path, now: float | None) -> tuple[str, str, float | None]:
    """The single-flight identity of one footprint measurement. ``now`` is part
    of the key so a synthetic as-of injected by one caller can never be served
    to another caller measuring the live clock."""
    return (str(root), str(base), None if now is None else float(now))


def _incomplete_preview(
    repo_root: Path, deadline_seconds: float, unmeasured: list[str]
) -> dict[str, Any]:
    """A bounded fail-safe when the on-disk measurement cannot finish in time.

    A partial scan must never be published as a completed footprint: reporting
    a smaller number than reality would invite an operator to quarantine on
    incomplete evidence. Every measured byte/candidate field is therefore
    withheld (``None``/empty), no actionable preview digest is emitted, and the
    result is explicitly labelled ``complete=False`` naming exactly which
    measurements did not finish.

    The response keeps the SAME keys the complete payload always emits, so a
    caller parses one schema whether or not the deadline was hit. The cheap
    policy fields need no filesystem walk and carry their real values; every
    field that depends on the unfinished walk -- ``footprint``,
    ``registration_health`` and every measured byte/candidate/pinned field -- is
    present but explicitly ``None``/empty and named in ``unmeasured`` rather than
    being absent (which would silently change the response shape).
    """
    policy_days, max_bytes = _policy(repo_root)
    # Every response field whose value depends on the unfinished walk is both
    # withheld (None/empty) below AND named here, so the docstring's promise --
    # a partial scan names exactly what it could not measure -- holds literally
    # rather than for only two of the fields.
    withheld_fields = {
        "current_bytes",
        "worktree_current_bytes",
        "footprint",
        "projected_bytes",
        "candidate_count",
        "candidate_bytes",
        "protected_count",
        "candidates",
        "protected",
        "pinned_predecessor_bytes",
        "pinned_predecessors",
        "registration_health",
        "preview_digest",
    }
    return {
        # ``ok`` tracks ``complete`` so a consumer keying on ``ok`` alone can never
        # read a partial measurement as a whole one: a completed footprint is
        # ``ok=True/complete=True``; an unfinished one is ``ok=False/complete=False``
        # (distinguished from a hard error by ``incomplete=True``).
        "ok": False,
        "schema_id": SCHEMA_ID,
        "dry_run": True,
        "complete": False,
        "incomplete": True,
        "repository_scoped": True,
        "incomplete_reason": "measurement_deadline_exceeded",
        "unmeasured": sorted(set(unmeasured) | withheld_fields),
        "deadline_seconds": deadline_seconds,
        "policy_days": policy_days,
        "max_bytes": max_bytes,
        "current_bytes": None,
        "worktree_current_bytes": None,
        "footprint": None,
        "projected_bytes": None,
        "candidate_count": None,
        "candidate_bytes": None,
        "protected_count": None,
        "candidates": [],
        "protected": [],
        # Pinned-predecessor accounting depends on the same unfinished walk (the
        # worktree sizes), so it is withheld here rather than reported as zero --
        # a partial scan must never publish a smaller pinned figure as complete.
        "pinned_predecessor_bytes": None,
        "pinned_predecessors": [],
        "registration_health": None,
        "preview_digest": "",
    }


def preview(
    repo_root: Path | str,
    *,
    base: Path | None = None,
    now: float | None = None,
    deadline_seconds: float | None = None,
) -> dict[str, Any]:
    """Bounded, read-only repository-scoped reclaim preview.

    The full footprint measurement runs under a wall-clock deadline
    (:data:`PREVIEW_DEADLINE_SECONDS` unless overridden) so this never blocks a
    caller to its own request timeout the way the unbounded path did on a
    Windows repository with hundreds of worktrees. If the deadline is exceeded
    the result is labelled incomplete (see :func:`_incomplete_preview`) rather
    than reporting a truncated -- and therefore smaller -- footprint.
    """
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root(root)).resolve()
    now = _resolve_now(now)
    deadline = _resolve_deadline(deadline_seconds)
    payload, complete = _measure_within_deadline(
        _measure_key(root, worktree_base, now),
        lambda: _preview_payload(root, worktree_base, now=now),
        deadline,
    )
    if not complete:
        return _incomplete_preview(
            root, deadline, ["worktree_footprint_scan", "reclaim_candidates"]
        )
    return _public_preview(payload)


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
    now = _resolve_now(now)
    # The quarantine action re-runs the SAME footprint measurement to reconfirm
    # the digest, so it acquires the IDENTICAL wall-clock bound and single-flight
    # walk as ``preview`` -- otherwise the dashboard's quarantine button would
    # still hang on the exact slow worktree walk this bound exists to cap. If the
    # measurement cannot finish in time we refuse rather than block: a write must
    # never proceed on an unverified footprint.
    current, complete = _measure_within_deadline(
        _measure_key(root, worktree_base, now),
        lambda: _preview_payload(root, worktree_base, now=now),
        _resolve_deadline(None),
    )
    if not complete:
        raise StorageRetentionError("retention_measurement_incomplete")
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
