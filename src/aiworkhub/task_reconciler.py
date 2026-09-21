"""Idempotent, provider-neutral durable worker reconciler (B412).

Closes the measured B411 launcher-loss gap: isolated worker request
``d6b6e8ee4080420fb555692e741452bf`` exited successfully at
2026-07-15T14:21:41Z but stayed ``processing`` until a coordinator manually
invoked reconciliation an hour later. The gap was structural, not a logic
bug: ``process_launcher.py::ProcessManager`` only re-scans persisted
requests when a NEW ``ProcessManager`` happens to be constructed (MCP
server start, or an explicit ``status``/``list_processes``/``reconcile``
call) -- if nothing calls it, an exited worker sits unfinalized forever.

This module adds no second task queue and no duplicate promotion logic. It
only repeatedly drives the EXISTING
``ProcessManager._reconcile_persisted_requests`` / ``_finalize_isolated_request``
path (scope validation, validation commands, promotion, ``taskctl review``
for a clean exit; review/blocked routing with a normalized outcome for
lost/timed-out/cancelled/failed work -- never pending, never automatic
retry/relaunch) on a bounded scan interval, from a process that survives
every MCP/VS Code/launcher restart. A single-instance advisory lock keeps
one reconciler running per repo; the reconciliation work itself is already
interprocess-safe via ``ProcessManager._registry_lock`` (flock), so a
duplicate scan -- from two lock holders racing, or the same holder scanning
twice -- is always a no-op: an already-terminal request is left alone, and
a still actively-supervised request is left alone.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import core
from . import process_launcher
from . import review_lifecycle
from . import review_orchestrator
from .platform_io import (
    DIRECTORY_DESCRIPTOR_BACKEND_NONE,
    chmod_fd,
    close_directory_descriptor,
    directory_descriptor_backend,
    is_windows,
    lock_fd,
    lock_file_open_flags,
    nofollow_open_flag,
    open_directory_descriptor,
    stat_owned_by_current_user,
    windows_descriptor_secret_trust,
    unlock_fd,
)


DEFAULT_SCAN_INTERVAL_SECONDS = 30.0
MIN_SCAN_INTERVAL_SECONDS = 5.0
MAX_SCAN_INTERVAL_SECONDS = 3600.0
SCAN_INTERVAL_ENV = "AIWORKHUB_RECONCILER_SCAN_INTERVAL_SECONDS"
# Repository-local, non-durable runtime tree (never the historical
# any package-install/monorepo lock path): .aiworkhub/runtime/locks/.
LOCK_REL_PATH = Path(".aiworkhub/runtime/locks/task_reconciler.lock")
# The reconciler is the only thing that finalizes an exited worker, and its
# health used to live in one process's memory: if the thread never started or
# died, every surface still answered "fine" and cards sat in `processing`
# forever. The scan record is written to disk so any process -- a manager chat,
# the dashboard, a later server -- can ask when the loop last closed and get an
# answer that outlives the process that produced it.
STATUS_REL_PATH = Path(".aiworkhub/runtime/task_reconciler_status.json")
# A heartbeat is only evidence while it is recent. Past this many missed
# intervals the record is reported stale rather than healthy, because a scan
# that last ran hours ago is indistinguishable from no reconciler at all.
STALE_SCAN_INTERVALS = 4.0
MIN_STALE_SCAN_SECONDS = 300.0
# Finalizing exited workers is correctness and runs every pass (0.01 s
# measured). Sweeping retained workspaces is housekeeping whose cost is
# dominated by re-proving that pinned rework predecessors are still pinned --
# ~100 of them at ~3 s each on this repository. Running both on one cadence
# made a finished card's time-to-review hostage to garbage collection, so the
# sweep runs on every Nth pass instead.
GC_SCAN_EVERY_N_PASSES = 20
AUTHORITY_RETRY_SECONDS = 0.25
# A cause that cannot change without the environment changing must not be
# retried at full speed.  A Windows host reported the acquisition counter
# climbing 88 -> 230 within seconds against a lock it could never open: the
# loop spun a core, wrote nothing durable, and buried the one fact that
# mattered (the same reason, every single time) under the attempt count.
#
# Classification rule -- read the code that RAISES the reason, never its name,
# exactly as ``dependency_autolaunch.DETERMINISTIC_DENIAL_REASONS`` does:
#   deterministic: every operand is a property of the lock path's own
#                  filesystem objects, so an identical retry can only
#                  reproduce the identical failure; only an environment change
#                  -- ownership, mode, the directory itself, the host's own
#                  primitives -- can clear it.
#   transient:     at least one operand lives outside this process's view.
#                  ``reconciler_lock_held`` is the whole of that set: another
#                  live owner, which may exit at any moment.
# Fail closed: a failure that cannot be PROVEN deterministic stays transient
# and keeps the fast retry.  Backing off a transient failure delays a
# legitimate takeover; leaving a deterministic one out costs only the retries
# it would have saved.
DETERMINISTIC_LOCK_FAILURE_REASONS = frozenset({"reconciler_lock_unsafe"})
AUTHORITY_BACKOFF_FACTOR = 2.0
AUTHORITY_BACKOFF_MAX_SECONDS = 60.0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scan_interval_seconds() -> float:
    try:
        value = float(os.environ.get(SCAN_INTERVAL_ENV, str(DEFAULT_SCAN_INTERVAL_SECONDS)))
    except (TypeError, ValueError):
        value = DEFAULT_SCAN_INTERVAL_SECONDS
    return max(MIN_SCAN_INTERVAL_SECONDS, min(value, MAX_SCAN_INTERVAL_SECONDS))


def _process_identity() -> dict[str, Any]:
    """Return the strongest process identity this platform can prove."""

    return {
        "owner_pid": os.getpid(),
        "owner_pid_start_ticks": process_launcher._pid_start_ticks(os.getpid()),
    }


class ReconcilerLockHeld(RuntimeError):
    """Another reconciler instance already holds the single-instance lock."""


class ReconcilerLockUnsafe(RuntimeError):
    """The authority path cannot safely identify an ordinary lock file."""


def lock_failure_reason(error: BaseException) -> str:
    """The stable token of one acquisition failure, without its path operand.

    Both acquisition exceptions carry ``<reason>:<path>``; the path is evidence
    for a human and noise for a classifier, so only the token is matched.
    """

    return str(error).split(":", 1)[0]


def classify_lock_failure(error: BaseException) -> str:
    """Classify one acquisition failure as ``deterministic`` or ``transient``.

    Fail closed.  Only a reason proven to depend solely on the lock path's own
    filesystem objects is deterministic; everything else -- including a reason
    nobody has classified yet -- keeps the fast retry.
    """

    if lock_failure_reason(error) in DETERMINISTIC_LOCK_FAILURE_REASONS:
        return "deterministic"
    return "transient"


def _lock_metadata_unsafe(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
        or not stat_owned_by_current_user(metadata)
    )


def _lock_path_metadata(lock_path: Path) -> os.stat_result | None:
    try:
        return os.stat(lock_path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path}") from exc


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{path}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
        or not stat_owned_by_current_user(metadata)
    ):
        raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{path}")
    return metadata.st_dev, metadata.st_ino


@contextlib.contextmanager
def single_instance_lock(lock_path: Path):
    """Bounded, non-blocking advisory single-instance lock.

    The repository descriptor is a stable guard for the canonical lock-parent
    chain on POSIX.  The lock itself is opened relative to a bound parent
    descriptor, whose pathname identity is revalidated after acquisition.

    Where the host cannot hold a descriptor on a directory at all -- Windows,
    which defines neither ``O_DIRECTORY`` nor ``O_NOFOLLOW`` and rejects
    ``os.open`` on a directory outright -- the parent is NOT pinned, and its
    identity is proven by re-``stat``ing the pathname instead.  That is a
    genuinely weaker guarantee, so it is named in the yielded identity
    (``parent_authority_backend`` and ``reduced_guarantees``) and carried into
    the durable status record: this lock never reports the same authority on
    two hosts while actually holding different guarantees.

    What is NOT reduced on either host: single-instance exclusion, and the
    proof that the file locked is the file validated.  Both rest on the
    ``before``/``after``/``locked_path`` device+inode chain below, and
    ``os.stat`` supplies a real volume serial and file index on Windows too.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_identity = _directory_identity(lock_path.parent)
    parent_backend = directory_descriptor_backend()
    parent_pinned = parent_backend != DIRECTORY_DESCRIPTOR_BACKEND_NONE
    reduced_guarantees = (
        [] if parent_pinned else ["lock_parent_not_pinned_to_a_descriptor"]
    )

    repo_fd: int | None = None
    repo_locked = False
    rel_parts = LOCK_REL_PATH.parts
    is_canonical_path = (
        len(lock_path.parts) > len(rel_parts)
        and lock_path.parts[-len(rel_parts):] == rel_parts
    )
    # The repository descriptor is a directory descriptor like any other, so it
    # is gated on the same capability rather than on a second platform test.
    if parent_pinned and is_canonical_path:
        repo_path = lock_path.parents[len(rel_parts) - 1]
        try:
            repo_fd = open_directory_descriptor(repo_path)
            if repo_fd is None:
                # Unreachable while ``parent_pinned`` holds; kept so the
                # descriptor stays typed as an int for the lock call.
                raise OSError("reconciler_repo_descriptor_unavailable")
            lock_fd(repo_fd, blocking=False)
            repo_locked = True
        except OSError as exc:
            if repo_fd is not None:
                os.close(repo_fd)
            raise ReconcilerLockHeld(f"reconciler_lock_held:{lock_path}") from exc

    parent_fd: int | None = None
    try:
        parent_fd = open_directory_descriptor(lock_path.parent)
    except OSError as exc:
        if repo_locked and repo_fd is not None:
            with contextlib.suppress(OSError):
                unlock_fd(repo_fd)
            os.close(repo_fd)
        raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path.parent}") from exc
    fd: int | None = None
    try:
        if parent_fd is not None:
            parent_metadata = os.fstat(parent_fd)
            observed_parent = (parent_metadata.st_dev, parent_metadata.st_ino)
        else:
            # No descriptor to fstat, so re-prove the pathname instead.  This
            # is the reduced guarantee named in the docstring: the directory is
            # not pinned, so a swap between this check and the open below is
            # detected afterwards rather than excluded outright.
            observed_parent = _directory_identity(lock_path.parent)
        if observed_parent != parent_identity:
            raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path.parent}")
        before = _lock_path_metadata(lock_path)
        if before is not None and _lock_metadata_unsafe(before):
            raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path}")
        flags = lock_file_open_flags(nofollow=True)
        try:
            if parent_fd is not None and os.open in os.supports_dir_fd:
                fd = os.open(lock_path.name, flags, 0o600, dir_fd=parent_fd)
            else:
                fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path}") from exc
        metadata = os.fstat(fd)
        after = _lock_path_metadata(lock_path)
        identity = (metadata.st_dev, metadata.st_ino)
        unsafe = (
            _lock_metadata_unsafe(metadata)
            or after is None
            or _lock_metadata_unsafe(after)
            or (after.st_dev, after.st_ino) != identity
            or (before is not None and (before.st_dev, before.st_ino) != identity)
        )
        if unsafe:
            raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path}")
        with contextlib.suppress(OSError):
            chmod_fd(fd, 0o600)
        try:
            lock_fd(fd, blocking=False)
        except OSError as exc:
            raise ReconcilerLockHeld(f"reconciler_lock_held:{lock_path}") from exc
        locked_path = _lock_path_metadata(lock_path)
        if (
            _directory_identity(lock_path.parent) != parent_identity
            or locked_path is None
            or _lock_metadata_unsafe(locked_path)
            or (locked_path.st_dev, locked_path.st_ino) != identity
        ):
            raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path}")
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {_utcnow()}\n".encode("utf-8"))
        except OSError:
            pass
        yield {
            **_process_identity(),
            "parent_authority_backend": parent_backend,
            "reduced_guarantees": list(reduced_guarantees),
        }
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                unlock_fd(fd)
            os.close(fd)
        close_directory_descriptor(parent_fd)
        if repo_locked and repo_fd is not None:
            with contextlib.suppress(OSError):
                unlock_fd(repo_fd)
            os.close(repo_fd)


