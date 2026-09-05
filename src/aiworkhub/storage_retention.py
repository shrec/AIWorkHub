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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from . import parallelism, repo_policy, task_store, worktree_storage
from .worker_workspace import configured_worktree_root, has_verified_rework_delta


SCHEMA_ID = "aiworkhub.storage_retention.v1"
MANIFEST_NAME = "manifest.json"
QUARANTINE_DIRNAME = ".aiworkhub-quarantine"
AUDIT_RELATIVE_PATH = Path(".aiworkhub/runtime/storage/retention.audit.jsonl")
LEGACY_LOG_RELATIVE_PATH = Path("logs")
CANONICAL_RUNTIME_RELATIVE_PATH = Path(".aiworkhub/runtime")
UNDO_DAYS = 7
MAX_MANIFEST_BYTES = 512 * 1024
# When unattributed/foreign worktree bytes reach this share of the observed
# footprint, the preview flags it prominently (byte figure + count) so a short
# reclaim-candidate list can never read as "this repository is nearly clean"
# while gigabytes sit outside every reclamation path (see _unattributed_alert).
MATERIAL_UNATTRIBUTED_SHARE = 0.10
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
_TERMINAL_NEEDFIX_STATUSES = frozenset({"rejected", "duplicate", "resolved", "archived"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
# A worktree HEAD recorded in a manifest is either a raw object name (sha1 or
# sha256) or a symbolic ref. Reconstructing a pruned registration writes this
# verbatim into the admin ``HEAD`` file, so it is validated before it is trusted.
_HEAD_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64}|ref: refs/[^\s]+)$")


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


