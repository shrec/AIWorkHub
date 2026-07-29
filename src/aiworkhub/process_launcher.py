"""Local, explicitly gated model-process launcher for AIWorkHub task workers.

The authoritative task queue is the selected repository's canonical
``.aiworkhub/tasking/task_queue.sqlite`` task store. This module only owns
process lifecycle evidence: start, observe, collect, timeout, and cancel. It
never selects a task by keywords and it never invokes a shell.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import html
import inspect
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import fcntl

from . import core
from . import quality_evidence
from . import task_engine
try:
    from . import project_context
except ImportError:
    class _FallbackProjectContextError(RuntimeError):
        pass

    class _FallbackProjectContext:
        RECEIPT_SCHEMA_ID = "aiworkhub.task_mcp.worker_context_receipt.v1"
        ProjectContextError = _FallbackProjectContextError
        ProjectContextResult = Any

        @staticmethod
        def collect_project_context(_repo: Path, _card: dict[str, Any]) -> None:
            return None

    project_context = _FallbackProjectContext()  # type: ignore[assignment]
from . import runtime_adapters
from . import vscode_lm_bridge
from . import worker_ai_tools_mcp
try:
    from . import deepseek_credentials
except ImportError:  # optional host-only credential helper in some worktrees
    deepseek_credentials = None  # type: ignore[assignment]
try:
    from . import glm_credentials
except ImportError:  # optional host-only credential helper in some worktrees
    glm_credentials = None  # type: ignore[assignment]
from .worker_workspace import (
    WorkerWorkspace,
    WorkspaceError,
    cleanup_workspace,
    create_workspace,
    enforce_scope,
    promote,
    provision_worker_mcp_runtime,
    run_validations,
    sandbox_argv,
    select_sandbox_backend,
    sanitized_env as _base_sanitized_env,
    unlink_if_regular,
    write_json_0600,
)
from . import worker_workspace as _worker_workspace

if hasattr(_worker_workspace, "assert_gc_safe_workspace_shape"):
    assert_gc_safe_workspace_shape = _worker_workspace.assert_gc_safe_workspace_shape
else:
    def assert_gc_safe_workspace_shape(
        request_id: str,
        path: Path,
        home: Path,
    ) -> None:
        for label, candidate in (("path", path), ("home", home)):
            if not str(candidate):
                raise WorkspaceError(f"gc_workspace_{label}_missing")
            if candidate.is_symlink():
                raise WorkspaceError(f"gc_workspace_{label}_symlink")
        if path == home or path in home.parents or home in path.parents:
            raise WorkspaceError("gc_workspace_path_home_overlap")
        for raw in (path.name, home.name):
            if request_id not in raw:
                raise WorkspaceError("gc_workspace_request_id_mismatch")

if hasattr(_worker_workspace, "validate_required_outputs"):
    validate_required_outputs = _worker_workspace.validate_required_outputs
else:
    def validate_required_outputs(
        workspace: WorkerWorkspace,
        required_outputs: list[str] | tuple[str, ...],
        allow_empty: tuple[str, ...] | None = None,
        allow_unchanged: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        unchanged_allowed = {str(v).strip().replace("\\", "/") for v in (allow_unchanged or [])}
        records: list[dict[str, Any]] = []
        for raw in required_outputs:
            pattern = str(raw or "").strip().replace("\\", "/")
            if not pattern:
                raise WorkspaceError("required_output_invalid")
            matches = sorted(workspace.path.glob(pattern))
            if not matches:
                raise WorkspaceError(f"required_output_no_matches:{pattern}")
            for path in matches:
                relative = path.relative_to(workspace.path).as_posix()
                if path.is_symlink():
                    raise WorkspaceError(f"required_output_symlink:{relative}")
                if not path.is_file():
                    raise WorkspaceError(f"required_output_non_file:{relative}")
                size = path.stat().st_size
                if size <= 0 and (allow_empty is None or relative not in allow_empty):
                    raise WorkspaceError(f"required_output_zero_bytes:{relative}")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                baseline = workspace.workspace_baseline.get(relative)
                current = f"file:{path.stat().st_mode & 0o777:o}:{digest}"
                is_unchanged = baseline in {digest, current}
                if is_unchanged:
                    if relative not in unchanged_allowed:
                        raise WorkspaceError(f"required_output_unchanged:{relative}")
                    if workspace.parent_baseline.get(relative) != current:
                        raise WorkspaceError(f"required_output_unchanged_parent_mismatch:{relative}")
                if not is_unchanged and relative in unchanged_allowed:
                    raise WorkspaceError(f"allow_unchanged_required_output_changed:{relative}")
                if is_unchanged and size <= 0:
                    raise WorkspaceError(f"required_output_unchanged:{relative}")
                records.append({
                    "path": relative,
                    "bytes": size,
                    "sha256": digest,
                    "unchanged_allowed": is_unchanged,
                })
        return records

    _worker_workspace.validate_required_outputs = validate_required_outputs


ALLOW_LAUNCH_ENV = "AIWORKHUB_ALLOW_LAUNCH"
ALLOW_WRITES_ENV = "AIWORKHUB_ALLOW_WRITES"
MAX_PROCESSES_ENV = "AIWORKHUB_MAX_PROCESSES"
EXTERNAL_READONLY_ROOTS: tuple[Path, ...] = (
    Path("/mnt/ssd/aiworkhub_data"),
    Path("/mnt/ssd/corpus"),
)
PROCESS_LOG_ENV = "AIWORKHUB_PROCESS_LOG_PATH"
PROCESS_DIR_ENV = "AIWORKHUB_PROCESS_DIR"
# Repository-local, non-durable runtime tree (never the historical
# any package-install/monorepo log path): .aiworkhub/runtime/process_logs/.
PROCESS_LOG_DEFAULT_REL = Path(".aiworkhub/runtime/process_logs/process_events.jsonl")
PROCESS_DIR_DEFAULT_REL = Path(".aiworkhub/runtime/process_logs/processes")
DEFAULT_MAX_PROCESSES = 4
MAX_CONFIGURED_PROCESSES = 32
MAX_LOG_TAIL_BYTES = 64 * 1024
MAX_RECEIPT_SCAN_BYTES = 2 * 1024 * 1024
LAUNCH_IMPLEMENTED = True
SUPERVISOR_GRACE_SECONDS = 90
# B894: narrowly scoped, one-task terminal-transition authority. Minted at
# launch time (the one moment launch_gates_open() is known true) and
# consumed by whichever process later reconciles this exact request's
# terminal outcome -- possibly a different process than the one that
# launched it, since the detached supervisor outlives the initiating MCP
# request. Never a substitute for general AIWORKHUB_ALLOW_WRITES: it is
# bound to one exact (repo, task_id, runner, topic, request_id) tuple and is
# consumed (deleted) on first use, whether or not it validates.
TERMINAL_AUTHORITY_SCHEMA_ID = "aiworkhub.task_mcp.terminal_authority.v1"
TERMINAL_AUTHORITY_KEY_FILENAME = ".terminal_authority_hmac.key"
ACTIVE_PROCESS_STATES = {"starting", "running", "cancel_requested"}
FINALIZATION_PENDING_STATES = {"release_pending", "review_pending", "reconcile_pending"}
TERMINAL_PROCESS_STATES = {
    "review_ready",
    "exited",
    "exited_without_review",
    "timed_out",
    "cancelled",
    "launch_failed",
    "worker_failed",
    "scope_rejected",
    "validation_failed",
    "promotion_conflict",
    "finalize_failed",
    "monitor_error",
    "blocked",
}


def sanitized_env(
    adapter_id: str,
    *,
    home: Path | None = None,
    isolated_task_queue_db: bool = False,
    provider_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the worker env and merge coordinator-loaded BYOK values only.

    ``worker_workspace.sanitized_env`` deliberately starts from a small
    allowlist.  This launcher wrapper keeps that behavior and adds only the
    explicit provider env returned by a credential helper after preflight.
    """
    safe = _base_sanitized_env(
        adapter_id,
        home=home,
        isolated_task_queue_db=isolated_task_queue_db,
    )
    if provider_env:
        safe.update({str(key): str(value) for key, value in provider_env.items()})
    return safe

# Failure workspaces remain available through coordinator review.  Once a
# coordinator has disposed that exact attempt (finished/archived, returned it
# to pending, or moved it to blocked), the retained workspace is no longer the
# authoritative review surface and is safe to collect.  While a card is in
# review, only the request_id named by terminal_review remains authoritative;
# older retained attempts for the same card are superseded and collectable.
GC_CANDIDATE_PROCESS_STATES = TERMINAL_PROCESS_STATES - {"blocked"}
GC_DISPOSED_CANONICAL_STATUSES = {"finished", "archived", "pending", "blocked"}


# --- B412: token-free liveness contract -------------------------------------
#
# Bounded, honest liveness states derived ONLY from the supervisor's own
# heartbeat artifact plus exact PID+/proc-start-tick identity -- never from
# "the process still exists" alone (see CLAUDE.md forbidden:
# infer_progress_from_process_exists_only) and never from a model/dashboard/
# MCP turn (forbidden: model_generated_heartbeat_or_poll_turns). Output growth
# is activity evidence only, never a correctness/percentage signal.
HEARTBEAT_LEASE_ENV = "AIWORKHUB_HEARTBEAT_LEASE_SECONDS"
QUIET_WARNING_ENV = "AIWORKHUB_QUIET_WARNING_SECONDS"
LOST_RECOVERY_GRACE_ENV = "AIWORKHUB_LOST_RECOVERY_GRACE_SECONDS"
# 4x the supervisor's default 15s heartbeat interval -- tolerant of scheduler
# jitter/GC pauses without mistaking a merely-slow heartbeat for unresponsive.
DEFAULT_HEARTBEAT_LEASE_SECONDS = 60.0
# 30 minutes of unchanged stdout/stderr size before a still-alive worker is
# surfaced as "quiet" -- an honest observation, never a failure signal.
DEFAULT_QUIET_WARNING_SECONDS = 1800.0
# Bounded grace after the heartbeat lease expires before an unresponsive-but-
# still-existing supervisor is escalated to "lost" and its exact process
# group is terminated by the reconciler.
DEFAULT_LOST_RECOVERY_GRACE_SECONDS = 120.0
LIVENESS_STATES = ("alive", "quiet", "unresponsive", "lost")


def _bounded_float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def heartbeat_lease_seconds() -> float:
    return _bounded_float_env(
        HEARTBEAT_LEASE_ENV, DEFAULT_HEARTBEAT_LEASE_SECONDS, minimum=15.0, maximum=3600.0
    )


def quiet_warning_seconds() -> float:
    return _bounded_float_env(
        QUIET_WARNING_ENV, DEFAULT_QUIET_WARNING_SECONDS, minimum=30.0, maximum=86_400.0
    )


def lost_recovery_grace_seconds() -> float:
    return _bounded_float_env(
        LOST_RECOVERY_GRACE_ENV, DEFAULT_LOST_RECOVERY_GRACE_SECONDS, minimum=15.0, maximum=3600.0
    )


def derive_liveness_state(
    *,
    now_epoch: float,
    supervisor_alive: bool,
    heartbeat_at_epoch: float | None,
    last_output_change_epoch: float | None,
    lease_seconds: float | None = None,
    warning_seconds: float | None = None,
    grace_seconds: float | None = None,
) -> dict[str, Any]:
    """Pure derivation of one bounded liveness state -- never a percentage or
    correctness claim, only alive/quiet/unresponsive/lost (see module docs).

    - ``lost``: exact PID identity no longer exists, OR an unresponsive
      supervisor exceeded the bounded recovery grace beyond its lease.
    - ``unresponsive``: the heartbeat lease expired while the exact process
      still exists (not yet past the recovery grace).
    - ``quiet``: alive with a fresh heartbeat, but stdout/stderr size has not
      changed within the warning interval -- NOT a failure.
    - ``alive``: fresh heartbeat plus exact PID identity.
    """
    lease = lease_seconds if lease_seconds is not None else heartbeat_lease_seconds()
    warning = warning_seconds if warning_seconds is not None else quiet_warning_seconds()
    grace = grace_seconds if grace_seconds is not None else lost_recovery_grace_seconds()

    heartbeat_age = (
        max(0.0, now_epoch - float(heartbeat_at_epoch)) if heartbeat_at_epoch is not None else None
    )
    activity_age = (
        max(0.0, now_epoch - float(last_output_change_epoch))
        if last_output_change_epoch is not None
        else None
    )

    if not supervisor_alive:
        state = "lost"
    elif heartbeat_age is None:
        # No heartbeat has landed yet (just started) -- fresh process start
        # is itself the freshness signal until the lease would otherwise
        # elapse.
        state = "alive"
    elif heartbeat_age > lease + grace:
        state = "lost"
    elif heartbeat_age > lease:
        state = "unresponsive"
    elif activity_age is not None and activity_age > warning:
        state = "quiet"
    else:
        state = "alive"

    return {
        "liveness_state": state,
        "heartbeat_age_seconds": heartbeat_age,
        "activity_age_seconds": activity_age,
        "heartbeat_lease_seconds": lease,
        "quiet_warning_seconds": warning,
        "lost_recovery_grace_seconds": grace,
    }


def read_supervisor_status(path: Path) -> dict[str, Any]:
    """Read one owner-only supervisor heartbeat/status artifact, failing
    CLOSED (returning ``{}``) on a symlink, non-regular file, foreign owner,
    or insecure permission bits -- a reused PID or tampered status must never
    be trusted silently. The terminal write from ``worker_supervisor.py``
    remains authoritative; this is a bounded, defensive read of it."""
    try:
        st = path.lstat()
    except OSError:
        return {}
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return {}
    if st.st_uid != os.getuid():
        return {}
    if stat.S_IMODE(st.st_mode) & 0o077:
        return {}
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            payload = json.loads(fh.read())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _terminal_authority_signing_material(
    *, repo: Path, task_id: str, runner: str, topic: str, request_id: str,
) -> bytes:
    return "|".join(
        [TERMINAL_AUTHORITY_SCHEMA_ID, str(repo), task_id, runner, topic, request_id]
    ).encode("utf-8")


def _load_or_create_terminal_authority_key(key_path: Path) -> bytes:
    """Owner-only, symlink-refusing HMAC key backing every terminal-
    authority grant issued under one ``process_dir``. Persisted (not
    per-process) so a grant minted by the process that performed the launch
    can still be verified by a different, later process reconciling this
    exact request's terminal outcome after the launcher has exited."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(key_path, flags)
    except OSError:
        fd = None
    if fd is not None:
        try:
            st = os.fstat(fd)
            if (
                stat.S_ISREG(st.st_mode)
                and stat.S_IMODE(st.st_mode) == 0o600
                and st.st_uid == os.getuid()
            ):
                with os.fdopen(fd, "rb") as fh:
                    data = fh.read()
                if len(data) == 32:
                    return data
            else:
                os.close(fd)
        except OSError:
            pass
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(key_path.parent, 0o700)
    key = os.urandom(32)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        new_fd = os.open(key_path, flags, 0o600)
    except FileExistsError:
        # Lost the create race to another process/thread -- read back
        # whatever it wrote instead of minting a second, divergent key.
        return _load_or_create_terminal_authority_key(key_path)
    os.fchmod(new_fd, 0o600)
    with os.fdopen(new_fd, "wb") as fh:
        fh.write(key)
    return key


def _write_terminal_authority_grant(
    path: Path,
    key: bytes,
    *,
    repo: Path,
    task_id: str,
    runner: str,
    topic: str,
    request_id: str,
) -> None:
    material = _terminal_authority_signing_material(
        repo=repo, task_id=task_id, runner=runner, topic=topic, request_id=request_id,
    )
    write_json_0600(path, {
        "schema_id": TERMINAL_AUTHORITY_SCHEMA_ID,
        "repo": str(repo),
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "request_id": request_id,
        "issued_at": _utcnow(),
        "signature": hmac.new(key, material, hashlib.sha256).hexdigest(),
    })


def _read_terminal_authority_grant(path: Path) -> dict[str, Any]:
    """Same fail-closed owner-only/symlink/permission/JSON contract as
    ``read_supervisor_status`` -- a tampered, foreign-owned, world-readable,
    or malformed grant is never trusted."""
    try:
        st = path.lstat()
    except OSError:
        return {}
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return {}
    if st.st_uid != os.getuid():
        return {}
    if stat.S_IMODE(st.st_mode) & 0o077:
        return {}
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            payload = json.loads(fh.read())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class LaunchRejected(RuntimeError):
    """A bounded, user-visible preflight rejection."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def launch_gates_open() -> bool:
    return (
        os.environ.get(ALLOW_LAUNCH_ENV, "0") == "1"
        and os.environ.get(ALLOW_WRITES_ENV, "0") == "1"
    )


def _configured_limit() -> int:
    try:
        value = int(os.environ.get(MAX_PROCESSES_ENV, str(DEFAULT_MAX_PROCESSES)))
    except ValueError:
        value = DEFAULT_MAX_PROCESSES
    return max(1, min(value, MAX_CONFIGURED_PROCESSES))