def status_path(repo: Path | str) -> Path:
    return Path(repo).resolve() / STATUS_REL_PATH


def write_status(repo: Path | str, payload: dict[str, Any]) -> None:
    """Record the scan outcome durably; never let bookkeeping break the loop."""

    target = status_path(repo)
    record = {"schema_id": "aiworkhub.task_reconciler_status.v1", **payload}
    tmp: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        try:
            fd_stat = os.fstat(fd)
            path_stat = os.stat(tmp, follow_symlinks=False)
            if (
                not stat.S_ISREG(fd_stat.st_mode)
                or fd_stat.st_nlink != 1
                or (fd_stat.st_dev, fd_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)
            ):
                raise OSError("unsafe reconciler status temporary file")
            with contextlib.suppress(OSError):
                chmod_fd(fd, 0o600)
            os.write(fd, json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, target)
        tmp = None
    except OSError:
        # A reconciler that cannot write its own heartbeat must still
        # reconcile; the missing record is itself reported as unknown health.
        return
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _status_privacy_unsafe(metadata: os.stat_result, fd: int) -> bool:
    """Is this heartbeat readable beyond its owner, in the host's own terms?

    NF-2026-00012: POSIX answers with the mode bits, but Windows synthesises
    ``0o666`` for every writable file, so ``mode & 0o077`` was ALWAYS true there
    and ``read_status`` discarded a heartbeat the reconciler had just written --
    which is why ``durable_status_present`` read false on a healthy Windows host.
    Windows answers the same question against the open descriptor's security
    descriptor instead, and still fails closed when it cannot be read.
    """

    if is_windows():
        trusted, _reason = windows_descriptor_secret_trust(fd)
        return not trusted
    return bool(stat.S_IMODE(metadata.st_mode) & 0o077)