def _terminal_needfix_task_ids(repo_root: Path) -> tuple[set[str], bool]:
    """Return converted tasks whose owning NeedFix is explicitly terminal.

    NeedFix's own lifecycle is the outer authority: archived/resolved/rejected/
    duplicate records are closed even if an old converted task row was left
    blocked.  Such stale rows must not pin a predecessor forever.  The read is
    bounded to one indexed status query and fails closed; an absent NeedFix
    database simply means there are no NeedFix-owned exemptions.
    """
    db_path = repo_root / ".aiworkhub" / "tasking" / "needfix.sqlite"
    if not db_path.is_file():
        return set(), True
    try:
        ro_uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(ro_uri, uri=True, timeout=5.0)
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout=5000")
            placeholders = ",".join("?" for _ in _TERMINAL_NEEDFIX_STATUSES)
            rows = conn.execute(
                f"SELECT converted_task_id FROM needfix "
                f"WHERE status IN ({placeholders}) AND converted_task_id IS NOT NULL",
                tuple(sorted(_TERMINAL_NEEDFIX_STATUSES)),
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return set(), False
    return {str(row[0]).strip() for row in rows if str(row[0] or "").strip()}, True


def _blocked_terminal_candidate_request_ids(
    card_json: Mapping[str, Any],
) -> frozenset[str]:
    """Every request id a ``blocked`` card's own terminal failure still implicates.

    ``task_store.mark_terminal_failure`` parks a post-launch failure (e.g.
    ``finalize_failed``) in the canonical ``blocked`` bucket WITHOUT clearing
    ``launch_request_id`` -- the worktree that failed is the one both that
    field and the recorded ``terminal_failure``/``terminal_review`` still name.
    A card blocked for any OTHER reason (a manager ``reject_review(...,
    to="blocked")`` park, for instance) carries no such record, so this
    returns an empty set and earns the card no new protection: only a genuine
    operational terminal failure -- a substatus in
    ``task_store.MARK_TERMINAL_FAILURE_SUBSTATUSES`` -- names a still-needed
    candidate, never a blanket rule for every blocked card or every past
    attempt it ever made.

    When the card's own ``launch_request_id`` and the recorded
    ``terminal_failure``/``terminal_review`` envelopes DISAGREE the retained
    identity is ambiguous: the evidence cannot say which worktree actually
    holds the failed finalize attempt. Collapsing them (the prior behaviour --
    one ``recorded_request_id`` chosen from ``terminal_failure`` else
    ``terminal_review``, then unioned only with the launch id) silently dropped
    the third envelope's distinct id, converting that ambiguity into reclaim
    permission for the worktree it named -- a fail-open bug. Every nonempty id
    is instead collected INDEPENDENTLY from all three envelopes
    (``terminal_failure``, ``terminal_review`` AND ``launch_request_id``), so
    protection fails CLOSED across a three-way disagreement: a mismatched or
    malformed terminal identity can never manufacture reclaim permission for a
    worktree that might still hold the only copy of that finalize attempt. A
    record carrying no usable request id at all names no worktree, so it
    protects nothing (there is nothing to reclaim either).
    """
    terminal_review = card_json.get("terminal_review")
    terminal_failure = card_json.get("terminal_failure")
    substatus = str(
        card_json.get("terminal_substatus")
        or (terminal_review.get("substatus") if isinstance(terminal_review, dict) else "")
        or (terminal_failure.get("substatus") if isinstance(terminal_failure, dict) else "")
        or ""
    )
    if substatus not in task_store.MARK_TERMINAL_FAILURE_SUBSTATUSES:
        return frozenset()
    implicated: list[str] = []
    for envelope in (terminal_failure, terminal_review):
        if isinstance(envelope, dict):
            implicated.append(str(envelope.get("request_id") or "").strip())
    implicated.append(str(card_json.get("launch_request_id") or "").strip())
    return frozenset(rid for rid in implicated if rid)


def _protected_attempt_ids(
    repo_root: Path,
) -> tuple[dict[str, str], bool, dict[str, list[str]], set[str]]:
    """Worktree ids a live attempt or in-flight rework lineage still holds.

    A worktree is protected -- and therefore never a quarantine candidate --
    while any of these hold, keyed strictly on the canonical card lifecycle
    (never on file mtime, age, or recency):

    * ``live_worker`` -- it is the *current* claimed attempt of a card that is
      actively ``processing`` or ``review`` (the newest attempt under
      evaluation), recorded by the claim path in ``card_json`` as
      ``launch_request_id``.
    * ``blocked_terminal_candidate_retained`` -- a card that is ``blocked`` by
      its own genuine operational terminal failure (a substatus in
      ``task_store.MARK_TERMINAL_FAILURE_SUBSTATUSES``, e.g. ``finalize_failed``)
      still names this exact request id via ``terminal_failure``/
      ``terminal_review`` and ``launch_request_id`` alike (see
      :func:`_blocked_terminal_candidate_request_ids`). ``mark_terminal_failure``
      never clears ``launch_request_id`` when it parks the card, so the
      worktree that failed is the only copy of that finalize attempt's
      evidence; a card merely parked ``blocked`` by a manager review
      (``reject_review(..., to="blocked")``) carries no such record and earns
      no protection here. When the recorded terminal id and
      ``launch_request_id`` disagree the identity is ambiguous, so BOTH are
      retained (fail closed) rather than granting reclaim permission on either.
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

    Returns ``(protected, verified, pinned_by, terminal_tasks)``. ``protected``
    maps each protected request id to its reason. ``pinned_by`` maps each
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
    terminal_tasks: set[str] = set()
    terminal_needfix_tasks, needfix_lineage_verified = _terminal_needfix_task_ids(repo_root)
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
            card_id = str(row["task_id"] or "").strip()
            if card_id and status in _FINISHED_STATUSES:
                terminal_tasks.add(card_id)
            closed_by_needfix = (
                needfix_lineage_verified and card_id in terminal_needfix_tasks
            )
            if status in ("processing", "review"):
                request_id = str(card_json.get("launch_request_id") or "").strip()
                if request_id and not closed_by_needfix:
                    protected[request_id] = "live_worker"
            if status == "blocked" and not closed_by_needfix:
                for blocked_request_id in _blocked_terminal_candidate_request_ids(card_json):
                    protected.setdefault(
                        blocked_request_id, "blocked_terminal_candidate_retained"
                    )
            if status not in _FINISHED_STATUSES and not closed_by_needfix:
                predecessor = card_json.get("rework_predecessor")
                if isinstance(predecessor, dict):
                    predecessor_id = str(predecessor.get("request_id") or "").strip()
                    if predecessor_id and not has_verified_rework_delta(
                        predecessor, authority_repo=repo_root
                    ):
                        protected.setdefault(predecessor_id, "rework_predecessor_retained")
                        if card_id:
                            holders = pinned_by.setdefault(predecessor_id, [])
                            if card_id not in holders:
                                holders.append(card_id)
    except (task_store.TaskStoreError, sqlite3.Error, OSError):
        return {}, False, {}, set()
    for holders in pinned_by.values():
        holders.sort()
    return protected, True, pinned_by, terminal_tasks


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
    of rejection count. Orphaned worktrees (broken Git metadata) remain
    excluded unless an exact request-ledger owner is independently terminal;
    unknown, foreign and nonterminal ownership stays fail-closed.

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
    protected_ids, lineage_verified, pinned_by, terminal_tasks = (
        _protected_attempt_ids(root)
    )
    terminal_needfix_tasks, needfix_lineage_verified = _terminal_needfix_task_ids(root)
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
        terminal_needfix_orphan = (
            needfix_lineage_verified
            and wt.get("ownership_source") == "request_ledger"
            and str(wt.get("owner_task_id") or "") in terminal_needfix_tasks
        )
        terminal_task_orphan = (
            lineage_verified
            and wt.get("ownership_source") == "request_ledger"
            and str(wt.get("owner_task_id") or "") in terminal_tasks
        )
        if (
            wt.get("class") == worktree_storage.CLASS_ORPHANED
            and not terminal_needfix_orphan
            and not terminal_task_orphan
        ):
            would_keep.append(wt)
            protection_reasons[wt_id] = "orphaned"
            continue
        if wt_id in protected_ids:
            would_keep.append(wt)
            protection_reasons[wt_id] = protected_ids[wt_id]
            continue
        if (
            wt.get("class") == worktree_storage.CLASS_REMOVABLE_SAFE
            or lineage_verified
            or terminal_needfix_orphan
            or terminal_task_orphan
        ):
            if terminal_needfix_orphan:
                wt = dict(wt, reclaim_authority="terminal_needfix_request_ledger")
            elif terminal_task_orphan:
                wt = dict(wt, reclaim_authority="terminal_task_request_ledger")
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


def repo_storage_footprint(
    repo_root: Path | str, *, base: Path | None = None, progress: Any | None = None
) -> dict[str, Any]:
    """Single, shared definition of on-disk footprint bytes for every
    ``worktree_max_bytes`` cap comparison across this subsystem's surfaces
    (the retention preview and the dashboard telemetry), so they can never
    silently disagree about what "current bytes" means.

    Repo-scoped worktree bytes alone under-report usage on a repository whose
    legacy ``logs/`` tree or ``.aiworkhub/runtime`` canonical data are large;
    ``observed_total_bytes`` -- global worktree bytes plus both of those -- is
    the authoritative figure for cap comparisons.

    The repo-scoped scan and the global aggregate come from ONE pass over the
    worktree tree (``scan_worktrees`` returns ``global_summary`` alongside the
    repo-scoped ``summary``): the previous two separate full walks each re-ran
    per-worktree git state over every entry, which -- together with the runtime
    subtotal re-walking the worktree bytes a third time -- is why the measurement
    could not finish. The runtime subtotal now prunes the worktree base (it lives
    under ``.aiworkhub/runtime``) so those bytes are walked and counted once.
    ``progress`` is threaded to the scan so a deadline-limited preview can report
    the candidates it did establish.
    """
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root(root)).resolve()
    legacy_log_root = root / LEGACY_LOG_RELATIVE_PATH
    canonical_runtime_root = root / CANONICAL_RUNTIME_RELATIVE_PATH
    components = (
        (
            "scan",
            lambda: worktree_storage.scan_worktrees(
                worktree_base, with_sizes=True, repo_root=root, progress=progress
            ),
        ),
        (
            "legacy_log_bytes",
            lambda: (
                worktree_storage.directory_size_bytes(legacy_log_root)
                if legacy_log_root.is_dir() and not legacy_log_root.is_symlink()
                else 0
            ),
        ),
        (
            "canonical_runtime_bytes",
            lambda: (
                worktree_storage.directory_size_bytes(
                    canonical_runtime_root, exclude=[worktree_base]
                )
                if canonical_runtime_root.is_dir()
                and not canonical_runtime_root.is_symlink()
                else 0
            ),
        ),
    )
    worker_count, _selection = parallelism.compute_worker_count(
        candidate_count=len(components), reserve=1, ceiling=len(components)
    )
    values: dict[str, Any] = {}
    if worker_count == 1:
        for name, component in components:
            values[name] = component()
    else:
        executor = ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="aiworkhub-retention-component"
        )
        futures = {executor.submit(component): name for name, component in components}
        try:
            for future in as_completed(futures):
                name = futures[future]
                try:
                    values[name] = future.result()
                except BaseException as exc:
                    _publish_component_failure(exc)
                    raise
        finally:
            # Futures cannot safely be cancelled once their filesystem walk has
            # started. Keep the composite worker alive (and its single-flight key
            # fenced) until every such sibling has terminated.
            executor.shutdown(wait=True, cancel_futures=True)
    scan = values["scan"]
    global_summary = scan.get("global_summary") or scan.get("summary") or {}
    repository_worktree_bytes = int(scan.get("summary", {}).get("total_bytes") or 0)
    global_worktree_bytes = int(global_summary.get("total_bytes") or 0)
    repository_worktree_count = int(scan.get("summary", {}).get("count") or 0)
    global_worktree_count = int(global_summary.get("count") or 0)
    legacy_log_bytes = int(values["legacy_log_bytes"])
    canonical_runtime_bytes = int(values["canonical_runtime_bytes"])
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
        "repository_worktree_count": repository_worktree_count,
        "global_worktree_count": global_worktree_count,
        "unattributed_or_foreign_worktree_count": max(
            0, global_worktree_count - repository_worktree_count
        ),
        "legacy_log_status": (
            "present_unmanaged" if legacy_log_bytes else "absent_or_empty"
        ),
    }


def _unattributed_alert(footprint: Mapping[str, Any]) -> dict[str, Any]:
    """Prominent flag when unattributed/foreign worktrees are a material share.

    A short reclaim-candidate list next to a large unattributed footprint reads
    as "nearly clean" while gigabytes sit outside every reclamation path. When
    that unattributed share crosses :data:`MATERIAL_UNATTRIBUTED_SHARE` of the
    observed footprint, the preview says so up front -- with the byte figure and
    the count -- and names the explicit recovery action, so the false impression
    cannot survive a glance at the candidate count alone.
    """
    observed = int(footprint.get("observed_total_bytes") or 0)
    unattributed_bytes = int(footprint.get("unattributed_or_foreign_worktree_bytes") or 0)
    count = int(footprint.get("unattributed_or_foreign_worktree_count") or 0)
    share = (unattributed_bytes / observed) if observed > 0 else 0.0
    material = unattributed_bytes > 0 and count > 0 and share >= MATERIAL_UNATTRIBUTED_SHARE
    message = ""
    if material:
        message = (
            f"{count} worktree(s) totalling "
            f"{worktree_storage._human_bytes(unattributed_bytes)} are unattributed or "
            "foreign and are NOT covered by the reclaim candidates below; run "
            "recover_stranded_worktrees() to re-register or reclaim them."
        )
    return {
        "material": material,
        "bytes": unattributed_bytes,
        "count": count,
        "share": round(share, 4),
        "threshold_share": MATERIAL_UNATTRIBUTED_SHARE,
        "recovery_action": "storage_retention.recover_stranded_worktrees",
        "message": message,
    }


def _preview_payload(
    repo_root: Path,
    base: Path,
    *,
    now: float | None = None,
    progress: "_PreviewProgress | None" = None,
) -> dict[str, Any]:
    policy_days, max_bytes = _policy(repo_root)
    if progress is not None:
        # Seed the partial-progress sink so it can classify each worktree the walk
        # measures as a definite reclaim candidate on the fly -- repository-scoped,
        # aged past policy, unprotected, and either removable-safe or on verified
        # lineage. Ownership is proved from the canonical task lifecycle
        # (_protected_attempt_ids) and the git common dir, never from a name or an
        # age alone. If the deadline is hit mid-walk the preview surfaces exactly
        # these instead of an empty list that reads as a clean repository.
        protected_ids, lineage_verified, _pinned_by, _terminal_tasks = (
            _protected_attempt_ids(repo_root)
        )
        progress.configure(
            repo_common_dir=worktree_storage._git_common_dir(repo_root),
            protected_ids=protected_ids,
            lineage_verified=lineage_verified,
            min_age_days=policy_days,
            now=now,
        )
    footprint = repo_storage_footprint(repo_root, base=base, progress=progress)
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
            "ownership_source": str(item.get("ownership_source") or ""),
            "owner_task_id": str(item.get("owner_task_id") or ""),
            "reclaim_authority": str(item.get("reclaim_authority") or ""),
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
        # Report-only fields (deliberately outside ``digest_input`` so the
        # quarantine digest reconfirmation is unaffected): make a material
        # unattributed/foreign footprint impossible to miss beside the candidates.
        "unattributed_or_foreign_worktree_bytes": int(
            footprint.get("unattributed_or_foreign_worktree_bytes") or 0
        ),
        "unattributed_or_foreign_worktree_count": int(
            footprint.get("unattributed_or_foreign_worktree_count") or 0
        ),
        "unattributed_alert": _unattributed_alert(footprint),
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
_measurement_local = threading.local()


class _Measurement:
    __slots__ = ("done", "ready", "value", "error", "succeeded", "progress")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.ready = threading.Event()
        self.value: Any = None
        self.error: Exception | None = None
        # Set True ONLY when ``fn()`` returns a value. Completion (``done``) plus
        # a falsy ``error`` is NOT proof of success: a control-flow BaseException
        # propagates out of the worker leaving ``error`` None, and the finally
        # still sets ``done``. Keying success on this explicit flag -- never on
        # ``error is None`` -- makes a failed measurement impossible to represent
        # as a ``(value, True)`` success by any path (see _measure_within_deadline).
        self.succeeded = False
        # The partial-progress sink of THIS single-flight walk. The starter binds
        # its ``_PreviewProgress`` here; every caller (the starter and any that
        # ATTACH to the same running walk) reads the sink back off the measurement,
        # so a second operator released at the deadline sees the SAME partial
        # evidence the shared walk established -- never its own empty sink handed
        # straight to ``_incomplete_preview`` as a false "clean" candidates=[].
        self.progress: "_PreviewProgress | None" = None


def _ready_event(measurement: Any) -> Any:
    """Native readiness when available; legacy measurements alias completion."""
    return getattr(measurement, "ready", measurement.done)


def _set_ready_once(measurement: Any) -> None:
    ready = _ready_event(measurement)
    if ready is not measurement.done and not ready.is_set():
        ready.set()


def _publish_component_failure(exc: BaseException) -> None:
    measurement = getattr(_measurement_local, "measurement", None)
    if measurement is None:
        return
    if isinstance(exc, Exception):
        measurement.error = exc
    _set_ready_once(measurement)


class _PreviewProgress:
    """Thread-safe partial-progress sink for the off-thread footprint walk.

    The measurement runs on a daemon thread (see :func:`_measure_within_deadline`).
    If the wall-clock deadline is hit while that walk is still running, ``preview``
    reads a SNAPSHOT of the reclaim candidates the walk has ALREADY fully
    established -- each worktree whose size and git state are both measured and
    which is provably eligible: repository-scoped, aged past policy, not protected,
    and either removable-safe or on verified lineage. Reporting those, explicitly
    marked partial with the worktrees not yet covered, is what stops a
    deadline-limited preview from reading as a clean repository (the same class of
    lie as a build that indexed nothing reporting success).

    A definite candidate here is the non-forcing subset of
    :func:`plan_worktree_reclaim`: the over-cap forcing pass (pulling under-age
    worktrees when the cap is breached) only ever ADDS candidates, so omitting it
    keeps the partial set a strict subset of the complete one -- never a superset
    that could name something the full plan would have protected. Protection is
    keyed on the canonical task lifecycle and the git common dir handed to
    :meth:`configure`, never on a directory name or an mtime.
    """

    __slots__ = (
        "_lock", "_configured", "_repo_common_dir", "_protected_ids",
        "_lineage_verified", "_min_age_seconds", "_now", "_all_ids", "_covered",
        "_candidates",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._configured = False
        self._repo_common_dir = ""
        self._protected_ids: frozenset[str] = frozenset()
        self._lineage_verified = False
        self._min_age_seconds = 0
        self._now: float | None = None
        self._all_ids: list[str] = []
        self._covered: set[str] = set()
        self._candidates: dict[str, dict[str, Any]] = {}

    def configure(
        self,
        *,
        repo_common_dir: str,
        protected_ids: Mapping[str, str] | Any,
        lineage_verified: bool,
        min_age_days: int,
        now: float | None,
    ) -> None:
        with self._lock:
            self._repo_common_dir = repo_common_dir or ""
            self._protected_ids = frozenset(protected_ids or ())
            self._lineage_verified = bool(lineage_verified)
            self._min_age_seconds = max(0, min(int(min_age_days), 3650)) * 86400
            self._now = time.time() if now is None else float(now)
            self._configured = True

    def begin(self, ids: Any) -> None:
        with self._lock:
            self._all_ids = [str(item) for item in (ids or [])]

    def observe(self, worktree: Mapping[str, Any]) -> None:
        with self._lock:
            wt_id = str(worktree.get("id") or "")
            if wt_id:
                self._covered.add(wt_id)
            if not self._configured or not wt_id:
                return
            # Repository scope only: a foreign or orphaned worktree is never a
            # candidate here, exactly as the complete plan excludes it. When the
            # repository's git common dir could not be resolved (``_git_common_dir``
            # returns "" on any ``git rev-parse`` failure -- a broken/inaccessible
            # ``.git`` or missing git), ownership can be proved for NO worktree.
            # ``scan_worktrees`` keeps its repo-scoped list empty in that exact
            # case (``in_repo = bool(repo_common_dir) and ...``); the partial sink
            # must fail closed the same way, or it would admit every foreign
            # worktree the walk observed and read as a SUPERSET of an empty
            # complete plan. Prove scope by the common dir, never by name or age.
            if not self._repo_common_dir or worktree.get("parent_git_dir") != self._repo_common_dir:
                return
            if worktree.get("class") == worktree_storage.CLASS_ORPHANED:
                return
            if wt_id in self._protected_ids:
                return
            eligible_class = worktree.get("class") == worktree_storage.CLASS_REMOVABLE_SAFE
            if not (eligible_class or self._lineage_verified):
                return
            age = (self._now or 0.0) - float(worktree.get("modified_at_epoch") or 0.0)
            if age < self._min_age_seconds:
                return
            self._candidates[wt_id] = {
                "id": wt_id,
                "head": str(worktree.get("head") or ""),
                "size_bytes": int(worktree.get("size_bytes") or 0),
                "modified_at_epoch": int(float(worktree.get("modified_at_epoch") or 0.0)),
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            candidates = sorted(self._candidates.values(), key=lambda item: item["id"])
            covered = set(self._covered)
            not_covered = [wt_id for wt_id in self._all_ids if wt_id not in covered]
            return {
                "candidates": candidates,
                "covered_count": len(covered),
                "total_count": len(self._all_ids),
                "not_covered": sorted(not_covered),
            }


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
    _measurement_local.measurement = measurement
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
        _set_ready_once(measurement)
        with _measure_lock:
            if _measurements.get(key) is measurement:
                _measurements.pop(key, None)
        _measurement_local.measurement = None


def _measure_within_deadline(
    key: Any, fn: Any, deadline_seconds: float, *, progress: "_PreviewProgress | None" = None
) -> tuple[Any, bool]:
    """Run ``fn`` under a shared single-flight walk; return ``(value, True)`` if
    it finishes within ``deadline_seconds``, else ``(<shared partial>, False)``.

    Concurrent or repeated callers for the same ``key`` attach to ONE running
    daemon walk instead of each starting their own, so a stalled measurement can
    never be amplified into stacked threads and stacked disk I/O. The caller is
    released at the deadline even while the walk is still running (matching the
    bounded snapshot path).

    ``progress`` is the STARTER's sink -- the one bound into ``fn``'s closure and
    populated by the running walk. It is recorded on the shared ``_Measurement``,
    so a caller that ATTACHES to an existing walk reads that SAME sink back
    (``measurement.progress``) rather than its own empty one: on a deadline miss
    every caller receives a snapshot of the evidence the shared walk actually
    established, not a false ``candidates=[]``. The incomplete result is returned
    ONLY once the walk is confirmed still in flight -- an honest "still measuring"
    partial, not a failure. If the walk has already raised by the time the caller
    is released, that exception is re-raised here rather than being silently
    discarded and presented as an incomplete ``ok: True`` success. A walk that
    finished without producing a value (``succeeded`` False -- e.g. a control-flow
    BaseException tore the worker down, leaving ``error`` None) is likewise raised
    as a measurement failure, never returned as ``(None, True)``.
    """
    with _measure_lock:
        measurement = _measurements.get(key)
        start = measurement is None
        if start:
            measurement = _Measurement()
            if progress is not None:
                # Bind the starter's sink to the single-flight measurement so every
                # attaching caller reads it back instead of its own empty sink.
                measurement.progress = progress
            _measurements[key] = measurement

    # The sink of the ONE walk this caller is bound to: the starter's, for an
    # attaching caller too. Read off the shared measurement, never the caller's own.
    shared_progress = measurement.progress

    if start:
        threading.Thread(
            target=_measure_worker,
            args=(key, measurement, fn),
            daemon=True,
            name="aiworkhub-retention-preview",
        ).start()

    ready = _ready_event(measurement)
    finished = ready.wait(deadline_seconds)
    if not finished and not ready.is_set():
        # Genuinely still walking: an honest incomplete result, never a failure
        # dressed as one -- no error has occurred yet at this point. Hand back the
        # SHARED walk's partial evidence so the attaching caller sees exactly what
        # the caller that started the walk sees.
        return (shared_progress.snapshot() if shared_progress is not None else None), False
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
    repo_root: Path,
    deadline_seconds: float,
    unmeasured: list[str],
    *,
    partial: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A bounded fail-safe when the on-disk measurement cannot finish in time.

    A partial scan must never be published as a *completed* footprint: reporting
    a smaller total than reality would invite an operator to quarantine on
    incomplete evidence. Every aggregate byte field and the actionable preview
    digest are therefore withheld (``None``/empty) and the result is explicitly
    labelled ``complete=False``.

    But an incomplete measurement must ALSO never read as a clean repository. When
    the walk established some reclaim candidates before the deadline (``partial``
    carries them and the ids ``not_covered``), those exact candidates are
    surfaced, ``partial`` is set True, and ``not_covered`` names the worktrees the
    walk did not reach -- so a deadline-hit preview shows real work to do with an
    honest boundary, never an empty list. When nothing was established the result
    is byte-for-byte the fully-withheld shape (empty candidates, ``partial``
    False), so a genuinely stalled walk still cannot masquerade as clean.

    The response keeps the SAME keys the complete payload always emits (plus
    ``partial``/``not_covered``), so a caller parses one schema whether or not the
    deadline was hit. The cheap policy fields carry their real values; every field
    that depends on the unfinished aggregate is present but explicitly
    ``None``/empty and named in ``unmeasured``.
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
        "unattributed_or_foreign_worktree_bytes",
        "unattributed_or_foreign_worktree_count",
        "unattributed_alert",
        "preview_digest",
    }
    established = list((partial or {}).get("candidates") or [])
    not_covered = list((partial or {}).get("not_covered") or [])
    result = {
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
        "unattributed_or_foreign_worktree_bytes": None,
        "unattributed_or_foreign_worktree_count": None,
        "unattributed_alert": None,
        "preview_digest": "",
        # Whether the walk established any reclaim candidates before the deadline,
        # and which worktrees it did not reach. Always present so the schema does
        # not change shape; ``partial`` is False (and ``not_covered`` whatever was
        # enumerated) when nothing was established.
        "partial": False,
        "not_covered": sorted(not_covered),
    }
    if established:
        # Surface the candidates the walk already fully established. They are a
        # strict subset of the complete plan's candidates, so they can be acted on
        # exactly like a complete result -- while the aggregate byte totals stay
        # withheld (unknown until the walk finishes) and ``not_covered`` bounds
        # what is missing. The candidate fields are no longer wholly unmeasured, so
        # they are dropped from ``unmeasured``.
        result["partial"] = True
        result["candidates"] = established
        result["candidate_count"] = len(established)
        result["candidate_bytes"] = sum(int(item.get("size_bytes") or 0) for item in established)
        result["unmeasured"] = sorted(
            (set(unmeasured) | withheld_fields)
            - {"candidates", "candidate_count", "candidate_bytes"}
        )
    return result


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
    progress = _PreviewProgress()
    # Pass the sink to the single-flight coordinator so it is recorded on the ONE
    # measurement. A caller that STARTS the walk populates this exact sink; a
    # caller that ATTACHES gets the starter's sink back from the coordinator, so
    # both see the same partial evidence on a deadline miss.
    payload, complete = _measure_within_deadline(
        _measure_key(root, worktree_base, now),
        lambda: _preview_payload(root, worktree_base, now=now, progress=progress),
        deadline,
        progress=progress,
    )
    if not complete:
        # ``payload`` is the snapshot of the SHARED walk's sink (the starter's),
        # so the candidates the walk established before the deadline are returned
        # -- explicitly partial and naming what was not covered -- rather than an
        # empty "clean" list, for the attaching caller exactly as for the starter.
        return _incomplete_preview(
            root,
            deadline,
            ["worktree_footprint_scan", "reclaim_candidates"],
            partial=payload,
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


# The states a worktree quarantine item ends in when nothing was moved into the
# batch for it: its on-disk identity changed between the digest snapshot and the
# move, so no worktree directory sits under the batch. A batch whose every item
# is one of these -- and which physically holds no files -- has nothing to
# restore. This mirrors the terminal-log retention empty-batch definition so both
# quarantine subsystems agree on "holds nothing to restore".
_EMPTY_BATCH_ITEM_STATES = frozenset({
    "skipped_invalid_id",
    "skipped_identity_changed",
    "skipped_missing",
    "skipped_git_state_changed",
})


def _batch_is_empty(manifest: Mapping[str, Any]) -> bool:
    """True when the record shows nothing was ever moved into the batch.

    Every item must be in a pre-move skipped state -- never ``quarantined``
    (whose worktree sits in the batch) nor ``restored``/``restore_conflict``
    (which an operator may still rely on). An item in any other state keeps the
    batch's full undo window.
    """
    for item in manifest.get("items") or []:
        if not isinstance(item, dict):
            return False
        if str(item.get("state") or "") not in _EMPTY_BATCH_ITEM_STATES:
            return False
    return True


def _batch_dir_has_payload(batch_path: Path) -> bool:
    """True when the batch physically holds any non-manifest regular file.

    Read from disk, never trusted from the manifest item states: a batch whose
    record reads empty can still hold bytes (a record-versus-disk disagreement),
    and such a batch must never be reaped on a stale "empty" record. Symlinks are
    never followed or counted.
    """
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
            return True
    return False


def _batch_reapable_empty(manifest: Mapping[str, Any], batch_path: Path) -> bool:
    """A batch is early-reapable only when its record AND its disk are both empty.

    Requiring the directory to be physically empty too makes a record-versus-disk
    disagreement impossible to act on destructively: a batch that still holds
    files keeps its full deadline regardless of what its item states say.
    """
    return _batch_is_empty(manifest) and not _batch_dir_has_payload(batch_path)


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
    authorities = {
        str(item.get("reclaim_authority") or "") for item in manifest["items"]
    }
    terminal_needfix_tasks, needfix_verified = (
        _terminal_needfix_task_ids(root)
        if "terminal_needfix_request_ledger" in authorities
        else (set(), False)
    )
    if "terminal_task_request_ledger" in authorities:
        _protected, task_lineage_verified, _pinned, terminal_tasks = (
            _protected_attempt_ids(root)
        )
    else:
        task_lineage_verified, terminal_tasks = False, set()
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
        # commits that were deliberately never going to be pushed. Broken Git
        # metadata is admitted only by explicit terminal task/NeedFix + exact
        # request-ledger authority established by the preview and rechecked here.
        git_state = worktree_storage._worktree_git_state(source / "worktree")
        terminal_needfix_orphan = (
            item.get("reclaim_authority") == "terminal_needfix_request_ledger"
            and item.get("ownership_source") == "request_ledger"
            and worktree_storage._request_ledger_owner_task_id(root, worktree_base, source)
            == item.get("owner_task_id")
        )
        if terminal_needfix_orphan:
            terminal_needfix_orphan = (
                needfix_verified
                and item.get("owner_task_id") in terminal_needfix_tasks
            )
        terminal_task_orphan = (
            item.get("reclaim_authority") == "terminal_task_request_ledger"
            and item.get("ownership_source") == "request_ledger"
            and worktree_storage._request_ledger_owner_task_id(
                root, worktree_base, source
            )
            == item.get("owner_task_id")
        )
        if terminal_task_orphan:
            terminal_task_orphan = (
                task_lineage_verified
                and item.get("owner_task_id") in terminal_tasks
            )
        if (
            (
                not terminal_needfix_orphan
                and not terminal_task_orphan
                and (
                    worktree_storage._classify(git_state)
                    == worktree_storage.CLASS_ORPHANED
                    or git_state.get("head") != item["head"]
                    or git_state.get("parent_git_dir") != repo_common_dir
                )
            )
            or not repo_common_dir
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
    if _batch_reapable_empty(manifest, batch):
        # Every candidate's identity changed between the digest snapshot and the
        # move (typically a concurrent sweep quarantined them first), so this pass
        # moved nothing. An empty batch holds nothing to restore, yet its seven-day
        # undo window would keep it on the storage panel forever, shape-identical to
        # a real batch -- the exact accumulation the terminal-log side already
        # closes. Reap it now, but only when the directory is also physically empty
        # so a batch that unexpectedly holds files is never rmtree'd here on a stale
        # "empty" record.
        shutil.rmtree(batch, ignore_errors=True)
        _append_audit(root, {
            "schema_id": "aiworkhub.storage_retention_audit.v1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "quarantine_empty_reaped",
            "batch_id": batch_id,
            "count": 0,
            "bytes": 0,
        })
        return {"ok": True, "batch_id": "", "quarantined": 0, "bytes": 0, "no_op": True}
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
                expired = datetime.now(timezone.utc) >= datetime.fromisoformat(deadline)
            except ValueError:
                expired = False
            # A batch is reapable now if its undo window has expired OR it is empty
            # in BOTH its record and on disk -- exactly what ``purge`` accepts. An
            # empty batch is surfaced purge_eligible even inside a live window so it
            # never sits on the storage panel forever protecting nothing.
            reapable_empty = _batch_reapable_empty(value, entry)
            rows.append({
                "batch_id": value["batch_id"],
                "created_at": str(value.get("created_at") or ""),
                "restore_deadline": deadline,
                "status": str(value.get("status") or "unknown"),
                "quarantined_count": states.count("quarantined"),
                "restored_count": states.count("restored"),
                "bytes": quarantined_bytes,
                "purge_eligible": expired or reapable_empty,
                # Exactly the batches ``purge_empty_batches`` collects: empty in
                # BOTH record and on disk. Distinct from ``purge_eligible``, which
                # also covers expired-but-still-full batches that collector never
                # takes.
                "reapable_empty": reapable_empty,
            })
    return {"ok": True, "batches": rows[:100], "count": len(rows[:100])}


def _worktree_admin_dir(checkout: Path) -> Path | None:
    """The ``.git/worktrees/<name>`` administrative dir a checkout points at.

    A linked worktree's ``worktree/.git`` is a FILE whose ``gitdir:`` line names
    the admin entry that registers it. Returns that path (resolved against the
    checkout for the relative-path layout), or ``None`` when the checkout has no
    readable ``.git`` pointer (e.g. it is a plain directory, not a linked
    worktree).
    """
    try:
        text = (checkout / ".git").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("gitdir:"):
            target = stripped[len("gitdir:"):].strip()
            if not target:
                return None
            candidate = Path(target)
            if not candidate.is_absolute():
                candidate = (checkout / candidate).resolve()
            return candidate
    return None


def _admin_under_repo(admin: Path, repo_common_dir: str) -> bool:
    """True only when ``admin`` is this repository's own ``.git/worktrees/<name>``.

    Proof of ownership before any re-registration: the admin entry must sit
    directly under the exact git common dir resolved for this repository, so a
    foreign or unprovable claim is never fabricated into a registration.
    """
    if not repo_common_dir or admin.parent.name != "worktrees":
        return False
    git_dir = admin.parent.parent
    try:
        resolved = os.path.normcase(str(git_dir.resolve()))
    except OSError:
        resolved = os.path.normcase(str(git_dir))
    return resolved == repo_common_dir


def _reconstruct_registration(
    admin: Path, checkout: Path, head: str, repo_common_dir: str
) -> None:
    """Recreate a pruned ``.git/worktrees/<name>`` entry for a restored checkout.

    Only ever recreates an entry this repository provably owns (``admin`` under
    this repo's git dir) with a validated HEAD; anything unprovable raises so the
    caller records a loud failure rather than inventing a registration.
    """
    if not _admin_under_repo(admin, repo_common_dir):
        raise StorageRetentionError("retention_restore_foreign_registration")
    head = str(head or "").strip()
    if not _HEAD_RE.fullmatch(head):
        raise StorageRetentionError("retention_restore_head_unknown")
    admin.mkdir(parents=True, exist_ok=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "gitdir").write_text(str(checkout / ".git") + "\n", encoding="utf-8")
    (admin / "HEAD").write_text(head + "\n", encoding="utf-8")


def _reinstate_registration(
    root: Path,
    worktree_base: Path,
    item: Mapping[str, Any],
    repo_common_dir: str,
    *,
    head: str | None = None,
) -> None:
    """Reconnect one restored worktree to this repository's git registration.

    When the admin entry merely dangled (the checkout was moved and moved back
    but never pruned) ``git worktree repair`` reconnects it. When it was pruned
    away entirely, the entry is first reconstructed from the recorded HEAD, then
    repaired to canonicalise the two-way link. The caller verifies attribution
    afterwards, so a checkout that cannot be reconnected is never mistaken for a
    reinstated one.
    """
    checkout = worktree_base / str(item.get("id") or "") / "worktree"
    admin = _worktree_admin_dir(checkout)
    if admin is not None and not admin.exists():
        recovered_head = head if head is not None else str(item.get("head") or "")
        _reconstruct_registration(admin, checkout, recovered_head, repo_common_dir)
    worktree_storage._git(root, "worktree", "repair", str(checkout))


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
    repo_common_dir = worktree_storage._git_common_dir(root)
    restored = 0
    reinstated: list[str] = []
    restored_orphaned: list[str] = []
    registration_lost: list[str] = []
    workspace_absent: list[str] = []
    authorities = {
        str(item.get("reclaim_authority") or "")
        for item in manifest["items"]
        if isinstance(item, dict)
    }
    terminal_needfix_tasks, needfix_verified = (
        _terminal_needfix_task_ids(root)
        if "terminal_needfix_request_ledger" in authorities
        else (set(), False)
    )
    if "terminal_task_request_ledger" in authorities:
        _protected, task_lineage_verified, _pinned, terminal_tasks = (
            _protected_attempt_ids(root)
        )
    else:
        task_lineage_verified, terminal_tasks = False, set()
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
        restored += 1
        checkout = destination / "worktree"
        if not checkout.is_dir() or checkout.is_symlink():
            # NF-2026-00162: the retained workspace itself is gone (swept), not
            # merely dis-registered. Reinstating the registration would fail its
            # git-identity check and surface as ``restored_registration_lost`` /
            # ``retention_restore_registration_failed`` -- a hash/identity drift
            # that sends the operator hunting for tampering instead of for a swept
            # directory. Name the absence directly and skip the registration path,
            # whose comparison assumes the checkout is present.
            item["state"] = "restored_workspace_absent"
            workspace_absent.append(item_id)
            _atomic_json(manifest_path, manifest)
            continue
        # Moving the checkout back is only half the round trip: the git worktree
        # registration the quarantine move orphaned must be reinstated too, or the
        # restored directory is attributed to nobody and every reclamation path
        # loses sight of it permanently. Reinstate best-effort, then VERIFY the
        # attribution below so one un-reinstatable entry can never silently ride
        # out as a success.
        try:
            _reinstate_registration(root, worktree_base, item, repo_common_dir)
        except StorageRetentionError:
            pass
        if repo_common_dir and worktree_storage._git_common_dir(checkout) == repo_common_dir:
            item["state"] = "restored"
            reinstated.append(item_id)
        elif item.get("reclaim_authority") in {
            "terminal_needfix_request_ledger",
            "terminal_task_request_ledger",
        }:
            owner_task_id = worktree_storage._request_ledger_owner_task_id(
                root, worktree_base, destination
            )
            owner_terminal = False
            if item.get("reclaim_authority") == "terminal_needfix_request_ledger":
                owner_terminal = (
                    needfix_verified and owner_task_id in terminal_needfix_tasks
                )
            else:
                owner_terminal = (
                    task_lineage_verified and owner_task_id in terminal_tasks
                )
            if owner_terminal and owner_task_id == item.get("owner_task_id"):
                # This checkout was already orphaned before quarantine. Restoring
                # its files to that exact, ledger-owned state is a truthful full
                # rollback; no Git registration existed that could be reinstated.
                item["state"] = "restored_orphaned"
                restored_orphaned.append(item_id)
            else:
                item["state"] = "restored_registration_lost"
                registration_lost.append(item_id)
        else:
            # Files are back, but the registration could not be reinstated: record
            # a loud, non-ok outcome instead of reporting the worktree restored
            # when it is in fact stranded.
            item["state"] = "restored_registration_lost"
            registration_lost.append(item_id)
        _atomic_json(manifest_path, manifest)
    manifest["status"] = "restored" if restored else manifest.get("status", "quarantined")
    manifest["restored_count"] = len(reinstated) + len(restored_orphaned)
    manifest["restored_orphaned"] = sorted(restored_orphaned)
    manifest["registration_lost"] = sorted(registration_lost)
    manifest["workspace_absent"] = sorted(workspace_absent)
    _atomic_json(manifest_path, manifest)
    _append_audit(root, {
        "schema_id": "aiworkhub.storage_retention_audit.v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "restore_completed",
        "batch_id": batch_id,
        "count": restored,
        "reinstated": len(reinstated),
        "restored_orphaned": sorted(restored_orphaned),
        "registration_lost": sorted(registration_lost),
        "workspace_absent": sorted(workspace_absent),
    })
    if workspace_absent:
        # Named ahead of any registration failure: a swept retained workspace is a
        # distinct condition from a registration/identity drift, and reporting it
        # as the latter is exactly the misdirection NF-2026-00162 removes.
        raise StorageRetentionError(
            "retention_restore_workspace_absent:" + ",".join(sorted(workspace_absent))
        )
    if registration_lost:
        # A restore that returns the files but not their registration is worse
        # than a failed restore: it looks like success while permanently
        # stranding storage. Fail loudly instead.
        raise StorageRetentionError(
            "retention_restore_registration_failed:" + ",".join(sorted(registration_lost))
        )
    return {
        "ok": True,
        "batch_id": batch_id,
        "restored": restored,
        "reinstated": len(reinstated),
        "restored_orphaned": len(restored_orphaned),
    }


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
    # The undo window protects every batch that still holds a worktree a restore
    # could return. A batch empty in BOTH its record and on disk holds nothing to
    # restore, so it is reapable before its deadline; a batch whose record reads
    # empty while its directory still holds bytes keeps its full window.
    if datetime.now(timezone.utc) < deadline and not _batch_reapable_empty(manifest, batch):
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


def purge_empty_batches(
    repo_root: Path | str, *, confirm: bool, base: Path | None = None
) -> dict[str, Any]:
    """Operator-invoked collector for empty worktree quarantine batches.

    ``quarantine`` now reaps an empty batch at the moment it opens one, so new
    empty batches never accumulate at the source. This is the consumer for any
    that already exist (created before that self-reap, or by an older build): one
    named, operator-reachable trigger that removes every batch empty in BOTH its
    record and on disk, and only those. A batch holding any file -- including a
    record-empty batch that still physically holds bytes -- is never touched here;
    it keeps its full undo window and is handled by the ordinary restore/purge
    path. Mirrors ``terminal_log_retention.purge_empty_batches``.
    """
    if not confirm:
        raise StorageRetentionError("explicit_confirmation_required")
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root(root)).resolve()
    qroot = _read_quarantine_root(root, worktree_base)
    if qroot is None:
        return {"ok": True, "purged": 0, "batch_ids": [], "bytes": 0}
    repo_id = _repo_id(root)
    purged: list[str] = []
    for entry in sorted(qroot.iterdir()):
        if not entry.is_dir() or entry.is_symlink() or not _ID_RE.fullmatch(entry.name):
            continue
        try:
            manifest = _load_manifest(entry / MANIFEST_NAME, repo_id)
        except StorageRetentionError:
            continue
        if _batch_reapable_empty(manifest, entry):
            shutil.rmtree(entry, ignore_errors=True)
            purged.append(entry.name)
    if purged:
        _append_audit(root, {
            "schema_id": "aiworkhub.storage_retention_audit.v1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "empty_batches_collected",
            "count": len(purged),
            "batch_ids": sorted(purged),
            "bytes": 0,
        })
    return {"ok": True, "purged": len(purged), "batch_ids": sorted(purged), "bytes": 0}


def _recorded_heads(root: Path, worktree_base: Path) -> dict[str, str]:
    """Map worktree id -> recorded HEAD from this repository's batch manifests.

    A worktree whose admin entry (and its HEAD) was pruned can still be proven to
    belong here when a quarantine batch this repository wrote recorded that exact
    id and head. Read-only: consults only manifests this repo's identity opens.
    """
    heads: dict[str, str] = {}
    try:
        qroot = _read_quarantine_root(root, worktree_base)
        repo_id = _repo_id(root)
    except StorageRetentionError:
        return heads
    if qroot is None:
        return heads
    for entry in sorted(qroot.iterdir()):
        if not entry.is_dir() or not _ID_RE.fullmatch(entry.name):
            continue
        try:
            manifest = _load_manifest(entry / MANIFEST_NAME, repo_id)
        except StorageRetentionError:
            continue
        for item in manifest.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            head = str(item.get("head") or "").strip()
            if item_id and _HEAD_RE.fullmatch(head):
                heads.setdefault(item_id, head)
    return heads


def recover_stranded_worktrees(
    repo_root: Path | str,
    *,
    confirm: bool = False,
    reason: str = "",
    base: Path | None = None,
) -> dict[str, Any]:
    """Explicit, operator-invoked recovery for already-stranded worktrees.

    A worktree stranded by a past lossy restore is on disk, counted in the
    footprint, yet attributed to nobody -- outside every reclamation path. This
    is the inverse of :func:`prune_stale_registrations` (which drops stale
    registrations): it reinstates the registration for stranded worktrees this
    repository provably owns, and reports the remainder as orphaned-but-
    reclaimable with their count and bytes so an operator can act.

    It is invoked ONLY by an explicit operator call (e.g. the dashboard's
    "recover stranded storage" action). It NEVER runs on import, on a read, or as
    a side effect of any preview/snapshot/restore path -- ``invoked_from`` in the
    result records exactly that. ``confirm=False`` (the default) is a read-only
    report of what it would do; ``confirm=True`` requires a non-empty ``reason``,
    performs the re-registration, and records the reason in the audit log.
    """
    root = Path(repo_root).resolve()
    worktree_base = (base or configured_worktree_root(root)).resolve()
    repo_common_dir = worktree_storage._git_common_dir(root)
    if not repo_common_dir:
        raise StorageRetentionError("retention_repo_git_dir_unresolved")
    recorded_heads = _recorded_heads(root, worktree_base)
    reattachable: list[dict[str, Any]] = []
    orphaned: list[dict[str, Any]] = []
    if worktree_base.is_dir():
        for entry in sorted(worktree_base.iterdir()):
            if (
                not entry.is_dir()
                or entry.name.startswith(".")
                or not _ID_RE.fullmatch(entry.name)
            ):
                continue
            checkout = entry / "worktree"
            if not checkout.is_dir():
                continue
            if worktree_storage._git_common_dir(checkout) == repo_common_dir:
                continue  # healthy: still attributed to this repository
            size = worktree_storage.directory_size_bytes(entry)
            admin = _worktree_admin_dir(checkout)
            admin_ours = admin is not None and _admin_under_repo(admin, repo_common_dir)
            head = recorded_heads.get(entry.name, "")
            # Provably ours when the admin entry it names is under this repo's git
            # dir AND is either still present (repair reconnects it) or its HEAD
            # was recorded by one of our own batches (reconstruct then repair).
            provable = bool(admin_ours) and (admin.exists() or bool(_HEAD_RE.fullmatch(head)))
            if provable:
                reattachable.append({"id": entry.name, "size_bytes": size})
            else:
                orphaned.append({
                    "id": entry.name,
                    "size_bytes": size,
                    "reason": "head_unrecoverable" if admin_ours else "unprovable_foreign_repo",
                })
    reattachable.sort(key=lambda item: item["id"])
    orphaned.sort(key=lambda item: item["id"])
    report: dict[str, Any] = {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "dry_run": not confirm,
        "invoked_from": (
            "storage_retention.recover_stranded_worktrees "
            "(explicit operator action; never on import or as a read side effect)"
        ),
        "reason": str(reason or ""),
        "reattachable": reattachable,
        "reattachable_count": len(reattachable),
        "reattachable_bytes": sum(int(item["size_bytes"]) for item in reattachable),
        "orphaned_reclaimable": orphaned,
        "orphaned_reclaimable_count": len(orphaned),
        "orphaned_reclaimable_bytes": sum(int(item["size_bytes"]) for item in orphaned),
    }
    if not confirm:
        return report
    if not str(reason or "").strip():
        raise StorageRetentionError("retention_recover_reason_required")
    reattached: list[str] = []
    failed: list[str] = []
    for item in reattachable:
        item_id = item["id"]
        try:
            _reinstate_registration(
                root,
                worktree_base,
                {"id": item_id, "head": recorded_heads.get(item_id, "")},
                repo_common_dir,
                head=recorded_heads.get(item_id, ""),
            )
        except StorageRetentionError:
            pass
        checkout = worktree_base / item_id / "worktree"
        if worktree_storage._git_common_dir(checkout) == repo_common_dir:
            reattached.append(item_id)
        else:
            failed.append(item_id)
    _append_audit(root, {
        "schema_id": "aiworkhub.storage_retention_audit.v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "stranded_recovery_completed",
        "reason": str(reason or ""),
        "reattached": sorted(reattached),
        "reattach_failed": sorted(failed),
        "orphaned_reclaimable_count": len(orphaned),
        "orphaned_reclaimable_bytes": report["orphaned_reclaimable_bytes"],
    })
    report["reattached"] = sorted(reattached)
    report["reattached_count"] = len(reattached)
    report["reattach_failed"] = sorted(failed)
    report["ok"] = not failed
    return report


ARTIFACT_GC_SCHEMA_ID = "aiworkhub.storage_retention.artifact_gc.v1"
ARTIFACT_GC_PHASE_SCHEMA_ID = "aiworkhub.storage_retention.artifact_gc.phase.v1"
_ARTIFACT_GC_PRESERVED_NAMES = frozenset({"manifest.json", "receipt.json", "receipts"})
_ARTIFACT_GC_PHASE_ORDER = (
    "validated",
    "inventoried",
    "ephemeral_removed",
    "predecessor_unpin_intent",
    "predecessor_unpinned",
    "completed",
)
_ARTIFACT_GC_LOADABLE_PHASES = _ARTIFACT_GC_PHASE_ORDER + ("quarantined",)


def _artifact_gc_phase_path(repo_root: Path, request_id: str) -> Path:
    return repo_root / ".aiworkhub" / "runtime" / "storage" / "artifact-gc" / f"{request_id}.json"


def _artifact_gc_receipt_path(entry: Path) -> Path:
    return entry / "artifact-gc.receipt.json"


def _artifact_gc_canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _artifact_gc_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_artifact_gc_canonical_json(dict(value)).encode("utf-8")).hexdigest()


def _artifact_gc_is_preserved(name: str) -> bool:
    return name in _ARTIFACT_GC_PRESERVED_NAMES or name.endswith(".receipt.json")


def _load_artifact_gc_phase(path: Path, request_id: str, digest: str) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("schema_id") != ARTIFACT_GC_PHASE_SCHEMA_ID:
        return None
    if str(raw.get("request_id") or "") != request_id:
        return None
    if str(raw.get("canonical_digest") or "") != digest:
        return None
    if str(raw.get("phase") or "") not in _ARTIFACT_GC_LOADABLE_PHASES:
        return None
    return raw


def _write_artifact_gc_phase(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_json(path, payload)


def _exact_registered_checkout(repo_root: Path, checkout: Path) -> Path | None:
    rc, output = worktree_storage._git(repo_root, "worktree", "list", "--porcelain")
    if rc != 0:
        raise StorageRetentionError("registered_worktree_list_failed")
    wanted = {
        os.path.normcase(str(checkout)),
        os.path.normcase(str(checkout.resolve(strict=False))),
    }
    matches: list[Path] = []
    for line in output.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line[len("worktree "):])
        try:
            resolved = os.path.normcase(str(candidate.resolve(strict=False)))
        except OSError:
            continue
        if os.path.normcase(str(candidate)) in wanted or resolved in wanted:
            matches.append(candidate)
    unique: list[Path] = []
    seen: set[str] = set()
    for match in matches:
        key = os.path.normcase(str(match.resolve(strict=False)))
        if key in seen:
            continue
        seen.add(key)
        unique.append(match)
    if len(unique) > 1:
        raise StorageRetentionError("registered_worktree_ambiguous")
    return unique[0] if unique else None


def _checkout_admin_dir(repo_root: Path, checkout: Path) -> Path | None:
    common = worktree_storage._git_common_dir(repo_root)
    if not common:
        return None
    admin_root = Path(common) / "worktrees"
    if admin_root.is_symlink() or not admin_root.is_dir():
        return None
    expected = os.path.normcase(str((checkout / ".git").resolve(strict=False)))
    matches: list[Path] = []
    try:
        entries = list(admin_root.iterdir())
    except OSError as exc:
        raise StorageRetentionError("registered_worktree_list_failed") from exc
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        gitdir_file = entry / "gitdir"
        if gitdir_file.is_symlink() or not gitdir_file.is_file():
            continue
        try:
            reverse = gitdir_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        reverse_path = Path(reverse)
        if not reverse_path.is_absolute():
            reverse_path = entry / reverse_path
        if os.path.normcase(str(reverse_path.resolve(strict=False))) == expected:
            matches.append(entry)
    if len(matches) > 1:
        raise StorageRetentionError("registered_worktree_ambiguous")
    return matches[0] if matches else None


def _artifact_gc_entry_reason(worktree_base: Path, entry: Path) -> str:
    if entry.is_symlink() or (
        entry.exists() and (entry.parent != worktree_base or not entry.is_dir())
    ):
        return "ambiguous_ownership"
    return ""


def _artifact_gc_checkout_reason(repo_root: Path, checkout: Path) -> str:
    if checkout.is_symlink() or (checkout.exists() and not checkout.is_dir()):
        return "ambiguous_ownership"
    if checkout.exists():
        repo_common = worktree_storage._git_common_dir(repo_root)
        checkout_common = worktree_storage._git_common_dir(checkout)
        if not repo_common or not checkout_common or checkout_common != repo_common:
            return "ambiguous_ownership"
        return ""
    try:
        _exact_registered_checkout(repo_root, checkout)
    except StorageRetentionError:
        return "ambiguous_ownership"
    return ""


def _artifact_gc_owned_paths_reason(
    repo_root: Path, worktree_base: Path, entry: Path
) -> str:
    reason = _artifact_gc_entry_reason(worktree_base, entry)
    if reason:
        return reason
    return _artifact_gc_checkout_reason(repo_root, entry / "worktree")


def _inventory_request_entry(
    entry: Path, repo_root: Path
) -> tuple[list[str], list[str], str]:
    ephemeral: list[str] = []
    preserved: list[str] = []
    if entry.exists():
        if entry.is_symlink() or not entry.is_dir():
            return [], [], "ambiguous_ownership"
        try:
            children = sorted(entry.iterdir(), key=lambda item: item.name)
        except OSError:
            return [], [], "ambiguous_ownership"
        for child in children:
            name = child.name
            if child.is_symlink():
                return [], [], "ambiguous_ownership"
            if _artifact_gc_is_preserved(name):
                preserved.append(name)
            else:
                ephemeral.append(name)
    if "worktree" not in ephemeral and "worktree" not in preserved:
        try:
            registered = _exact_registered_checkout(repo_root, entry / "worktree")
        except StorageRetentionError:
            return [], [], "ambiguous_ownership"
        if registered is not None:
            ephemeral.append("worktree")
            ephemeral.sort()
    return ephemeral, preserved, ""


def _remove_registered_checkout(repo_root: Path, checkout: Path) -> None:
    if _artifact_gc_checkout_reason(repo_root, checkout):
        raise StorageRetentionError("artifact_gc_identity_changed")
    registered = _exact_registered_checkout(repo_root, checkout)
    if registered is None and not checkout.exists():
        return
    target = registered if registered is not None else checkout
    rc, _output = worktree_storage._git(
        repo_root, "worktree", "remove", "--force", str(target)
    )
    still_registered = _exact_registered_checkout(repo_root, checkout) is not None
    if rc == 0 and not checkout.exists() and not still_registered:
        return
    if not checkout.exists():
        admin = _checkout_admin_dir(repo_root, checkout)
        if admin is not None:
            shutil.rmtree(admin)
        if _exact_registered_checkout(repo_root, checkout) is None:
            return
    raise StorageRetentionError("registered_worktree_remove_failed")


def _remove_ephemeral_names(
    repo_root: Path, entry: Path, names: list[str], *, removed: list[str] | None = None
) -> list[str]:
    if removed is None:
        removed = []
    if entry.is_symlink() or (entry.exists() and not entry.is_dir()):
        raise StorageRetentionError("artifact_gc_identity_changed")
    recorded = set(removed)
    for name in names:
        if _artifact_gc_is_preserved(name) or name in {".", ".."} or "/" in name or "\\" in name:
            continue
        target = entry / name
        if target.parent != entry:
            raise StorageRetentionError("artifact_gc_identity_changed")
        if name in recorded:
            continue
        if name == "worktree":
            if _artifact_gc_checkout_reason(repo_root, target):
                raise StorageRetentionError("artifact_gc_identity_changed")
            _remove_registered_checkout(repo_root, target)
            removed.append(name)
            recorded.add(name)
            continue
        if not entry.is_dir():
            removed.append(name)
            recorded.add(name)
            continue
        if target.is_symlink():
            raise StorageRetentionError("artifact_gc_identity_changed")
        if not target.exists():
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        removed.append(name)
        recorded.add(name)
    return removed


def _artifact_gc_live_process_reason(repo_root: Path, request_id: str) -> str:
    from . import task_retention

    identity, identity_verified = task_retention._request_process_identity_state(
        repo_root, request_id
    )
    if not identity_verified or identity == "unknown":
        return "ambiguous_ownership"
    if identity == "live":
        return "live_process"
    holders, verified = task_retention.live_process_holders(repo_root, request_id)
    if not verified:
        return "ambiguous_ownership"
    if holders:
        return "live_process"
    return ""


def _reclaim_predecessor_entry(
    repo_root: Path,
    worktree_base: Path,
    predecessor_id: str,
) -> str:
    pred_entry = worktree_base / predecessor_id
    if _artifact_gc_entry_reason(worktree_base, pred_entry):
        return "predecessor_ambiguous_ownership"
    if _artifact_gc_checkout_reason(repo_root, pred_entry / "worktree"):
        return "predecessor_ambiguous_ownership"
    pred_ephemeral, _pred_preserved, pred_ambiguous = _inventory_request_entry(
        pred_entry, repo_root
    )
    if pred_ambiguous:
        return pred_ambiguous
    _remove_ephemeral_names(repo_root, pred_entry, pred_ephemeral)
    return ""


def _clear_predecessor_pin(repo_root: Path, task_id: str, predecessor_id: str) -> None:
    db_path = task_store.canonical_db_path(repo_root)
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT card_json FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            connection.commit()
            return
        try:
            card = json.loads(row[0] or "{}")
        except json.JSONDecodeError:
            connection.commit()
            return
        if not isinstance(card, dict):
            connection.commit()
            return
        predecessor = card.get("rework_predecessor")
        if (
            isinstance(predecessor, dict)
            and str(predecessor.get("request_id") or "").strip() == predecessor_id
        ):
            updated = dict(card)
            updated.pop("rework_predecessor", None)
            connection.execute(
                "UPDATE tasks SET card_json=? WHERE task_id=?",
                (json.dumps(updated, ensure_ascii=False, sort_keys=True), task_id),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _fail_closed_artifact_gc(
    *,
    reason: str,
    task_id: str,
    request_id: str,
    canonical_digest: str,
    ephemeral: list[str] | None = None,
    preserved: list[str] | None = None,
    removed: list[str] | None = None,
    predecessor_unpinned: str = "",
) -> dict[str, Any]:
    return {
        "canonical_digest": canonical_digest,
        "deleted": False,
        "ephemeral": list(ephemeral or []),
        "ok": False,
        "predecessor_unpinned": predecessor_unpinned,
        "preserved": list(preserved or []),
        "reason": reason,
        "receipt_digest": "",
        "removed": list(removed or []),
        "replayed": False,
        "request_id": request_id,
        "schema_id": ARTIFACT_GC_SCHEMA_ID,
        "status": "failed_closed",
        "task_id": task_id,
    }


def _restore_quarantined_request_entry(
    repo_root: Path,
    worktree_base: Path,
    entry: Path,
    *,
    request_id: str,
    batch_id: str,
) -> str:
    if entry.exists():
        if entry.is_symlink() or entry.parent != worktree_base or not entry.is_dir():
            return "ambiguous_ownership"
        return ""
    if not batch_id or not _ID_RE.fullmatch(batch_id):
        return ""
    qroot = _quarantine_root(repo_root, worktree_base)
    quarantined = qroot / batch_id / request_id
    if not quarantined.exists():
        return ""
    if (
        quarantined.is_symlink()
        or not quarantined.is_dir()
        or quarantined.parent.parent != qroot
        or quarantined.name != request_id
    ):
        return "ambiguous_ownership"
    shutil.move(str(quarantined), str(entry))
    return ""


def _quarantine_failed_artifact_gc(
    repo_root: Path,
    worktree_base: Path,
    entry: Path,
    *,
    task_id: str,
    request_id: str,
    canonical_digest: str,
    phase: str,
    error: str,
    phase_path: Path,
    ephemeral: list[str] | None = None,
    preserved: list[str] | None = None,
    removed: list[str] | None = None,
    predecessor_unpinned: str = "",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    batch_id = f"agc{now.strftime('%Y%m%dT%H%M%S')}-{request_id[:12]}"
    resume_phase = phase if phase in _ARTIFACT_GC_PHASE_ORDER else "validated"
    retry_evidence = {
        "canonical_digest": canonical_digest,
        "error": str(error)[:500],
        "phase": resume_phase,
        "request_id": request_id,
        "retryable": True,
        "schema_id": ARTIFACT_GC_PHASE_SCHEMA_ID,
        "task_id": task_id,
    }
    moved = False
    phase_written = False
    quarantined: Path | None = None
    try:
        qroot = _ensure_quarantine_root(repo_root, worktree_base)
        batch = qroot / batch_id
        batch.mkdir(parents=True, exist_ok=False, mode=0o700)
        quarantined = batch / request_id
        if entry.exists() and entry.is_dir() and not entry.is_symlink() and entry.parent == worktree_base:
            shutil.move(str(entry), str(quarantined))
            moved = True
        _atomic_json(
            batch / MANIFEST_NAME,
            {
                "batch_id": batch_id,
                "created_at": now.isoformat(),
                "items": [{"id": request_id, "state": "quarantined_failed_cleanup"}],
                "repo_id": _repo_id(repo_root),
                "retry_evidence": retry_evidence,
                "schema_id": SCHEMA_ID,
                "status": "artifact_gc_failed",
            },
        )
        _write_artifact_gc_phase(
            phase_path,
            {
                "batch_id": batch_id,
                "canonical_digest": canonical_digest,
                "ephemeral": list(ephemeral or []),
                "phase": "quarantined",
                "predecessor_unpinned": predecessor_unpinned,
                "preserved": list(preserved or []),
                "removed": list(removed or []),
                "request_id": request_id,
                "retry_evidence": retry_evidence,
                "schema_id": ARTIFACT_GC_PHASE_SCHEMA_ID,
                "task_id": task_id,
                "updated_at": now.isoformat(),
            },
        )
        phase_written = True
        _append_audit(
            repo_root,
            {
                "action": "artifact_gc_quarantined",
                "batch_id": batch_id,
                "request_id": request_id,
                "schema_id": "aiworkhub.storage_retention_audit.v1",
                "task_id": task_id,
                "timestamp": now.isoformat(),
            },
        )
    except Exception:
        retry_evidence = dict(
            retry_evidence,
            batch_id=batch_id,
            error=str(error)[:500],
            quarantine_failed=True,
        )
        if not phase_written:
            if (
                moved
                and quarantined is not None
                and quarantined.exists()
                and not entry.exists()
            ):
                try:
                    shutil.move(str(quarantined), str(entry))
                except Exception:
                    retry_evidence = dict(retry_evidence, restore_failed=True)
            return {
                "batch_id": "",
                "canonical_digest": canonical_digest,
                "deleted": False,
                "ephemeral": list(ephemeral or []),
                "ok": False,
                "predecessor_unpinned": predecessor_unpinned,
                "preserved": list(preserved or []),
                "reason": "cleanup_failed",
                "receipt_digest": "",
                "removed": list(removed or []),
                "replayed": False,
                "request_id": request_id,
                "retry_evidence": retry_evidence,
                "schema_id": ARTIFACT_GC_SCHEMA_ID,
                "status": "failed_closed",
                "task_id": task_id,
            }
        return {
            "batch_id": batch_id,
            "canonical_digest": canonical_digest,
            "deleted": False,
            "ephemeral": list(ephemeral or []),
            "ok": False,
            "predecessor_unpinned": predecessor_unpinned,
            "preserved": list(preserved or []),
            "reason": "quarantine_audit_failed",
            "receipt_digest": "",
            "removed": list(removed or []),
            "replayed": False,
            "request_id": request_id,
            "retry_evidence": retry_evidence,
            "schema_id": ARTIFACT_GC_SCHEMA_ID,
            "status": "failed_closed",
            "task_id": task_id,
        }
    return {
        "batch_id": batch_id,
        "canonical_digest": canonical_digest,
        "deleted": False,
        "ephemeral": list(ephemeral or []),
        "ok": False,
        "predecessor_unpinned": predecessor_unpinned,
        "preserved": list(preserved or []),
        "reason": "cleanup_failed",
        "receipt_digest": "",
        "removed": list(removed or []),
        "replayed": False,
        "request_id": request_id,
        "retry_evidence": retry_evidence,
        "schema_id": ARTIFACT_GC_SCHEMA_ID,
        "status": "quarantined",
        "task_id": task_id,
    }


def _resume_quarantined_artifact_gc(
    repo_root: Path,
    worktree_base: Path,
    entry: Path,
    *,
    request_id: str,
    phase_state: Mapping[str, Any],
) -> tuple[str, str]:
    retry = phase_state.get("retry_evidence")
    resume_phase = ""
    if isinstance(retry, dict):
        resume_phase = str(retry.get("phase") or "")
    restore_error = _restore_quarantined_request_entry(
        repo_root,
        worktree_base,
        entry,
        request_id=request_id,
        batch_id=str(phase_state.get("batch_id") or ""),
    )
    if restore_error:
        return "", restore_error
    owned = _artifact_gc_owned_paths_reason(repo_root, worktree_base, entry)
    if owned:
        return "", owned
    if resume_phase in _ARTIFACT_GC_PHASE_ORDER and resume_phase != "completed":
        return resume_phase, ""
    return "validated", ""


def _complete_artifact_gc(
    repo_root: Path,
    entry: Path,
    phase_path: Path,
    *,
    task_id: str,
    request_id: str,
    digest: str,
    removed: list[str],
    preserved: list[str],
    predecessor_unpinned: str,
    ephemeral: list[str] | None = None,
) -> dict[str, Any]:
    status = "already_cleaned" if not removed else "cleaned"
    receipt = {
        "canonical_digest": digest,
        "deleted": bool(removed),
        "ephemeral": list(ephemeral or []),
        "ok": True,
        "predecessor_unpinned": predecessor_unpinned,
        "preserved": list(preserved),
        "reason": "",
        "removed": list(removed),
        "replayed": False,
        "request_id": request_id,
        "schema_id": ARTIFACT_GC_SCHEMA_ID,
        "status": status,
        "task_id": task_id,
    }
    receipt["receipt_digest"] = _artifact_gc_digest(
        {key: value for key, value in receipt.items() if key != "receipt_digest"}
    )
    if entry.is_dir() and not entry.is_symlink():
        _atomic_json(_artifact_gc_receipt_path(entry), receipt)
    phase_payload = {
        "canonical_digest": digest,
        "ephemeral": list(ephemeral or []),
        "phase": "completed",
        "predecessor_unpinned": predecessor_unpinned,
        "preserved": preserved,
        "receipt": receipt,
        "removed": removed,
        "request_id": request_id,
        "schema_id": ARTIFACT_GC_PHASE_SCHEMA_ID,
        "task_id": task_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_artifact_gc_phase(phase_path, phase_payload)
    return receipt


def _load_replay_accepted_artifact_gc(
    repo_root: Path | str,
    evidence: Mapping[str, Any],
    base: Path | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    from . import task_retention

    root = Path(repo_root).resolve()
    request_hint = ""
    digest_hint = ""
    if isinstance(evidence, Mapping):
        request_hint = str(evidence.get("request_id") or "").strip()
        digest_hint = str(evidence.get("canonical_digest") or "").strip().lower()
    phase_hint = None
    if request_hint and digest_hint:
        hint_path = _artifact_gc_phase_path(root, request_hint)
        if hint_path.is_file() and not hint_path.is_symlink():
            phase_hint = _load_artifact_gc_phase(hint_path, request_hint, digest_hint)
    verdict = task_retention.validate_accepted_cleanup_evidence(
        root, evidence, phase_evidence=phase_hint
    )
    task_id = str(verdict.get("task_id") or "")
    request_id = str(verdict.get("request_id") or "")
    digest = str(verdict.get("canonical_digest") or "")
    if not verdict.get("ok"):
        return None, _fail_closed_artifact_gc(
            reason=str(verdict.get("reason") or "unknown_identity"),
            task_id=task_id,
            request_id=request_id,
            canonical_digest=digest,
        )
    worktree_base = (base or configured_worktree_root(root)).resolve()
    entry = worktree_base / request_id
    owned = _artifact_gc_owned_paths_reason(root, worktree_base, entry)
    if owned:
        return None, _fail_closed_artifact_gc(
            reason=owned,
            task_id=task_id,
            request_id=request_id,
            canonical_digest=digest,
        )
    phase_path = _artifact_gc_phase_path(root, request_id)
    if phase_path.exists() and (phase_path.is_symlink() or not phase_path.is_file()):
        return None, _fail_closed_artifact_gc(
            reason="ambiguous_ownership",
            task_id=task_id,
            request_id=request_id,
            canonical_digest=digest,
        )
    phase_state = _load_artifact_gc_phase(phase_path, request_id, digest)
    if phase_path.is_file() and phase_state is None:
        return None, _fail_closed_artifact_gc(
            reason="ambiguous_ownership",
            task_id=task_id,
            request_id=request_id,
            canonical_digest=digest,
        )
    if isinstance(phase_state, dict) and phase_state.get("phase") == "completed":
        stored = phase_state.get("receipt")
        if isinstance(stored, dict) and stored.get("schema_id") == ARTIFACT_GC_SCHEMA_ID:
            expected_digest = _artifact_gc_digest(
                {key: value for key, value in stored.items() if key != "receipt_digest"}
            )
            if str(stored.get("receipt_digest") or "") != expected_digest:
                return None, _fail_closed_artifact_gc(
                    reason="unknown_identity",
                    task_id=task_id,
                    request_id=request_id,
                    canonical_digest=digest,
                )
            replayed = dict(stored)
            replayed["replayed"] = True
            return None, replayed
    return {
        "digest": digest,
        "entry": entry,
        "ephemeral": list((phase_state or {}).get("ephemeral") or []),
        "phase": str((phase_state or {}).get("phase") or ""),
        "phase_path": phase_path,
        "phase_state": phase_state,
        "predecessor_unpinned": str((phase_state or {}).get("predecessor_unpinned") or ""),
        "preserved": list((phase_state or {}).get("preserved") or []),
        "removed": list((phase_state or {}).get("removed") or []),
        "request_id": request_id,
        "root": root,
        "task_id": task_id,
        "verdict": verdict,
        "worktree_base": worktree_base,
    }, None


def _artifact_gc_ctx_inventory(ctx: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ephemeral": list(ctx.get("ephemeral") or []),
        "predecessor_unpinned": str(ctx.get("predecessor_unpinned") or ""),
        "preserved": list(ctx.get("preserved") or []),
        "removed": list(ctx.get("removed") or []),
    }


def _resume_quarantined_or_validate_artifact_gc(ctx: dict[str, Any]) -> dict[str, Any] | None:
    if ctx["phase"] == "quarantined":
        phase, resume_error = _resume_quarantined_artifact_gc(
            ctx["root"],
            ctx["worktree_base"],
            ctx["entry"],
            request_id=ctx["request_id"],
            phase_state=ctx["phase_state"] or {},
        )
        if resume_error:
            return _fail_closed_artifact_gc(
                reason=resume_error,
                task_id=ctx["task_id"],
                request_id=ctx["request_id"],
                canonical_digest=ctx["digest"],
                **_artifact_gc_ctx_inventory(ctx),
            )
        ctx["phase"] = phase
    if ctx["phase"] not in _ARTIFACT_GC_PHASE_ORDER:
        _write_artifact_gc_phase(
            ctx["phase_path"],
            {
                "canonical_digest": ctx["digest"],
                "ephemeral": ctx["ephemeral"],
                "phase": "validated",
                "predecessor_unpinned": ctx["predecessor_unpinned"],
                "preserved": ctx["preserved"],
                "removed": ctx["removed"],
                "request_id": ctx["request_id"],
                "schema_id": ARTIFACT_GC_PHASE_SCHEMA_ID,
                "task_id": ctx["task_id"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        ctx["phase"] = "validated"
    return None


def _inventory_accepted_artifact_gc(ctx: dict[str, Any]) -> dict[str, Any] | None:
    if ctx["phase"] != "validated":
        return None
    ephemeral, preserved, ambiguous = _inventory_request_entry(ctx["entry"], ctx["root"])
    if ambiguous:
        return _fail_closed_artifact_gc(
            reason=ambiguous,
            task_id=ctx["task_id"],
            request_id=ctx["request_id"],
            canonical_digest=ctx["digest"],
            **_artifact_gc_ctx_inventory(ctx),
        )
    _write_artifact_gc_phase(
        ctx["phase_path"],
        {
            "canonical_digest": ctx["digest"],
            "ephemeral": ephemeral,
            "phase": "inventoried",
            "predecessor_unpinned": ctx["predecessor_unpinned"],
            "preserved": preserved,
            "removed": ctx["removed"],
            "request_id": ctx["request_id"],
            "schema_id": ARTIFACT_GC_PHASE_SCHEMA_ID,
            "task_id": ctx["task_id"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    ctx["phase_state"] = {
        "ephemeral": ephemeral,
        "preserved": preserved,
    }
    ctx["ephemeral"] = ephemeral
    ctx["preserved"] = preserved
    ctx["phase"] = "inventoried"
    return None


def _delete_ephemeral_accepted_artifacts(ctx: dict[str, Any]) -> dict[str, Any] | None:
    if ctx["phase"] != "inventoried":
        return None
    pending = list((ctx["phase_state"] or {}).get("ephemeral") or ctx.get("ephemeral") or [])
    preserved = ctx["preserved"]
    if not pending:
        pending, preserved, ambiguous = _inventory_request_entry(ctx["entry"], ctx["root"])
        if ambiguous:
            return _fail_closed_artifact_gc(
                reason=ambiguous,
                task_id=ctx["task_id"],
                request_id=ctx["request_id"],
                canonical_digest=ctx["digest"],
                **_artifact_gc_ctx_inventory(ctx),
            )
        ctx["ephemeral"] = pending
        ctx["preserved"] = preserved
    else:
        ctx["ephemeral"] = pending
    process_reason = _artifact_gc_live_process_reason(ctx["root"], ctx["request_id"])
    if process_reason:
        return _fail_closed_artifact_gc(
            reason=process_reason,
            task_id=ctx["task_id"],
            request_id=ctx["request_id"],
            canonical_digest=ctx["digest"],
            **_artifact_gc_ctx_inventory(ctx),
        )
    removed = list(ctx.get("removed") or [])
    try:
        _remove_ephemeral_names(ctx["root"], ctx["entry"], pending, removed=removed)
    except Exception as exc:
        ctx["removed"] = removed
        return _quarantine_failed_artifact_gc(
            ctx["root"],
            ctx["worktree_base"],
            ctx["entry"],
            task_id=ctx["task_id"],
            request_id=ctx["request_id"],
            canonical_digest=ctx["digest"],
            phase=ctx["phase"],
            error=str(exc),
            phase_path=ctx["phase_path"],
            **_artifact_gc_ctx_inventory(ctx),
        )
    _write_artifact_gc_phase(
        ctx["phase_path"],
        {
            "canonical_digest": ctx["digest"],
            "ephemeral": pending,
            "phase": "ephemeral_removed",
            "predecessor_unpinned": ctx["predecessor_unpinned"],
            "preserved": preserved,
            "removed": removed,
            "request_id": ctx["request_id"],
            "schema_id": ARTIFACT_GC_PHASE_SCHEMA_ID,
            "task_id": ctx["task_id"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    ctx["ephemeral"] = pending
    ctx["preserved"] = preserved
    ctx["removed"] = removed
    ctx["phase"] = "ephemeral_removed"
    return None


def _reclaim_unpin_predecessor_artifacts(ctx: dict[str, Any]) -> dict[str, Any] | None:
    from . import task_retention

    if ctx["phase"] not in {"ephemeral_removed", "predecessor_unpin_intent"}:
        return None
    predecessor_id = str(ctx["verdict"].get("predecessor_request_id") or "").strip()
    if not predecessor_id:
        predecessor_id = str(ctx.get("predecessor_unpinned") or "").strip()
    predecessor_unpinned = ""
    if predecessor_id:
        if not _ID_RE.fullmatch(predecessor_id):
            return _fail_closed_artifact_gc(
                reason="predecessor_identity_invalid",
                task_id=ctx["task_id"],
                request_id=ctx["request_id"],
                canonical_digest=ctx["digest"],
                **_artifact_gc_ctx_inventory(ctx),
            )
        pins, pin_verified = task_retention.live_rework_references(
            ctx["root"], predecessor_id
        )
        if not pin_verified:
            return _quarantine_failed_artifact_gc(
                ctx["root"],
                ctx["worktree_base"],
                ctx["entry"],
                task_id=ctx["task_id"],
                request_id=ctx["request_id"],
                canonical_digest=ctx["digest"],
                phase=ctx["phase"],
                error="predecessor_lineage_unverified",
                phase_path=ctx["phase_path"],
                **_artifact_gc_ctx_inventory(ctx),
            )
        if not pins:
            pred_process_reason = _artifact_gc_live_process_reason(
                ctx["root"], predecessor_id
            )
            if pred_process_reason:
                return _fail_closed_artifact_gc(
                    reason=pred_process_reason,
                    task_id=ctx["task_id"],
                    request_id=ctx["request_id"],
                    canonical_digest=ctx["digest"],
                    **_artifact_gc_ctx_inventory(ctx),
                )
            if ctx["phase"] != "predecessor_unpin_intent":
                _write_artifact_gc_phase(
                    ctx["phase_path"],
                    {
                        "canonical_digest": ctx["digest"],
                        "ephemeral": ctx["ephemeral"],
                        "phase": "predecessor_unpin_intent",
                        "predecessor_unpinned": predecessor_id,
                        "preserved": ctx["preserved"],
                        "removed": ctx["removed"],
                        "request_id": ctx["request_id"],
                        "schema_id": ARTIFACT_GC_PHASE_SCHEMA_ID,
                        "task_id": ctx["task_id"],
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                ctx["phase"] = "predecessor_unpin_intent"
                ctx["predecessor_unpinned"] = predecessor_id
            reclaim_error = _reclaim_predecessor_entry(
                ctx["root"], ctx["worktree_base"], predecessor_id
            )
            if reclaim_error:
                return _quarantine_failed_artifact_gc(
                    ctx["root"],
                    ctx["worktree_base"],
                    ctx["entry"],
                    task_id=ctx["task_id"],
                    request_id=ctx["request_id"],
                    canonical_digest=ctx["digest"],
                    phase=ctx["phase"],
                    error=reclaim_error,
                    phase_path=ctx["phase_path"],
                    **_artifact_gc_ctx_inventory(ctx),
                )
            _clear_predecessor_pin(ctx["root"], ctx["task_id"], predecessor_id)
            predecessor_unpinned = predecessor_id
    _write_artifact_gc_phase(
        ctx["phase_path"],
        {
            "canonical_digest": ctx["digest"],
            "ephemeral": ctx["ephemeral"],
            "phase": "predecessor_unpinned",
            "predecessor_unpinned": predecessor_unpinned,
            "preserved": ctx["preserved"],
            "removed": ctx["removed"],
            "request_id": ctx["request_id"],
            "schema_id": ARTIFACT_GC_PHASE_SCHEMA_ID,
            "task_id": ctx["task_id"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    ctx["predecessor_unpinned"] = predecessor_unpinned
    ctx["phase"] = "predecessor_unpinned"
    return None


def cleanup_accepted_artifacts(
    repo_root: Path | str,
    *,
    evidence: Mapping[str, Any],
    base: Path | None = None,
) -> dict[str, Any]:
    ctx, early = _load_replay_accepted_artifact_gc(repo_root, evidence, base)
    if ctx is None:
        return early if early is not None else _fail_closed_artifact_gc(
            reason="unknown_identity",
            task_id="",
            request_id="",
            canonical_digest="",
        )
    try:
        early = _resume_quarantined_or_validate_artifact_gc(ctx)
        if early is not None:
            return early
        early = _inventory_accepted_artifact_gc(ctx)
        if early is not None:
            return early
        early = _delete_ephemeral_accepted_artifacts(ctx)
        if early is not None:
            return early
        early = _reclaim_unpin_predecessor_artifacts(ctx)
        if early is not None:
            return early
        return _complete_artifact_gc(
            ctx["root"],
            ctx["entry"],
            ctx["phase_path"],
            task_id=ctx["task_id"],
            request_id=ctx["request_id"],
            digest=ctx["digest"],
            removed=ctx["removed"],
            preserved=ctx["preserved"],
            predecessor_unpinned=ctx["predecessor_unpinned"],
            ephemeral=ctx["ephemeral"],
        )
    except Exception as exc:
        return _quarantine_failed_artifact_gc(
            ctx["root"],
            ctx["worktree_base"],
            ctx["entry"],
            task_id=ctx["task_id"],
            request_id=ctx["request_id"],
            canonical_digest=ctx["digest"],
            phase=ctx["phase"],
            error=str(exc),
            phase_path=ctx["phase_path"],
            **_artifact_gc_ctx_inventory(ctx),
        )


__all__ = [
    "ARTIFACT_GC_SCHEMA_ID",
    "AUDIT_RELATIVE_PATH",
    "QUARANTINE_DIRNAME",
    "SCHEMA_ID",
    "StorageRetentionError",
    "cleanup_accepted_artifacts",
    "list_batches",
    "plan_worktree_reclaim",
    "preview",
    "prune_stale_registrations",
    "purge",
    "purge_empty_batches",
    "quarantine",
    "recover_stranded_worktrees",
    "restore",
]