def _safe_tail(path: Path, max_bytes: int = MAX_LOG_TAIL_BYTES) -> str:
    if not path.is_file():
        return ""
    # O_NOFOLLOW makes the open itself refuse a symlink atomically -- the
    # is_file() check above is only a pre-filter and cannot close the race
    # window between check and open on its own (a log path could be replaced
    # by a symlink to an arbitrary file between process exit and collection).
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return ""
    try:
        with os.fdopen(fd, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


# --- B855: bounded, single-task Live Output read ----------------------------
#
# ``read_live_output_for_task`` is the low-level implementation deliberately
# kept in THIS module (not dashboard.py) even though ``dashboard.py`` already
# hosts the multi-task ``read_process_runs`` scan: dashboard.py imports
# process_launcher, so putting the reverse import here (process_launcher ->
# dashboard) would create a circular import. process_launcher.py already owns
# every low-level primitive this needs (PROCESS_LOG_ENV, _safe_tail,
# read_supervisor_status, derive_liveness_state, _pid_matches), so the single-
# task lookup lives here and dashboard_mcp_app.py calls it directly.

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# Every C0 control byte except \n (0x0A) and \t (0x09), plus DEL (0x7F).
_C0_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Long token-like runs (24+ alnum/underscore/dash chars) -- the same bounded
# heuristic style as vscode-extension/extension.js's sanitizeStderrChunk:
# never a claim of perfect secret detection, just a defensive mask so an API
# key/session token pasted into a worker's stdout never survives verbatim
# into the dashboard's Live Output panel.
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{24,}")


def _redact_long_tokens(match: "re.Match[str]") -> str:
    token = match.group(0)
    if len(token) <= 8:
        return "…redacted…"
    return f"{token[:4]}…redacted…{token[-2:]}"


def _sanitize_live_output_text(text: str) -> str:
    """Strip ANSI/C0 control sequences, redact long token-like runs, then
    HTML-escape -- in that order, so escaping never re-introduces a byte the
    control-strip pass would otherwise have removed."""
    if not text:
        return ""
    stripped = _ANSI_ESCAPE_RE.sub("", text)
    stripped = _C0_CONTROL_RE.sub("", stripped)
    redacted = _LONG_TOKEN_RE.sub(_redact_long_tokens, stripped)
    return html.escape(redacted)


def _read_byte_range(path: Path, offset: int, length: int) -> str:
    """Read exactly ``length`` bytes starting at ``offset`` from ``path``,
    O_NOFOLLOW-guarded like ``_safe_tail``. Returns ``""`` on any OS error
    (missing file, symlink, permission) -- fails closed, never raises."""
    if length <= 0:
        return ""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return ""
    try:
        with os.fdopen(fd, "rb") as fh:
            fh.seek(max(0, offset))
            return fh.read(length).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _process_log_path(repo: Path) -> Path:
    return Path(
        os.environ.get(
            PROCESS_LOG_ENV,
            str(Path(repo) / PROCESS_LOG_DEFAULT_REL),
        )
    )


def _latest_process_row_for_task(
    task_id: str, repo: Path, *, max_scan_bytes: int = 8 * 1024 * 1024
) -> dict[str, Any] | None:
    """Return the most recent merged process-log row for EXACTLY ``task_id``.

    Scans (a bounded tail of) the process event log filtering by ``task_id``
    as each line is parsed -- no row for any other task is ever merged,
    retained, or returned. This is the only place that reads
    ``process_events.jsonl`` for the Live Output feature; every stdout/stderr
    tail read that follows uses only the one path pair this row carries.
    """
    path = _process_log_path(repo)
    if not path.is_file():
        return None
    size = path.stat().st_size
    start = max(0, size - max_scan_bytes)
    with path.open("rb") as handle:
        if start:
            handle.seek(start - 1)
            if handle.read(1) != b"\n":
                handle.readline()
        payload = handle.read(max_scan_bytes)

    latest: dict[str, dict[str, Any]] = {}
    for raw_line in payload.splitlines():
        try:
            event = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        if str(event.get("task_id") or "") != task_id:
            continue
        request_id = str(event.get("request_id") or "").strip()
        if not request_id:
            continue
        latest[request_id] = {**latest.get(request_id, {}), **event, "request_id": request_id}

    if not latest:
        return None
    rows = sorted(
        latest.values(),
        key=lambda row: str(row.get("timestamp") or row.get("finished_at") or row.get("started_at") or ""),
    )
    return rows[-1]


def read_live_output_for_task(
    task_id: str,
    *,
    repo: Path,
    cursor: int = 0,
    max_bytes: int = MAX_LOG_TAIL_BYTES,
) -> dict[str, Any]:
    """Bounded, single-task Live Output read.

    Looks up ONLY the most recent process-log row for ``task_id`` (see
    ``_latest_process_row_for_task`` -- no dashboard-wide fan-out across
    other tasks' logs), then does an incremental, cursor-bounded read of that
    task's stdout log starting at ``cursor`` (never re-sends bytes already
    delivered), plus a small fixed-size stderr tail. Every returned string is
    ANSI/control-stripped, long-token-redacted, and HTML-escaped. Reports
    ``next_cursor``/``truncated`` for the caller's next incremental call, and
    the task's ``liveness_state``/``last_activity_at`` when a supervisor
    status artifact is available.

    Fails closed to ``{"ok": False, "error": "output_unavailable", ...}``
    for: no process-log row at all for this task_id (an opaque adapter with
    no CLI launch, e.g. Copilot/Claude-Chat-only), a row with no recorded log
    path, or a log file that has since been deleted -- this never raises and
    never fabricates output.
    """
    repo = Path(repo)
    bounded_max = max(1024, min(int(max_bytes), MAX_LOG_TAIL_BYTES))
    safe_cursor = max(0, int(cursor))
    base: dict[str, Any] = {
        "ok": False,
        "task_id": task_id,
        "cursor": safe_cursor,
        "next_cursor": safe_cursor,
        "truncated": False,
        "output": "",
        "stderr_tail": "",
        "liveness_state": None,
        "last_activity_at": None,
    }

    row = _latest_process_row_for_task(task_id, repo)
    if row is None:
        return {**base, "error": "output_unavailable", "reason": "no_process_log_record_for_task"}

    request_id = str(row.get("request_id") or "")
    base["request_id"] = request_id
    base["state"] = row.get("state")

    stdout_raw = row.get("stdout_path")
    stderr_raw = row.get("stderr_path")
    if not stdout_raw and not stderr_raw:
        return {**base, "error": "output_unavailable", "reason": "no_log_path_recorded"}

    stdout_path = Path(str(stdout_raw)) if stdout_raw else None
    stderr_path = Path(str(stderr_raw)) if stderr_raw else None
    stdout_exists = bool(stdout_path is not None and stdout_path.is_file())
    stderr_exists = bool(stderr_path is not None and stderr_path.is_file())
    if not stdout_exists and not stderr_exists:
        return {**base, "error": "output_unavailable", "reason": "log_file_missing"}

    new_text = ""
    next_cursor = safe_cursor
    truncated = False
    if stdout_exists:
        stdout_size = stdout_path.stat().st_size  # type: ignore[union-attr]
        if safe_cursor >= stdout_size:
            next_cursor = stdout_size
        else:
            available = stdout_size - safe_cursor
            read_len = min(available, bounded_max)
            truncated = read_len < available
            new_text = _read_byte_range(stdout_path, safe_cursor, read_len)  # type: ignore[arg-type]
            next_cursor = safe_cursor + read_len

    stderr_tail_raw = (
        _safe_tail(stderr_path, max_bytes=min(bounded_max, 8192)) if stderr_exists else ""  # type: ignore[arg-type]
    )

    liveness_state = None
    last_activity_at = None
    status_path_raw = row.get("supervisor_status_path")
    if status_path_raw:
        supervisor_status = read_supervisor_status(Path(str(status_path_raw)))
        if supervisor_status:
            try:
                pid = int(row.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            supervisor_alive = bool(pid and _pid_matches(pid, row.get("pid_start_ticks")))
            liveness = derive_liveness_state(
                now_epoch=time.time(),
                supervisor_alive=supervisor_alive,
                heartbeat_at_epoch=supervisor_status.get("heartbeat_at_epoch"),
                last_output_change_epoch=supervisor_status.get("last_output_change_epoch"),
            )
            liveness_state = liveness["liveness_state"]
            last_activity_epoch = supervisor_status.get(
                "last_output_change_epoch"
            ) or supervisor_status.get("heartbeat_at_epoch")
            if isinstance(last_activity_epoch, (int, float)):
                last_activity_at = datetime.fromtimestamp(
                    float(last_activity_epoch), tz=timezone.utc
                ).isoformat()

    return {
        **base,
        "ok": True,
        "exit_code": row.get("exit_code"),
        "liveness_state": liveness_state,
        "last_activity_at": last_activity_at,
        "next_cursor": next_cursor,
        "truncated": truncated,
        "output": _sanitize_live_output_text(new_text),
        "stderr_tail": _sanitize_live_output_text(stderr_tail_raw),
    }


def _touch_0600(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _usage_from_output(path: Path) -> dict[str, Any]:
    """Extract bounded Claude/Codex JSON usage without trusting free text."""
    result = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.0,
    }
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        return result

    def as_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def as_float(value: Any) -> float:
        try:
            return max(0.0, float(value or 0.0))
        except (TypeError, ValueError, OverflowError):
            return 0.0

    raw = path.read_text(encoding="utf-8", errors="replace")
    candidates: list[dict[str, Any]] = []
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            candidates.append(value)
    except json.JSONDecodeError:
        for line in raw.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                candidates.append(value)
    for item in candidates:
        usage = item.get("usage")
        if not isinstance(usage, dict):
            # Copilot JSONL and DeepSeek's OpenAI-compatible objects nest usage
            # under a variety of containers (response/result/data/stats/message).
            # Search them without inventing any numeric field.
            nested: Any = None
            for container_key in ("response", "result", "data", "stats", "message"):
                container = item.get(container_key)
                if isinstance(container, dict) and isinstance(container.get("usage"), dict):
                    nested = container["usage"]
                    break
            if nested is None:
                nested = item.get("token_usage") or item.get("tokens")
            usage = nested if isinstance(nested, dict) else {}
        input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details")
        if not isinstance(input_details, dict):
            input_details = {}
        result["input_tokens"] = max(
            result["input_tokens"],
            as_int(
                usage.get("input_tokens")
                or usage.get("prompt_tokens")
                or usage.get("input")
            ),
        )
        result["output_tokens"] = max(
            result["output_tokens"],
            as_int(
                usage.get("output_tokens")
                or usage.get("completion_tokens")
                or usage.get("output")
            ),
        )
        result["cached_input_tokens"] = max(
            result["cached_input_tokens"],
            as_int(
                usage.get("cache_read_input_tokens")
                or usage.get("cached_input_tokens")
                # DeepSeek's OpenAI-compatible cache-hit field. For an
                # OpenAI-shaped provider prompt_tokens already INCLUDES cached
                # input, so this is reported for observability only and is not
                # re-added to the ledger input (see _ledger_input_tokens).
                or usage.get("prompt_cache_hit_tokens")
                or input_details.get("cached_tokens")
            ),
        )
        result["cache_creation_input_tokens"] = max(
            result["cache_creation_input_tokens"],
            as_int(usage.get("cache_creation_input_tokens")),
        )
        result["cost_usd"] = max(
            result["cost_usd"],
            as_float(item.get("total_cost_usd") or item.get("cost_usd")),
        )
    return result


def _ledger_input_tokens(usage: dict[str, Any], adapter_id: str) -> int:
    """Return taskctl's total input count without double-counting cache hits."""
    base = int(usage.get("input_tokens") or 0)
    if adapter_id == "claude_cli":
        # Anthropic reports uncached, cache-read, and cache-creation input as
        # disjoint fields. OpenAI reports cached_input_tokens as a subset of
        # input_tokens, so Codex must use the base count unchanged.
        return (
            base
            + int(usage.get("cached_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
        )
    return base


def _project_context_delivery(
    context_result: project_context.ProjectContextResult | None,
    prompt_hash: str,
) -> dict[str, Any]:
    if context_result is None:
        return {"injected": False, "bundle_sha256": "", "prompt_sha256": prompt_hash}
    metadata = context_result.metadata
    return {
        "injected": True,
        "schema_id": metadata.get("schema_id"),
        "bundle_sha256": metadata.get("bundle_sha256"),
        "bundle_bytes": metadata.get("bundle_bytes"),
        "prompt_sha256": prompt_hash,
        "section_count": metadata.get("section_count"),
    }


def _receipt_text_candidates(raw_line: str) -> list[str]:
    """Return bounded text payloads used by the supported JSONL adapters."""
    candidates = [raw_line.strip()]
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        return candidates
    if not isinstance(event, dict):
        return candidates

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    item = event.get("item")
    if isinstance(item, dict):
        add(item.get("text"))  # Codex JSONL agent_message
    data = event.get("data")
    if isinstance(data, dict):
        add(data.get("content"))  # DeepSeek/Copilot assistant.message
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            for block in content[:32]:
                if isinstance(block, dict):
                    add(block.get("text"))  # Claude stream-json
        else:
            add(content)
    return candidates[:40]


def _project_context_receipt_from_output(
    path: Path,
    *,
    expected_bundle_sha256: str = "",
) -> dict[str, Any]:
    result = {
        "schema_id": project_context.RECEIPT_SCHEMA_ID,
        "acknowledged": False,
        "bundle_sha256": "",
        "prompt_sha256": "",
        "section_count": 0,
        "reason": "receipt_not_found",
    }
    # A receipt is normally emitted near the beginning of a streaming JSONL
    # run.  Reading only the final 16 KiB loses it as soon as tool results make
    # the stream larger (the 0.6.11 live canary produced 135 KiB).  Scan a
    # bounded whole log; for unusually large logs keep symmetric head/tail
    # windows so early receipts and late adapter summaries remain visible.
    try:
        size = path.stat().st_size if path.is_file() and not path.is_symlink() else 0
    except OSError:
        size = 0
    if size <= MAX_RECEIPT_SCAN_BYTES:
        text = _read_byte_range(path, 0, size)
    else:
        half = MAX_RECEIPT_SCAN_BYTES // 2
        text = _read_byte_range(path, 0, half) + "\n" + _read_byte_range(path, size - half, half)
    prefix = "PROJECT_CONTEXT_RECEIPT:"
    expected = expected_bundle_sha256.strip().lower()
    for line in reversed(text.splitlines()):
        for candidate in _receipt_text_candidates(line):
            marker = candidate.rfind(prefix)
            if marker >= 0:
                candidate = candidate[marker + len(prefix):].strip()
            try:
                value, _end = json.JSONDecoder().raw_decode(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict) or value.get("schema_id") != project_context.RECEIPT_SCHEMA_ID:
                continue
            bundle_sha = str(value.get("bundle_sha256") or "").strip().lower()
            section_raw = value.get("section_count") or 0
            section_count = int(section_raw) if str(section_raw).isdigit() else 0
            valid_sha = len(bundle_sha) == 64 and all(ch in "0123456789abcdef" for ch in bundle_sha)
            matches = not expected or bundle_sha == expected
            acknowledged = bool(value.get("acknowledged")) and valid_sha and matches and section_count > 0
            reason = str(value.get("reason") or "")[:160]
            if not valid_sha:
                reason = "receipt_bundle_sha256_invalid"
            elif not matches:
                reason = "receipt_bundle_sha256_mismatch"
            elif section_count <= 0:
                reason = "receipt_section_count_invalid"
            return {
                "schema_id": project_context.RECEIPT_SCHEMA_ID,
                "acknowledged": acknowledged,
                "bundle_sha256": bundle_sha[:80],
                "prompt_sha256": str(value.get("prompt_sha256") or "")[:80],
                "section_count": section_count,
                "reason": reason,
            }
    return result


# Sections the launcher can inject and that the worker MCP gate also accepts as
# a live tool. A section name outside this set is never credited.
_GATEABLE_CONTEXT_SECTIONS = frozenset(
    {"source_graph", "session_current_state", "ai_memory", "kb"}
)


def _injected_context_satisfaction(metadata: dict[str, Any]) -> tuple[bool, set[str]]:
    """Which required tools a VERIFIED injected project-context section already
    satisfies, and whether the worker acknowledged the injected bundle.

    Injection and a live worker call are ALTERNATIVE valid satisfaction sources
    for the same required tool -- the launcher already ran Session Manager / AI
    Memory / KB / Source Graph and injected their results with a hash receipt,
    so a worker need not re-run them by hand. A section is credited only when:

    * the whole bundle receipt is acknowledged -- the worker echoed a
      ``PROJECT_CONTEXT_RECEIPT`` whose ``bundle_sha256`` equals the stored one.
      That sha binds repository/scope identity (the bundle embeds
      ``repo_identity.scope_root``), so a tampered, repo-mismatched, or
      unacknowledged receipt yields ``(False, set())`` and the gate stays
      fail-closed; AND
    * the section itself was ``executed`` with an empty ``degraded_reason``.

    ``hit_count`` is deliberately NOT required to be > 0: an executed section
    that returned zero rows (e.g. AI Memory with no matches) is a valid, real
    result, never a "missing call" (B948/B951 regression). A degraded / stale /
    failed section is not credited, so a live recovery call is still required.
    """
    context = metadata.get("project_context") or {}
    if not isinstance(context, dict):
        return False, set()
    bundle_sha256 = str(context.get("bundle_sha256") or "").strip()
    stdout_path = str(metadata.get("stdout_path") or "").strip()
    if not bundle_sha256 or not stdout_path:
        return False, set()
    receipt = _project_context_receipt_from_output(
        Path(stdout_path), expected_bundle_sha256=bundle_sha256
    )
    if not receipt.get("acknowledged"):
        return False, set()
    satisfied: set[str] = set()
    for section in context.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = str(section.get("name") or "")
        if name not in _GATEABLE_CONTEXT_SECTIONS:
            continue
        if not section.get("executed"):
            continue
        if str(section.get("degraded_reason") or "").strip():
            continue
        satisfied.add(name)
    return True, satisfied


def _expected_context_bundle_sha(metadata_path: Path | None) -> str:
    if metadata_path is None or not metadata_path.is_file():
        return ""
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    context = payload.get("project_context")
    if not isinstance(context, dict):
        return ""
    value = str(context.get("bundle_sha256") or "").strip().lower()
    return value if len(value) == 64 else ""


def _parse_card(result: dict[str, Any], task_id: str) -> dict[str, Any]:
    if result.get("returncode") != 0:
        raise LaunchRejected(f"task_lookup_failed:{task_id}:{result.get('stderr', '')[:160]}")
    try:
        card = json.loads(result.get("stdout", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise LaunchRejected(f"task_lookup_invalid_json:{task_id}") from exc
    if card.get("task_id") != task_id:
        raise LaunchRejected("task_identity_mismatch")
    return card


def _validate_scope(repo: Path, card: dict[str, Any]) -> None:
    allowed = card.get("allowed_writes")
    if allowed is None:
        # The key is absent -> a genuinely under-specified card. This is
        # distinct from an intentionally-empty readonly list (see below).
        raise LaunchRejected("allowed_writes_missing")
    if not isinstance(allowed, list):
        raise LaunchRejected("allowed_writes_invalid")
    if not allowed and (card.get("required_outputs") or []):
        # An empty write scope is valid ONLY for a readonly / no-output card
        # (a canary/verification task that declares no required_outputs). A
        # card that declares required_outputs but no allowed_writes is a real
        # misconfiguration -- never accepted, and never via a NO_WRITES sentinel.
        raise LaunchRejected("allowed_writes_empty")
    root = repo.resolve()
    for raw in allowed:
        if not isinstance(raw, str) or not raw.strip():
            raise LaunchRejected("allowed_writes_invalid")
        normalized = raw.strip().replace("\\", "/")
        if normalized == ".git" or normalized.startswith(".git/"):
            raise LaunchRejected("git_metadata_write_forbidden")
        # Glob patterns are checked at their static prefix; taskctl performs
        # the final staged-path enforcement after the worker returns.
        prefix = raw.split("*", 1)[0].split("?", 1)[0]
        candidate = (root / prefix).resolve()
        if candidate != root and root not in candidate.parents:
            raise LaunchRejected(f"allowed_write_outside_repo:{raw}")


def _validate_required_outputs_contract(card: dict[str, Any]) -> None:
    raw = card.get("required_outputs")
    if raw is None:
        if card.get("allow_empty_required_outputs") is not None:
            raise LaunchRejected(
                "allow_empty_required_outputs_requires_required_outputs"
            )
        if card.get("allow_unchanged_required_outputs") is not None:
            raise LaunchRejected(
                "allow_unchanged_required_outputs_requires_required_outputs"
            )
        return
    if not isinstance(raw, list) or not raw:
        raise LaunchRejected("required_outputs_invalid")
    allowed = card.get("allowed_writes") or []
    if not isinstance(allowed, list):
        raise LaunchRejected("allowed_writes_invalid")
    for item in raw:
        if not isinstance(item, str) or not item.strip() or "\x00" in item:
            raise LaunchRejected("required_outputs_invalid")
    _validate_allow_empty_required_outputs(card, raw, allowed)
    _validate_allow_unchanged_required_outputs(card, raw, allowed)


def _validate_allow_empty_required_outputs(
    card: dict[str, Any],
    required_outputs: list[str],
    allowed_writes: list[str],
) -> None:
    allow_empty = card.get("allow_empty_required_outputs")
    if allow_empty is None:
        return
    if not isinstance(allow_empty, list) or not allow_empty:
        raise LaunchRejected("allow_empty_required_outputs_invalid")
    for path in allow_empty:
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise LaunchRejected("allow_empty_required_outputs_invalid")
        # Reject absolute paths, traversal, and glob characters.
        normalized = path.strip().replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise LaunchRejected("allow_empty_required_outputs_invalid")
        if any(ch in normalized for ch in "*?["):
            raise LaunchRejected("allow_empty_required_outputs_invalid")
        # Must be a subset of required_outputs (exact or fnmatch).
        if not any(
            normalized == req or fnmatch.fnmatchcase(normalized, req)
            for req in required_outputs
        ):
            raise LaunchRejected(
                f"allow_empty_not_in_required_outputs:{normalized}"
            )
        # Must also fnmatch at least one allowed_writes pattern.
        if not any(
            fnmatch.fnmatchcase(normalized, aw)
            for aw in allowed_writes
        ):
            raise LaunchRejected(
                f"allow_empty_not_in_allowed_writes:{normalized}"
            )


def _validate_allow_unchanged_required_outputs(
    card: dict[str, Any],
    required_outputs: list[str],
    allowed_writes: list[str],
) -> None:
    allow_unchanged = card.get("allow_unchanged_required_outputs")
    if allow_unchanged is None:
        return
    if not isinstance(allow_unchanged, list) or not allow_unchanged:
        raise LaunchRejected("allow_unchanged_required_outputs_invalid")
    for path in allow_unchanged:
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise LaunchRejected("allow_unchanged_required_outputs_invalid")
        normalized = path.strip().replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise LaunchRejected("allow_unchanged_required_outputs_invalid")
        if any(ch in normalized for ch in "*?["):
            raise LaunchRejected("allow_unchanged_required_outputs_invalid")
        if normalized not in required_outputs:
            raise LaunchRejected(
                f"allow_unchanged_not_in_required_outputs:{normalized}"
            )
        if normalized not in allowed_writes:
            raise LaunchRejected(
                f"allow_unchanged_not_in_allowed_writes:{normalized}"
            )


def _external_readonly_dirs(
    card: dict[str, Any], adapter_id: str
) -> list[str]:
    """Validate optional card-declared external inputs and return Copilot dirs.

    The outer task sandbox remains the write authority.  This list only grants
    Copilot permission to *read* a bounded, pre-existing directory beneath the
    coordinator's static data roots; traversal and symlink escapes fail closed.
    """
    raw_sources = card.get("external_readonly_sources")
    if raw_sources is None:
        return []
    if adapter_id not in {
        runtime_adapters.DEEPSEEK_COPILOT_ADAPTER,
        runtime_adapters.GLM_COPILOT_ADAPTER,
    }:
        raise LaunchRejected(
            "external_readonly_sources_requires_deepseek_copilot_cli"
        )
    if not isinstance(raw_sources, list) or not raw_sources:
        raise LaunchRejected("external_readonly_sources_invalid")

    roots: list[Path] = []
    for raw_root in EXTERNAL_READONLY_ROOTS:
        try:
            root = raw_root.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise LaunchRejected("external_readonly_root_unavailable") from exc
        if not root.is_dir():
            raise LaunchRejected("external_readonly_root_not_directory")
        roots.append(root)

    directories: list[Path] = []
    for raw in raw_sources:
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise LaunchRejected("external_readonly_source_invalid")
        candidate = Path(raw.strip())
        if not candidate.is_absolute():
            raise LaunchRejected("external_readonly_source_not_absolute")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise LaunchRejected(
                f"external_readonly_source_unavailable:{raw}"
            ) from exc
        if not any(resolved == root or root in resolved.parents for root in roots):
            raise LaunchRejected(f"external_readonly_source_outside_roots:{raw}")
        if not (resolved.is_file() or resolved.is_dir()):
            raise LaunchRejected(f"external_readonly_source_not_file_or_dir:{raw}")
        directory = resolved if resolved.is_dir() else resolved.parent
        directories.append(directory)

    # Grant the smallest covering set.  A declared file necessarily requires
    # Copilot's directory-granular --add-dir permission; nested dirs become
    # redundant when their already-declared parent is present.
    result: list[Path] = []
    for directory in sorted(set(directories), key=lambda p: (len(p.parts), str(p))):
        if any(directory == parent or parent in directory.parents for parent in result):
            continue
        result = [child for child in result if directory not in child.parents]
        result.append(directory)
    return [str(path) for path in result]


def _validate_adapter_identity(runner: str, adapter_id: str) -> None:
    if runner.startswith("claude_"):
        allowed: tuple[str, ...] = ("claude_cli",)
    elif runner.startswith("codex_"):
        allowed = ("codex_cli",)
    elif runner.startswith("deepseek_"):
        # deepseek_* runners launch locally via the Copilot BYOK adapter;
        # deepseek_manual stays an explicit fallback the coordinator may pick.
        # Never a GitHub-hosted Claude/GPT adapter for a DeepSeek-labeled task.
        allowed = ("deepseek_copilot_cli", "deepseek_manual")
    elif runner.startswith("glm_"):
        # Prefer the credential-free VS Code Language Model API bridge.  Keep
        # the explicit BYOK adapter as a backwards-compatible fallback.
        allowed = ("glm_vscode_lm", "glm_copilot_cli")
    else:
        return
    if adapter_id not in allowed:
        raise LaunchRejected(
            f"runner_adapter_mismatch:runner={runner}:expected={'|'.join(allowed)}:got={adapter_id}"
        )


def _worker_mcp_bundle_payload(
    context_result: project_context.ProjectContextResult | None,
) -> dict[str, Any]:
    """Best-effort parse of the same bundle JSON already sent to the worker.

    Never raises: a malformed/absent bundle degrades to an empty dict, which
    the two helpers below turn into safe defaults (no target allowlist, the
    task's own topic for Session Manager).
    """
    if context_result is None or not context_result.prompt_bundle.strip():
        return {}
    try:
        return json.loads(context_result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    except (IndexError, TypeError, json.JSONDecodeError):
        return {}


def _worker_mcp_source_graph_targets(
    context_result: project_context.ProjectContextResult | None,
) -> list[str]:
    if context_result is not None:
        targets = getattr(context_result, "worker_source_graph_targets", ())
        if isinstance(targets, (list, tuple)) and targets:
            return [str(t) for t in targets]
    payload = _worker_mcp_bundle_payload(context_result)
    targets = (payload.get("source_graph") or {}).get("targets")
    return [str(t) for t in targets] if isinstance(targets, list) else []


def _worker_mcp_session_topic(
    context_result: project_context.ProjectContextResult | None,
    fallback_topic: str,
) -> str:
    if context_result is not None:
        topic = getattr(context_result, "worker_session_topic", "")
        if isinstance(topic, str) and topic.strip():
            return topic
    payload = _worker_mcp_bundle_payload(context_result)
    topic = (payload.get("session") or {}).get("topic")
    return str(topic) if isinstance(topic, str) and topic.strip() else fallback_topic


def _changed_path_hashes(
    workspace: WorkerWorkspace, changed: list[str]
) -> dict[str, str | None]:
    """Bounded sha256 evidence for each declared-changed path, read from the
    isolated workspace only -- never the canonical repo. ``None`` records a
    declared deletion (the path no longer exists in the workspace)."""
    hashes: dict[str, str | None] = {}
    for relative in changed:
        source = workspace.path / relative
        if source.is_symlink() or not source.is_file():
            hashes[relative] = None
            continue
        hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    return hashes


def _path_manifest(base: Path, declared: list[str]) -> dict[str, dict[str, Any]]:
    """Bounded, deterministic manifest for declared repo-relative paths.

    Used to detect canonical input/dependency drift between the review
    evidence captured at claim time (``base`` == the canonical repo, read
    just before ``task_engine.claim_start_exact``) and the state read again
    immediately before promotion in ``ProcessManager.accept_review`` (B919,
    closing the B914 race: a retained-worktree validation had passed against
    a 29-row dependency snapshot while the canonical dependency had already
    advanced to 3,522 rows by promotion time). A directory entry never
    content-hashes its children -- only ``entry_count`` plus a
    ``listing_sha256`` over each immediate child's name/size -- so cost stays
    proportional to the declared path count, never a broad repository walk.
    """
    try:
        base_resolved = base.resolve()
    except OSError:
        base_resolved = base
    manifest: dict[str, dict[str, Any]] = {}
    for relative in declared:
        relative = str(relative)
        target = base / relative
        if target.is_symlink():
            manifest[relative] = {"kind": "missing"}
            continue
        try:
            resolved = target.resolve()
        except OSError:
            manifest[relative] = {"kind": "missing"}
            continue
        if resolved != base_resolved and base_resolved not in resolved.parents:
            manifest[relative] = {"kind": "missing"}
            continue
        if resolved.is_dir():
            try:
                names = sorted(p.name for p in resolved.iterdir())
            except OSError:
                manifest[relative] = {"kind": "missing"}
                continue
            digest = hashlib.sha256()
            for name in names:
                child = resolved / name
                try:
                    size = child.stat().st_size if child.is_file() else -1
                except OSError:
                    size = -1
                digest.update(f"{name}:{size}\n".encode("utf-8"))
            manifest[relative] = {
                "kind": "dir",
                "entry_count": len(names),
                "listing_sha256": digest.hexdigest(),
            }
        elif resolved.is_file():
            try:
                data = resolved.read_bytes()
            except OSError:
                manifest[relative] = {"kind": "missing"}
                continue
            manifest[relative] = {
                "kind": "file",
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "line_count": data.count(b"\n"),
            }
        else:
            manifest[relative] = {"kind": "missing"}
    return manifest


def _task_authority_repo(repo: Path, card: dict[str, Any]) -> Path:
    resolver = getattr(project_context, "resolve_task_repository_root", None)
    if resolver is None:
        return repo.resolve()
    return resolver(repo, card)


def _provision_worker_mcp_runtime_for_authority(
    workspace: WorkerWorkspace,
    *,
    request_id: str,
    task_id: str,
    runner: str,
    topic: str,
    backend: str,
    authority_repo: Path,
    source_graph_targets: list[str],
    session_topic: str,
) -> worker_ai_tools_mcp.WorkerMcpRuntime:
    kwargs: dict[str, Any] = {
        "request_id": request_id,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "backend": backend,
        "source_graph_targets": source_graph_targets,
        "session_topic": session_topic,
    }
    try:
        signature = inspect.signature(provision_worker_mcp_runtime)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and "authority_repo" in signature.parameters:
        kwargs["authority_repo"] = authority_repo
        return provision_worker_mcp_runtime(workspace, **kwargs)
    try:
        workspace = replace(workspace, repo=authority_repo)
    except TypeError:
        pass
    return provision_worker_mcp_runtime(workspace, **kwargs)


def _worker_mcp_live_call_gate(metadata: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Bounded, redacted B833 completion-gate summary.

    Gated only for ``task_context_policy.task_type == "code"`` (a
    ``project_context`` contract must be present to reach that value at all).
    Data-classification and research tasks -- and any task without a
    ``project_context`` contract -- are exempt and never blocked here; they
    keep whatever policy already applied to them (e.g. the immutable input
    shard for data tasks). Fails CLOSED on a gated task: a missing worker_mcp
    runtime record, an unreadable ledger, or zero verified live
    ``source_graph`` calls all resolve to ``satisfied: False``. Source Graph
    must be fresh and non-empty; Session Manager and requested Memory/KB
    sections must have a successful canonical call. A tampered or
    forged ledger line is dropped by ``verify_audit_ledger`` before it ever
    reaches this count, so a worker cannot satisfy the gate by writing text
    that merely looks like an audit entry.
    """
    task_type = str(
        ((metadata.get("project_context") or {}).get("task_context_policy") or {}).get("task_type") or ""
    )
    worker_mcp_meta = metadata.get("worker_mcp") or {}
    gated = task_type == "code"
    sections = (metadata.get("project_context") or {}).get("sections") or []
    required_tools = ["source_graph"]
    for section in sections:
        if not isinstance(section, dict) or not section.get("requested", True):
            continue
        name = str(section.get("name") or "")
        if name in {"session_current_state", "ai_memory", "kb"} and name not in required_tools:
            required_tools.append(name)
    result: dict[str, Any] = {
        "gated": gated,
        "task_type": task_type,
        "required_tools": required_tools if gated else [],
        "missing_tools": [],
        "satisfied": True,
        "reason": "",
        "satisfaction_by_tool": {},
        "injected_context_acknowledged": False,
    }
    if not gated:
        return result
    ledger_path = worker_mcp_meta.get("audit_ledger_path")
    key_path = worker_mcp_meta.get("audit_hmac_key_path")
    if not ledger_path or not key_path:
        result["satisfied"] = False
        result["reason"] = "worker_mcp_runtime_not_provisioned"
        return result
    verification = worker_ai_tools_mcp.verify_audit_ledger(
        Path(str(ledger_path)),
        Path(str(key_path)),
        task_id=str(metadata["task_id"]),
        runner=str(metadata["runner"]),
        topic=str(metadata["topic"]),
        request_id=request_id,
    )
    # Bounded/redacted by construction: verify_audit_ledger never returns raw
    # paths, prompts, or database contents -- only counts and a short reason.
    result["verification"] = {k: v for k, v in verification.items() if k != "schema_id"}
    successful = verification.get("successful_call_count_by_tool") or {}
    # A verified-injected project-context section is an alternative, equally
    # valid satisfaction source to a live worker call for the SAME required
    # tool (B948/B951): the launcher already ran the tool and injected its
    # hash-receipted result, so correct work is no longer failed just because
    # the worker did not additionally re-run it by hand. Fail-closed integrity
    # (tampered / repo-mismatched / unacknowledged receipt, degraded section)
    # is preserved inside _injected_context_satisfaction.
    injected_acknowledged, injected_tools = _injected_context_satisfaction(metadata)
    result["injected_context_acknowledged"] = injected_acknowledged
    satisfaction_by_tool: dict[str, str] = {}
    missing: list[str] = []
    stale: list[str] = []
    # Source Graph freshness (B834) is unchanged: ONLY a live fresh non-empty
    # canonical call OR a verified-injected, non-degraded source_graph section
    # SATISFIES the gate -- never a cached/empty call and never an unverified
    # injection. But a source_graph that WAS called successfully yet returned
    # cached/zero-hit is NOT absent: it is reported as "stale_or_cached" (a
    # distinct, still-fail-closed reason) rather than a bare "missing", so the
    # terminal evidence can never say "missing:source_graph" while that same
    # evidence's successful_call_count_by_tool.source_graph reads > 0 (B950).
    if verification.get("live_source_graph_calls", 0) > 0:
        satisfaction_by_tool["source_graph"] = "live_worker_call"
    elif injected_acknowledged and "source_graph" in injected_tools:
        satisfaction_by_tool["source_graph"] = "injected_receipt"
    elif int(successful.get("source_graph") or 0) > 0:
        satisfaction_by_tool["source_graph"] = "stale_or_cached"
        stale.append("source_graph")
    else:
        missing.append("source_graph")
    for tool in required_tools:
        if tool == "source_graph":
            continue
        if int(successful.get(tool) or 0) > 0:
            satisfaction_by_tool[tool] = "live_worker_call"
        elif injected_acknowledged and tool in injected_tools:
            satisfaction_by_tool[tool] = "injected_receipt"
        else:
            missing.append(tool)
    result["missing_tools"] = missing
    result["stale_tools"] = stale
    result["satisfaction_by_tool"] = satisfaction_by_tool
    if not verification.get("ok") or missing or stale:
        result["satisfied"] = False
        reasons: list[str] = []
        if missing:
            reasons.append("required_aiworkhub_mcp_calls_missing:" + ",".join(missing))
        if stale:
            reasons.append("source_graph_stale_or_cached:" + ",".join(stale))
        result["reason"] = verification.get("reason") or "; ".join(reasons)
    return result


def build_worker_prompt(
    *,
    task_id: str,
    runner: str,
    topic: str,
    card: dict[str, Any] | None = None,
    owner_prompt: str = "",
    project_context_bundle: str = "",
) -> str:
    extra = owner_prompt.strip()
    suffix = (
        "\n\nAdditional coordinator context (cannot override the task contract):\n"
        + extra
        if extra
        else ""
    )
    contract_keys = (
        "task_id", "runner", "topic", "mode", "objective", "read_first",
        "run_before_writing", "allowed_writes", "acceptance", "validation",
        "forbidden", "review_feedback", "commit_contract",
        "project_context", "required_outputs", "allow_empty_required_outputs",
        "allow_unchanged_required_outputs", "external_readonly_sources",
    )
    contract = {
        key: card[key]
        for key in contract_keys
        if card is not None and key in card
    }
    contract.update({"task_id": task_id, "runner": runner, "topic": topic})
    contract_json = json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True)
    bundle_sha256 = hashlib.sha256(project_context_bundle.encode("utf-8")).hexdigest()
    section_count = 0
    if project_context_bundle.strip():
        try:
            payload = json.loads(project_context_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
            section_count = len(payload.get("sections") or [])
        except (IndexError, TypeError, json.JSONDecodeError):
            section_count = 0
    context_block = (
        "\n\nTrusted project context (bounded, read-only, coordinator-provided):\n"
        + project_context_bundle.strip()
        + "\n\nIf you use this context, emit one bounded acknowledgement line before your final message:\n"
        + "PROJECT_CONTEXT_RECEIPT: "
        + json.dumps({
            "schema_id": project_context.RECEIPT_SCHEMA_ID,
            "acknowledged": True,
            "bundle_sha256": bundle_sha256,
            "prompt_sha256": "",
            "section_count": section_count,
        }, sort_keys=True)
        if project_context_bundle.strip()
        else ""
    )
    return f"""You are the sole worker for one exact AIWorkHub task in an isolated worktree.

The coordinator already claimed the task. Do not run taskctl lifecycle commands,
do not commit, and do not modify .git. Work only on the contract below. The
coordinator will independently enforce allowed_writes, rerun validation, promote
accepted files, and request review after your process exits successfully.

TASK_CONTRACT_JSON:
{contract_json}

Read every read_first path before editing. Create the required evidence and run
the listed validation commands. Never use git add -A or git add . and never
touch paths outside allowed_writes.

MANDATORY_AIWORKHUB_TOOLS:
- For code discovery call aiworkhub_worker_source_graph_query first. Raw Grep,
  Glob, grep, rg, find and tree discovery are provider-blocked.
- Call aiworkhub_worker_session_current_state for continuity. If the contract
  requests AI Memory or KB, call their injected worker tools once with one
  bounded task-specific query.
- The coordinator verifies an HMAC-authenticated MCP audit ledger and rejects
  completion when a required call is missing. Text claims cannot satisfy it.
- If Source Graph reports an exact target unsupported/unindexed, stop and
  report that target. Only a new coordinator-authorized fallback card may use
  raw discovery for it.
{context_block} Your final message must be at most 12 lines
and name tests plus changed paths.{suffix}"""


@dataclass
class _LiveProcess:
    request_id: str
    task_id: str
    runner: str
    topic: str
    adapter_id: str
    model: str | None
    process: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path
    started_at: str
    timeout_seconds: int
    isolated: bool = False
    metadata_path: Path | None = None
    supervisor_status_path: Path | None = None
    pid_start_ticks: int | None = None


class ProcessManager:
    """Thread-safe local process registry with append-only lifecycle events."""

    def __init__(
        self,
        *,
        repo: Path | None = None,
        process_log_path: Path | None = None,
        process_dir: Path | None = None,
        show_task: Callable[[str], dict[str, Any]] | None = None,
        collision_guard: Callable[..., dict[str, Any]] | None = None,
        adapter_builder: Callable[..., Any] | None = None,
        popen_factory: Callable[..., subprocess.Popen[bytes]] | None = None,
        isolation_enabled: bool = True,
    ) -> None:
        self.repo = (repo or core.repo_root()).resolve()
        self.process_log_path = process_log_path or Path(
            os.environ.get(
                PROCESS_LOG_ENV,
                str(self.repo / PROCESS_LOG_DEFAULT_REL),
            )
        )
        self.process_dir = process_dir or Path(
            os.environ.get(
                PROCESS_DIR_ENV,
                str(self.repo / PROCESS_DIR_DEFAULT_REL),
            )
        )
        self._show_task = show_task or self._default_show_task
        self._collision_guard = collision_guard or core.collision_guard
        self._adapter_builder = adapter_builder
        self._popen = popen_factory or subprocess.Popen
        self.isolation_enabled = isolation_enabled
        self._lock = threading.RLock()
        self._live: dict[str, _LiveProcess] = {}
        self._cancelled: set[str] = set()
        self._watching: set[str] = set()
        self._authority_key: bytes | None = None
        if self.isolation_enabled and self.process_log_path.is_file():
            self._reconcile_persisted_requests()

    def _default_show_task(self, task_id: str) -> dict[str, Any]:
        """The sole claim/finalization authority: ``self.repo`` -- the exact
        repository this ProcessManager (and every isolated workspace it
        launched) is bound to -- never an independently, ambiently
        re-resolved repository (``core.show_task`` -> ``core.repo_root()``).
        Every internal caller of ``self._show_task`` (preflight, exact-claim-
        state, GC eligibility, status) goes through this one binding, so the
        launcher and the finalizer can never disagree about which
        ``.aiworkhub/tasking/task_queue.sqlite`` is canonical for this
        worker's workspace."""
        return task_engine.show_task(self.repo, task_id)

    def _load_dependency_card(self, dep_id: str) -> dict[str, Any]:
        """Best-effort read of an arbitrary dependency card by task_id alone.

        Unlike ``_preflight_card`` this never raises and never checks
        runner/topic/lifecycle: a missing, archived, or malformed dependency
        simply yields ``{}`` so input enrichment degrades to "add nothing".
        Goes through the same repo-bound ``self._show_task`` binding so it can
        never disagree about which task store is canonical for this launcher.
        """
        try:
            envelope = self._show_task(dep_id)
        except Exception:  # noqa: BLE001 -- a dep lookup must never break a launch
            return {}
        if not isinstance(envelope, dict) or envelope.get("returncode") not in (0, None):
            return {}
        stdout = envelope.get("stdout")
        if isinstance(stdout, dict):
            card = stdout
        elif isinstance(stdout, str) and stdout.strip():
            try:
                card = json.loads(stdout)
            except json.JSONDecodeError:
                return {}
        else:
            return {}
        return card if isinstance(card, dict) else {}

    def _with_dependency_inputs(self, card: dict[str, Any]) -> dict[str, Any]:
        """Materialize a dependent's ``depends_on`` outputs into its worktree.

        A completed dependency's artifacts are promoted into the canonical
        working tree UNCOMMITTED (``worker_workspace.promote`` -- no git add /
        commit), and ``create_workspace`` seeds a new worktree from git
        ``HEAD`` and then overlays only DECLARED paths from that canonical
        working tree. So a dependency's outputs are invisible to a dependent
        unless the dependent declares them -- the measured defect where a
        promoted-but-uncommitted dependency artifact never reached a dependent's
        isolated worktree (a completed B948 output unseen by B951).

        This returns a copy of ``card`` whose ``immutable_inputs`` is extended
        with each ``depends_on`` dependency's declared write scope
        (``allowed_writes`` plus any ``required_outputs``), so both the seed
        copy in ``create_workspace`` and the B919 input-drift manifest cover
        them. Paths the dependent already declares (its own ``immutable_inputs``
        or ``allowed_writes``) are not re-added. Not-yet-produced paths are
        harmless: ``_copy_one`` silently skips a missing source. The added set
        is recorded under ``dependency_materialized_inputs`` for audit.
        """
        deps = card.get("depends_on")
        if not isinstance(deps, list) or not deps:
            return card
        existing = [str(p) for p in (card.get("immutable_inputs") or [])]
        own_writes = {str(p).strip() for p in (card.get("allowed_writes") or [])}
        have = set(existing)
        added: list[str] = []
        for raw_dep in deps:
            dep_id = str(raw_dep or "").strip()
            if not dep_id:
                continue
            dep_card = self._load_dependency_card(dep_id)
            for key in ("allowed_writes", "required_outputs"):
                values = dep_card.get(key)
                if not isinstance(values, list):
                    continue
                for raw in values:
                    pattern = str(raw or "").strip()
                    if pattern and pattern not in have and pattern not in own_writes:
                        have.add(pattern)
                        added.append(pattern)
        if not added:
            return card
        enriched = dict(card)
        enriched["immutable_inputs"] = existing + added
        enriched["dependency_materialized_inputs"] = added
        return enriched

    @contextmanager
    def _registry_lock(self):
        """Serialize duplicate-check + spawn across MCP server processes."""
        lock_path = Path(f"{self.process_log_path}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_APPEND | os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        clean = {
            "schema_id": "aiworkhub.task_mcp.process_event.v1",
            "timestamp": _utcnow(),
            **event,
        }
        self.process_log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(clean, ensure_ascii=False, sort_keys=True) + "\n"
        # One append write keeps records intact across concurrently monitoring
        # threads. O_APPEND is used deliberately; the file is never rewritten.
        fd = os.open(
            self.process_log_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        return clean

    def _events(self) -> list[dict[str, Any]]:
        if not self.process_log_path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.process_log_path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def _latest_by_request(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for event in self._events():
            request_id = str(event.get("request_id") or "")
            if request_id:
                latest[request_id] = event
        return latest

    def _build_adapter(self, **kwargs: Any) -> Any:
        if self._adapter_builder is not None:
            return self._adapter_builder(**kwargs)
        from .runtime_adapters import build_adapter_command

        return build_adapter_command(**kwargs)

    def _resolve_provider_env(
        self, adapter_id: str, model: str | None
    ) -> tuple[dict[str, str] | None, str | None]:
        """Resolve the BYOK provider env + effective model before claim-start.

        For Copilot BYOK adapters this loads the coordinator-only provider
        credential and builds the minimum provider environment (the API key
        enters ONLY the child env as ``COPILOT_PROVIDER_API_KEY``). A missing
        or invalid credential, or an unsupported model, raises
        ``LaunchRejected`` -- and because this runs BEFORE
        ``core.claim_start_exact``, the task is left pending/unclaimed.
        Non-BYOK adapters return ``(None, model)`` unchanged.
        """
        if adapter_id == runtime_adapters.DEEPSEEK_COPILOT_ADAPTER:
            resolved_model, model_error = runtime_adapters.resolve_deepseek_model(model)
            if model_error:
                raise LaunchRejected(f"deepseek_model_rejected:{model_error}")
            assert resolved_model is not None
            if deepseek_credentials is None:
                raise LaunchRejected("deepseek_credential_missing:helper_unavailable")
            try:
                credential = deepseek_credentials.load_credential(repo=self.repo)
            except deepseek_credentials.CredentialError as exc:
                raise LaunchRejected(f"deepseek_credential_missing:{exc.reason}") from exc
            return credential.provider_env(resolved_model), resolved_model
        if adapter_id == runtime_adapters.GLM_COPILOT_ADAPTER:
            resolved_model, model_error = runtime_adapters.resolve_glm_model(model)
            if model_error:
                raise LaunchRejected(f"glm_model_rejected:{model_error}")
            assert resolved_model is not None
            if glm_credentials is None:
                raise LaunchRejected("glm_credential_missing:helper_unavailable")
            try:
                credential = glm_credentials.load_credential(repo=self.repo)
            except glm_credentials.CredentialError as exc:
                raise LaunchRejected(f"glm_credential_missing:{exc.reason}") from exc
            return credential.provider_env(resolved_model), resolved_model
        if adapter_id == runtime_adapters.GLM_VSCODE_LM_ADAPTER:
            resolved_model, model_error = runtime_adapters.resolve_glm_model(model)
            if model_error:
                raise LaunchRejected(f"glm_model_rejected:{model_error}")
            assert resolved_model is not None
            readiness = vscode_lm_bridge.bridge_readiness(
                self.repo, model=resolved_model
            )
            if not readiness.get("launchable"):
                raise LaunchRejected(
                    "glm_vscode_lm_unavailable:"
                    + str(readiness.get("blocker_reason") or "not_launchable")
                )
            return None, resolved_model
        else:
            return None, model

    def _preflight_card(self, task_id: str, runner: str, topic: str) -> dict[str, Any]:
        # B314_F006 (reviewed, accepted-by-design): there is a TOCTOU window
        # between this preflight read and the atomic core.claim_start_exact()
        # call made later in _launch_isolated -- a second MCP server process
        # could observe the same "pending" card here. That race can only
        # ever produce a clean rejection, never a double-claim: taskctl's
        # claim-start uses its own fcntl-serialized atomic guard
        # (AITools/taskctl.py::_task_queue_lock), so the loser's later
        # claim_start_exact() call simply fails and this launch is rejected
        # (see the except branch below, which releases/cleans up on any
        # LaunchRejected). The cost of losing the race is a wasted workspace
        # creation + git worktree add, not a safety violation, so this is
        # accepted as a bounded inefficiency rather than restructured to move
        # the claim before workspace creation (which would then require
        # releasing a successful claim on a later workspace-creation
        # failure -- a strictly worse trade for a correctness-neutral race).
        for label, value in (("task_id", task_id), ("runner", runner), ("topic", topic)):
            reason = core._is_malformed_identity_token(value)
            if reason:
                raise LaunchRejected(f"malformed_{label}:{reason}")
        card = _parse_card(self._show_task(task_id), task_id)
        if card.get("runner") != runner:
            raise LaunchRejected(f"runner_mismatch:{card.get('runner')}")
        if card.get("topic") != topic:
            raise LaunchRejected(f"topic_mismatch:{card.get('topic')}")
        if core._lifecycle_state(card) != "pending":
            raise LaunchRejected(f"task_not_pending:{core._lifecycle_state(card)}")
        if str(card.get("worker_status") or "unclaimed") != "unclaimed":
            raise LaunchRejected(f"task_not_unclaimed:{card.get('worker_status')}")
        if card.get("claimed_by"):
            raise LaunchRejected(f"task_already_claimed:{card.get('claimed_by')}")
        _validate_scope(self.repo, card)
        _validate_required_outputs_contract(card)
        collision = self._collision_guard(print_json=True)
        if collision.get("returncode") != 0:
            raise LaunchRejected("collision_guard_failed")
        return card

    def _active_request_ids(self) -> set[str]:
        dead = [rid for rid, live in self._live.items() if live.process.poll() is not None]
        for rid in dead:
            self._live.pop(rid, None)
        active = {
            rid for rid, live in self._live.items() if live.process.poll() is None
        }
        for request_id, event in self._latest_by_request().items():
            if event.get("state") not in ACTIVE_PROCESS_STATES:
                continue
            pid = int(event.get("pid") or 0)
            ticks = event.get("pid_start_ticks")
            if pid and (
                (not event.get("metadata_path") and _pid_matches(pid, ticks))
                or (ticks not in (None, "") and _pid_matches(pid, ticks))
            ):
                active.add(request_id)
        return active

    def _active_count(self) -> int:
        return len(self._active_request_ids())

    def launch(
        self,
        *,
        task_id: str,
        runner: str,
        topic: str,
        adapter_id: str,
        model: str | None = None,
        owner_prompt: str = "",
        timeout_seconds: int = 7200,
    ) -> dict[str, Any]:
        if self.isolation_enabled:
            return self._launch_isolated(
                task_id=task_id,
                runner=runner,
                topic=topic,
                adapter_id=adapter_id,
                model=model,
                owner_prompt=owner_prompt,
                timeout_seconds=timeout_seconds,
            )
        return self._launch_direct_for_tests(
            task_id=task_id,
            runner=runner,
            topic=topic,
            adapter_id=adapter_id,
            model=model,
            owner_prompt=owner_prompt,
            timeout_seconds=timeout_seconds,
        )

    def _launch_isolated(
        self,
        *,
        task_id: str,
        runner: str,
        topic: str,
        adapter_id: str,
        model: str | None,
        owner_prompt: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        if not launch_gates_open():
            return self._blocked(
                task_id, runner, topic, adapter_id,
                "dual_gate_closed: require AIWORKHUB_ALLOW_LAUNCH=1 and AIWORKHUB_ALLOW_WRITES=1",
            )
        if timeout_seconds < 30 or timeout_seconds > 86_400:
            return self._blocked(task_id, runner, topic, adapter_id, "timeout_out_of_range")

        request_id: str | None = None
        workspace: WorkerWorkspace | None = None
        spec_path: Path | None = None
        authority_path: Path | None = None
        bridge_request: vscode_lm_bridge.BridgeRequest | None = None
        claimed = False
        provider_env: dict[str, str] | None = None
        try:
            _validate_adapter_identity(runner, adapter_id)
            # Materialize completed dependencies' promoted (accepted-but-not-yet-
            # committed) outputs into this dependent's isolated worktree by
            # declaring them as immutable inputs before create_workspace and the
            # B919 input-drift snapshot see the card.
            card = self._with_dependency_inputs(self._preflight_card(task_id, runner, topic))
            external_readonly_dirs = _external_readonly_dirs(card, adapter_id)
            authority_repo = _task_authority_repo(self.repo, card)
            context_result = project_context.collect_project_context(self.repo, card)
            # Load the BYOK credential (deepseek_copilot_cli) BEFORE claim-start.
            # A missing/invalid credential raises here, leaving the task
            # pending/unclaimed -- never claim on a missing credential.
            provider_env, model = self._resolve_provider_env(adapter_id, model)
            sandbox_backend = select_sandbox_backend()
            with self._lock, self._registry_lock():
                if self._active_count() >= _configured_limit():
                    raise LaunchRejected("concurrency_limit_reached")
                self._assert_no_duplicate_task(task_id)

                request_id = uuid.uuid4().hex
                self.process_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(self.process_dir, 0o700)
                stdout_path = self.process_dir / f"{request_id}.stdout.log"
                stderr_path = self.process_dir / f"{request_id}.stderr.log"
                status_path = self.process_dir / f"{request_id}.supervisor.json"
                cancel_path = self.process_dir / f"{request_id}.cancel.json"
                metadata_path = self.process_dir / f"{request_id}.request.json"
                spec_path = self.process_dir / f"{request_id}.supervisor-spec.json"
                _touch_0600(stdout_path)
                _touch_0600(stderr_path)

                self._append_event({
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": topic,
                    "adapter_id": adapter_id,
                    "model": model,
                    "state": "starting",
                    "timeout_seconds": timeout_seconds,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "metadata_path": str(metadata_path),
                    "authority": f"coordinator_claim_isolated_worktree_{sandbox_backend}",
                    "sandbox_backend": sandbox_backend,
                    "project_context": (
                        context_result.metadata if context_result is not None else None
                    ),
                })

                workspace = create_workspace(self.repo, request_id, card, adapter_id)
                worker_mcp_runtime = _provision_worker_mcp_runtime_for_authority(
                    workspace,
                    request_id=request_id,
                    task_id=task_id,
                    runner=runner,
                    topic=topic,
                    backend=sandbox_backend,
                    authority_repo=authority_repo,
                    source_graph_targets=_worker_mcp_source_graph_targets(context_result),
                    session_topic=_worker_mcp_session_topic(context_result, topic),
                )
                prompt = build_worker_prompt(
                    task_id=task_id,
                    runner=runner,
                    topic=topic,
                    card=card,
                    owner_prompt=owner_prompt,
                    project_context_bundle=(
                        context_result.prompt_bundle if context_result is not None else ""
                    ),
                )
                if adapter_id == runtime_adapters.GLM_VSCODE_LM_ADAPTER:
                    bridge_request = vscode_lm_bridge.create_request(
                        repo=self.repo,
                        request_id=request_id,
                        workspace_path=workspace.path,
                        workspace_home=workspace.home,
                        prompt=prompt,
                        model=str(model or runtime_adapters.GLM_DEFAULT_MODEL),
                        allowed_writes=workspace.allowed_writes,
                        timeout_seconds=timeout_seconds,
                    )
                    plan = runtime_adapters.RuntimeAdapterPlan(
                        adapter_id=adapter_id,
                        argv=[sys.executable, "-m", "aiworkhub.vscode_lm_worker"],
                        cwd=str(workspace.path),
                        executable=sys.executable,
                        launchable=True,
                        manual_only=False,
                        validation_ok=True,
                        validation_reason="",
                    )
                else:
                    plan = self._build_adapter(
                        adapter_id=adapter_id,
                        prompt=prompt,
                        repo=workspace.path,
                        model=model,
                        outer_sandbox_backend=sandbox_backend,
                        additional_readonly_dirs=external_readonly_dirs,
                    )
                if not getattr(plan, "launchable", False):
                    reason = getattr(plan, "reason", "adapter_not_launchable")
                    raise LaunchRejected(reason or "adapter_not_launchable")
                if isinstance(plan, runtime_adapters.RuntimeAdapterPlan):
                    worker_mcp_config_path = {
                        "claude_cli": worker_mcp_runtime.claude_mcp_config_path,
                        runtime_adapters.DEEPSEEK_COPILOT_ADAPTER: worker_mcp_runtime.copilot_mcp_config_path,
                        runtime_adapters.GLM_COPILOT_ADAPTER: worker_mcp_runtime.copilot_mcp_config_path,
                    }.get(adapter_id)
                    if worker_mcp_config_path is not None:
                        plan = runtime_adapters.inject_worker_mcp_config(plan, worker_mcp_config_path)
                worker_argv = sandbox_argv(
                    workspace,
                    adapter_id,
                    list(plan.argv),
                    backend=sandbox_backend,
                    package_import_root=worker_ai_tools_mcp.resolve_host_package_import_root(),
                )

                # B919: snapshot every declared immutable/dependency input from
                # the canonical repo *before* claim_start_exact, while it is
                # still exactly the input state this launch will validate
                # against. accept_review re-reads the same declared paths
                # from the canonical repo immediately before promotion and
                # fails closed on any drift (B914).
                declared_immutable_inputs = [
                    str(p) for p in (card.get("immutable_inputs") or [])
                ]
                immutable_input_manifest = _path_manifest(
                    self.repo, declared_immutable_inputs
                )

                claim = task_engine.claim_start_exact(
                    self.repo, task_id, runner, topic, request_id=request_id
                )
                if not claim.get("ok"):
                    raise LaunchRejected(
                        "claim_start_failed:" + str(claim.get("stderr") or claim.get("stdout") or "")[:300]
                    )
                claimed = True

                prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                context_delivery = _project_context_delivery(context_result, prompt_hash)
                metadata = {
                    "schema_id": "aiworkhub.task_mcp.isolated_request.v1",
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": topic,
                    "adapter_id": adapter_id,
                    "model": model,
                    "timeout_seconds": timeout_seconds,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "supervisor_status_path": str(status_path),
                    "cancel_path": str(cancel_path),
                    "metadata_path": str(metadata_path),
                    "prompt_sha256": prompt_hash,
                    "project_context": (
                        context_result.metadata if context_result is not None else None
                    ),
                    "project_context_delivery": context_delivery,
                    "worker_mcp": {
                        "schema_id": worker_ai_tools_mcp.RUNTIME_SCHEMA_ID,
                        "server_name": worker_mcp_runtime.server_name,
                        "tool_names": list(worker_mcp_runtime.tool_names),
                        "audit_ledger_path": str(worker_mcp_runtime.audit_ledger_path),
                        "audit_hmac_key_path": str(worker_mcp_runtime.audit_hmac_key_path),
                        "claude_mcp_config_path": str(worker_mcp_runtime.claude_mcp_config_path),
                        "copilot_mcp_config_path": str(worker_mcp_runtime.copilot_mcp_config_path),
                        "codex_config_toml_path": str(worker_mcp_runtime.codex_config_toml_path),
                        "authority_repo": str(authority_repo),
                    },
                    "sandbox_backend": sandbox_backend,
                    "validation": list(card.get("validation") or []),
                    "required_outputs": list(card.get("required_outputs") or []),
                    "allow_empty_required_outputs": list(
                        card.get("allow_empty_required_outputs") or []
                    ),
                    "allow_unchanged_required_outputs": list(
                        card.get("allow_unchanged_required_outputs") or []
                    ),
                    "immutable_inputs": declared_immutable_inputs,
                    "immutable_input_manifest": immutable_input_manifest,
                    "external_readonly_dirs": external_readonly_dirs,
                    "workspace": workspace.as_metadata(),
                }
                write_json_0600(metadata_path, metadata)
                authority_path = self._terminal_authority_grant_path(request_id)
                _write_terminal_authority_grant(
                    authority_path,
                    self._terminal_authority_key(),
                    repo=self.repo,
                    task_id=task_id,
                    runner=runner,
                    topic=topic,
                    request_id=request_id,
                )
                write_json_0600(spec_path, {
                    "argv": worker_argv,
                    "cwd": "/",
                    "timeout_seconds": timeout_seconds,
                    "status_path": str(status_path),
                    "cancel_path": str(cancel_path),
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                })

                supervisor = Path(__file__).with_name("worker_supervisor.py")
                # landlock confines the *real* isolated workspace.home
                # directory directly, so HOME must literally be that path.
                # bubblewrap instead remounts workspace.home onto the string
                # from worker_workspace.bubblewrap_home_env_value() inside
                # its own mount namespace (see sandbox_argv); passing
                # home=None here makes sanitized_env() seed that identical
                # shared string as HOME so the two line up by construction,
                # not by two independently-coincidental Path.home() calls
                # (B314_F004).
                process = self._popen(
                    [sys.executable, str(supervisor), "--spec", str(spec_path)],
                    cwd="/",
                    env=sanitized_env(
                        adapter_id,
                        home=workspace.home if sandbox_backend == "landlock" else None,
                        isolated_task_queue_db=True,
                        provider_env=provider_env,
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    start_new_session=True,
                )
                started_at = _utcnow()
                start_ticks = _pid_start_ticks(process.pid)
                if start_ticks is None:
                    _terminate_process_group(process.pid, grace_seconds=5.0)
                    raise LaunchRejected("supervisor_pid_identity_unavailable")
                live = _LiveProcess(
                    request_id=request_id,
                    task_id=task_id,
                    runner=runner,
                    topic=topic,
                    adapter_id=adapter_id,
                    model=model,
                    process=process,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    started_at=started_at,
                    timeout_seconds=timeout_seconds,
                    isolated=True,
                    metadata_path=metadata_path,
                    supervisor_status_path=status_path,
                    pid_start_ticks=start_ticks,
                )
                self._live[request_id] = live
                event = self._append_event({
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": topic,
                    "adapter_id": adapter_id,
                    "model": model,
                    "state": "running",
                    "pid": process.pid,
                    "pid_start_ticks": start_ticks,
                    "started_at": started_at,
                    "timeout_seconds": timeout_seconds,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "metadata_path": str(metadata_path),
                    "supervisor_status_path": str(status_path),
                    "prompt_sha256": prompt_hash,
                    "project_context": (
                        context_result.metadata if context_result is not None else None
                    ),
                    "project_context_delivery": context_delivery,
                    "workspace_isolated": True,
                    "sandbox_backend": sandbox_backend,
                    "shell": False,
                })
                thread = threading.Thread(
                    target=self._monitor,
                    args=(live,),
                    name=f"aiworkhub-task-{request_id[:8]}",
                    daemon=True,
                )
                thread.start()

            return {
                "ok": True,
                "launch_implemented": LAUNCH_IMPLEMENTED,
                "launch_enabled": True,
                "request_id": request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": topic,
                "adapter_id": adapter_id,
                "model": model,
                "state": event["state"],
                "pid": process.pid,
                "card_priority": card.get("priority"),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "workspace_isolated": True,
                "sandbox_backend": sandbox_backend,
                "shell": False,
            }
        except (LaunchRejected, project_context.ProjectContextError, WorkspaceError, OSError, ValueError) as exc:
            reason = str(exc)
            # Once the public launch path has accepted an exact task identity,
            # every outcome belongs to the manager review ledger.  Some
            # failures (context collection, credential resolution, worktree
            # creation, adapter planning) happen before the normal claim
            # point; claim the still-pending exact card here so the canonical
            # terminal transition cannot silently leave it in Pending.
            if not claimed:
                terminal_claim = task_engine.claim_start_exact(
                    self.repo, task_id, runner, topic, request_id=request_id
                )
                claimed = bool(terminal_claim.get("ok"))
                if not claimed:
                    reason += ":terminal_claim_failed:" + str(
                        terminal_claim.get("stderr") or terminal_claim.get("stdout") or ""
                    )[:200]
            if claimed:
                reviewed = task_engine.mark_terminal_review(
                    self.repo,
                    task_id,
                    runner,
                    "launch_failed",
                    evidence={"request_id": request_id, "error": reason[:500]},
                )
                if not reviewed.get("ok"):
                    reason += ":terminal_review_failed:" + str(reviewed.get("stderr") or "")[:200]
            if workspace is not None:
                try:
                    cleanup_workspace(workspace.repo, workspace.path, workspace.home)
                except WorkspaceError as cleanup_exc:
                    reason += f":cleanup_failed:{cleanup_exc}"
            if spec_path is not None:
                unlink_if_regular(spec_path)
            if authority_path is not None:
                unlink_if_regular(authority_path)
            if bridge_request is not None:
                vscode_lm_bridge.cancel_request(bridge_request)
            return self._blocked(
                task_id,
                runner,
                topic,
                adapter_id,
                reason,
                request_id=request_id,
                state="launch_failed" if claimed else "blocked",
            )

    def _assert_no_duplicate_task(self, task_id: str) -> None:
        for live in self._live.values():
            if live.task_id == task_id and live.process.poll() is None:
                raise LaunchRejected(f"duplicate_live_task:{live.request_id}")
        for event in self._latest_by_request().values():
            if event.get("task_id") != task_id or event.get("state") not in ACTIVE_PROCESS_STATES:
                continue
            pid = int(event.get("pid") or 0)
            ticks = event.get("pid_start_ticks")
            if pid and _pid_matches(pid, ticks):
                raise LaunchRejected(f"duplicate_persisted_task:{event.get('request_id')}")

    def _launch_direct_for_tests(
        self,
        *,
        task_id: str,
        runner: str,
        topic: str,
        adapter_id: str,
        model: str | None = None,
        owner_prompt: str = "",
        timeout_seconds: int = 7200,
    ) -> dict[str, Any]:
        if not launch_gates_open():
            return self._blocked(
                task_id, runner, topic, adapter_id,
                "dual_gate_closed: require AIWORKHUB_ALLOW_LAUNCH=1 and AIWORKHUB_ALLOW_WRITES=1",
            )
        if timeout_seconds < 30 or timeout_seconds > 86_400:
            return self._blocked(task_id, runner, topic, adapter_id, "timeout_out_of_range")

        request_id: str | None = None
        provider_env: dict[str, str] | None = None
        try:
            _validate_adapter_identity(runner, adapter_id)
            card = self._preflight_card(task_id, runner, topic)
            external_readonly_dirs = _external_readonly_dirs(card, adapter_id)
            context_result = project_context.collect_project_context(self.repo, card)
            provider_env, model = self._resolve_provider_env(adapter_id, model)
            prompt = build_worker_prompt(
                task_id=task_id,
                runner=runner,
                topic=topic,
                card=card,
                owner_prompt=owner_prompt,
                project_context_bundle=(
                    context_result.prompt_bundle if context_result is not None else ""
                ),
            )
            plan = self._build_adapter(
                adapter_id=adapter_id,
                prompt=prompt,
                repo=self.repo,
                model=model,
                additional_readonly_dirs=external_readonly_dirs,
            )
            if not getattr(plan, "launchable", False):
                reason = getattr(plan, "reason", "adapter_not_launchable")
                raise LaunchRejected(reason or "adapter_not_launchable")

            with self._lock, self._registry_lock():
                if self._active_count() >= _configured_limit():
                    raise LaunchRejected("concurrency_limit_reached")
                for live in self._live.values():
                    if live.task_id == task_id and live.process.poll() is None:
                        raise LaunchRejected(f"duplicate_live_task:{live.request_id}")
                for event in self._latest_by_request().values():
                    if event.get("task_id") == task_id and event.get("state") in {"starting", "running"}:
                        pid = int(event.get("pid") or 0)
                        # PID + proc start-tick match, consistently with every
                        # other duplicate/liveness check in this module (see
                        # _assert_no_duplicate_task, _active_request_ids,
                        # cancel()). A bare _pid_alive() check would falsely
                        # match an unrelated process that recycled the PID.
                        if pid and _pid_matches(pid, event.get("pid_start_ticks")):
                            raise LaunchRejected(f"duplicate_persisted_task:{event.get('request_id')}")

                request_id = uuid.uuid4().hex
                self.process_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(self.process_dir, 0o700)
                stdout_path = self.process_dir / f"{request_id}.stdout.log"
                stderr_path = self.process_dir / f"{request_id}.stderr.log"
                _touch_0600(stdout_path)
                _touch_0600(stderr_path)
                prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                context_delivery = _project_context_delivery(context_result, prompt_hash)
                self._append_event({
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": topic,
                    "adapter_id": adapter_id,
                    "model": model,
                    "state": "starting",
                    "prompt_sha256": prompt_hash,
                    "project_context": (
                        context_result.metadata if context_result is not None else None
                    ),
                    "project_context_delivery": context_delivery,
                    "timeout_seconds": timeout_seconds,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "authority": "dual_env_gate",
                })

                # Launch authority belongs to the MCP parent, not to a nested
                # worker process. sanitized_env() builds an explicit minimal
                # allowlist -- it never starts from os.environ.copy() -- so
                # ALLOW_LAUNCH_ENV/ALLOW_WRITES_ENV/MAX_PROCESSES_ENV, the
                # coordinator token env vars, and every other unrelated
                # inherited secret are excluded by construction, on this
                # direct (non-isolated) launch path exactly like the isolated
                # path (see _launch_isolated's Popen call below).
                child_env = sanitized_env(adapter_id, provider_env=provider_env)
                child_env["AIWORKHUB_REPO"] = str(self.repo)
                with stdout_path.open("ab", buffering=0) as stdout_fh, stderr_path.open(
                    "ab", buffering=0
                ) as stderr_fh:
                    process = self._popen(
                        list(plan.argv),
                        cwd=str(plan.cwd),
                        env=child_env,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_fh,
                        stderr=stderr_fh,
                        shell=False,
                        start_new_session=True,
                    )
                start_ticks = _pid_start_ticks(process.pid)
                live = _LiveProcess(
                    request_id=request_id,
                    task_id=task_id,
                    runner=runner,
                    topic=topic,
                    adapter_id=adapter_id,
                    model=model,
                    process=process,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    started_at=_utcnow(),
                    timeout_seconds=timeout_seconds,
                    pid_start_ticks=start_ticks,
                )
                self._live[request_id] = live
                event = self._append_event({
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": topic,
                    "adapter_id": adapter_id,
                    "model": model,
                    "state": "running",
                    "pid": process.pid,
                    "pid_start_ticks": start_ticks,
                    "started_at": live.started_at,
                    "timeout_seconds": timeout_seconds,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "prompt_sha256": prompt_hash,
                    "project_context": (
                        context_result.metadata if context_result is not None else None
                    ),
                    "project_context_delivery": context_delivery,
                })
                thread = threading.Thread(
                    target=self._monitor,
                    args=(live,),
                    name=f"aiworkhub-task-{request_id[:8]}",
                    daemon=True,
                )
                thread.start()

            return {
                "ok": True,
                "launch_implemented": LAUNCH_IMPLEMENTED,
                "launch_enabled": True,
                "request_id": request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": topic,
                "adapter_id": adapter_id,
                "model": model,
                "state": event["state"],
                "pid": process.pid,
                "card_priority": card.get("priority"),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "shell": False,
            }
        except (LaunchRejected, project_context.ProjectContextError, OSError, ValueError) as exc:
            return self._blocked(
                task_id, runner, topic, adapter_id, str(exc), request_id=request_id
            )

    def _blocked(
        self,
        task_id: str,
        runner: str,
        topic: str,
        adapter_id: str,
        reason: str,
        *,
        request_id: str | None = None,
        state: str = "blocked",
    ) -> dict[str, Any]:
        event = self._append_event({
            "request_id": request_id or uuid.uuid4().hex,
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "adapter_id": adapter_id,
            "state": state,
            "blocked_reason": reason[:500],
        })
        return {
            "ok": False,
            "launch_implemented": LAUNCH_IMPLEMENTED,
            "launch_enabled": launch_gates_open(),
            "request_id": event["request_id"],
            "task_id": task_id,
            "state": state,
            "blocked_reason": reason[:500],
            "shell": False,
        }

    def _monitor(self, live: _LiveProcess) -> None:
        if live.isolated:
            try:
                live.process.wait(timeout=live.timeout_seconds + SUPERVISOR_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                _terminate_process_group(live.process.pid, grace_seconds=5.0)
            except Exception as exc:
                self._append_event({
                    "request_id": live.request_id,
                    "task_id": live.task_id,
                    "runner": live.runner,
                    "topic": live.topic,
                    "adapter_id": live.adapter_id,
                    "state": "reconcile_pending",
                    "pid": live.process.pid,
                    "pid_start_ticks": live.pid_start_ticks,
                    "error": str(exc)[:500],
                    "metadata_path": str(live.metadata_path or ""),
                    "supervisor_status_path": str(live.supervisor_status_path or ""),
                })
            self._finalize_isolated_request(live.request_id, live.process.poll())
            with self._lock:
                self._live.pop(live.request_id, None)
                self._cancelled.discard(live.request_id)
            return

        self._monitor_direct_for_tests(live)

    def _monitor_direct_for_tests(self, live: _LiveProcess) -> None:
        timed_out = False
        try:
            returncode = live.process.wait(timeout=live.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(live.process.pid, grace_seconds=5.0)
            returncode = live.process.poll()
        except Exception as exc:  # bounded background failure record
            returncode = live.process.poll()
            self._append_event({
                "request_id": live.request_id,
                "task_id": live.task_id,
                "runner": live.runner,
                "topic": live.topic,
                "adapter_id": live.adapter_id,
                "state": "monitor_error",
                "pid": live.process.pid,
                "error": str(exc)[:500],
            })
        try:
            card = _parse_card(self._show_task(live.task_id), live.task_id)
            task_state = core._lifecycle_state(card)
        except Exception:
            task_state = "unknown"
        with self._lock:
            was_cancelled = live.request_id in self._cancelled
        state = "cancelled" if was_cancelled else "timed_out" if timed_out else "exited"
        if not timed_out and not was_cancelled and returncode == 0 and task_state == "review":
            state = "review_ready"
        elif not timed_out and not was_cancelled and returncode == 0 and task_state != "review":
            state = "exited_without_review"
        usage, usage_recorded, usage_error = self._record_usage(
            live.request_id,
            live.task_id,
            live.runner,
            live.adapter_id,
            live.model or live.adapter_id,
            live.stdout_path,
            topic=live.topic,
        )
        context_ack = _project_context_receipt_from_output(
            live.stdout_path,
            expected_bundle_sha256=_expected_context_bundle_sha(live.metadata_path),
        )
        self._append_event({
            "request_id": live.request_id,
            "task_id": live.task_id,
            "runner": live.runner,
            "topic": live.topic,
            "adapter_id": live.adapter_id,
            "model": live.model,
            "state": state,
            "pid": live.process.pid,
            "exit_code": returncode,
            "task_state": task_state,
            "finished_at": _utcnow(),
            "stdout_path": str(live.stdout_path),
            "stderr_path": str(live.stderr_path),
            "usage": usage,
            "usage_recorded": usage_recorded,
            "usage_error": usage_error,
            "project_context_acknowledgement": context_ack,
        })
        with self._lock:
            self._live.pop(live.request_id, None)
            self._cancelled.discard(live.request_id)

    def _record_usage(
        self,
        request_id: str,
        task_id: str,
        runner: str,
        adapter_id: str,
        model: str,
        stdout_path: Path,
        topic: str | None = None,
    ) -> tuple[dict[str, Any], bool, str]:
        usage = _usage_from_output(stdout_path)
        usage_recorded = False
        usage_error = ""
        total_input = _ledger_input_tokens(usage, adapter_id)
        usage["recorded_input_tokens"] = total_input
        if total_input or usage["output_tokens"] or usage["cost_usd"]:
            note = f"task_mcp_request:{request_id}"
            try:
                card = _parse_card(self._show_task(task_id), task_id)
                topic = topic or str(card.get("topic") or "")
                records = card.get("usage_records") or []
                if isinstance(records, list) and any(
                    isinstance(record, dict)
                    and record.get("source") == "task_mcp_launcher"
                    and record.get("note") == note
                    for record in records
                ):
                    return usage, True, ""
            except Exception:
                pass
            args = [
                "usage", task_id,
                "--runner", runner,
                "--model", model,
                "--provider", adapter_id.removesuffix("_cli"),
                "--source", "task_mcp_launcher",
                "--note", note,
                "--input-tokens", str(total_input),
                "--output-tokens", str(usage["output_tokens"]),
                "--total-tokens", str(total_input + int(usage["output_tokens"])),
                "--cost-usd", str(usage["cost_usd"]),
            ]
            try:
                result = core.run_taskctl(
                    args,
                    allow_write=True,
                    runner=runner,
                    topic=topic,
                )
                usage_recorded = result.returncode == 0
                usage_error = result.stderr[:300] if not usage_recorded else ""
            except Exception as exc:
                usage_error = str(exc)[:300]
        return usage, usage_recorded, usage_error

    def _request_events(self, request_id: str) -> list[dict[str, Any]]:
        return [event for event in self._events() if event.get("request_id") == request_id]

    @staticmethod
    def _metadata_from_events(events: list[dict[str, Any]]) -> Path | None:
        for event in reversed(events):
            raw = event.get("metadata_path")
            if raw:
                return Path(str(raw))
        return None

    def _exact_claim_state(self, metadata: dict[str, Any]) -> str:
        task_id = str(metadata["task_id"])
        runner = str(metadata["runner"])
        topic = str(metadata["topic"])
        card = _parse_card(self._show_task(task_id), task_id)
        if card.get("runner") != runner or card.get("topic") != topic:
            raise WorkspaceError("claim_ownership_lost:task_identity_changed")
        if card.get("claimed_by") != runner:
            raise WorkspaceError(
                f"claim_ownership_lost:claimed_by={card.get('claimed_by') or ''}"
            )
        state = core._lifecycle_state(card)
        if state == "processing":
            return state
        if state == "review" and card.get("review_requested_by") == runner:
            return state
        raise WorkspaceError(f"claim_ownership_lost:state={state}")

    def _release_exact(self, metadata: dict[str, Any], reason: str) -> dict[str, Any]:
        task_id = str(metadata["task_id"])
        runner = str(metadata["runner"])
        try:
            card = _parse_card(self._show_task(task_id), task_id)
            lifecycle = core._lifecycle_state(card)
            # A stale persisted process event must not keep retrying a queue
            # transition after the canonical task has already reached Review
            # or Finished. The task queue is authoritative; close the process
            # record as an idempotent no-op instead of appending release_pending
            # forever on every reconciler scan.
            if lifecycle in {"review", "finished"}:
                return {
                    "ok": True,
                    "idempotent_noop": True,
                    "canonical_lifecycle": lifecycle,
                }
            if (
                lifecycle == "pending"
                and not card.get("claimed_by")
                and card.get("launch_released_by") == core.CODEX_RUNNER
            ):
                return {"ok": True, "idempotent_noop": True}
        except Exception:
            pass
        return core.release_launch(task_id, runner, reason[:300])

    def _terminal_authority_key(self) -> bytes:
        if self._authority_key is None:
            self._authority_key = _load_or_create_terminal_authority_key(
                self.process_dir / TERMINAL_AUTHORITY_KEY_FILENAME
            )
        return self._authority_key

    def _terminal_authority_grant_path(self, request_id: str) -> Path:
        return self.process_dir / f"{request_id}.terminal-authority.json"

    def _consume_terminal_authority_grant(
        self,
        request_id: str,
        *,
        repo: Path,
        task_id: str,
        runner: str,
        topic: str,
    ) -> bool:
        """One-shot verify-and-consume of the exact-scoped grant minted at
        launch time. The grant file is removed on this call regardless of
        whether it validates -- a tampered, wrong-task, wrong-repo, or
        otherwise mismatched artifact is rejected and can never be
        presented again (B894: replay/cross-task/cross-repo fail closed)."""
        path = self._terminal_authority_grant_path(request_id)
        payload = _read_terminal_authority_grant(path)
        unlink_if_regular(path)
        if not payload or payload.get("schema_id") != TERMINAL_AUTHORITY_SCHEMA_ID:
            return False
        if (
            str(payload.get("repo") or "") != str(repo)
            or str(payload.get("task_id") or "") != task_id
            or str(payload.get("runner") or "") != runner
            or str(payload.get("topic") or "") != topic
            or str(payload.get("request_id") or "") != request_id
        ):
            return False
        signature = str(payload.get("signature") or "")
        if not signature:
            return False
        expected = hmac.new(
            self._terminal_authority_key(),
            _terminal_authority_signing_material(
                repo=repo, task_id=task_id, runner=runner, topic=topic, request_id=request_id,
            ),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

    def _review_terminal_exact(
        self,
        metadata: dict[str, Any],
        substatus: str,
        *,
        request_id: str,
        error: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = str(metadata["task_id"])
        runner = str(metadata["runner"])
        payload = {
            "request_id": request_id,
            "adapter_id": metadata.get("adapter_id"),
            "model": metadata.get("model"),
            "error": error[:500],
            **(evidence or {}),
        }
        return task_engine.mark_terminal_review(
            self.repo,
            task_id,
            runner,
            substatus,
            evidence=payload,
        )

    @staticmethod
    def _read_supervisor_status(path: Path) -> dict[str, Any]:
        return read_supervisor_status(path)

    def _watch_persisted_request(
        self,
        request_id: str,
        pid: int,
        start_ticks: Any,
    ) -> None:
        try:
            while _pid_matches(pid, start_ticks):
                time.sleep(0.2)
            self._finalize_isolated_request(request_id)
        finally:
            with self._lock:
                self._watching.discard(request_id)

    def _reconcile_persisted_requests(self) -> dict[str, int]:
        watched = 0
        finalized = 0
        candidates = {
            request_id: event
            for request_id, event in self._latest_by_request().items()
            if event.get("state") in ACTIVE_PROCESS_STATES | FINALIZATION_PENDING_STATES
            and event.get("metadata_path")
        }
        for request_id, event in candidates.items():
            pid = int(event.get("pid") or 0)
            ticks = event.get("pid_start_ticks")
            if (
                event.get("state") in ACTIVE_PROCESS_STATES
                and ticks not in (None, "")
                and _pid_matches(pid, ticks)
            ):
                with self._lock:
                    if request_id in self._watching or request_id in self._live:
                        continue
                    self._watching.add(request_id)
                thread = threading.Thread(
                    target=self._watch_persisted_request,
                    args=(request_id, pid, ticks),
                    name=f"aiworkhub-reconcile-{request_id[:8]}",
                    daemon=True,
                )
                thread.start()
                watched += 1
            else:
                if self._finalize_isolated_request(request_id) is not None:
                    finalized += 1
        return {"watched": watched, "finalized": finalized}

    def reconcile(self) -> dict[str, Any]:
        result = self._reconcile_persisted_requests()
        gc_result = self._gc_finalized_workspaces()
        return {"ok": True, **result, **gc_result}

    def _gc_finalized_workspaces(self) -> dict[str, int]:
        """Run one idempotent, fail-closed sweep of retained workspaces."""
        scanned = 0
        cleaned = 0
        skipped = 0
        for request_id, event in list(self._latest_by_request().items()):
            result = self._gc_finalized_workspace(request_id, event)
            if result is None:
                continue
            scanned += 1
            if result.get("gc"):
                cleaned += 1
            else:
                skipped += 1
        return {"gc_scanned": scanned, "gc_cleaned": cleaned, "gc_skipped": skipped}

    @staticmethod
    def _gc_disposition(card: dict[str, Any], request_id: str) -> tuple[bool, str]:
        """Return whether ``request_id`` is no longer a live review surface.

        This deliberately fails closed for processing cards and malformed
        review authority.  A review card may have many historical retained
        worker attempts, but its canonical ``terminal_review`` names exactly
        one current request; only older, different request ids are collected.
        """
        canonical_status = _canonical_task_status(card)
        if canonical_status in GC_DISPOSED_CANONICAL_STATUSES:
            return True, f"disposed_task_status:{canonical_status}"
        if canonical_status != "review":
            return False, f"task_not_disposed:{canonical_status}"

        terminal_review = card.get("terminal_review")
        if not isinstance(terminal_review, dict):
            return False, "review_request_identity_missing"
        evidence = terminal_review.get("evidence")
        if not isinstance(evidence, dict):
            return False, "review_request_identity_missing"
        identity = evidence.get("request_identity")
        if not isinstance(identity, dict):
            return False, "review_request_identity_missing"
        current_request_id = str(identity.get("request_id") or "").strip()
        if not current_request_id:
            return False, "review_request_identity_missing"
        if current_request_id == request_id:
            return False, "current_review_request"
        return True, f"superseded_review_request:{current_request_id}"

    def _gc_finalized_workspace(
        self, request_id: str, event: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Delete only a finalized task's retained, proven-dead workspace."""
        if event.get("state") not in GC_CANDIDATE_PROCESS_STATES:
            return None
        if not event.get("workspace_retained"):
            return None
        with self._lock:
            if request_id in self._active_request_ids():
                return None
        with self._registry_lock():
            events = self._request_events(request_id)
            if not events:
                return None
            latest = events[-1]
            if (
                latest.get("state") not in GC_CANDIDATE_PROCESS_STATES
                or not latest.get("workspace_retained")
            ):
                return None

            metadata_raw = latest.get("metadata_path")
            if not metadata_raw:
                return {"request_id": request_id, "gc": False, "reason": "metadata_path_missing"}
            metadata_path = Path(str(metadata_raw))
            if not metadata_path.is_file():
                return {"request_id": request_id, "gc": False, "reason": "metadata_missing"}
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                workspace_meta = metadata["workspace"]
                repo = Path(str(workspace_meta["repo"]))
                path = Path(str(workspace_meta["path"]))
                home = Path(str(workspace_meta["home"]))
                meta_request_id = str(workspace_meta["request_id"])
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                return {"request_id": request_id, "gc": False, "reason": "metadata_invalid"}
            if meta_request_id != request_id or repo.resolve() != self.repo:
                return {"request_id": request_id, "gc": False, "reason": "workspace_identity_mismatch"}

            task_id = str(latest.get("task_id") or "")
            runner = str(latest.get("runner") or "")
            if not task_id or not runner:
                return {"request_id": request_id, "gc": False, "reason": "task_identity_missing"}
            try:
                card = _parse_card(self._show_task(task_id), task_id)
            except Exception as exc:
                return {
                    "request_id": request_id,
                    "gc": False,
                    "reason": f"task_lookup_failed:{exc}"[:200],
                }
            eligible, disposition = self._gc_disposition(card, request_id)
            if not eligible:
                return {
                    "request_id": request_id,
                    "gc": False,
                    "reason": disposition,
                }

            pid = int(latest.get("pid") or 0)
            ticks = latest.get("pid_start_ticks")
            if not _process_proven_dead(pid, ticks):
                return {"request_id": request_id, "gc": False, "reason": "process_not_proven_dead"}
            try:
                assert_gc_safe_workspace_shape(meta_request_id, path, home)
            except WorkspaceError as exc:
                return {
                    "request_id": request_id,
                    "gc": False,
                    "reason": f"unsafe_workspace_shape:{exc}"[:200],
                }
            try:
                cleanup_workspace(repo, path, home)
            except WorkspaceError as exc:
                return {
                    "request_id": request_id,
                    "gc": False,
                    "reason": f"cleanup_failed:{exc}"[:200],
                }

            self._append_event({
                "request_id": request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": latest.get("topic"),
                "adapter_id": latest.get("adapter_id"),
                "state": latest.get("state"),
                "workspace_retained": False,
                "workspace_gc": True,
                "workspace_gc_at": _utcnow(),
                "workspace_gc_reason": disposition,
            })
            return {"request_id": request_id, "gc": True, "reason": disposition}

    def _finalize_isolated_request(
        self,
        request_id: str,
        supervisor_returncode: int | None = None,
    ) -> dict[str, Any] | None:
        with self._registry_lock():
            events = self._request_events(request_id)
            if not events:
                return None
            latest = events[-1]
            if latest.get("state") in TERMINAL_PROCESS_STATES:
                return latest
            metadata_path = self._metadata_from_events(events)
            if metadata_path is None or not metadata_path.is_file():
                return None
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                workspace = WorkerWorkspace.from_metadata(metadata["workspace"])
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                return self._append_event({
                    **self._event_identity(events),
                    "state": "finalize_failed",
                    "error": f"metadata_invalid:{exc}"[:500],
                    "finished_at": _utcnow(),
                })

            status_path = Path(str(metadata["supervisor_status_path"]))
            supervisor_status = self._read_supervisor_status(status_path)
            supervisor_pid = int(latest.get("pid") or 0)
            supervisor_ticks = latest.get("pid_start_ticks")
            supervisor_alive = bool(
                supervisor_ticks not in (None, "")
                and _pid_matches(supervisor_pid, supervisor_ticks)
            )
            # The supervisor process is spawned before its first atomic status
            # write.  A concurrent reconciler can therefore observe the exact
            # live PID while the status file is still absent for a few
            # milliseconds.  That launch window is active work, not evidence
            # of failure; the PID watcher will call us again after exit.
            if supervisor_alive and not supervisor_status:
                return None
            liveness_lost = False
            if supervisor_status.get("state") in {"starting", "running"} and supervisor_alive:
                liveness = derive_liveness_state(
                    now_epoch=time.time(),
                    supervisor_alive=supervisor_alive,
                    heartbeat_at_epoch=supervisor_status.get("heartbeat_at_epoch"),
                    last_output_change_epoch=supervisor_status.get("last_output_change_epoch"),
                )
                if liveness["liveness_state"] in {"alive", "quiet", "unresponsive"}:
                    return None
                # liveness_state == "lost": the heartbeat lease AND bounded
                # recovery grace both elapsed while the exact supervisor PID
                # still exists (a hung/deadlocked supervisor, not merely a
                # slow one). Recheck identity ONE more time immediately
                # before any termination action -- still under this whole
                # call's registry lock -- then terminate ONLY the exact
                # matching supervisor/child process group(s). Never act on a
                # bare "process exists" signal alone.
                liveness_lost = True
                if _pid_matches(supervisor_pid, supervisor_ticks):
                    _terminate_process_group(supervisor_pid, grace_seconds=5.0)
                supervisor_alive = False

            # An abruptly lost (or heartbeat-lease-lost) supervisor may leave
            # its child running. Kill only when both PID and proc start time
            # still match the durable status record, preventing PID-reuse
            # termination.
            child_pid = int(supervisor_status.get("child_pid") or 0)
            child_ticks = supervisor_status.get("child_pid_start_ticks")
            if (
                not supervisor_alive
                and supervisor_status.get("state") in {"starting", "running"}
                and child_pid
                and _pid_matches(child_pid, child_ticks)
            ):
                _terminate_process_group(child_pid, grace_seconds=5.0)

            exit_code = supervisor_status.get("exit_code")
            supervisor_state = str(supervisor_status.get("state") or "")
            error = str(supervisor_status.get("error") or "")[:500]
            if liveness_lost and not error:
                error = f"liveness_lost:heartbeat_lease_and_recovery_grace_exceeded:rc={supervisor_returncode}"
            if supervisor_state == "timed_out":
                terminal_state = "timed_out"
            elif supervisor_state == "cancelled":
                terminal_state = "cancelled"
            elif supervisor_state == "exited" and exit_code == 0:
                terminal_state = "exited"
            elif supervisor_state in {"exited", "spawn_failed", "supervisor_error"}:
                terminal_state = "worker_failed"
            else:
                # A missing, malformed, or stale running status is never proof
                # that the worker ran successfully. Cancellation intent is the
                # sole safe special case.
                if latest.get("state") == "cancel_requested":
                    terminal_state = "cancelled"
                else:
                    terminal_state = "worker_failed"
                detail = supervisor_state or "missing"
                error = error or (
                    f"supervisor_incomplete:state={detail}:rc={supervisor_returncode}"
                )

            changed: list[str] = []
            promoted: list[str] = []
            validations: list[dict[str, Any]] = []
            required_output_records: list[dict[str, Any]] = []
            review_result: dict[str, Any] | None = None
            release_result: dict[str, Any] | None = None
            worker_mcp_gate: dict[str, Any] | None = None
            quality_gate: dict[str, Any] | None = None
            cleanup = True
            try:
                if terminal_state != "exited":
                    # Coordinator review-first: timed_out/cancelled/
                    # worker_failed are terminal outcomes that move to
                    # review, never a silent
                    # pending. Retain the isolated worktree/candidate and
                    # logs until Codex's accept/reject decision instead of
                    # deleting the only evidence of what the worker did.
                    cleanup = False
                    # A non-exited terminal outcome never promotes/writes and
                    # so never needs the one-task authority grant -- remove
                    # it now so it cannot linger as a stale, unconsumed
                    # artifact for a request that will never reach the
                    # success branch below.
                    unlink_if_regular(self._terminal_authority_grant_path(request_id))
                    release_result = self._review_terminal_exact(
                        metadata,
                        terminal_state,
                        request_id=request_id,
                        error=error,
                        evidence={
                            "supervisor_state": supervisor_state,
                            "exit_code": exit_code,
                            "liveness_lost": liveness_lost,
                        },
                    )
                    if not release_result.get("ok"):
                        terminal_state = "release_pending"
                        error = error or (
                            "terminal_review_transition_failed:"
                            + str(
                                release_result.get("stderr")
                                or release_result.get("stdout")
                                or ""
                            )[:300]
                        )
                else:
                    claim_state = self._exact_claim_state(metadata)
                    # B894: the ambient AIWORKHUB_ALLOW_WRITES flag is only
                    # ever set in the process that is actively handling an
                    # MCP request -- it is gone by the time a detached
                    # reconciliation scan (a different, later process)
                    # observes a clean exit. Fall back to the narrowly
                    # scoped, single-use grant this exact launch minted while
                    # the ambient gate WAS open, rather than stalling every
                    # successful outcome at review_pending forever. Both
                    # checks always run (never short-circuited) so the grant
                    # is consumed -- and thus can never be replayed -- even
                    # when the ambient gate alone already authorized this.
                    ambient_writes_allowed = core.writes_allowed()
                    granted = self._consume_terminal_authority_grant(
                        request_id,
                        repo=self.repo,
                        task_id=str(metadata["task_id"]),
                        runner=str(metadata["runner"]),
                        topic=str(metadata["topic"]),
                    )
                    if not (ambient_writes_allowed or granted):
                        cleanup = False
                        terminal_state = "review_pending"
                        error = "write_gate_closed_during_reconciliation"
                    else:
                        changed = enforce_scope(workspace)
                        required_output_records = validate_required_outputs(
                            workspace,
                            metadata.get("required_outputs") or [],
                            allow_empty=tuple(
                                metadata.get("allow_empty_required_outputs") or []
                            ),
                            allow_unchanged=tuple(
                                metadata.get("allow_unchanged_required_outputs") or []
                            ),
                        )
                        worker_mcp_gate = _worker_mcp_live_call_gate(metadata, request_id)
                        if worker_mcp_gate.get("gated") and not worker_mcp_gate.get("satisfied", True):
                            raise WorkspaceError(
                                "validation_required_aiworkhub_mcp_call_missing:"
                                + str(worker_mcp_gate.get("reason") or "")
                            )
                        validations = run_validations(
                            workspace, metadata.get("validation") or []
                        )
                        changed = enforce_scope(workspace)
                        # B561: union validated required-output exact paths into
                        # the review candidate set.  validate_required_outputs
                        # finds gitignored files that changed_paths (git-diff /
                        # git-ls-files --exclude-standard) silently omits.
                        validated_required_paths = {
                            rec["path"]
                            for rec in required_output_records
                            if not rec.get("unchanged_allowed")
                        }
                        changed = sorted(set(changed) | validated_required_paths)
                        if not changed:
                            raise WorkspaceError("no_effect")
                        quality_gate = quality_evidence.run_completion_quality_gate(
                            workspace.path, changed_paths=changed
                        )
                        if not quality_gate.get("passed"):
                            blockers = quality_gate.get("blocking_checks") or []
                            reason = quality_gate.get("config_error") or ",".join(str(v) for v in blockers)
                            raise WorkspaceError("quality_gate_failed:" + str(reason)[:400])
                        # Phase 1 review-first reconcile: a successful worker
                        # exit no longer promotes into the canonical repo nor
                        # marks review via core.mark_review directly. The
                        # isolated workspace is retained and the coordinator's
                        # review ledger receives every check's evidence
                        # (validation, required outputs, the MCP gate, changed
                        # paths + their hashes, and the exact request/workspace
                        # identity) so a later coordinator-accept step is the
                        # only path that can ever touch the canonical repo.
                        changed_path_hashes = _changed_path_hashes(workspace, changed)
                        cleanup = False
                        terminal_state = "review_ready"
                        review_result = {"ok": True, "idempotent_noop": True}
                        release_result = self._review_terminal_exact(
                            metadata,
                            "review_ready",
                            request_id=request_id,
                            evidence={
                                "changed_paths": changed,
                                "changed_path_hashes": changed_path_hashes,
                                "required_outputs": required_output_records,
                                "validation": validations,
                                "worker_mcp_gate": worker_mcp_gate,
                                "quality_gate": quality_gate,
                                "claim_state": claim_state,
                                "immutable_inputs": metadata.get("immutable_inputs") or [],
                                "immutable_input_manifest": (
                                    metadata.get("immutable_input_manifest") or {}
                                ),
                                "workspace": workspace.as_metadata(),
                                "request_identity": {
                                    "request_id": request_id,
                                    "task_id": str(metadata["task_id"]),
                                    "runner": str(metadata["runner"]),
                                    "topic": str(metadata["topic"]),
                                },
                            },
                        )
                        if not release_result.get("ok"):
                            cleanup = False
                            terminal_state = "review_pending"
                            error = "review_transition_failed:" + str(
                                release_result.get("stderr")
                                or release_result.get("stdout")
                                or ""
                            )[:300]
            except WorkspaceError as exc:
                error = str(exc)
                if error.startswith("scope_violation") or error.startswith("symlink_output"):
                    terminal_state = "scope_rejected"
                elif error.startswith(("validation", "required_output", "quality_gate")):
                    terminal_state = "validation_failed"
                elif error.startswith(("parent_changed", "promotion_scope")):
                    terminal_state = "promotion_conflict"
                else:
                    terminal_state = "finalize_failed"
                # Keep the isolated candidate intact for coordinator
                # diagnosis/retry on every genuine terminal failure, including
                # a lost-claim race (error starts with "claim_ownership_lost").
                # Deleting it here forces needless model reruns and destroys
                # the evidence needed to distinguish a product defect from a
                # validator/promotion-race defect (the coordinator
                # review-first lifecycle owns cleanup after its accept/reject
                # decision, not the worker's own finalize path). B863: a
                # claim_ownership_lost read is not reliable proof that a
                # different runner legitimately owns this task -- it can also
                # be a false positive from a launcher/finalizer canonical-
                # authority disagreement (the B860/B861 failure mode), and
                # this worker's own claim episode may still be exact-current.
                # Cleanup here used to be unconditional on ownership_lost,
                # which deleted still-valid worktrees on every false positive.
                # Deletion is deferred entirely to the canonical-status-gated
                # sweep in _gc_finalized_workspace, which independently
                # re-reads this exact self.repo's task_queue.sqlite and
                # requires a genuinely finished/archived status before ever
                # touching disk.
                ownership_lost = error.startswith("claim_ownership_lost")
                cleanup = False
                if not ownership_lost and not promoted:
                    release_result = self._review_terminal_exact(
                        metadata,
                        terminal_state,
                        request_id=request_id,
                        error=error,
                        evidence={
                            "changed_paths": changed,
                            "promoted_paths": promoted,
                            "required_outputs": required_output_records,
                            "validation": validations,
                            "worker_mcp_gate": worker_mcp_gate,
                            "quality_gate": quality_gate,
                        },
                    )
                    if not release_result.get("ok"):
                        cleanup = False
                        terminal_state = "release_pending"
            except Exception as exc:
                error = str(exc)[:500]
                if promoted:
                    cleanup = False
                    terminal_state = "review_pending"
                else:
                    terminal_state = "finalize_failed"
                    cleanup = False
                    release_result = self._review_terminal_exact(
                        metadata,
                        terminal_state,
                        request_id=request_id,
                        error=error,
                        evidence={
                            "changed_paths": changed,
                            "promoted_paths": promoted,
                        },
                    )
                    if not release_result.get("ok"):
                        cleanup = False
                        terminal_state = "release_pending"

            stdout_path = Path(str(metadata["stdout_path"]))
            usage: dict[str, Any] = {}
            usage_recorded = False
            usage_error = ""
            if terminal_state not in FINALIZATION_PENDING_STATES:
                usage, usage_recorded, usage_error = self._record_usage(
                    request_id,
                    str(metadata["task_id"]),
                    str(metadata["runner"]),
                    str(metadata["adapter_id"]),
                    str(metadata.get("model") or metadata["adapter_id"]),
                    stdout_path,
                    topic=str(metadata["topic"]),
                )
            context_ack = _project_context_receipt_from_output(
                stdout_path,
                expected_bundle_sha256=str(
                    (metadata.get("project_context") or {}).get("bundle_sha256") or ""
                ),
            )
            event = self._append_event({
                "request_id": request_id,
                "task_id": metadata["task_id"],
                "runner": metadata["runner"],
                "topic": metadata["topic"],
                "adapter_id": metadata["adapter_id"],
                "model": metadata.get("model"),
                "state": terminal_state,
                "worker_terminal_state": (
                    terminal_state
                    if terminal_state not in FINALIZATION_PENDING_STATES
                    else latest.get("worker_terminal_state")
                ),
                "pid": supervisor_pid,
                "pid_start_ticks": latest.get("pid_start_ticks"),
                "child_pid": supervisor_status.get("child_pid"),
                "exit_code": exit_code,
                "finished_at": _utcnow(),
                "stdout_path": metadata["stdout_path"],
                "stderr_path": metadata["stderr_path"],
                "metadata_path": str(metadata_path),
                "supervisor_status_path": str(status_path),
                "cancel_path": metadata.get("cancel_path"),
                "workspace_isolated": True,
                "workspace_retained": not cleanup,
                "sandbox_backend": metadata.get("sandbox_backend"),
                "changed_paths": changed,
                "promoted_paths": promoted,
                "required_outputs": required_output_records,
                "validation": validations,
                "review_transition_ok": bool(review_result and review_result.get("ok")),
                "release_transition_ok": bool(release_result and release_result.get("ok")),
                "liveness_lost": liveness_lost,
                "error": error[:500],
                "usage": usage,
                "usage_recorded": usage_recorded,
                "usage_error": usage_error,
                "project_context": metadata.get("project_context"),
                "project_context_delivery": metadata.get("project_context_delivery"),
                "project_context_acknowledgement": context_ack,
                "worker_mcp_gate": worker_mcp_gate,
                "quality_gate": quality_gate,
            })
            if cleanup:
                try:
                    cleanup_workspace(workspace.repo, workspace.path, workspace.home)
                except WorkspaceError as exc:
                    self._append_event({
                        **self._event_identity(events + [event]),
                        "state": terminal_state,
                        "finished_at": _utcnow(),
                        "error": f"cleanup_failed:{exc}"[:500],
                    })
            return event

    @staticmethod
    def _event_identity(events: list[dict[str, Any]]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for event in events:
            for key in (
                "request_id", "task_id", "runner", "topic", "adapter_id", "model",
                "pid", "pid_start_ticks", "stdout_path", "stderr_path", "metadata_path",
            ):
                if event.get(key) is not None:
                    merged[key] = event[key]
        return merged

    @staticmethod
    def _liveness_snapshot(latest: dict[str, Any]) -> dict[str, Any]:
        """Bounded, read-only liveness derivation for one event row.

        Returns ``{}`` when there is no isolated supervisor status artifact
        to read (e.g. a direct/non-isolated launch, or a request that never
        reached "running"). Never mutates anything -- a pure read + derive.
        """
        status_raw = latest.get("supervisor_status_path")
        if not status_raw:
            return {}
        supervisor_status = read_supervisor_status(Path(str(status_raw)))
        if not supervisor_status:
            return {}
        pid = int(latest.get("pid") or 0)
        ticks = latest.get("pid_start_ticks")
        supervisor_alive = bool(pid and ticks not in (None, "") and _pid_matches(pid, ticks))
        child_pid = int(supervisor_status.get("child_pid") or 0)
        child_ticks = supervisor_status.get("child_pid_start_ticks")
        child_alive = bool(child_pid and _pid_matches(child_pid, child_ticks))
        liveness = derive_liveness_state(
            now_epoch=time.time(),
            supervisor_alive=supervisor_alive,
            heartbeat_at_epoch=supervisor_status.get("heartbeat_at_epoch"),
            last_output_change_epoch=supervisor_status.get("last_output_change_epoch"),
        )
        started_epoch = supervisor_status.get("started_at_epoch")
        runtime_seconds = (
            max(0.0, time.time() - float(started_epoch))
            if isinstance(started_epoch, (int, float))
            else None
        )
        return {
            **liveness,
            "supervisor_alive": supervisor_alive,
            "child_alive": child_alive,
            "heartbeat_seq": supervisor_status.get("heartbeat_seq"),
            "runtime_seconds": runtime_seconds,
            "stdout_bytes": supervisor_status.get("stdout_bytes"),
            "stderr_bytes": supervisor_status.get("stderr_bytes"),
        }

    def status(self, request_id: str) -> dict[str, Any]:
        events = self._request_events(request_id)
        if not events:
            return {"ok": False, "request_id": request_id, "state": "not_found"}
        latest = events[-1]
        if (
            latest.get("state") in ACTIVE_PROCESS_STATES | FINALIZATION_PENDING_STATES
            and latest.get("metadata_path")
        ):
            pid = int(latest.get("pid") or 0)
            ticks = latest.get("pid_start_ticks")
            if (
                latest.get("state") in FINALIZATION_PENDING_STATES
                or ticks in (None, "")
                or not _pid_matches(pid, ticks)
            ):
                self._finalize_isolated_request(request_id)
                events = self._request_events(request_id)
                latest = events[-1]
        with self._lock:
            live = self._live.get(request_id)
            if live is not None:
                code = live.process.poll()
                process_alive = code is None
            else:
                code = latest.get("exit_code")
                pid = int(latest.get("pid") or 0)
                process_alive = bool(
                    pid
                    and latest.get("state") in ACTIVE_PROCESS_STATES
                    and latest.get("pid_start_ticks") not in (None, "")
                    and _pid_matches(pid, latest.get("pid_start_ticks"))
                )
        task_id = str(latest.get("task_id") or events[0].get("task_id") or "")
        card: dict[str, Any] | None = None
        try:
            card = _parse_card(self._show_task(task_id), task_id)
        except Exception:
            pass
        return {
            "ok": True,
            "request_id": request_id,
            "task_id": task_id,
            "state": latest.get("state"),
            "process_alive": process_alive,
            "exit_code": code,
            "pid": latest.get("pid"),
            "runner": latest.get("runner"),
            "topic": latest.get("topic"),
            "adapter_id": latest.get("adapter_id"),
            "model": latest.get("model"),
            "task_state": core._lifecycle_state(card) if card else "unknown",
            "task_card": card,
            "event_count": len(events),
            "latest_event": latest,
            "liveness": self._liveness_snapshot(latest),
        }

    def collect(self, request_id: str, max_log_bytes: int = MAX_LOG_TAIL_BYTES) -> dict[str, Any]:
        status = self.status(request_id)
        if not status.get("ok"):
            return status
        latest = status.get("latest_event") or {}
        stdout_path = Path(str(latest.get("stdout_path") or ""))
        stderr_path = Path(str(latest.get("stderr_path") or ""))
        limit = max(1024, min(int(max_log_bytes), MAX_LOG_TAIL_BYTES))
        return {
            **status,
            "stdout_tail": _safe_tail(stdout_path, limit),
            "stderr_tail": _safe_tail(stderr_path, limit),
            "review_ready": status.get("task_state") == "review",
            "terminal": status.get("state") in {
                *TERMINAL_PROCESS_STATES,
            },
        }

    def cancel(self, request_id: str, reason: str = "owner_cancelled") -> dict[str, Any]:
        with self._lock:
            live = self._live.get(request_id)
        status = self.status(request_id)
        if not status.get("ok"):
            return status
        if status.get("state") in TERMINAL_PROCESS_STATES:
            return {
                "ok": True,
                "request_id": request_id,
                "state": status.get("state"),
                "idempotent_noop": True,
            }
        if live is not None and not live.isolated:
            with self._lock:
                self._cancelled.add(request_id)
            _terminate_process_group(live.process.pid, grace_seconds=5.0)
            event = self._append_event({
                "request_id": request_id,
                "task_id": live.task_id,
                "runner": live.runner,
                "topic": live.topic,
                "adapter_id": live.adapter_id,
                "state": "cancelled",
                "pid": live.process.pid,
                "reason": reason[:300],
                "finished_at": _utcnow(),
                "stdout_path": str(live.stdout_path),
                "stderr_path": str(live.stderr_path),
            })
            with self._lock:
                self._live.pop(request_id, None)
            return {"ok": True, "request_id": request_id, "state": event["state"]}

        if status.get("state") in FINALIZATION_PENDING_STATES:
            return {
                "ok": False,
                "request_id": request_id,
                "state": status.get("state"),
                "blocked_reason": "finalization_pending",
            }

        should_finalize = False
        with self._registry_lock():
            events = self._request_events(request_id)
            latest = events[-1]
            if latest.get("state") in TERMINAL_PROCESS_STATES:
                return {
                    "ok": True,
                    "request_id": request_id,
                    "state": latest.get("state"),
                    "idempotent_noop": True,
                }
            pid = int(latest.get("pid") or 0)
            ticks = latest.get("pid_start_ticks")
            if ticks in (None, "") or not pid or not _pid_matches(pid, ticks):
                should_finalize = True
            else:
                metadata_path = self._metadata_from_events(events)
                if metadata_path is None or not metadata_path.is_file():
                    return {
                        "ok": False,
                        "request_id": request_id,
                        "state": latest.get("state"),
                        "blocked_reason": "request_metadata_missing",
                    }
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    cancel_path = Path(str(metadata["cancel_path"]))
                    write_json_0600(cancel_path, {
                        "request_id": request_id,
                        "reason": reason[:300],
                        "requested_at": _utcnow(),
                    })
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    return {
                        "ok": False,
                        "request_id": request_id,
                        "state": latest.get("state"),
                        "blocked_reason": f"cancel_marker_failed:{exc}"[:500],
                    }
                event = self._append_event({
                    "request_id": request_id,
                    "task_id": latest.get("task_id"),
                    "runner": latest.get("runner"),
                    "topic": latest.get("topic"),
                    "adapter_id": latest.get("adapter_id"),
                    "state": "cancel_requested",
                    "pid": pid,
                    "pid_start_ticks": ticks,
                    "reason": reason[:300],
                    "requested_at": _utcnow(),
                    "stdout_path": latest.get("stdout_path"),
                    "stderr_path": latest.get("stderr_path"),
                    "metadata_path": str(metadata_path),
                    "supervisor_status_path": metadata.get("supervisor_status_path"),
                    "cancel_path": str(cancel_path),
                })
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    should_finalize = True

        if should_finalize:
            self._finalize_isolated_request(request_id)
            refreshed = self.status(request_id)
            return {
                "ok": bool(refreshed.get("ok")),
                "request_id": request_id,
                "state": refreshed.get("state"),
                "blocked_reason": "supervisor_not_alive",
            }
        return {"ok": True, "request_id": request_id, "state": event["state"]}

    def accept_review(self, request_id: str, task_id: str) -> dict[str, Any]:
        """Coordinator/write-gated acceptance of one ``review_ready`` request.

        This is Phase 2 of the review-first lifecycle: ``_finalize_isolated_request``
        already retained the isolated worktree and recorded every check's
        evidence on the canonical card's ``terminal_review`` (changed paths +
        hashes, required outputs, validation, the worker MCP gate, and the
        exact request/task/runner/topic identity). Nothing before this method
        may ever touch the canonical repo for a review-first request.  Every
        precondition below is required; any failure leaves the canonical repo
        untouched and the task in ``review`` with the reason returned as
        ``error``.  A retry after this method already promoted and finished
        the exact same request returns ``already_accepted`` instead of
        re-promoting or re-validating anything.
        """
        with self._registry_lock():
            events = self._request_events(request_id)
            if not events:
                return {"ok": False, "error": "request_not_found", "request_id": request_id}
            latest = events[-1]
            if str(latest.get("task_id") or "") != task_id:
                return {
                    "ok": False,
                    "error": "request_task_identity_mismatch",
                    "request_id": request_id,
                    "task_id": task_id,
                }
            runner = str(latest.get("runner") or "")
            topic = str(latest.get("topic") or "")
            if not runner or not topic:
                return {
                    "ok": False,
                    "error": "request_identity_incomplete",
                    "request_id": request_id,
                    "task_id": task_id,
                }

            try:
                card = _parse_card(self._show_task(task_id), task_id)
            except LaunchRejected as exc:
                return {
                    "ok": False,
                    "error": f"task_lookup_failed:{exc}",
                    "request_id": request_id,
                    "task_id": task_id,
                }

            canonical = _canonical_task_status(card)
            if canonical == "finished":
                already = str(card.get("accepted_request_id") or "") == request_id
                return {
                    "ok": already,
                    "already_accepted": already,
                    "request_id": request_id,
                    "task_id": task_id,
                    "error": "" if already else "task_already_finished_by_other_request",
                }
            if canonical != "review":
                return {
                    "ok": False,
                    "error": f"task_not_in_review:{canonical}",
                    "request_id": request_id,
                    "task_id": task_id,
                }
            if card.get("runner") != runner or card.get("topic") != topic:
                return {
                    "ok": False,
                    "error": "task_identity_mismatch",
                    "request_id": request_id,
                    "task_id": task_id,
                }
            if card.get("claimed_by") != runner:
                return {
                    "ok": False,
                    "error": "claim_ownership_mismatch",
                    "request_id": request_id,
                    "task_id": task_id,
                }

            terminal_review = card.get("terminal_review") or {}
            if str(terminal_review.get("substatus") or "") != "review_ready":
                return {
                    "ok": False,
                    "error": (
                        "terminal_substatus_not_review_ready:"
                        + str(terminal_review.get("substatus") or "")
                    ),
                    "request_id": request_id,
                    "task_id": task_id,
                }
            deterministic = card.get("deterministic_verification") or {}
            if deterministic.get("applicable") and not deterministic.get("pass"):
                return {
                    "ok": False,
                    "error": "deterministic_verification_failed",
                    "request_id": request_id,
                    "task_id": task_id,
                }

            evidence = terminal_review.get("evidence") or {}
            request_identity = evidence.get("request_identity") or {}
            if (
                str(request_identity.get("request_id") or "") != request_id
                or str(request_identity.get("task_id") or "") != task_id
                or str(request_identity.get("runner") or "") != runner
                or str(request_identity.get("topic") or "") != topic
            ):
                return {
                    "ok": False,
                    "error": "evidence_request_identity_mismatch",
                    "request_id": request_id,
                    "task_id": task_id,
                }

            workspace_meta = evidence.get("workspace")
            if not isinstance(workspace_meta, dict):
                return {
                    "ok": False,
                    "error": "evidence_workspace_missing",
                    "request_id": request_id,
                    "task_id": task_id,
                }
            try:
                workspace = WorkerWorkspace.from_metadata(workspace_meta)
            except (KeyError, TypeError, ValueError) as exc:
                return {
                    "ok": False,
                    "error": f"evidence_workspace_invalid:{exc}",
                    "request_id": request_id,
                    "task_id": task_id,
                }
            if workspace.repo != self.repo or workspace.request_id != request_id:
                return {
                    "ok": False,
                    "error": "workspace_identity_mismatch",
                    "request_id": request_id,
                    "task_id": task_id,
                }
            try:
                assert_gc_safe_workspace_shape(request_id, workspace.path, workspace.home)
            except WorkspaceError as exc:
                return {
                    "ok": False,
                    "error": f"unsafe_workspace_shape:{exc}",
                    "request_id": request_id,
                    "task_id": task_id,
                }
            if workspace.path.is_symlink() or not workspace.path.is_dir():
                return {
                    "ok": False,
                    "error": "workspace_missing",
                    "request_id": request_id,
                    "task_id": task_id,
                }

            stored_hashes = evidence.get("changed_path_hashes")
            if not isinstance(stored_hashes, dict) or not stored_hashes:
                return {
                    "ok": False,
                    "error": "evidence_changed_hashes_missing",
                    "request_id": request_id,
                    "task_id": task_id,
                }

            # B919: fail closed before copying any output whenever a declared
            # immutable/dependency input has drifted since the claim-time
            # snapshot (B914 -- a retained-worktree validation had passed
            # against a stale dependency population). Untouched when a task
            # declares no ``immutable_inputs`` at all.
            declared_immutable_inputs = [
                str(p) for p in (evidence.get("immutable_inputs") or [])
            ]
            if declared_immutable_inputs:
                stored_input_manifest = evidence.get("immutable_input_manifest")
                if not isinstance(stored_input_manifest, dict):
                    return {
                        "ok": False,
                        "error": "stale_input:dependency_manifest_missing",
                        "request_id": request_id,
                        "task_id": task_id,
                    }
                current_input_manifest = _path_manifest(self.repo, declared_immutable_inputs)
                changed_inputs = sorted(
                    relative
                    for relative in declared_immutable_inputs
                    if current_input_manifest.get(relative) != stored_input_manifest.get(relative)
                )
                if changed_inputs:
                    return {
                        "ok": False,
                        "error": (
                            "stale_input:dependency_changed:" + ",".join(changed_inputs)
                        )[:500],
                        "request_id": request_id,
                        "task_id": task_id,
                    }

            if not core.writes_allowed():
                return {
                    "ok": False,
                    "error": "write_gate_closed",
                    "request_id": request_id,
                    "task_id": task_id,
                }

            try:
                changed = enforce_scope(workspace)
                required_output_records = validate_required_outputs(
                    workspace,
                    card.get("required_outputs") or [],
                    allow_empty=tuple(card.get("allow_empty_required_outputs") or []),
                    allow_unchanged=tuple(card.get("allow_unchanged_required_outputs") or []),
                )
                validated_required_paths = {
                    rec["path"]
                    for rec in required_output_records
                    if not rec.get("unchanged_allowed")
                }
                changed = sorted(set(changed) | validated_required_paths)
                if not changed:
                    raise WorkspaceError("no_effect")
                worker_mcp_gate = evidence.get("worker_mcp_gate")
                if (
                    isinstance(worker_mcp_gate, dict)
                    and worker_mcp_gate.get("gated")
                    and not worker_mcp_gate.get("satisfied", True)
                ):
                    raise WorkspaceError(
                        "validation_required_aiworkhub_mcp_call_missing:"
                        + str(worker_mcp_gate.get("reason") or "")
                    )
                quality_gate = quality_evidence.run_completion_quality_gate(
                    workspace.path, changed_paths=changed
                )
                if not quality_gate.get("passed"):
                    blockers = quality_gate.get("blocking_checks") or []
                    reason = quality_gate.get("config_error") or ",".join(str(v) for v in blockers)
                    raise WorkspaceError("quality_gate_failed:" + str(reason)[:400])
                validations = run_validations(workspace, card.get("validation") or [])
                current_hashes = _changed_path_hashes(workspace, changed)
                if set(current_hashes) != set(stored_hashes) or any(
                    current_hashes[relative] != stored_hashes.get(relative)
                    for relative in current_hashes
                ):
                    raise WorkspaceError("stored_hash_mismatch")
            except WorkspaceError as exc:
                return {
                    "ok": False,
                    "error": f"revalidation_failed:{exc}",
                    "request_id": request_id,
                    "task_id": task_id,
                }

            promoted = promote(workspace, changed)

            accept_result = task_engine.accept_review(
                self.repo,
                task_id,
                runner=runner,
                topic=topic,
                request_id=request_id,
                evidence={
                    "promoted_paths": promoted,
                    "validation": validations,
                    "required_outputs": required_output_records,
                    "quality_gate": quality_gate,
                },
            )
            if not accept_result.get("ok"):
                return {
                    "ok": False,
                    "error": (
                        "promotion_finalize_failed:"
                        + str(accept_result.get("stderr") or accept_result.get("stdout") or "")
                    )[:500],
                    "request_id": request_id,
                    "task_id": task_id,
                    "promoted_paths": promoted,
                }

            try:
                cleanup_workspace(workspace.repo, workspace.path, workspace.home)
            except WorkspaceError as exc:
                self._append_event({
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": topic,
                    "adapter_id": latest.get("adapter_id"),
                    "state": "accepted",
                    "accepted": True,
                    "promoted_paths": promoted,
                    "workspace_retained": True,
                    "cleanup_error": str(exc)[:500],
                    "finished_at": _utcnow(),
                })
                return {
                    "ok": True,
                    "request_id": request_id,
                    "task_id": task_id,
                    "promoted_paths": promoted,
                    "cleanup_error": str(exc)[:500],
                }

            self._append_event({
                "request_id": request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": topic,
                "adapter_id": latest.get("adapter_id"),
                "state": "accepted",
                "accepted": True,
                "promoted_paths": promoted,
                "workspace_retained": False,
                "finished_at": _utcnow(),
            })
            return {
                "ok": True,
                "request_id": request_id,
                "task_id": task_id,
                "promoted_paths": promoted,
            }

    def list_processes(self, limit: int = 100) -> dict[str, Any]:
        self._reconcile_persisted_requests()
        latest = list(self._latest_by_request().values())
        latest.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
        rows = latest[: max(1, min(limit, 1000))]
        for row in rows:
            liveness = self._liveness_snapshot(row)
            if liveness:
                row["liveness"] = liveness
        return {
            "ok": True,
            "launch_implemented": LAUNCH_IMPLEMENTED,
            "launch_enabled": launch_gates_open(),
            "active_in_memory": self._active_count(),
            "concurrency_limit": _configured_limit(),
            "total_requests": len(latest),
            "processes": rows,
        }


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _pid_start_ticks(pid: int) -> int | None:
    """Read Linux proc starttime so a recycled PID cannot be cancelled."""
    if pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        _head, separator, tail = raw.rpartition(")")
        if not separator:
            return None
        fields = tail.strip().split()
        # tail starts at field 3 (state); starttime is field 22.
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _pid_matches(pid: int, expected_start_ticks: Any) -> bool:
    if not _pid_alive(pid):
        return False
    if expected_start_ticks in (None, ""):
        return True
    try:
        expected = int(expected_start_ticks)
    except (TypeError, ValueError):
        return False
    return _pid_start_ticks(pid) == expected


def _canonical_task_status(card: dict[str, Any]) -> str:
    """Return lifecycle status while preserving archived as a distinct gate."""
    if str(card.get("archived_at") or "").strip():
        return "archived"
    return core._lifecycle_state(card)


def _process_proven_dead(pid: int, ticks: Any) -> bool:
    """Fail closed: a live PID with unknown start ticks is not proven dead."""
    if pid <= 0:
        return True
    if not _pid_alive(pid):
        return True
    if ticks in (None, ""):
        return False
    try:
        expected = int(ticks)
    except (TypeError, ValueError):
        return False
    return _pid_start_ticks(pid) != expected


def _terminate_process_group(pid: int, grace_seconds: float) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


_DEFAULT_MANAGER: ProcessManager | None = None
_DEFAULT_MANAGER_LOCK = threading.Lock()


def default_manager() -> ProcessManager:
    global _DEFAULT_MANAGER
    with _DEFAULT_MANAGER_LOCK:
        if _DEFAULT_MANAGER is None:
            _DEFAULT_MANAGER = ProcessManager()
        return _DEFAULT_MANAGER