def read_status(repo: Path | str) -> dict[str, Any]:
    target = status_path(repo)
    try:
        before = _lock_path_metadata(target)
    except ReconcilerLockUnsafe:
        return {}
    if before is None or _lock_metadata_unsafe(before):
        return {}
    flags = os.O_RDONLY | nofollow_open_flag()
    try:
        fd = os.open(target, flags)
    except OSError:
        return {}
    try:
        metadata = os.fstat(fd)
        after = _lock_path_metadata(target)
        identity = (metadata.st_dev, metadata.st_ino)
        if (
            _lock_metadata_unsafe(metadata)
            or after is None
            or _lock_metadata_unsafe(after)
            or (after.st_dev, after.st_ino) != identity
            or (before.st_dev, before.st_ino) != identity
            or _status_privacy_unsafe(metadata, fd)
        ):
            return {}
        with os.fdopen(fd, encoding="utf-8") as stream:
            fd = -1
            record = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError, ReconcilerLockUnsafe):
        return {}
    finally:
        if fd >= 0:
            os.close(fd)
    return record if isinstance(record, dict) else {}


def lock_is_held(lock_path: Path) -> bool:
    """Probe an existing authority lock without creating any path."""

    flags = os.O_RDWR | nofollow_open_flag()
    try:
        fd = os.open(lock_path, flags)
    except OSError:
        return False
    try:
        try:
            lock_fd(fd, blocking=False)
        except OSError:
            return True
        unlock_fd(fd)
        return False
    finally:
        os.close(fd)


def _staleness(record: dict[str, Any]) -> tuple[bool, float | None]:
    """Return (stale, age_seconds) for a durable scan record.

    A pass that is still running is measured from when it STARTED: it is
    evidence of a live reconciler, and calling it stale the moment it begins
    would report every long sweep as a dead loop.
    """

    at = record.get("scan_finished_epoch")
    if record.get("scan_in_progress") and not isinstance(at, (int, float)):
        at = record.get("scan_started_epoch")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return True, None
    age = max(0.0, time.time() - float(at))
    interval = record.get("scan_interval_seconds")
    interval = float(interval) if isinstance(interval, (int, float)) and not isinstance(interval, bool) else DEFAULT_SCAN_INTERVAL_SECONDS
    budget = max(MIN_STALE_SCAN_SECONDS, interval * STALE_SCAN_INTERVALS)
    return age > budget, age


def run_scan(
    manager: process_launcher.ProcessManager | None = None,
    *,
    repo: Path | None = None,
    include_gc: bool = True,
) -> dict[str, Any]:
    """Run one idempotent, bounded reconciliation scan.

    Reuses ProcessManager.reconcile() for both correctness paths: expired
    pid-null reviewer reservations are terminalized and their durable intents
    settled through the canonical task-store API, then exited workers follow
    the existing finalize path. Exact PID/start-tick and spawn-commit evidence
    keeps live or ambiguous reservations untouched. Repeated scans see the
    terminal event and perform no second retirement transition. Never touches
    AITools/taskdb.py directly and never invokes a model/chat endpoint.

    The GC pass also prunes stale pending callback rows. Measured 2026-09-08:
    84 pending Claude outbox rows (age p50 124 h) whose tasks were long
    superseded or archived fenced task hygiene with ``callback_live`` on every
    run, and were pruned only inside the route rebind of an OPTIONAL manager
    call -- so a repository nobody bootstrapped kept the fence forever. The
    rule stays the store's own ``_task_still_in_matching_terminal_state``:
    a wake for a task still in review is left pending. Shares
    ``core._prune_stale_callbacks`` with the hygiene pass rather than opening
    a second copy of the same three lines.
    """
    mgr = manager or process_launcher.ProcessManager(repo=repo)
    result = mgr.reconcile(include_gc=include_gc)
    callback_prune: dict[str, Any] = {"state": "skipped", "reason": "gc_not_included"}
    task_hygiene: dict[str, Any] = {"state": "skipped", "reason": "gc_not_included"}
    if include_gc:
        try:
            callback_prune = core._prune_stale_callbacks(Path(mgr.repo).resolve())
        except Exception as exc:  # noqa: BLE001 -- a scan must never fail on hygiene
            callback_prune = {"state": "skipped", "reason": f"{type(exc).__name__}"[:80]}
        try:
            # The durable owner of the archive sweep. Bootstrap offers to run
            # it only on an explicit tool call, so a repository nobody
            # bootstraps -- or one whose manager only ever touches the route
            # gate -- would otherwise never be swept. The throttle is the same
            # one bootstrap consults, so this cannot double-run it.
            task_hygiene = core._schedule_task_hygiene(run_when_due=True)
        except Exception as exc:  # noqa: BLE001 -- see above
            task_hygiene = {"state": "skipped", "reason": f"{type(exc).__name__}"[:80]}
    review_recovery = _scan_review_ready_recovery(mgr)
    # Bounded to cards whose durable exact binding has no verdict yet; a pass
    # with nothing pending reads one index range and writes nothing.
    wave_goal_bindings = _scan_wave_goal_bindings(Path(mgr.repo).resolve())
    return {
        "ok": True,
        "scanned_at": _utcnow(),
        "gc_included": bool(include_gc),
        "callback_prune": callback_prune,
        "task_hygiene": task_hygiene,
        **result,
        "review_recovery": review_recovery,
        "wave_goal_bindings": wave_goal_bindings,
    }


# 0.11.37 measured what a fixed one-action policy costs: a 143.496 s
# recovery pass advanced exactly one durable effect and returned with 35
# actions still pending, so NF865 -- a current target seeded behind that
# backlog -- faced 35 further passes before its own first lens could run.
# The capability was never missing; ``ReviewOrchestrator.drain`` has always
# taken ``max_actions`` and this scan asked for one.
#
# A batch is the repair because a pass is dominated by what drain pays once
# per pass rather than per action: one routing-catalog build (the
# orchestrator's own note measures +1.49 s per launch if it were rebuilt per
# action), one route/dead-chain reconcile, one manager-ready publish and one
# counts verification. Each additional action in the same pass adds only its
# own reservation and effect, so throughput rises while the fixed cost does
# not.
#
# The ceiling is headroom, not a throughput target: it stays at half the
# orchestrator's own per-pass bound (``DEFAULT_DRAIN_MAX_ACTIONS`` = 12) so a
# recovery pass can never claim the whole outbox window, and so the loop
# still returns to worker finalization -- the correctness path this daemon
# exists for -- and to unrelated Task MCP callers inside the scan cadence.
# The floor guarantees the property the incident lacked: whenever there is
# work at all a pass advances strictly more than one action, so a backlog
# costs passes proportional to backlog/REVIEW_DRAIN_MAX_ACTIONS instead of
# one pass per action. Neither bound grows with the backlog, and drain
# remains the only thing that decides WHICH action runs.
REVIEW_DRAIN_MIN_ACTIONS = 2
REVIEW_DRAIN_MAX_ACTIONS = 6


def _review_drain_budget(ready: int) -> int:
    """Size one pass's action batch from the observed ready workload."""
    if ready <= 0:
        return 0
    return max(REVIEW_DRAIN_MIN_ACTIONS, min(REVIEW_DRAIN_MAX_ACTIONS, ready))


def _review_drain_projection(review_db: Path) -> review_lifecycle.DrainProjection:
    """Read this pass's outbox depth and drain targets under one authentication.

    ``REVIEW_DRAIN_MAX_ACTIONS`` bounds the prediction because no pass may
    reserve more than that; the batch actually primed is sliced back to the
    budget this pass computes, and a prefix of the drain's own reservation
    order is exactly what a smaller budget consumes.
    """
    return review_lifecycle.drain_projection(
        review_db, max_actions=REVIEW_DRAIN_MAX_ACTIONS
    )


def _review_backlog(counts: Mapping[str, int]) -> dict[str, int]:
    """Observed outbox depth, from the lifecycle's own truthful counts.

    ``pending_reservable`` and never ``pending``: an action parked behind a
    failed action in its own chain can never be reserved, so counting it
    would size a batch against work this pass cannot possibly do.
    """
    return {
        "pending": int(counts.get("pending") or 0),
        "pending_parked": int(counts.get("pending_parked") or 0),
        "pending_reservable": int(counts.get("pending_reservable") or 0),
    }


def _prime_drain_process_events(
    manager: Any, projection: review_lifecycle.DrainProjection, budget: int
) -> int:
    """Project this drain's process-event truth from one bounded ledger pass.

    Recovery asks the process manager for a status per action, and every
    reviewer launch appends to the same ledger.  Read cold and action by
    action that was O(full ledger x action): the measured 22-action backlog
    replayed the whole ledger once per action.  Naming the drain's request ids
    first folds them all in a single pass, after which the projection absorbs
    each append incrementally instead of being rebuilt.

    The ids come from the drain's own reservation ordering, capped at the
    actions this budget can reserve -- not from a fixed requests-per-action
    guess over the lowest chain ids, which primed rows the rotating cursor had
    already passed and left the ones it was about to reserve cold.

    Best effort by construction.  It reserves nothing and decides nothing, so
    a failure here costs only the old per-action read -- never a launch, a
    skipped action or a success claimed from missing event data.  Returns how
    many request ids the projection now answers for.
    """

    prime = getattr(manager, "prime_request_events", None)
    if not callable(prime):
        return 0
    try:
        targets = projection.request_ids(max(0, int(budget)))
        if not targets:
            return 0
        return int(prime(targets))
    except Exception:  # noqa: BLE001 -- a warm projection is never a precondition
        return 0


def _scan_review_ready_recovery(manager: Any) -> dict[str, Any]:
    """Ensure missing review chains, then let the orchestrator launch."""
    empty: dict[str, Any] = {
        "state": "skipped",
        "reason": "no_work",
        "review_recovery_scanned": 0,
        "review_recovery_ensured": 0,
        "review_recovery_skipped": 0,
        "review_recovery_failed": 0,
        "review_recovery_reasons": {},
        "review_recovery_failures": [],
        "review_recovery_drain": {
            "state": "skipped", "reason": "no_work", "budget": 0,
        },
    }
    try:
        review_db = review_orchestrator.canonical_review_db(manager)
        if review_db is None:
            empty["reason"] = "review_db_unavailable"
            return empty
        recovery = review_orchestrator.recover_review_ready_targets(
            manager, db_path=review_db,
        )
        # Launch delegation belongs to the system-owned orchestrator alone;
        # the scan only hands it work and a budget. Draining on every pass
        # with ready work -- not only on a pass that ensured something -- is
        # safe because an action is a durable, leased, exactly-once outbox
        # row: a repeated pass re-reserves nothing an earlier pass completed
        # and can never create duplicate chains, children, requests or
        # provider launches. Gating on ``ensured`` alone was the other half
        # of the starvation, because a backlog nobody added to was never
        # worked off at all.
        # ONE authenticated read of the outbox for the whole pass. The same
        # projection sizes the batch and names the process requests that batch
        # will read; asking those two questions through two entry points ran
        # the lifecycle's full-store chain authentication twice per pass, on a
        # 30 s cadence, for rows that had not moved in between.
        projection = _review_drain_projection(review_db)
        backlog = _review_backlog(projection.counts)
        ensured = int(recovery.get("review_recovery_ensured") or 0)
        # Chains ensured just above have already written their pending
        # actions, so the reservable count already includes them; ``max``
        # keeps an ensure visible without counting it twice.
        ready = max(int(backlog["pending_reservable"]), ensured)
        budget = _review_drain_budget(ready)
        if budget:
            projected = _prime_drain_process_events(manager, projection, budget)
            drain = review_orchestrator.ReviewOrchestrator(
                manager, db_path=review_db,
            ).drain(max_actions=budget)
            # attempted/completed/failed/pending come from drain itself and
            # ``review_actions`` is the post-pass ledger, so one receipt
            # answers both what this budget bought and what is left.
            receipt = dict(drain.as_dict())
            receipt.update({
                "state": "ok",
                "budget": budget,
                "ready": ready,
                "backlog": backlog,
                # How many request ids this drain's process-event truth was
                # projected from, in one pass, before any action ran.
                "process_events_projected": projected,
            })
            recovery["review_recovery_drain"] = receipt
        else:
            recovery.setdefault(
                "review_recovery_drain",
                {
                    "state": "skipped",
                    "reason": "no_work",
                    "budget": 0,
                    "ready": ready,
                    "backlog": backlog,
                },
            )
        recovery.setdefault("state", "ok")
        return recovery
    except Exception as exc:  # noqa: BLE001 -- never block worker reconcile
        empty["reason"] = f"{type(exc).__name__}"[:80]
        return empty


# A card's own create normally applies its wave-goal binding in the same call,
# so this pass only ever sees bindings whose Roadmap write was interrupted. The
# bound is headroom for a burst of those, not a throughput target.
WAVE_GOAL_BINDING_REPAIR_LIMIT = 16


def _scan_wave_goal_bindings(repo: Path) -> dict[str, Any]:
    """Converge only cards whose durable exact wave-goal binding has no verdict.

    Candidates come from ``core.pending_wave_goal_bindings``, an index-bounded
    read of unresolved binding events, so no unrelated historical card is ever
    loaded. Nothing is written unless ``AIWORKHUB_ALLOW_WRITES=1``: with writes
    disabled the pending cards are reported and left untouched. A repeated or
    concurrent pass converges on the Roadmap's ``already_applied`` and adds no
    second event.
    """

    try:
        pending = core.pending_wave_goal_bindings(
            repo, limit=WAVE_GOAL_BINDING_REPAIR_LIMIT
        )
    except Exception as exc:  # noqa: BLE001 -- never block worker reconcile
        return {"state": "skipped", "reason": f"{type(exc).__name__}"[:80], "pending": 0}
    task_ids = [str(task_id) for task_id in pending.get("task_ids") or []]
    receipt: dict[str, Any] = {
        "pending": len(task_ids),
        "truncated": bool(pending.get("truncated")),
    }
    if not task_ids:
        return {"state": "skipped", "reason": "no_work", **receipt}
    if not core.writes_allowed():
        return {
            "state": "skipped",
            "reason": "writes_disabled",
            **receipt,
            "task_ids": task_ids,
        }
    outcomes: list[dict[str, str]] = []
    for task_id in task_ids:
        try:
            outcome = core.apply_wave_goal_binding(repo, task_id)
        except Exception as exc:  # noqa: BLE001 -- one card never blocks the rest
            outcome = {"state": "pending", "reason": f"{type(exc).__name__}"[:80]}
        outcomes.append({
            "task_id": task_id,
            "state": str(outcome.get("state") or ""),
            "reason": str(outcome.get("reason") or ""),
        })
    return {"state": "ok", **receipt, "outcomes": outcomes}


class ReconcilerService:
    """Repo-bound in-process reconciler lifecycle for an MCP child."""

    # Class-level default so the counter exists on any instance, including one
    # built for a test without running __init__.
    _pass_index = 0

    def __init__(self, repo: Path, *, scan_interval_seconds: float | None = None) -> None:
        self.repo = repo.resolve()
        raw_interval = scan_interval_seconds if scan_interval_seconds is not None else _scan_interval_seconds()
        self.scan_interval_seconds = max(
            MIN_SCAN_INTERVAL_SECONDS, min(float(raw_interval), MAX_SCAN_INTERVAL_SECONDS)
        )
        self._manager = process_launcher.ProcessManager(repo=self.repo)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._last_scan: dict[str, Any] = {}
        self._last_error = ""
        self._pass_index = 0
        self._authority_state = "acquiring"
        self._authority_identity: dict[str, Any] = {}
        self._acquisition_attempts = 0
        self._last_acquisition_error = ""
        self._acquisition_backoff_seconds = 0.0

    def is_running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _loop(self) -> None:
        lock_path = self.repo / LOCK_REL_PATH
        # Grows only while the SAME deterministic cause keeps repeating, and is
        # reset by acquisition or by any transient outcome, so a real takeover
        # is never delayed by a stale penalty.
        backoff_seconds = AUTHORITY_RETRY_SECONDS
        while not self._stop_event.is_set():
            with self._state_lock:
                self._authority_state = "acquiring"
                self._acquisition_attempts = getattr(self, "_acquisition_attempts", 0) + 1
            try:
                authority = single_instance_lock(lock_path)
                with authority as identity:
                    backoff_seconds = AUTHORITY_RETRY_SECONDS
                    with self._state_lock:
                        self._authority_state = "active_owner"
                        self._authority_identity = dict(identity)
                        self._last_acquisition_error = ""
                        self._acquisition_backoff_seconds = 0.0
                    self._run_as_owner()
            except ReconcilerLockHeld as exc:
                # Another live owner is the one transient cause: it may exit at
                # any moment, so a takeover attempt must stay fast.
                backoff_seconds = AUTHORITY_RETRY_SECONDS
                with self._state_lock:
                    self._authority_state = "standby"
                    self._authority_identity = {}
                    self._last_acquisition_error = str(exc)
                    self._acquisition_backoff_seconds = AUTHORITY_RETRY_SECONDS
                self._stop_event.wait(AUTHORITY_RETRY_SECONDS)
            except ReconcilerLockUnsafe as exc:
                deterministic = classify_lock_failure(exc) == "deterministic"
                wait_seconds = backoff_seconds if deterministic else AUTHORITY_RETRY_SECONDS
                backoff_seconds = (
                    min(wait_seconds * AUTHORITY_BACKOFF_FACTOR, AUTHORITY_BACKOFF_MAX_SECONDS)
                    if deterministic
                    else AUTHORITY_RETRY_SECONDS
                )
                with self._state_lock:
                    self._authority_state = "acquisition_failed"
                    self._authority_identity = {}
                    self._last_acquisition_error = str(exc)
                    self._acquisition_backoff_seconds = wait_seconds
                self._stop_event.wait(wait_seconds)
            finally:
                with self._state_lock:
                    if self._authority_state == "active_owner":
                        self._authority_state = "released"
                        self._authority_identity = {}

    def _run_as_owner(
        self,
        *,
        max_iterations: int | None = None,
        on_scan: Any = None,
        stop_requested: Any = None,
    ) -> None:
        iterations = 0
        while not self._stop_event.is_set():
            if stop_requested is not None and stop_requested():
                break
            started = time.time()
            # The first pass sweeps, so a freshly started reconciler still
            # reclaims immediately; after that housekeeping is periodic.
            include_gc = self._pass_index % GC_SCAN_EVERY_N_PASSES == 0
            self._pass_index += 1
            # Announce the pass BEFORE running it. The record is the only way to
            # tell "no reconciler" from "a reconciler mid-pass", and a sweep can
            # run for minutes -- writing only on completion left the loop
            # invisible for exactly as long as it was busiest.
            previous = read_status(self.repo)
            owner = _process_identity()
            # The authority the lock actually granted travels into the durable
            # record: a reader in another process must be able to tell that a
            # scan ran under a REDUCED guarantee, not merely that it ran.
            with self._state_lock:
                granted = dict(self._authority_identity)
            authority_evidence = {
                "parent_authority_backend": str(
                    granted.get("parent_authority_backend", "")
                ),
                "reduced_guarantees": list(granted.get("reduced_guarantees", [])),
            }
            write_status(self.repo, {
                "pid": owner["owner_pid"],
                **owner,
                "repo": str(self.repo),
                "authority_state": "active_owner",
                "acquisition_state": "held",
                **authority_evidence,
                "scan_started_epoch": started,
                "scan_finished_epoch": None,
                "scan_in_progress": True,
                "scan_interval_seconds": self.scan_interval_seconds,
                "gc_included": include_gc,
                "last_error": "",
                "last_completed_scan": previous.get("last_completed_scan", previous if previous.get("scan_finished_epoch") else {}),
            })
            try:
                result = run_scan(self._manager, repo=self.repo, include_gc=include_gc)
                with self._state_lock:
                    self._last_scan = result
                    self._last_error = ""
                error = ""
            except Exception as exc:  # noqa: BLE001 -- lifecycle safety net
                result = {}
                error = f"{type(exc).__name__}:{exc}"[:500]
                with self._state_lock:
                    self._last_error = error
            # Written on success AND on failure: a loop that is running but
            # failing every scan must not look the same as one that is working.
            completed = {
                "scan_finished_epoch": time.time(),
                "scan_duration_seconds": round(time.time() - started, 3),
                "last_error": error,
                "finalized": result.get("finalized", 0),
                "watched": result.get("watched", 0),
                "gc_included": include_gc,
                "gc_cleaned": result.get("gc_cleaned", 0),
                "scanned_at": result.get("scanned_at", ""),
            }
            write_status(self.repo, {
                "pid": owner["owner_pid"],
                **owner,
                "repo": str(self.repo),
                "authority_state": "active_owner",
                "acquisition_state": "held",
                **authority_evidence,
                "scan_started_epoch": started,
                "scan_in_progress": False,
                "scan_interval_seconds": self.scan_interval_seconds,
                **completed,
                "last_completed_scan": completed,
            })
            if on_scan is not None:
                on_scan(result)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            if self._stop_event.wait(self.scan_interval_seconds):
                break

    def start(self) -> None:
        with self._state_lock:
            if self.is_running():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name=f"aiworkhub-task-reconciler:{self.repo.name}",
                daemon=True,
            )
            thread = self._thread
        thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        with self._state_lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None

    def health(self) -> dict[str, Any]:
        with self._state_lock:
            authority_state = getattr(self, "_authority_state", "unknown")
            return {
                "ok": (
                    self.is_running()
                    and not self._last_error
                    and authority_state != "acquisition_failed"
                ),
                "running": self.is_running(),
                "repo": str(self.repo),
                "authority_state": authority_state,
                "active_owner": authority_state == "active_owner",
                "standby": authority_state in {"acquiring", "standby"},
                "authority_identity": dict(getattr(self, "_authority_identity", {})),
                "acquisition_attempts": getattr(self, "_acquisition_attempts", 0),
                "last_acquisition_error": getattr(self, "_last_acquisition_error", ""),
                # Named so a caller can tell "retrying hard" from "backed off
                # against a cause that cannot change" without counting attempts.
                "acquisition_backoff_seconds": float(
                    getattr(self, "_acquisition_backoff_seconds", 0.0)
                ),
                # The guarantees this host could actually supply for the lock.
                # An authority that reports the same state while holding less
                # must say so here, not silently.
                "parent_authority_backend": str(
                    getattr(self, "_authority_identity", {}).get(
                        "parent_authority_backend", ""
                    )
                ),
                "reduced_guarantees": list(
                    getattr(self, "_authority_identity", {}).get(
                        "reduced_guarantees", []
                    )
                ),
                "scan_interval_seconds": self.scan_interval_seconds,
                "last_scan": dict(self._last_scan),
                "last_error": self._last_error,
            }


_SERVICES: dict[str, ReconcilerService] = {}
_SERVICES_LOCK = threading.Lock()


def ensure_started(repo: Path | str) -> ReconcilerService:
    """Start exactly one reconciliation service per canonical repository."""

    root = Path(repo).resolve()
    key = str(root)
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
        if service is None:
            service = ReconcilerService(root)
            _SERVICES[key] = service
        service.start()
        return service


def stop_reconciler(repo: Path | str) -> bool:
    key = str(Path(repo).resolve())
    with _SERVICES_LOCK:
        service = _SERVICES.pop(key, None)
    if service is None:
        return False
    service.stop()
    return True


def reconciler_health(repo: Path | str) -> dict[str, Any]:
    """Report reconciler health, in-process first and durably otherwise.

    "This process has no reconciler registered" is not the same claim as "no
    reconciler has run against this repository". A manager chat asking whether
    exited workers are being finalized needs the second answer, so the durable
    record answers when the in-process service is absent -- and a record too old
    to still be evidence is reported stale rather than healthy.
    """

    key = str(Path(repo).resolve())
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
    record = read_status(key)
    stale, age = _staleness(record) if record else (True, None)
    recorded_error = str(record.get("last_error") or "")
    claims_owner = (
        record.get("authority_state") == "active_owner"
        or record.get("acquisition_state") == "held"
    )
    owner_pid = record.get("owner_pid")
    owner_ticks = record.get("owner_pid_start_ticks")
    owner_identity_live = (
        isinstance(owner_pid, int)
        and not isinstance(owner_pid, bool)
        and (owner_ticks is None or isinstance(owner_ticks, int))
        and lock_is_held(Path(key) / LOCK_REL_PATH)
        and process_launcher._pid_matches(owner_pid, owner_ticks)
    )
    durable_authority_live = not claims_owner or owner_identity_live
    durable = {
        "durable_status_present": bool(record),
        "durable_scan_stale": stale,
        "durable_scan_age_seconds": round(age, 1) if age is not None else None,
        "durable_authority_live": durable_authority_live,
        "durable_last_scan": record,
    }
    if service is None:
        healthy = (
            bool(record)
            and not stale
            and durable_authority_live
            and not recorded_error
        )
        return {
            "ok": healthy,
            "running": False,
            "repo": key,
            "last_scan": record,
            "last_error": (
                ""
                if healthy
                else (
                    recorded_error
                    or (
                        "reconciler_recorded_owner_not_live"
                        if record and not stale and claims_owner
                        else "reconciler_unregistered_and_no_recent_scan"
                    )
                )
            ),
            "startup_error": str(record.get("startup_error") or ""),
            **durable,
        }
    return {**service.health(), **durable}


def _install_stop_handler(
    stop_flag: dict[str, bool], wake_event: threading.Event | None = None
) -> dict[int, Any]:
    def _stop(_signum: int, _frame: Any) -> None:
        stop_flag["stop"] = True
        if wake_event is not None:
            wake_event.set()

    previous = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGINT: signal.getsignal(signal.SIGINT),
    }
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    return previous


def run_daemon(
    *,
    repo: Path | None = None,
    scan_interval_seconds: float | None = None,
    max_iterations: int | None = None,
    on_scan: Any = None,
) -> int:
    """Continuous bounded reconciliation loop with passive lock failover.

    Builds exactly ONE ``ProcessManager`` for the whole daemon lifetime (not
    one per iteration). A contended daemon remains alive in standby and retries
    the non-blocking repository authority until the owner releases it or this
    process receives a stop signal.
    """
    interval = scan_interval_seconds if scan_interval_seconds is not None else _scan_interval_seconds()
    interval = max(MIN_SCAN_INTERVAL_SECONDS, min(interval, MAX_SCAN_INTERVAL_SECONDS))
    stop_flag = {"stop": False}

    root = Path(repo).resolve() if repo is not None else core.repo_root()
    service = ReconcilerService(root, scan_interval_seconds=interval)
    previous_handlers = _install_stop_handler(stop_flag, service._stop_event)
    emit = on_scan or (
        lambda result: print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    )
    lock_path = root / LOCK_REL_PATH
    try:
        while not service._stop_event.is_set():
            with service._state_lock:
                service._authority_state = "acquiring"
                service._acquisition_attempts += 1
            try:
                with single_instance_lock(lock_path) as identity:
                    with service._state_lock:
                        service._authority_state = "active_owner"
                        service._authority_identity = dict(identity)
                        service._last_acquisition_error = ""
                    service._run_as_owner(
                        max_iterations=max_iterations,
                        on_scan=emit,
                        stop_requested=lambda: stop_flag["stop"],
                    )
                    return 0
            except ReconcilerLockHeld as exc:
                with service._state_lock:
                    service._authority_state = "standby"
                    service._authority_identity = {}
                    service._last_acquisition_error = str(exc)
                service._stop_event.wait(AUTHORITY_RETRY_SECONDS)
            except ReconcilerLockUnsafe as exc:
                error = str(exc)
                with service._state_lock:
                    service._authority_state = "acquisition_failed"
                    service._authority_identity = {}
                    service._last_acquisition_error = error
                write_status(root, {
                    "authority_state": "acquisition_failed",
                    "acquisition_state": "failed",
                    "acquisition_error": error,
                    "owner_pid": None,
                    "owner_pid_start_ticks": None,
                    "scan_in_progress": False,
                    "scan_started_epoch": None,
                    "scan_finished_epoch": None,
                    "scan_interval_seconds": interval,
                    "last_error": error,
                })
                return 4
        return 0
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aiworkhub-reconciler",
        description=(
            "Idempotent durable reconciler for AIWorkHub MCP isolated "
            "workers -- token-free, filesystem/process/SQLite lifecycle "
            "checks only; never calls Claude, Codex, DeepSeek, or any chat "
            "endpoint."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_once = sub.add_parser("run-once", help="Run exactly one bounded reconciliation scan")
    run_once.add_argument("--repo", default=None)

    daemon = sub.add_parser("daemon", help="Run continuous bounded reconciliation scans")
    daemon.add_argument("--repo", default=None)
    daemon.add_argument("--scan-interval-seconds", type=float, default=None)
    daemon.add_argument("--max-iterations", type=int, default=None)

    status = sub.add_parser("status", help="Report single-instance lock presence (read-only)")
    status.add_argument("--repo", default=None)

    args = parser.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve() if args.repo else core.repo_root()
    lock_path = repo / LOCK_REL_PATH

    if args.command == "status":
        lock_present = lock_path.is_file()
        lock_held = lock_is_held(lock_path) if lock_present else False
        record = read_status(repo) if lock_held else {}
        print(json.dumps({
            "ok": True,
            "lock_path": str(lock_path),
            "lock_present": lock_present,
            "lock_held": lock_held,
            "owner_pid": record.get("owner_pid"),
            "owner_pid_start_ticks": record.get("owner_pid_start_ticks"),
        }, sort_keys=True))
        return 0

    try:
        if args.command == "run-once":
            with single_instance_lock(lock_path):
                result = run_scan(repo=repo)
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 0
        return run_daemon(
            repo=repo,
            scan_interval_seconds=args.scan_interval_seconds,
            max_iterations=args.max_iterations,
        )
    except ReconcilerLockHeld as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
