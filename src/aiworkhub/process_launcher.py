"""Local, explicitly gated model-process launcher for AIWorkHub task workers.

The authoritative task queue is the selected repository's canonical
``.aiworkhub/tasking/task_queue.sqlite`` task store. This module only owns
process lifecycle evidence: start, observe, collect, timeout, and cancel. It
never selects a task by keywords and it never invokes a shell.
"""

from __future__ import annotations

import ast
import fnmatch
import ctypes
import difflib
import hashlib
import hmac
import html
import inspect
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import core
from . import agent_tool_instructions
from . import attempt_artifacts
from . import claude_auth
from . import context_write_intents
from . import context_writes
from . import evidence_levels
from . import kilo_auth
from .platform_io import (
    AdvisoryLockTimeout,
    chmod_fd,
    chmod_path,
    lock_fd,
    process_is_alive,
    unlock_fd,
)
from . import quality_evidence
from . import quality_review_scope
from . import process_event_ledger
from . import provider_usage
from . import repo_policy
from . import read_efficiency
from . import runtime_temp
from . import task_engine
from . import task_fsm
from . import task_store
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
from . import quality_review
from . import quality_reviewer
from . import terminal_authority
from . import vscode_lm_bridge
from . import worker_ai_tools_mcp
# NF389: bounded, authenticated provider-call identity and provenance. These
# re-exports give the ProcessManager (and the completion gate) the exact same
# fail-closed validators the worker audit ledger uses, so spoofed or oversized
# values are rejected with a named error instead of reaching the ledger.
validate_provider_call_id = worker_ai_tools_mcp.validate_provider_call_id
validate_provenance = worker_ai_tools_mcp.validate_provenance
WorkerToolError = worker_ai_tools_mcp.WorkerToolError
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
    GitCommandTimeout,
    ValidationEnvironmentBlocked,
    ValidationRunError,
    WorkspaceError,
    cleanup_workspace,
    build_residual_contract_manifest,
    create_combined_validation_workspace,
    create_quality_review_workspace,
    create_workspace,
    enforce_scope,
    materialize_rework_overlay,
    promote,
    provision_worker_mcp_runtime,
    run_validations,
    sandbox_argv,
    select_sandbox_backend,
    sanitized_env as _base_sanitized_env,
    dispose_worker_temp,
    unlink_if_regular,
    validate_residual_contract,
    worker_temp_environment,
    VSCODE_LM_IN_PROCESS_BACKEND,
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
        *,
        repo: Path | None = None,
    ) -> Path:
        for label, candidate in (("path", path), ("home", home)):
            if not str(candidate):
                raise WorkspaceError(f"gc_workspace_{label}_missing")
            if candidate.is_symlink():
                raise WorkspaceError(f"gc_workspace_{label}_symlink")
        if path == home or path in home.parents or home in path.parents:
            raise WorkspaceError("gc_workspace_path_home_overlap")
        if (
            path.name != "worktree"
            or home.name != "home"
            or path.parent != home.parent
            or path.parent.name != request_id
        ):
            raise WorkspaceError("gc_workspace_request_id_mismatch")
        return path.parent.parent

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
                if is_unchanged and size <= 0:
                    raise WorkspaceError(f"required_output_unchanged:{relative}")
                records.append({
                    "path": relative,
                    "bytes": size,
                    "sha256": current,
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
MAX_RESEARCH_RESULT_BYTES = 32 * 1024 * 1024
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
TERMINAL_AUTHORITY_SCHEMA_ID = terminal_authority.SCHEMA_ID
TERMINAL_AUTHORITY_KEY_FILENAME = terminal_authority.KEY_FILENAME
ACTIVE_PROCESS_STATES = {"starting", "running", "cancel_requested"}
# A reviewer attempt reservation outlives the synchronous MCP handler: it is
# established before expensive preparation and must cover preparation plus
# provider spawn under one background owner. It remains a bounded pid-null
# ``starting`` reservation reconciled by the same expiry rules -- never an
# elapsed/quiet-time classification of a live provider.
QUALITY_REVIEW_ATTEMPT_RESERVATION_SECONDS = 600.0
WORKER_BRIDGE_AUTHORIZED_PROCESS_STATES = {"starting", "running"}
_PERSISTED_WATCH_UNKNOWN_MAX_CONSECUTIVE = 3
FINALIZATION_PENDING_STATES = {
    "finalizing", "release_pending", "review_pending", "reconcile_pending",
}
# The terminal process states the launcher can emit. Owned by ``task_fsm``
# (``LAUNCHER_TERMINAL_SUBSTATUSES``, a named subset of the single terminal
# vocabulary); imported here rather than restated (NF-2026-00339).
TERMINAL_PROCESS_STATES = task_fsm.LAUNCHER_TERMINAL_SUBSTATUSES


# Terminal-transition failures that prove the target card is no longer in a
# processing state this finalizer can move. When ``mark_terminal_failure``
# reports one of these the card was archived, deleted, or reclaimed, so
# retrying is futile forever: the finalizer must abandon with a named cause
# instead of re-arming on every reconcile round. Any other reason may still be
# a live processing card whose legitimate retry must survive.
_FINALIZER_CARD_NOT_PROCESSING_REASONS = (
    "not_processing",
    "not_claimed",
    "task_not_found",
    "claim_owner_mismatch",
    "runner_mismatch",
    "launch_request_mismatch",
)


def _finalizer_card_not_processing(reason: str) -> str | None:
    """Return the named cause when a failed terminal transition proves the
    target card is no longer processing (archived/gone/reclaimed), else None
    so the finalizer keeps ``reconcile_pending`` and its retry survives."""
    reason = (reason or "").strip()
    for token in _FINALIZER_CARD_NOT_PROCESSING_REASONS:
        if reason == token or reason.startswith(token + ":"):
            return token
    return None


class _BridgeCancellationDeferred(RuntimeError):
    """Bridge terminal decision could not be published; never finalize yet."""


class _PidIdentityUnknownDeferred(RuntimeError):
    """Supervisor identity is temporarily unknowable; retain active work."""


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
    if adapter_id == runtime_adapters.GROK_KILO_ADAPTER:
        # Kilo follows the XDG base-directory contract.  Point every mutable
        # Kilo surface at the same request-local HOME that the selected
        # sandbox exposes (the real workspace HOME for Landlock/AppContainer,
        # or the shared mount alias for bubblewrap).  This prevents the CLI
        # from consulting or mutating the coordinator's ambient Kilo state.
        isolated_home = Path(safe["HOME"])
        safe.update(
            {
                "XDG_DATA_HOME": str(isolated_home / ".local" / "share"),
                "XDG_CONFIG_HOME": str(isolated_home / ".config"),
                "XDG_CACHE_HOME": str(isolated_home / ".cache"),
            }
        )
    if provider_env:
        safe.update({str(key): str(value) for key, value in provider_env.items()})
    return safe


def worker_launch_env(
    adapter_id: str,
    *,
    repo: Path,
    request_id: str,
    home: Path | None = None,
    isolated_task_queue_db: bool = False,
    provider_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the sanitized worker env, then route TMPDIR/TMP/TEMP at the exact
    request-owned repository-local temp authority (NF430).

    Both real ProcessManager launch paths -- the isolated supervisor spawn and
    the direct native launch -- call this before spawning a child, so a
    worker-run pytest/tempfile lands in
    ``<repo>/.aiworkhub/temp/worker/<request_id>/tmp`` (provisioned 0700 and
    owner-stamped here) rather than the shared system temp or inside the
    candidate worktree.  Nothing else changes: the sanitized allowlist, the
    request-scoped HOME, and the explicit BYOK provider env are exactly as
    ``sanitized_env`` built them; only the three temp keys are overlaid, from
    the single declaration in ``runtime_adapters.WORKER_TEMP_ENV_VARS``.
    """
    env = sanitized_env(
        adapter_id,
        home=home,
        isolated_task_queue_db=isolated_task_queue_db,
        provider_env=provider_env,
    )
    temp_env = worker_temp_environment(repo, request_id)
    for key in runtime_adapters.WORKER_TEMP_ENV_VARS:
        env[key] = temp_env[key]
    return env


def _worker_supervisor_script() -> Path:
    sibling = Path(__file__).with_name("worker_supervisor.py")
    if sibling.is_file() and sibling.name == "worker_supervisor.py":
        return sibling
    try:
        host_script = (
            worker_ai_tools_mcp.resolve_host_package_import_root()
            / "aiworkhub"
            / "worker_supervisor.py"
        )
    except OSError:
        host_script = None
    if (
        host_script is not None
        and host_script.is_file()
        and host_script.name == "worker_supervisor.py"
    ):
        return host_script
    seen: set[str] = set()
    for entry in sys.path:
        if not entry:
            continue
        candidate = Path(entry) / "aiworkhub" / "worker_supervisor.py"
        try:
            resolved = str(candidate.resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.is_file() and candidate.name == "worker_supervisor.py":
            return candidate
    raise LaunchRejected("worker_supervisor_script_missing")


def _vscode_lm_worker_env(
    provider_env: dict[str, str] | None,
    package_import_root: Path,
) -> dict[str, str]:
    """Return the minimal environment needed by a packaged VS Code LM worker.

    The worker runs as ``python -m aiworkhub.vscode_lm_worker`` from an
    isolated repository worktree, so the repository cwd cannot make the
    installed/bundled ``aiworkhub`` package importable.  Reuse the exact
    package root already resolved and sandbox-rewritten for the worker MCP
    runtime instead of depending on a developer checkout or global install.
    """
    env = dict(provider_env or {})
    env[worker_ai_tools_mcp.ENV_PYTHONPATH] = str(package_import_root)
    return env


# Backwards-compatible private alias retained for installed integrations and
# tests that predate the generic VS Code Auth Model Broker.
_glm_vscode_worker_env = _vscode_lm_worker_env

_VSCODE_LM_IN_PROCESS_ADAPTERS = frozenset(
    {
        runtime_adapters.VSCODE_LM_ADAPTER,
        runtime_adapters.GLM_VSCODE_LM_ADAPTER,
        runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER,
    }
)


def _sandbox_backend_for_adapter(adapter_id: str) -> str:
    """Resolve the execution boundary for this exact adapter.

    Native CLI adapters still require the host OS sandbox. VS Code LM routes
    execute the provider/model inside the editor host and launch only the
    bounded response-applier subprocess, whose complete output is validated
    before its first workspace write.
    """
    if adapter_id in _VSCODE_LM_IN_PROCESS_ADAPTERS:
        return VSCODE_LM_IN_PROCESS_BACKEND
    return select_sandbox_backend()


def _validation_route_kwargs(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact launch-bound validation route, failing on drift.

    Finalization must not rediscover a host-native sandbox for an editor-hosted
    worker.  Conversely, a forged/stale metadata backend must never let a
    native CLI route borrow the in-process boundary.  A coordinator-authorized
    validation-only replay records ``deterministic_validation`` as its
    execution lane because no provider was launched.  That value is not a
    provider sandbox and must not be compared to the original adapter backend;
    the adapter identity still selects the validation safety boundary.
    """
    adapter_id = str(metadata.get("adapter_id") or "").strip()
    if not adapter_id:
        raise WorkspaceError("validation_route_adapter_missing")
    expected_backend = _sandbox_backend_for_adapter(adapter_id)
    recorded_backend = str(metadata.get("sandbox_backend") or "").strip()
    execution_mode = str(metadata.get("execution_mode") or "").strip()
    route: dict[str, Any] = {
        "backend": expected_backend,
        "adapter_id": adapter_id,
    }
    if execution_mode == "validation_only_replay":
        if recorded_backend and recorded_backend not in {
            expected_backend,
            "deterministic_validation",
        }:
            raise WorkspaceError(
                "validation_route_backend_mismatch:"
                f"expected={expected_backend}:recorded={recorded_backend}"
            )
        route["outer_validation_authority"] = True
        return route
    if recorded_backend and recorded_backend != expected_backend:
        raise WorkspaceError(
            "validation_route_backend_mismatch:"
            f"expected={expected_backend}:recorded={recorded_backend}"
        )
    return route


def _declared_validation_commands(authority: Mapping[str, Any]) -> list[str]:
    """Return the exact non-empty validation contract from card/metadata.

    An empty contract is authoritative: it must not resolve a route, provision
    executable scratch, or run a child process.  Keeping this decision above
    keyword-argument evaluation prevents downstream route setup from turning
    ``validation=[]`` into an operational failure before ``run_validations``
    can apply its own empty fast path.
    """
    raw = authority.get("validation")
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise WorkspaceError("validation_commands_invalid")
    commands: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise WorkspaceError("validation_command_invalid")
        commands.append(value)
    return commands


def _requires_bridge_cancellation(metadata: Mapping[str, Any]) -> bool:
    """Return whether finalization must publish a provider bridge decision.

    Coordinator-authorized validation-only replay never launches a provider
    or creates a fresh VS Code LM bridge receipt.  Skip bridge cancellation
    only when both durable facts agree; every other editor-hosted lifecycle
    remains fail-closed.
    """
    return not (
        str(metadata.get("execution_mode") or "").strip()
        == "validation_only_replay"
        and metadata.get("provider_launched") is False
    )


def _run_declared_validations(
    workspace: WorkerWorkspace,
    authority: Mapping[str, Any],
    route_metadata: Mapping[str, Any],
) -> list[dict[str, Any]]:
    commands = _declared_validation_commands(authority)
    if not commands:
        return []
    try:
        _work_kind, roles = quality_evidence.normalize_behavioral_contract(
            authority.get("work_kind"),
            commands,
            authority.get("validation_roles"),
        )
    except ValueError as exc:
        raise WorkspaceError(str(exc)) from exc

    def with_roles(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        materialized = [dict(row) for row in rows]
        if len(materialized) != len(roles):
            raise WorkspaceError("validation_receipt_count_mismatch")
        for row, role in zip(materialized, roles, strict=True):
            row["behavioral_role"] = role
        return materialized

    try:
        results = run_validations(
            workspace,
            commands,
            **_validation_route_kwargs(route_metadata),
        )
    except ValidationRunError as exc:
        rows = with_roles(exc.results)
        if isinstance(exc, ValidationEnvironmentBlocked):
            # Preserve the recoverable environment-blocked subtype so the
            # finalizer can route it away from acceptance-blocking
            # ``validation_failed`` (NF-2026-00271). Re-attaching the roles
            # must not strip ``terminal_state``/``restriction``/``recoverable``.
            exc.results = rows
            raise
        raise ValidationRunError(str(exc), rows) from exc
    return with_roles(results)


def _enforce_behavioral_gate(
    authority: Mapping[str, Any],
    validations: Iterable[Mapping[str, Any]],
    quality_gate: dict[str, Any],
) -> dict[str, Any]:
    gate = quality_evidence.evaluate_behavioral_gate(authority, validations)
    quality_gate["behavioral_gate"] = gate
    if gate.get("applicable") and not gate.get("passed"):
        raise WorkspaceError(
            "behavioral_gate_failed:" + str(gate.get("reason") or "unknown")[:300]
        )
    return gate


def _is_operational_validation_failure(terminal_state: str, error: str) -> bool:
    """Classify retryable infrastructure validation failures consistently."""
    return terminal_state == "validation_failed" and (
        error.startswith("validation_exec_scratch_unavailable:")
        or error.startswith(
            "validation_failed:validation_exec_scratch_unavailable:"
        )
    )


# Narrow, explicit allowlist of the exact recoverable ``WorkspaceError`` tokens
# that are genuine environment/sandbox restrictions: the declared
# validator/interpreter (or its trusted runtime root) could not be resolved, the
# exec scratch could not be provisioned, or the sandbox backend itself could not
# be selected. These are recoverable by re-running in a corrected environment,
# so they route to the retryable ``finalize_failed`` bucket. Every other
# ``validation_*`` token -- including every security refusal (a world-writable
# or untrusted-owner executable/runtime-root, a symlink-forbidden pytest
# runtime, an unapproved or non-executable validator) -- is a deterministic
# candidate/card defect and must stay acceptance-blocking ``validation_failed``.
# Tokens are exact and colon-terminated so a broad family prefix can never
# fail-open and reclassify a security refusal as recoverable.
_VALIDATION_ENVIRONMENT_RESTRICTION_PREFIXES = (
    "validation_executable_unavailable:",
    "validation_pytest_runtime_unavailable:",
    "validation_pytest_runtime_missing_pytest:",
    "validation_exec_scratch_unavailable:",
    "unsupported_sandbox_backend:",
    "validation_unsupported_in_sandbox:",
)


def _terminal_state_for_workspace_error(exc: WorkspaceError) -> str:
    """Map a caught ``WorkspaceError`` to its truthful terminal process state.

    ``ValidationEnvironmentBlocked`` is a recoverable, supersede-free
    environment/sandbox restriction (NF-2026-00271): the candidate did not fail
    its gate, the declared validator could not run here. It must never surface
    as the acceptance-blocking ``validation_failed``; route it to the retryable
    ``finalize_failed`` bucket so ``retry_finalization`` can re-run deterministic
    validation without relaunching the provider, while ``error`` keeps the exact
    restriction.

    Genuine gate failures are class-based: a plain ``ValidationRunError`` (or a
    ``required_output``/``quality_gate``/``behavioral_gate``/``residual_contract``
    /``research_result`` ``WorkspaceError``) stays ``validation_failed``. The
    only plain ``WorkspaceError`` tokens that become ``finalize_failed`` are the
    exact recoverable environment/sandbox restrictions enumerated in
    ``_VALIDATION_ENVIRONMENT_RESTRICTION_PREFIXES`` (executable/pytest-runtime
    resolution, exec-scratch provisioning, and sandbox-backend selection). The
    allowlist is colon-terminated and exact: a security refusal (a world-writable
    or untrusted-owner executable/runtime-root, a symlink-forbidden pytest
    runtime, an unapproved or non-executable validator) and every remaining
    ``validation_``-prefixed token -- plus the legacy ``invalid_validation_command``
    spelling -- are deterministic candidate/card defects (a malformed declared
    command, a contract-shape violation, a receipt count mismatch, an over-limit
    command list, or an unresolvable ``cwd``/``PYTHONPATH``) and must stay
    acceptance-blocking so a provider-free retry loop can never re-run a defect
    the candidate itself authored.
    """
    if isinstance(exc, ValidationEnvironmentBlocked):
        return "finalize_failed"
    if isinstance(exc, ValidationRunError):
        return "validation_failed"
    error = str(exc)
    if error.startswith("scope_violation") or error.startswith("symlink_output"):
        return "scope_rejected"
    if error.startswith((
        "required_output", "quality_gate", "behavioral_gate",
        "residual_contract", "research_result",
    )):
        return "validation_failed"
    if error.startswith(("parent_changed", "promotion_scope")):
        return "promotion_conflict"
    if error.startswith(_VALIDATION_ENVIRONMENT_RESTRICTION_PREFIXES):
        return "finalize_failed"
    if error.startswith("validation_") or error.startswith(
        "invalid_validation_command"
    ):
        return "validation_failed"
    return "finalize_failed"

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
# Bounded threshold past which a preparation heartbeat that stops advancing is
# treated as a stall. Before the provider process exists a launch has no pid to
# read liveness from; the preparation heartbeat epoch it republishes as it
# advances through phases is the only progress signal. When it stops advancing
# the launch is stuck and must fail with a named reason instead of sitting in a
# pid-null reservation until the reservation window merely expires.
PREPARATION_STALL_ENV = "AIWORKHUB_PREPARATION_STALL_SECONDS"
DEFAULT_PREPARATION_STALL_SECONDS = 180.0
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


def preparation_stall_seconds() -> float:
    return _bounded_float_env(
        PREPARATION_STALL_ENV, DEFAULT_PREPARATION_STALL_SECONDS, minimum=30.0, maximum=3600.0
    )


def _meaningful_progress_sequence(status: dict[str, Any]) -> Any:
    """Read the new semantic sequence with an old-status fallback."""

    return status.get(
        "last_meaningful_progress_sequence",
        status.get("last_progress_sequence"),
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


def derive_preparation_stall(
    *,
    now_epoch: float,
    preparation_heartbeat_epoch: float | None,
    preparation_phase: str | None = None,
    stall_seconds: float | None = None,
) -> dict[str, Any]:
    """Pure detection of a frozen preparation heartbeat -- WITHOUT any pid.

    A launch that never spawned a process has no pid, so liveness here comes
    only from the preparation heartbeat epoch the launcher republishes as it
    advances. When that epoch stops advancing past the bounded stall threshold
    the launch is stuck; the returned ``reason`` names the stall and the exact
    frozen phase so recovery does not depend on someone cancelling pid-null
    reservations by hand. A heartbeat that has not landed yet (``None``) is not
    a stall -- there is nothing that could have stopped advancing.
    """

    threshold = (
        stall_seconds if stall_seconds is not None else preparation_stall_seconds()
    )
    age = (
        max(0.0, now_epoch - float(preparation_heartbeat_epoch))
        if preparation_heartbeat_epoch is not None
        else None
    )
    stalled = age is not None and age > threshold
    phase = str(preparation_phase or "unknown_preparation_phase")
    return {
        "preparation_stalled": stalled,
        "preparation_heartbeat_age_seconds": age,
        "preparation_stall_seconds": threshold,
        "preparation_phase": phase,
        "reason": (
            f"preparation_heartbeat_stalled:{phase}:age={age:.1f}s>{threshold:.0f}s"
            if stalled
            else ""
        ),
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
    if os.name != "nt":
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


_terminal_authority_signing_material = terminal_authority.signing_material
_load_or_create_terminal_authority_key = terminal_authority.load_or_create_key
_write_terminal_authority_grant = terminal_authority.write_grant
_read_terminal_authority_grant = terminal_authority.read_grant


class LaunchRejected(RuntimeError):
    """A bounded, user-visible preflight rejection."""


class _QualityReviewFinalized(RuntimeError):
    """Internal control signal: reviewer evidence reached canonical review."""


class _ReviewerReservationTerminalized(RuntimeError):
    """Internal control signal: a reserved reviewer attempt was terminalized.

    Raised by the ownership-aware ``_launch_isolated`` checkpoints when a stale
    pre-provider owner discovers its exact ``starting`` reservation was already
    terminalized by the bounded launch owner or reconcile.  It must never be
    raised once a real provider process exists.
    """


class PromotionVersionRegression(RuntimeError):
    """Promotion would move a version constant/projection BACKWARDS (NF-2026-00315).

    Raised at the promotion boundary when a candidate -- typically one whose
    worktree was cut BEFORE a release landed -- still carries an OLDER version
    constant or release-metadata projection than canonical.  Promoting it would
    silently revert ``src/aiworkhub/_version.py`` and every projection derived
    from it (the extension's ``EXPECTED_MCP_PACKAGE_VERSION`` runtime-compat
    check, ``vscode-extension/package.json``/``package-lock.json``), which would
    break every extension preflight.  The message names each offending file with
    both its candidate value and the canonical value it would clobber.
    """


PROMOTION_VERSION_REGRESSION_SCHEMA_ID = "aiworkhub.promotion_version_regression.v1"

VERSION_ORDER_REGRESSED = "regressed"
VERSION_ORDER_EQUAL = "equal"
VERSION_ORDER_AHEAD = "ahead"
VERSION_ORDER_UNVERIFIABLE = "unverifiable"

# Semantic-version core: ``MAJOR.MINOR.PATCH`` with an optional pre-release/build
# suffix, matching ``scripts/release_metadata.py``'s ``VALID_VERSION`` shape.
_RELEASE_VERSION_CORE_RE = re.compile(
    r"^\s*v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+][0-9A-Za-z.-]+)?\s*$"
)


def _release_version_tuple(value: Any) -> tuple[int, int, int] | None:
    match = _RELEASE_VERSION_CORE_RE.match(str(value or ""))
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _promotion_version_fields(projection: Mapping[str, Any]) -> tuple[str, str, str]:
    file = str(projection.get("file") or projection.get("path") or "")
    candidate = projection.get("candidate_version", projection.get("candidate"))
    canonical = projection.get("canonical_version", projection.get("canonical"))
    return (
        file,
        "" if candidate is None else str(candidate),
        "" if canonical is None else str(canonical),
    )


def evaluate_promotion_version_regression(
    projections: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify each release-version projection a promotion is about to write.

    For every ``{file, candidate_version, canonical_version}`` projection the
    candidate value the promotion would WRITE is compared against the canonical
    value already on disk:

    * candidate BEHIND canonical  -> ``regressed`` (a named refusal reason);
    * candidate EQUAL to canonical -> ``equal`` (the normal case -- silent, no
      reason, nothing to refuse);
    * candidate AHEAD of canonical -> ``ahead`` (the release moved forward; the
      write is allowed);
    * either value unparseable -> ``unverifiable`` (fail-closed: a promotion
      that cannot prove it is not a regression is refused, naming the file).

    The returned record lists every regressed/unverifiable projection with both
    values, so the caller can refuse and name exactly which file, which
    candidate value and which canonical value.
    """

    checked: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    for projection in projections or ():
        if not isinstance(projection, Mapping):
            continue
        file, candidate, canonical = _promotion_version_fields(projection)
        cand_tuple = _release_version_tuple(candidate)
        canon_tuple = _release_version_tuple(canonical)
        if cand_tuple is None or canon_tuple is None:
            order = VERSION_ORDER_UNVERIFIABLE
            reason = (
                f"promotion_version_unverifiable:{file}:candidate={candidate!r}:"
                f"canonical={canonical!r} -- a release version could not be "
                "parsed, so the promotion cannot prove it is not a regression"
            )
        elif cand_tuple < canon_tuple:
            order = VERSION_ORDER_REGRESSED
            reason = (
                f"promotion_version_regression:{file}:candidate={candidate}:"
                f"canonical={canonical} -- promoting would move this version "
                "BACKWARDS relative to canonical and silently revert the release"
            )
        elif cand_tuple == canon_tuple:
            order = VERSION_ORDER_EQUAL
            reason = ""
        else:
            order = VERSION_ORDER_AHEAD
            reason = ""
        record = {
            "file": file,
            "candidate_version": candidate,
            "canonical_version": canonical,
            "order": order,
            "reason": reason,
        }
        checked.append(record)
        if order in (VERSION_ORDER_REGRESSED, VERSION_ORDER_UNVERIFIABLE):
            regressions.append(record)
    return {
        "schema_id": PROMOTION_VERSION_REGRESSION_SCHEMA_ID,
        "ok": not regressions,
        "refused": bool(regressions),
        "checked": checked,
        "regressions": regressions,
        "reasons": [record["reason"] for record in regressions],
    }


def refuse_version_regression(
    projections: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Promotion-boundary guard: raise when any version projection regresses.

    This is the check NF-2026-00315 was missing.  It belongs at the promotion
    boundary -- where both the candidate value about to be written and the
    canonical value already on disk are visible -- not in the worker, which
    cannot know what landed while it was running, and not in the card's
    ``forbidden`` list, which already barred those files and did not help
    because the reversion comes from a stale BASE, not from an edit.

    When the candidate equals canonical (the normal case) it is silent and
    returns the evaluation; when the candidate is ahead it allows the write;
    when any projection moves backwards (or cannot be verified) it raises
    :class:`PromotionVersionRegression`, whose message names every offending
    file with its candidate and canonical value -- a refusal, not a logged
    warning.
    """

    report = evaluate_promotion_version_regression(projections)
    if report["refused"]:
        raise PromotionVersionRegression("; ".join(report["reasons"]))
    return report


# ---------------------------------------------------------------------------
# Promotion-boundary version projections (NF-2026-00315).
#
# ``refuse_version_regression`` compares candidate-vs-canonical version values,
# but that comparison is only possible once the values are READ off disk at the
# promotion boundary: the candidate value about to be written lives in the
# candidate worktree, the canonical value already on disk lives in the manager
# repository.  These helpers extract each recognised release-version file's
# value from raw file bytes and assemble the projections the guard classifies,
# so a stale-base candidate still carrying an OLDER version cannot silently
# revert the release when promoted.
# ---------------------------------------------------------------------------
_VERSION_PROJECTION_FILES: tuple[str, ...] = (
    "src/aiworkhub/_version.py",
    "vscode-extension/extension.js",
    "vscode-extension/package.json",
    "vscode-extension/package-lock.json",
)
_VERSION_PY_LITERAL_RE = re.compile(
    r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE
)
_RUNTIME_LITERAL_RE = re.compile(
    r'EXPECTED_MCP_PACKAGE_VERSION\s*=\s*["\']([^"\']+)["\']'
)


def _extract_projection_version(relative: str, text: str) -> str | None:
    """Read one recognised release-version file's value from its raw bytes."""

    if relative == "src/aiworkhub/_version.py":
        match = _VERSION_PY_LITERAL_RE.search(text)
        return match.group(1) if match else None
    if relative == "vscode-extension/extension.js":
        match = _RUNTIME_LITERAL_RE.search(text)
        return match.group(1) if match else None
    if relative in (
        "vscode-extension/package.json",
        "vscode-extension/package-lock.json",
    ):
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(data, Mapping):
            return None
        value = data.get("version")
        return None if value is None else str(value)
    return None


def _promotion_version_projections(
    repo_root: Path, workspace_root: Path, changed: Iterable[str]
) -> list[dict[str, Any]]:
    """Build ``{file, candidate_version, canonical_version}`` projections.

    Only the files a promotion is ABOUT to write (``changed``) are inspected,
    and only those that are recognised release-version files whose CANONICAL
    copy currently carries a version -- so there is a release value on disk that
    a regression could revert.  The candidate value is read from the candidate
    worktree (the exact bytes ``promote`` would write); an unreadable or
    unparseable candidate value for such a file is left empty so the guard fails
    closed (``unverifiable``) rather than promoting a version it cannot verify.
    """

    repo_root = Path(repo_root)
    workspace_root = Path(workspace_root)
    projections: list[dict[str, Any]] = []
    for raw in changed or ():
        relative = str(raw).replace("\\", "/")
        if relative not in _VERSION_PROJECTION_FILES:
            continue
        try:
            canonical_text = (repo_root / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            # Canonical carries no such file: no release value on disk for a
            # promotion to move backwards, so there is nothing to guard here.
            continue
        canonical_version = _extract_projection_version(relative, canonical_text)
        if canonical_version is None:
            continue
        try:
            candidate_text = (workspace_root / relative).read_text(encoding="utf-8")
            candidate_version = _extract_projection_version(relative, candidate_text)
        except (OSError, UnicodeError):
            candidate_version = None
        projections.append(
            {
                "file": relative,
                "candidate_version": (
                    "" if candidate_version is None else candidate_version
                ),
                "canonical_version": canonical_version,
            }
        )
    return projections


# ---------------------------------------------------------------------------
# Candidate reachability inputs (NF-2026-00304).
#
# The reachability gate in ``quality_evidence.run_completion_quality_gate``
# reports every candidate addition no recognised entry point can reach.  It
# needs three things Source Graph already carries: the symbols the candidate
# defines in its changed files, the call/reference edges among them, and the
# recognised entry points.  These helpers read them from the CANDIDATE's own
# Source Graph index (the worker built it while running) -- the only index that
# carries edges INTO the candidate's new symbols -- and normalise everything to
# short symbol names so an unresolved edge target still matches its definition.
# Everything is best-effort and never raises: reachability is a non-blocking
# observation and must never break a promotion.
# ---------------------------------------------------------------------------
_REACHABILITY_MAX_EDGE_ROWS = 200_000


def _candidate_changed_symbols(
    workspace_root: Path, changed_py: Iterable[str]
) -> list[dict[str, Any]]:
    """Enumerate the symbols the candidate defines in its changed Python files.

    This is symbol ENUMERATION over the candidate source (top-level functions,
    classes and their methods), not a call-graph analyser: the call/reference
    edges that decide reachability come from Source Graph.
    """

    symbols: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _add(name: str, relative: str) -> None:
        key = (relative, name)
        if name and key not in seen:
            seen.add(key)
            symbols.append(
                {"symbol": name, "file": relative, "change": quality_review.CHANGE_MODIFIED}
            )

    for relative in changed_py:
        try:
            source = (workspace_root / relative).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, UnicodeError, SyntaxError, ValueError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _add(node.name, relative)
            elif isinstance(node, ast.ClassDef):
                _add(node.name, relative)
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        _add(sub.name, relative)
    return symbols


def _read_candidate_short_name_edges(
    db_path: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]]] | None:
    """Read call/reference edges from the candidate Source Graph, short-named.

    Returns ``(call_edges, reference_edges)`` or ``None`` when the index cannot
    be read.  Endpoints are reduced to their short symbol name so an unresolved
    edge target (recorded only as ``dst_name``) still matches a candidate
    definition; short-name collisions bias toward reporting a symbol as reached
    (quiet), never toward a false unreachable finding that would cry wolf.
    """

    try:
        connection = sqlite3.connect(
            f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True
        )
    except (OSError, sqlite3.Error):
        return None
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT kind, src_qualname, dst_qualname, dst_name FROM edges LIMIT ?",
            (_REACHABILITY_MAX_EDGE_ROWS,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()

    call_edges: list[dict[str, str]] = []
    reference_edges: list[dict[str, str]] = []
    for kind, src_qualname, dst_qualname, dst_name in rows:
        src = str(src_qualname or "").rsplit(".", 1)[-1]
        dst = str(dst_name or dst_qualname or "").rsplit(".", 1)[-1]
        if not src or not dst:
            continue
        edge = {"src": src, "dst": dst}
        if str(kind or "") == "call":
            call_edges.append(edge)
        else:
            reference_edges.append(edge)
    return call_edges, reference_edges


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _legacy_timeout_fields(timeout_seconds: int) -> dict[str, Any]:
    return {
        "timeout_seconds": int(timeout_seconds),
        "timeout_enforced": False,
    }


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
    if path.is_symlink() or not path.is_file():
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


def _bounded_launch_diagnostic(
    exc: BaseException,
    *,
    phase: str,
    repo: Path,
) -> dict[str, str]:
    """Return bounded coordinator traceback evidence for a pre-supervisor bug."""

    rendered = "".join(
        traceback.TracebackException.from_exception(exc, limit=12).format()
    )
    for raw, replacement in (
        (str(repo), "<repo>"),
        (str(Path.home()), "<home>"),
    ):
        if raw:
            rendered = rendered.replace(raw, replacement)
    return {
        "phase": str(phase or "unknown")[:120],
        "exception_type": type(exc).__name__[:120],
        "message": str(exc)[:500],
        "traceback": rendered[-4000:],
    }


def _declared_failure_denominators(metadata: dict[str, Any]) -> dict[str, Any]:
    """Preserve gates a worker never reached without inventing results."""
    validations: list[dict[str, Any]] = []
    for raw in list(metadata.get("validation") or []):
        command: Any = list(raw) if isinstance(raw, (list, tuple)) else str(raw)
        validations.append(
            {
                "command": command,
                "returncode": None,
                "not_run": True,
                "reason": "worker_terminal_before_validation",
            }
        )
    required_outputs = [
        {
            "pattern": str(raw),
            "path": str(raw),
            "bytes": None,
            "sha256": "",
            "missing": True,
            "reason": "worker_terminal_before_output_validation",
        }
        for raw in list(metadata.get("required_outputs") or [])
    ]
    return {"validation": validations, "required_outputs": required_outputs}


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
    chmod_path(path.parent, 0o700)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        chmod_fd(fd, 0o600)
    finally:
        os.close(fd)


def _worker_launch_cwd(
    workspace_path: Path,
    *,
    platform_name: str | None = None,
) -> str:
    """Return a real task directory on Windows while preserving POSIX root."""

    if (platform_name or os.name) != "nt":
        return "/"
    resolved = workspace_path.resolve()
    if not resolved.is_dir():
        raise LaunchRejected(f"windows_launch_cwd_unavailable:{resolved}")
    return str(resolved)


def _validation_only_replay_authorization(
    card: Mapping[str, Any], task_id: str
) -> dict[str, Any] | None:
    """Return one exact provider-free replay grant or fail closed.

    The task store mints this coordinator-only grant while recovering a
    blocked task.  Merely finding a similarly named field must never select
    the deterministic lane: every immutable episode binding is checked before
    a claim, workspace mutation, credential lookup, or provider operation.
    Output bytes are checked again by ``validate_required_outputs`` inside the
    ordinary finalizer, so this routing check cannot authorize stale content.
    """

    raw = card.get("validation_only_replay_authorization")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LaunchRejected("validation_only_replay_authorization_invalid")
    if raw.get("one_episode_binding") is not True:
        raise LaunchRejected("validation_only_replay_episode_binding_missing")
    if str(raw.get("task_id") or "") != task_id:
        raise LaunchRejected("validation_only_replay_task_mismatch")
    if str(raw.get("actor") or "") != core.CODEX_RUNNER:
        raise LaunchRejected("validation_only_replay_actor_mismatch")

    predecessor = card.get("rework_predecessor")
    if not isinstance(predecessor, dict):
        raise LaunchRejected("validation_only_replay_predecessor_missing")
    predecessor_request_id = str(predecessor.get("request_id") or "").strip()
    if not predecessor_request_id or str(
        raw.get("predecessor_request_id") or ""
    ) != predecessor_request_id:
        raise LaunchRejected("validation_only_replay_predecessor_mismatch")
    predecessor_hashes = predecessor.get("changed_path_hashes")
    authorized_hashes = raw.get("changed_path_hashes")
    if (
        not isinstance(predecessor_hashes, dict)
        or not predecessor_hashes
        or not isinstance(authorized_hashes, dict)
        or authorized_hashes != predecessor_hashes
    ):
        raise LaunchRejected("validation_only_replay_hash_manifest_mismatch")
    if not all(
        isinstance(path, str)
        and path.strip()
        and isinstance(digest, str)
        and re.fullmatch(r"[a-f0-9]{64}", digest)
        for path, digest in authorized_hashes.items()
    ):
        raise LaunchRejected("validation_only_replay_hash_manifest_invalid")
    try:
        authorized_epoch = int(str(raw.get("next_claim_epoch")))
        claim_epoch = int(str(card.get("claim_epoch")))
    except (TypeError, ValueError):
        raise LaunchRejected("validation_only_replay_claim_epoch_invalid") from None
    if authorized_epoch != claim_epoch:
        raise LaunchRejected("validation_only_replay_claim_epoch_mismatch")
    if not list(card.get("required_outputs") or []):
        raise LaunchRejected("validation_only_replay_required_outputs_missing")
    # A replay may exist solely to re-finalize hash-pinned inherited outputs
    # after an operational finalizer failure. An explicitly empty validation
    # contract is authoritative and must not force executable scratch or a
    # provider call; required-output/hash verification still gates review.
    return dict(raw)


def _usage_from_output(
    path: Path,
    *,
    include_samples: bool = False,
) -> dict[str, Any]:
    """Extract bounded structured usage without trusting provider prose."""

    usage = provider_usage.read_provider_usage(
        path,
        include_samples=include_samples,
    )
    if include_samples:
        return usage
    # Preserve the compact historical helper contract for callers that need
    # only aggregate accounting. Durable process events explicitly request
    # samples and model evidence through ``include_samples=True``.
    return {
        key: usage[key]
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_creation_input_tokens",
            "usage_observed",
            "cache_metrics_observed",
            "cost_usd",
            "cost_observed",
        )
    }


def _provider_tool_denials_from_output(path: Path) -> dict[str, Any]:
    """Extract only bounded denial counts from provider JSON/JSONL output.

    Providers may expose ``permission_denials`` (or the camelCase/tool-denial
    equivalents) in their terminal result.  The raw payload can contain
    commands, paths or prompt fragments, so it is never persisted.  This
    parser returns only counts and a fixed allowlist of raw-discovery labels.
    Absence of a denial field means *not observed*, never proof of zero
    attempts.
    """

    result: dict[str, Any] = {
        "schema_id": "aiworkhub.provider_tool_denials.v1",
        "evidence_observed": False,
        "permission_denials_total": 0,
        "raw_discovery_denials": 0,
        "raw_discovery_labels": [],
    }
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            return result
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result

    candidates: list[Any] = []
    try:
        candidates.append(json.loads(raw))
    except json.JSONDecodeError:
        for line in raw.splitlines():
            try:
                candidates.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    denial_keys = {
        "permission_denials", "permissionDenials", "tool_denials", "toolDenials",
    }
    patterns = {
        "grep": re.compile(r"(?<![a-z0-9_])grep(?![a-z0-9_])", re.IGNORECASE),
        "glob": re.compile(r"(?<![a-z0-9_])glob(?![a-z0-9_])", re.IGNORECASE),
        "rg": re.compile(r"(?<![a-z0-9_])rg(?![a-z0-9_])", re.IGNORECASE),
        "find": re.compile(r"(?<![a-z0-9_])find(?![a-z0-9_])", re.IGNORECASE),
        "tree": re.compile(r"(?<![a-z0-9_])tree(?![a-z0-9_])", re.IGNORECASE),
    }
    labels: set[str] = set()

    def record(value: Any) -> None:
        rows = value if isinstance(value, list) else [value]
        for row in rows[:256]:
            result["permission_denials_total"] += 1
            try:
                bounded = json.dumps(row, ensure_ascii=False, sort_keys=True)[:4096]
            except (TypeError, ValueError):
                bounded = str(row)[:4096]
            matched = {name for name, pattern in patterns.items() if pattern.search(bounded)}
            if matched:
                result["raw_discovery_denials"] += 1
                labels.update(matched)

    def walk(value: Any, *, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, dict):
            for key, nested in list(value.items())[:256]:
                if key in denial_keys:
                    result["evidence_observed"] = True
                    record(nested)
                else:
                    walk(nested, depth=depth + 1)
        elif isinstance(value, list):
            for nested in value[:256]:
                walk(nested, depth=depth + 1)

    for candidate in candidates[:2048]:
        walk(candidate)
    result["raw_discovery_labels"] = sorted(labels)
    return result


def _strict_read_command_event(
    command: Any,
    output: Any,
    *,
    timestamp: float,
) -> dict[str, Any] | None:
    """Recognize a small, explicit set of shell-free-equivalent reads.

    This parser is observability-only: it never executes provider text.  It
    deliberately ignores pipelines, compound commands and ambiguous shell
    syntax instead of guessing.  POSIX shell wrappers and PowerShell
    ``-Command`` wrappers are unwrapped once, then only exact ``sed -n``,
    ``head -n``, ``cat`` and ``Get-Content`` shapes are accepted.
    """

    if not isinstance(command, str) or not command.strip():
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    executable = Path(tokens[0]).name.lower()
    if executable in {"bash", "sh", "zsh"}:
        try:
            flag_index = next(
                index for index, token in enumerate(tokens) if token in {"-c", "-lc"}
            )
            tokens = shlex.split(tokens[flag_index + 1], posix=True)
        except (StopIteration, IndexError, ValueError):
            return None
    elif executable in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        try:
            command_index = next(
                index for index, token in enumerate(tokens)
                if token.lower() in {"-command", "-c"}
            )
            tokens = shlex.split(tokens[command_index + 1], posix=True)
        except (StopIteration, IndexError, ValueError):
            return None
    path = ""
    offset: int | None = None
    limit: int | None = None
    drop_output_first_line = False
    composite_read = False
    # Codex commonly pairs an exact line-count probe with one bounded sed read
    # of the same declared path.  Recognize only that exact, side-effect-free
    # compound shape; every other compound command remains unclassified.
    if (
        len(tokens) == 8
        and Path(tokens[0]).name.lower() == "wc"
        and tokens[1] == "-l"
        and tokens[3] == "&&"
        and Path(tokens[4]).name.lower() == "sed"
        and tokens[5] == "-n"
        and tokens[2] == tokens[7]
    ):
        match = re.fullmatch(r"(\d+),(\d+)p", tokens[6])
        if not match:
            return None
        start, end = int(match.group(1)), int(match.group(2))
        if start < 1 or end < start:
            return None
        path, offset, limit = tokens[7], start, end - start + 1
        drop_output_first_line = True
        composite_read = True
    elif not tokens or any(
        token in {"|", ";", "&&", "||", ">", ">>"} for token in tokens
    ):
        return None

    range_unit = "lines"
    executable = Path(tokens[0]).name.lower()
    if composite_read:
        pass
    elif executable == "sed" and len(tokens) == 4 and tokens[1] == "-n":
        match = re.fullmatch(r"(\d+),(\d+)p", tokens[2])
        if not match:
            return None
        start, end = int(match.group(1)), int(match.group(2))
        if start < 1 or end < start:
            return None
        path, offset, limit = tokens[3], start, end - start + 1
    elif executable == "head" and len(tokens) == 4 and tokens[1] in {"-n", "--lines"}:
        try:
            limit = int(tokens[2])
        except ValueError:
            return None
        if limit <= 0:
            return None
        path, offset = tokens[3], 1
    elif executable == "cat" and len(tokens) == 2:
        path = tokens[1]
        range_unit = "file"
    elif executable in {"get-content", "gc"}:
        remaining = tokens[1:]
        if "-Path" in remaining:
            path_index = remaining.index("-Path") + 1
        elif "-LiteralPath" in remaining:
            path_index = remaining.index("-LiteralPath") + 1
        else:
            path_index = 0
        if path_index >= len(remaining):
            return None
        path = remaining[path_index]
        lowered = [token.lower() for token in remaining]
        if "-totalcount" in lowered:
            count_index = lowered.index("-totalcount") + 1
            try:
                limit = int(remaining[count_index])
            except (IndexError, ValueError):
                return None
            if limit <= 0:
                return None
            offset = 1
    else:
        return None

    if not path or path.startswith("-"):
        return None
    output_text = output if isinstance(output, str) else ""
    if drop_output_first_line:
        _line_count, separator, after_first_line = output_text.partition("\n")
        if not separator:
            return None
        output_text = after_first_line
    encoded = output_text.encode("utf-8")
    return {
        "event_type": "read",
        "path": path,
        "offset": offset,
        "limit": limit,
        "range_unit": range_unit,
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes_returned": len(encoded),
        "timestamp": timestamp,
        "classification_source": "strict_provider_command_shape",
    }


def _provider_read_efficiency_from_output(path: Path) -> dict[str, Any]:
    """Return a bounded, path-free read-efficiency summary from JSON output.

    Only explicit provider tool-use records and strict read-command shapes are
    accepted.  Raw commands, paths and contents never leave this function.
    Missing provider evidence is labelled unobserved rather than reported as
    a measured zero.
    """

    empty = {
        "schema_id": "aiworkhub.provider_read_efficiency.v2",
        "evidence_observed": False,
        "provider_records_scanned": 0,
        "recognized_read_events": 0,
        "recognized_source_graph_events": 0,
        "measurement_label": "observed_provider_events_and_bytes_only_no_token_or_cost_claim",
    }
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            return empty
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return empty

    candidates: list[Any] = []
    try:
        candidates.append(json.loads(raw))
    except json.JSONDecodeError:
        for line in raw.splitlines()[:20000]:
            try:
                candidates.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    events: list[dict[str, Any]] = []
    pending_reads: dict[str, dict[str, Any]] = {}
    read_tool_names = {"read", "read_file", "readfile", "file_read"}

    def tool_payload(node: dict[str, Any]) -> dict[str, Any]:
        for key in ("input", "arguments", "tool_input", "args"):
            value = node.get(key)
            if isinstance(value, dict):
                return value
        return {}

    def walk(node: Any, *, ordinal: int, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            node_type = str(node.get("type") or "").strip().lower()
            name = str(
                node.get("name") or node.get("tool_name") or node.get("tool") or ""
            ).strip()
            normalized_name = name.lower().replace("-", "_")
            payload = tool_payload(node)
            if node_type in {"tool_use", "tool_call", "mcp_tool_call"}:
                if str(node.get("status") or "").lower() == "in_progress":
                    return
                if "source_graph_query" in normalized_name:
                    events.append({
                        "event_type": "source_graph",
                        "source_graph_mode": str(payload.get("mode") or "")[:40],
                        "source_graph_timestamp": float(ordinal),
                    })
                elif normalized_name in read_tool_names:
                    read_path = payload.get("file_path") or payload.get("path")
                    if read_path:
                        event = {
                            "event_type": "read",
                            "path": str(read_path),
                            "offset": payload.get("offset"),
                            "limit": payload.get("limit"),
                            "timestamp": float(ordinal),
                            "classification_source": "provider_read_tool",
                        }
                        events.append(event)
                        tool_id = str(node.get("id") or node.get("tool_use_id") or "")
                        if tool_id:
                            pending_reads[tool_id] = event
            if node_type == "tool_result":
                tool_id = str(node.get("tool_use_id") or node.get("id") or "")
                pending_event = pending_reads.get(tool_id)
                content = node.get("content")
                if pending_event is not None and content is not None:
                    if isinstance(content, str):
                        result_text = content
                    else:
                        try:
                            result_text = json.dumps(
                                content, ensure_ascii=False, sort_keys=True,
                            )
                        except (TypeError, ValueError):
                            result_text = ""
                    encoded = result_text.encode("utf-8")
                    pending_event["content_sha256"] = hashlib.sha256(encoded).hexdigest()
                    pending_event["bytes_returned"] = len(encoded)
            for value in list(node.values())[:256]:
                walk(value, ordinal=ordinal, depth=depth + 1)
        elif isinstance(node, list):
            for value in node[:256]:
                walk(value, ordinal=ordinal, depth=depth + 1)

    for ordinal, candidate in enumerate(candidates[:20000]):
        if isinstance(candidate, dict):
            item = candidate.get("item")
            # Codex emits the same command twice: ``item.started`` carries an
            # empty result and ``item.completed`` carries the authoritative
            # output.  Counting both inflates reads and fabricates an unknown
            # repetition for every successful command.
            if (
                str(candidate.get("type") or "") == "item.completed"
                and isinstance(item, dict)
                and str(item.get("type") or "") == "command_execution"
            ):
                event = _strict_read_command_event(
                    item.get("command"), item.get("aggregated_output"),
                    timestamp=float(ordinal),
                )
                if event is not None:
                    events.append(event)
            walk(candidate, ordinal=ordinal)

    read_events = [event for event in events if event.get("event_type") == "read"]
    graph_events = [
        event for event in events if event.get("event_type") == "source_graph"
    ]
    report = read_efficiency.analyze_read_efficiency(
        events, correlation_window=64,
    ).to_dict()
    report.pop("events", None)
    return {
        **empty,
        **report,
        "evidence_observed": bool(read_events or graph_events),
        "provider_records_scanned": len(candidates),
        "recognized_read_events": len(read_events),
        "recognized_source_graph_events": len(graph_events),
    }


def _semantic_edit_evidence_from_output(
    path: Path,
    *,
    worker_mcp_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract bounded runtime-authored semantic-edit byte accounting.

    Only the local ``vscode_lm_worker`` result schema is accepted. Provider
    prose cannot create these fields, and byte labels are kept explicitly
    separate from token/cost claims.
    """

    empty = {
        "schema_id": "aiworkhub.semantic_edit_runtime_evidence.v1",
        "observed": False,
        "file_count": 0,
        "range_count": 0,
        "file_bytes": 0,
        "old_region_bytes": 0,
        "replacement_bytes": 0,
        "model_reemitted_old_bytes": 0,
        "token_savings_claimed": False,
    }

    def from_authenticated_ledger() -> dict[str, Any]:
        verification = (
            worker_mcp_gate.get("verification")
            if isinstance(worker_mcp_gate, dict) else None
        )
        rows = (
            verification.get("semantic_edit_apply_receipts")
            if isinstance(verification, dict) else None
        )
        if not isinstance(rows, list):
            return empty
        bounded = [row for row in rows[:128] if isinstance(row, dict)]
        if not bounded:
            return empty
        return {
            **empty,
            "observed": True,
            "file_count": len(bounded),
            "range_count": sum(int(row.get("range_count") or 0) for row in bounded),
            "file_bytes": sum(int(row.get("file_bytes") or 0) for row in bounded),
            "old_region_bytes": sum(
                int(row.get("old_region_bytes") or 0) for row in bounded
            ),
            "replacement_bytes": sum(
                int(row.get("replacement_bytes") or 0) for row in bounded
            ),
            "model_reemitted_old_bytes": sum(
                int(row.get("model_reemitted_old_bytes") or 0) for row in bounded
            ),
        }
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            return from_authenticated_ledger()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return from_authenticated_ledger()
    for raw_line in reversed(lines[-20000:]):
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(event, dict)
            or event.get("type") != "result"
            or event.get("is_error") is not False
            or event.get("edit_protocol")
            != vscode_lm_bridge.EDIT_RESPONSE_SCHEMA_ID
        ):
            continue
        rows = event.get("semantic_edit_metrics")
        if not isinstance(rows, list):
            return empty
        bounded = [row for row in rows[:128] if isinstance(row, dict)]
        return {
            **empty,
            "observed": True,
            "file_count": len(bounded),
            "range_count": sum(int(row.get("range_count") or 0) for row in bounded),
            "file_bytes": sum(int(row.get("file_bytes") or 0) for row in bounded),
            "old_region_bytes": sum(
                int(row.get("old_region_bytes") or 0) for row in bounded
            ),
            "replacement_bytes": sum(
                int(row.get("replacement_bytes") or 0) for row in bounded
            ),
            "model_reemitted_old_bytes": sum(
                int(row.get("model_reemitted_old_bytes") or 0) for row in bounded
            ),
        }
    return from_authenticated_ledger()


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


def _ledger_output_tokens(usage: dict[str, Any]) -> int:
    """Return provider-billed output including separately reported reasoning."""

    return int(usage.get("output_tokens") or 0) + int(
        usage.get("reasoning_output_tokens") or 0
    )


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


def _launch_project_context(
    repo: Path,
    card: dict[str, Any],
    quality_review_binding: dict[str, Any] | None,
) -> project_context.ProjectContextResult | None:
    """Skip the generic envelope when a reviewer already owns a bound packet."""

    if quality_review_binding is not None:
        return None
    return project_context.collect_project_context(repo, card)


def _launch_source_graph_request(
    card: dict[str, Any],
    quality_review_binding: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Reviewer Source Graph is live/on-demand, never a duplicate prefetch."""

    if quality_review_binding is not None:
        return None
    request = (card.get("project_context") or {}).get("source_graph")
    return request if isinstance(request, dict) else None


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


def _readonly_research_contract(
    *,
    task_type: Any,
    read_only: Any,
    allowed_writes: Any,
    required_outputs: Any,
) -> bool:
    """Return whether a card is the narrow no-repository-output contract.

    Task type does not create write authority.  Only a card with an explicit
    ``read_only: true`` declaration and both lists empty may use its
    authenticated provider result as evidence.  A card that declares even one
    write or required output follows the normal candidate/diff lifecycle and
    can never use textual stdout as a substitute for repository evidence.

    ``task_type`` remains in the signature for compatibility with existing
    call sites and receipts; it is intentionally not an admission gate.
    """

    allowed_is_empty = allowed_writes is None or (
        isinstance(allowed_writes, (list, tuple)) and not allowed_writes
    )
    outputs_are_empty = required_outputs is None or (
        isinstance(required_outputs, (list, tuple)) and not required_outputs
    )
    return read_only is True and allowed_is_empty and outputs_are_empty


def _metadata_is_readonly_research(
    metadata: dict[str, Any], workspace: WorkerWorkspace
) -> bool:
    context = metadata.get("project_context") or {}
    policy = context.get("task_context_policy") if isinstance(context, dict) else {}
    task_type = policy.get("task_type") if isinstance(policy, dict) else ""
    return _readonly_research_contract(
        task_type=task_type,
        read_only=metadata.get("read_only"),
        allowed_writes=workspace.allowed_writes,
        required_outputs=metadata.get("required_outputs"),
    )


def _card_is_readonly_research(card: dict[str, Any]) -> bool:
    context = card.get("project_context") or {}
    task_type = context.get("task_type") if isinstance(context, dict) else ""
    return _readonly_research_contract(
        task_type=task_type,
        read_only=card.get("read_only"),
        allowed_writes=card.get("allowed_writes"),
        required_outputs=card.get("required_outputs"),
    )


def _card_is_readonly_quality_review(card: dict[str, Any]) -> bool:
    """Return whether a card is the bound no-write reviewer contract."""

    return (
        str(card.get("topic") or "") == "quality_review"
        and _card_is_readonly_research(card)
    )


def _research_result_text(event: dict[str, Any]) -> str:
    """Extract only known provider final/assistant result text shapes."""

    event_type = str(event.get("type") or "")
    if event_type == "result":
        if event.get("is_error") is True or str(event.get("subtype") or "") == "error":
            return ""
        value = event.get("result")
        return value.strip() if isinstance(value, str) else ""
    if event_type == "item.completed":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            value = item.get("text")
            return value.strip() if isinstance(value, str) else ""
        return ""
    if event_type in {"assistant.message", "assistant_message"}:
        data = event.get("data")
        value = data.get("content") if isinstance(data, dict) else None
        return value.strip() if isinstance(value, str) else ""
    if event_type == "text":
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") != "text":
            return ""
        value = part.get("text")
        return value.strip() if isinstance(value, str) else ""
    return ""


def _strip_project_context_receipt_prefix(text: str) -> str:
    """Strip authenticated PROJECT_CONTEXT_RECEIPT prefixes per line.

    A provider may emit the whole ``PROJECT_CONTEXT_RECEIPT: {json} |
    evidence`` acknowledgement on one result line, or wrap the receipt
    inside a multi-line message. Only the bounded JSON object after the
    marker is stripped so any same-line evidence suffix survives; receipt
    lines without a suffix and lines whose marker is not valid JSON are
    dropped.
    """

    kept: list[str] = []
    for line in text.splitlines():
        stripped_line = line.strip()
        if not stripped_line.startswith("PROJECT_CONTEXT_RECEIPT:"):
            kept.append(line)
            continue
        remainder = stripped_line[len("PROJECT_CONTEXT_RECEIPT:") :].lstrip()
        decoder = json.JSONDecoder()
        try:
            _, end = decoder.raw_decode(remainder)
        except json.JSONDecodeError:
            continue
        suffix = remainder[end:].strip(" |")
        if suffix:
            kept.append(suffix)
    return "\n".join(kept).strip()


def _provider_auth_failure_from_output(path: Path) -> dict[str, Any] | None:
    """Return a bounded, body-classified provider-refusal record, no secret text.

    Only provider-owned JSONL fields are authoritative. Model prose and raw
    error bodies are deliberately ignored so an agent cannot spoof a launch
    failure or leak credentials into durable task state.

    When a provider-owned ``api_error`` names an HTTP refusal status, its own
    status and machine error code -- never model prose -- are handed to
    ``runtime_adapters.classify_provider_outcome`` so the recorded reason is
    derived from the response body at the boundary where it is still in hand.
    A quota or rate refusal is therefore named as such instead of collapsing
    into ``worker_failed`` downstream (NF-2026-00275), and a bare 401/403 whose
    body distinguishes nothing is recorded as ``cause_not_distinguished`` rather
    than guessed as an authentication failure (NF-2026-00326). The detection was
    widened from 401/403 alone to every refusal status/code so the quota case
    that item one measured is no longer lost before classification.
    """

    try:
        st = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
        return None
    size = int(st.st_size)
    if size <= MAX_RECEIPT_SCAN_BYTES:
        text = _read_byte_range(path, 0, size)
    else:
        half = MAX_RECEIPT_SCAN_BYTES // 2
        text = _read_byte_range(path, 0, half) + "\n" + _read_byte_range(
            path, size - half, half
        )
    # Provider-owned HTTP refusal statuses: authentication (401/403), payment
    # required / balance (402) and rate/quota (429). 5xx is left to the worker
    # path unchanged -- a transient upstream outage is not a launch refusal here.
    # The status set and the machine-code vocabulary are OWNED by
    # ``runtime_adapters`` and reused here so the gate that forwards a body and
    # the classifier that names it can never drift onto different statuses or
    # token forms again (NF-2026-00275 rework: a forwarded 402 that the
    # classifier could not name collapsed back into ``worker_failed``).
    refusal_statuses = runtime_adapters.PROVIDER_REFUSAL_STATUSES
    for raw_line in text.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        raw_status = event.get("error_status", event.get("api_error_status"))
        status = raw_status if isinstance(raw_status, int) and not isinstance(raw_status, bool) else 0
        error_code = str(event.get("error") or "").strip().lower()
        subtype = str(event.get("subtype") or "").strip().lower()
        structured_auth_error = error_code in {
            "authentication_failed",
            "unauthorized",
            "invalid_api_key",
        }
        # A provider-owned refusal is present when the status is a refusal code
        # OR the machine error code itself names a quota/rate/credential cause.
        status_refusal = status in refusal_statuses
        code_refusal = structured_auth_error or runtime_adapters.provider_body_names_cause(
            error_code
        )
        structured_result_error = (
            event.get("type") == "result"
            and event.get("is_error") is True
            and str(event.get("terminal_reason") or "").strip().lower() == "api_error"
            and (status_refusal or code_refusal)
        )
        structured_retry_error = (
            event.get("type") == "system"
            and subtype == "api_retry"
            and (status_refusal or code_refusal)
        )
        if not (structured_result_error or structured_retry_error):
            continue
        # Hand the provider's OWN status and machine error code to the classifier
        # -- never the ``result``/message prose, which an agent could author.
        provider_body = f"http_status={status} {error_code}".strip()
        outcome = runtime_adapters.classify_provider_outcome(
            exit_code=1, message=provider_body
        )
        if outcome.get("outcome") != runtime_adapters.OUTCOME_PROVIDER_REFUSED:
            # The launch-time detector ESTABLISHED a provider refusal above -- a
            # refusal status, or a body/machine code that named an auth cause --
            # yet the classifier could not name WHICH cause from the forwarded
            # status and code alone (e.g. a status-less generic ``unauthorized``:
            # http_status=0 carrying no distinguishing token).  Returning the
            # classifier's ``worker_failed`` verdict here would record a provider
            # refusal that the detector matched BECAUSE it named an auth cause as
            # a worker crash -- exactly the NF-2026-00275 invariant this card
            # exists to hold, and specifically the dead-credential case where an
            # operator must re-authenticate and would instead be told their code
            # failed.  The honest verdict is that a refusal occurred whose cause
            # the response did not distinguish, so emit ``cause_not_distinguished``
            # -- the same reason the classifier uses for a bare 401 -- rather than
            # collapse back onto the worker path.  ``structured_auth_error`` and
            # the classifier's cause vocabulary are two lists that legitimately
            # disagree about a status-less ``unauthorized`` (the detector treats
            # it as an auth signal; the classifier excludes it because it names
            # nothing); this branch reconciles that disagreement honestly instead
            # of letting a matched refusal fall through to ``worker_failed``.
            return {
                "schema_id": "aiworkhub.provider_launch_failure.v1",
                "reason": (
                    f"provider_refused:http_status={status}"
                    ":cause_not_distinguished_by_response"
                ),
                "refusal_kind": runtime_adapters.REFUSAL_CAUSE_NOT_DISTINGUISHED,
                "recoverable": False,
                "http_status": status,
            }
        return {
            "schema_id": "aiworkhub.provider_launch_failure.v1",
            "reason": str(outcome.get("reason") or "provider_refused"),
            "refusal_kind": str(outcome.get("refusal_kind") or ""),
            "recoverable": bool(outcome.get("recoverable")),
            "http_status": status,
        }
    return None


def _provider_timeout_failure_from_output(path: Path) -> dict[str, Any] | None:
    """Return exact structured VS Code LM timeout evidence.

    The editor bridge owns its response deadline.  It may exit immediately
    before the outer supervisor's matching deadline, leaving the supervisor
    with the otherwise ambiguous pair ``state=exited, exit_code=1``.  Trust
    only the bridge's machine-generated result envelope; never classify model
    prose containing the word ``timeout`` as lifecycle evidence.
    """

    try:
        st = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
        return None
    size = int(st.st_size)
    if size <= MAX_RECEIPT_SCAN_BYTES:
        text = _read_byte_range(path, 0, size)
    else:
        half = MAX_RECEIPT_SCAN_BYTES // 2
        text = _read_byte_range(path, 0, half) + "\n" + _read_byte_range(
            path, size - half, half
        )
    for raw_line in text.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if (
            event.get("type") == "result"
            and event.get("is_error") is True
            and str(event.get("subtype") or "").strip().lower() == "error"
            and str(event.get("error") or "").strip() == "vscode_lm_response_timeout"
        ):
            return {
                "schema_id": "aiworkhub.provider_timeout_failure.v1",
                "reason": "vscode_lm_response_timeout",
            }
    return None


def _readonly_research_result_evidence(path: Path) -> dict[str, Any]:
    """Digest and validate one bounded provider stdout as research evidence.

    A zero exit code, tool chatter, or a project-context receipt alone is not
    a deliverable.  At least one supported final/assistant event must carry
    non-empty text.  The full bounded byte stream is hashed so coordinator
    acceptance can re-read the exact immutable evidence instead of trusting a
    worker-declared verdict or persisting its potentially sensitive prose in
    the task card.
    """

    base: dict[str, Any] = {
        "schema_id": "aiworkhub.readonly_research_result.v1",
        "meaningful_output": False,
        "bytes": 0,
        "sha256": "",
        "result_event_count": 0,
        "result_chars": 0,
        "reason": "research_result_missing",
    }
    try:
        st = path.lstat()
    except OSError:
        return base
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return {**base, "reason": "research_result_path_invalid"}
    size = int(st.st_size)
    if size <= 0:
        return base
    if size > MAX_RESEARCH_RESULT_BYTES:
        return {**base, "bytes": size, "reason": "research_result_too_large"}

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return {**base, "bytes": size, "reason": "research_result_unreadable"}
    try:
        with os.fdopen(fd, "rb") as handle:
            payload = handle.read(MAX_RESEARCH_RESULT_BYTES + 1)
    except OSError:
        return {**base, "bytes": size, "reason": "research_result_unreadable"}
    if len(payload) != size or len(payload) > MAX_RESEARCH_RESULT_BYTES:
        return {**base, "bytes": size, "reason": "research_result_changed_during_read"}

    result_count = 0
    result_chars = 0
    for raw_line in payload.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        text = _research_result_text(event)
        if not text:
            continue
        # The receipt and its evidence may share one result line as
        # "PROJECT_CONTEXT_RECEIPT: {json} | evidence". Strip only the
        # authenticated JSON prefix; the same-line suffix is the deliverable.
        without_receipts = _strip_project_context_receipt_prefix(text)
        if not _research_result_text_is_meaningful(without_receipts):
            continue
        result_count += 1
        result_chars += len(without_receipts)

    digest = hashlib.sha256(payload).hexdigest()
    meaningful = result_count > 0 and result_chars > 0
    return {
        **base,
        "meaningful_output": meaningful,
        "bytes": size,
        "sha256": digest,
        "result_event_count": result_count,
        "result_chars": result_chars,
        "reason": "" if meaningful else "research_result_missing",
    }


_RESEARCH_PLACEHOLDER_RESULTS = frozenset(
    {
        "complete",
        "completed",
        "done",
        "n/a",
        "no findings",
        "no result",
        "no results",
        "none",
        "null",
        "ok",
        "placeholder",
        "research completed",
        "success",
        "successful",
        "task completed",
        "tbd",
        "todo",
    }
)


def _research_result_text_is_meaningful(value: str) -> bool:
    """Reject bounded content-free finals without judging research quality.

    This is deliberately a narrow anti-collapse gate. Detailed correctness is
    still manager/reviewer work, while punctuation-only output and common
    completion placeholders cannot become research evidence merely because a
    provider emitted them in a successful final event.
    """

    compact = " ".join(str(value or "").split()).strip()
    if not compact:
        return False
    folded = compact.casefold().strip(" .,…!?:;\\/-_*#`~()[]{}<>'\"")
    if not folded or folded in _RESEARCH_PLACEHOLDER_RESULTS:
        return False
    return any(character.isalnum() for character in compact)


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
    if not allowed:
        if card.get("read_only") is not True:
            raise LaunchRejected("read_only_declaration_required")
        if card.get("required_outputs") or []:
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
    if not isinstance(raw, list):
        raise LaunchRejected("required_outputs_invalid")
    if not raw:
        # Its evidence is the authenticated worker transcript/MCP receipt.
        # The intent must be explicit so an accidentally empty code card does
        # not spend provider tokens on an unpromotable result.
        if card.get("read_only") is True and not (card.get("allowed_writes") or []):
            return
        raise LaunchRejected("required_outputs_invalid")
    allowed = card.get("allowed_writes") or []
    if not isinstance(allowed, list):
        raise LaunchRejected("allowed_writes_invalid")
    for item in raw:
        if not isinstance(item, str) or not item.strip() or "\x00" in item:
            raise LaunchRejected("required_outputs_invalid")
        try:
            normalized = _worker_workspace._relative_repo_path(item)
            output_allowed = _worker_workspace._matches(normalized, allowed)
        except WorkspaceError as exc:
            raise LaunchRejected(f"required_output_path_invalid:{exc}") from exc
        if not output_allowed:
            raise LaunchRejected(f"required_output_not_allowed:{normalized}")
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
    if runner == core.CODEX_RUNNER:
        raise LaunchRejected("coordinator_runner_cannot_launch_worker")
    if runner.startswith("claude_"):
        allowed: tuple[str, ...] = ("vscode_lm", "claude_cli")
    elif runner.startswith("codex_"):
        allowed = ("vscode_lm", "codex_cli")
    elif runner.startswith("deepseek_"):
        # Prefer the editor-owned VS Code Language Model API authorization.
        # BYOK and manual modes remain explicit compatibility fallbacks.
        allowed = ("vscode_lm", "deepseek_vscode_lm", "deepseek_copilot_cli", "deepseek_manual")
    elif runner.startswith("glm_"):
        # Prefer the credential-free VS Code Language Model API bridge.  Keep
        # the explicit BYOK adapter as a backwards-compatible fallback.
        allowed = ("vscode_lm", "glm_vscode_lm", "glm_copilot_cli")
    elif runner.startswith("copilot_"):
        # Copilot-owned models are a distinct workforce from first-party
        # Claude Code/Codex subscriptions. They may use only the editor's
        # public VS Code Language Model API bridge.
        allowed = ("vscode_lm",)
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
        return json.loads(
            context_result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1]
        )
    except (IndexError, TypeError, json.JSONDecodeError):
        return {}


def _worker_context_section_count(payload: dict[str, Any]) -> int:
    """Count delivered evidence across project-context bundle versions."""

    evidence = payload.get("evidence")
    if isinstance(evidence, dict):
        return len(evidence)
    sections = payload.get("sections")
    return len(sections) if isinstance(sections, list) else 0


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


def _committed_claim_card(
    result: Mapping[str, Any],
    *,
    request_id: str,
    task_id: str,
    runner: str,
    topic: str,
) -> dict[str, Any]:
    """Return the exact task card committed by ``claim_start_exact``."""
    if not isinstance(result, Mapping):
        raise LaunchRejected("claim_receipt_invalid:result_not_mapping")
    if result.get("ok") is not True:
        raise LaunchRejected("claim_receipt_invalid:claim_not_committed")
    returncode = result.get("returncode")
    if type(returncode) is not int or returncode != 0:
        raise LaunchRejected("claim_receipt_invalid:returncode")
    raw = result.get("stdout")
    if not isinstance(raw, str) or not raw.strip():
        raise LaunchRejected("claim_receipt_invalid:card_missing")
    try:
        card = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LaunchRejected("claim_receipt_invalid:card_malformed") from exc
    if not isinstance(card, dict):
        raise LaunchRejected("claim_receipt_invalid:card_not_object")
    expected = {
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "launch_request_id": request_id,
    }
    mismatches = [
        key for key, value in expected.items() if card.get(key) != value
    ]
    if mismatches:
        raise LaunchRejected(
            "claim_receipt_invalid:identity_mismatch:" + ",".join(mismatches)
        )
    claim_epoch = card.get("claim_epoch")
    if type(claim_epoch) is not int or claim_epoch < 1:
        raise LaunchRejected("claim_receipt_invalid:claim_epoch")
    return card


def _terminal_rework_delta_evidence(
    workspace: WorkerWorkspace,
    metadata: Mapping[str, Any],
    request_id: str,
    changed: list[str],
) -> dict[str, Any] | None:
    """Seal a validation-failed candidate outside its disposable worktree.

    The returned evidence is intentionally small and identity-bound.  The
    artifact itself contains the exact changed bytes (and deletion markers)
    and is verified by ``worker_workspace`` when a successor materializes it.
    """
    if not changed:
        return None
    task_id = str(metadata.get("task_id") or "").strip()
    claim_epoch = metadata.get("claim_epoch")
    if not task_id or type(claim_epoch) is not int or claim_epoch < 1:
        return {
            "schema_id": "aiworkhub.rework_delta_seal.v1",
            "sealed": False,
            "reason": "rework_delta_identity_invalid",
        }

    entries: list[tuple[str, bytes | None]] = []
    try:
        for relative in changed:
            source = workspace.path / relative
            if source.is_symlink():
                raise WorkspaceError(f"rework_delta_symlink_forbidden:{relative}")
            entries.append((relative, source.read_bytes() if source.is_file() else None))
        authority_repo = workspace.repo.resolve(strict=False)
        artifact_dir = (
            _worker_workspace.configured_runtime_root(authority_repo)
            / "rework_deltas"
        )
        sealed = _worker_workspace.seal_rework_delta_artifact(
            authority_repo,
            task_id,
            request_id,
            claim_epoch,
            entries,
            artifact_dir,
        )
    except (OSError, ValueError, WorkspaceError) as exc:
        return {
            "schema_id": "aiworkhub.rework_delta_seal.v1",
            "sealed": False,
            "reason": f"rework_delta_seal_failed:{exc}"[:300],
        }
    return {
        "schema_id": "aiworkhub.rework_delta_descriptor.v1",
        "sealed": True,
        "authority_repo": str(authority_repo),
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
        "artifact_path": str(sealed["path"]),
        "artifact_sha256": str(sealed["digest"]),
    }


REVIEW_WORKSPACE_RETENTION_AUDIT_SCHEMA_ID = (
    "aiworkhub.review_workspace_retention_audit.v1"
)

# Durable declaration that one exact reviewer claim must be terminalized.  It
# is written before the ledger event that terminalizes the reservation, so a
# crash between the two durable stores stays recoverable instead of stranding
# the reviewer card in ``processing``.
REVIEWER_TERMINAL_INTENT_SCHEMA_ID = (
    "aiworkhub.task_mcp.reviewer_terminal_intent.v1"
)

# One bounded operator-visible record per terminal intent that can never be
# settled -- unreadable bytes, a foreign schema, or an identity that cannot be
# bound to an exact task/request/claim epoch.  Such an intent is deliberately
# never deleted and never acted on, so without this record it is silent: the
# reviewer card stays in ``processing`` with nothing in any ledger saying why.
REVIEWER_TERMINAL_INTENT_DIAGNOSTIC_SCHEMA_ID = (
    "aiworkhub.task_mcp.reviewer_terminal_intent_diagnostic.v1"
)


def review_workspace_retention_audit_path(process_log_path: Path) -> Path:
    """Sibling append-only ledger recording every review-workspace removal."""
    return Path(process_log_path).with_name("review_workspace_retention_audit.jsonl")


def reviewer_terminal_intent_diagnostic_path(process_log_path: Path) -> Path:
    """Sibling append-only ledger of terminal intents that can never settle."""
    return Path(process_log_path).with_name(
        "reviewer_terminal_intent_diagnostics.jsonl"
    )


def record_review_workspace_retention_audit(
    process_log_path: Path,
    *,
    request_id: str,
    task_id: str,
    card_status: str,
    reason: str,
    action: str,
    moved_to: str | None = None,
) -> dict[str, Any]:
    """Durably record one review-workspace removal.

    Quarantine and eventual purge are both removals from the live review
    surface, and neither may happen without a record naming the request id,
    card and reason so a manager can account for every worktree that left the
    tree.  Returns the appended record.
    """

    record: dict[str, Any] = {
        "schema_id": REVIEW_WORKSPACE_RETENTION_AUDIT_SCHEMA_ID,
        "recorded_at": _utcnow(),
        "request_id": str(request_id),
        "task_id": str(task_id),
        "card_status": str(card_status),
        "action": str(action),
        "reason": str(reason),
    }
    if moved_to is not None:
        record["moved_to"] = str(moved_to)
    audit_path = review_workspace_retention_audit_path(process_log_path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def review_workspace_quarantine_root(process_log_path: Path) -> Path:
    return Path(process_log_path).with_name("review_workspace_quarantine")


def quarantine_review_workspace(
    process_log_path: Path,
    *,
    request_id: str,
    path: Path,
    home: Path,
) -> Path:
    """Move a corrupted review workspace into quarantine instead of deleting it.

    A failed integrity check proves the retained bytes disagree with the sealed
    hashes; it is not authority to destroy them.  The exact bytes a manager
    needs to diff against the sealed hashes are relocated under a
    request-scoped quarantine directory and never unlinked here.  Returns the
    quarantine directory.
    """
    if not isinstance(request_id, str) or re.fullmatch(r"[0-9a-f]{32}", request_id) is None:
        raise ValueError(
            f"refusing quarantine for unsafe request_id {request_id!r}: only "
            "canonical 32-character lowercase hexadecimal request IDs may name "
            "a quarantine destination"
        )

    root = review_workspace_quarantine_root(process_log_path)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / str(request_id)
    suffix = 0
    while dest.exists():
        suffix += 1
        dest = root / f"{request_id}.{suffix}"
    dest.mkdir(parents=True)
    for label, source in (("worktree", Path(path)), ("home", Path(home))):
        if source.is_symlink() or not source.exists():
            continue
        shutil.move(str(source), str(dest / label))
    return dest


def _pid_ticks_to_surface_str(value: Any) -> str | None:
    """Serialize a pid start-tick counter for a JavaScript consumer.

    ``pid_start_ticks`` is the boot-relative counter that stops a reused pid
    from being mistaken for a live worker.  On some hosts (observed on Windows
    11, AWH-OBS-011) it exceeds ``2**53`` where a JavaScript ``Number`` can no
    longer hold it exactly, so it is carried as a string across every surface a
    JS consumer reads.  Returns ``None`` when the counter is absent.
    """

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _decode_ledger_int(value: Any) -> int | None:
    """Decode one authenticated audit-ledger numeric field, or ``None``.

    A malformed number from the authenticated ledger is a named refusal, never
    an exception escaping the completion-gate boundary.  ``None`` (absent) reads
    as ``0`` to preserve the historic ``or 0`` semantics; any non-integral or
    non-numeric shape is reported as malformed.

    ``int()`` is the predicate, not ``str.isdigit()``: the latter admits shapes
    ``int()`` rejects (``'--5'``, superscript ``'²'``) and silently accepts
    non-ASCII digits (``'١٢٣'``) from an authenticated ledger.  We refuse
    anything but ASCII digits with an optional single leading ``-`` and let
    ``int()`` make the final decision, so no shape this guard admits can raise
    past this boundary.
    """

    if value is None:
        return 0
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        body = stripped[1:] if stripped.startswith("-") else stripped
        if not body.isascii() or not body.isdigit():
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def pid_identity_surface(event: Mapping[str, Any]) -> dict[str, Any]:
    """Lossless, JS-safe pid-identity fields shared by every status surface.

    ``task_show`` and ``agent_task_status`` must agree, so both derive their
    pid-identity view from this one function: the raw integer counters are
    replaced by string forms that survive a JSON round-trip into a
    ``Number``-typed JavaScript consumer without rounding.  The source event is
    never mutated; internal pid comparisons keep reading the exact integer.
    """

    fields: dict[str, Any] = {}
    ticks = _pid_ticks_to_surface_str(event.get("pid_start_ticks"))
    if ticks is not None:
        fields["pid_start_ticks"] = ticks
    provider_ticks = _pid_ticks_to_surface_str(
        event.get("provider_pid_start_ticks")
    )
    if provider_ticks is not None:
        fields["provider_pid_start_ticks"] = provider_ticks
    return fields


def _retained_candidate_identity_evidence(
    workspace: WorkerWorkspace,
    metadata: dict[str, Any],
    request_id: str,
    changed: list[str],
    claim_state: str,
) -> dict[str, Any]:
    """Return the exact identity needed to preserve a failed candidate.

    Mechanical validation failure is not acceptance, but it also is not
    authority to discard already-produced bytes. ``core.reject_review`` can
    pin and materialize a rework predecessor only when terminal evidence has
    hashes, workspace metadata and exact request identity.
    """

    if not changed:
        return {}
    changed_path_hashes = _changed_path_hashes(workspace, changed)
    if not changed_path_hashes:
        return {}
    return {
        "changed_path_hashes": changed_path_hashes,
        "claim_state": claim_state,
        "workspace": workspace.as_metadata(),
        "request_identity": {
            "request_id": request_id,
            "task_id": str(metadata["task_id"]),
            "runner": str(metadata["runner"]),
            "topic": str(metadata["topic"]),
        },
    }


DELTA_RETAINING_TERMINAL_STATES = frozenset({"validation_failed", "timed_out"})


def _is_rework_attempt(metadata: Mapping[str, Any]) -> bool:
    """Whether this attempt is a rework materialized from a predecessor delta."""

    predecessor = metadata.get("rework_predecessor")
    return isinstance(predecessor, dict) and bool(predecessor)


def retained_rework_candidate_evidence(
    terminal_state: str,
    workspace: WorkerWorkspace,
    metadata: dict[str, Any],
    request_id: str,
    changed: list[str],
    claim_state: str,
) -> dict[str, Any]:
    """Rework-predecessor evidence for a terminal state that left a delta.

    A timed-out worker's partial delta is as recoverable as a validation
    failure's; pinning it lets the successor start from the work instead of
    from nothing.  A terminal state that never produces a usable delta, or an
    empty change set, retains nothing.
    """

    if terminal_state not in DELTA_RETAINING_TERMINAL_STATES or not changed:
        return {}
    try:
        return _retained_candidate_identity_evidence(
            workspace, metadata, request_id, changed, claim_state,
        )
    except WorkspaceError:
        return {}


def _release_launch_request_resources(
    *,
    bridge_request: "vscode_lm_bridge.BridgeRequest | None",
    workspace: WorkerWorkspace | None,
    cancel: Callable[[Any], Any] = vscode_lm_bridge.cancel_request,
    cleanup: Callable[..., Any] = cleanup_workspace,
) -> list[str]:
    """Release a failed launch's resources, claim before workspace.

    A VS Code LM claim refers to the request workspace, so the claim must be
    cancelled BEFORE that workspace is deleted -- otherwise the claim outlives
    the workspace it names.  Returns the ordered release errors (empty when
    clean); a claim-cancel failure never prevents the workspace cleanup.
    """

    errors: list[str] = []
    if bridge_request is not None:
        try:
            cancel(bridge_request)
        except vscode_lm_bridge.BridgeError as exc:
            errors.append(f"bridge_cancel_failed:{exc}")
    if workspace is not None:
        try:
            cleanup(workspace.repo, workspace.path, workspace.home)
        except WorkspaceError as exc:
            errors.append(f"cleanup_failed:{exc}")
    return errors


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
    allowed_writes: list[str] | None = None,
    quality_review_packet_path: Path | None = None,
    rework_overlay_path: Path | None = None,
) -> worker_ai_tools_mcp.WorkerMcpRuntime:
    kwargs: dict[str, Any] = {
        "request_id": request_id,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "backend": backend,
        "source_graph_targets": source_graph_targets,
        "allowed_writes": list(allowed_writes or []),
        "session_topic": session_topic,
    }
    if quality_review_packet_path is not None:
        kwargs["quality_review_packet_path"] = quality_review_packet_path
    if rework_overlay_path is not None:
        kwargs["rework_overlay_path"] = rework_overlay_path
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


def _materialize_worker_rework_overlay(
    workspace: WorkerWorkspace,
    *,
    task_id: str,
    card: Mapping[str, Any],
) -> tuple[Path | None, dict[str, Any] | None]:
    """Seal inherited predecessor bytes for this request's Source Graph.

    ``create_workspace`` already performed the strong predecessor workspace,
    repository and hash verification.  This helper serializes exactly those
    verified paths into the request-private HOME before the provider starts;
    it never scans beyond ``inherited_rework_paths``.
    """

    if not workspace.inherited_rework_paths:
        return None, None
    predecessor = card.get("rework_predecessor")
    if not isinstance(predecessor, Mapping):
        raise WorkspaceError("rework_overlay_predecessor_missing")
    predecessor_request_id = str(predecessor.get("request_id") or "").strip()
    predecessor_task_id = str(predecessor.get("task_id") or task_id).strip()
    hashes = predecessor.get("changed_path_hashes")
    if not predecessor_request_id or not predecessor_task_id or not isinstance(hashes, Mapping):
        raise WorkspaceError("rework_overlay_predecessor_invalid")

    entries: list[tuple[str, str | None, bytes | None]] = []
    for relative in workspace.inherited_rework_paths:
        if relative not in hashes:
            raise WorkspaceError(f"rework_overlay_hash_missing:{relative}")
        expected = hashes.get(relative)
        candidate = workspace.path / relative
        if expected is None:
            if candidate.exists() or candidate.is_symlink():
                raise WorkspaceError(
                    f"rework_overlay_deleted_path_present:{relative}"
                )
            entries.append((relative, None, None))
            continue
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise WorkspaceError(f"rework_overlay_hash_invalid:{relative}")
        if candidate.is_symlink() or not candidate.is_file():
            raise WorkspaceError(f"rework_overlay_file_missing:{relative}")
        content = candidate.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise WorkspaceError(f"rework_overlay_hash_mismatch:{relative}")
        entries.append((relative, expected, content))

    try:
        packet_bytes = materialize_rework_overlay(
            workspace.request_id,
            task_id,
            predecessor_request_id,
            predecessor_task_id,
            workspace.repo,
            entries,
        )
        packet = json.loads(packet_bytes.decode("utf-8"))
    except (ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"rework_overlay_materialization_failed:{exc}") from exc
    path = workspace.home / "task_mcp_worker_runtime" / "rework_overlay.json"
    write_json_0600(path, packet)
    return path, packet


def _materialize_crash_retry_packet(
    process_dir: Path,
    workspace: WorkerWorkspace,
    *,
    task_id: str,
    card: Mapping[str, Any],
    rework_overlay_packet: Mapping[str, Any] | None,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Bind bounded failed-stream evidence to one verified rework overlay.

    The predecessor workspace bytes remain authoritative through the overlay;
    this packet only salvages diagnostics that would otherwise be reread from
    old process logs. Missing, successful, cross-task, cross-repository, or
    oversized predecessor metadata fails closed by omitting the packet.
    """

    predecessor = card.get("rework_predecessor")
    if not isinstance(predecessor, Mapping) or not isinstance(
        rework_overlay_packet, Mapping
    ):
        return None, None
    request_id = str(predecessor.get("request_id") or "").strip()
    predecessor_task_id = str(predecessor.get("task_id") or task_id).strip()
    if (
        not request_id
        or predecessor_task_id != task_id
        or str(rework_overlay_packet.get("predecessor_request_id") or "")
        != request_id
        or str(rework_overlay_packet.get("predecessor_task_id") or "")
        != task_id
    ):
        raise WorkspaceError("crash_retry_predecessor_identity_mismatch")

    metadata_path = process_dir / f"{request_id}.request.json"
    status_path = process_dir / f"{request_id}.supervisor.json"
    try:
        if (
            metadata_path.is_symlink()
            or status_path.is_symlink()
            or metadata_path.stat().st_size > 1024 * 1024
            or status_path.stat().st_size > 1024 * 1024
        ):
            raise WorkspaceError("crash_retry_predecessor_artifact_unsafe")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"crash_retry_predecessor_artifact_invalid:{exc}") from exc
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        raise WorkspaceError("crash_retry_predecessor_artifact_shape_invalid")
    predecessor_workspace = metadata.get("workspace")
    if (
        str(metadata.get("request_id") or "") != request_id
        or str(metadata.get("task_id") or "") != task_id
        or not isinstance(predecessor_workspace, dict)
    ):
        raise WorkspaceError("crash_retry_predecessor_metadata_identity_mismatch")
    try:
        predecessor_repo = Path(str(predecessor_workspace.get("repo") or "")).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceError("crash_retry_predecessor_repo_invalid") from exc
    if predecessor_repo != workspace.repo.resolve():
        raise WorkspaceError("crash_retry_predecessor_repo_mismatch")

    state = str(status.get("state") or "")
    returncode = status.get("exit_code")
    if state == "exited" and returncode == 0:
        return None, None
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    # The packet is JSON, so JSON encoding already neutralises every
    # metacharacter. The HTML-oriented live-output sanitiser escaped and
    # redacted bytes the successor needs verbatim, so carry the predecessor's
    # diagnostics unescaped and unredacted; the tail hashes below then cover
    # exactly the bytes delivered rather than a pre-sanitised original.
    stdout_tail = _safe_tail(stdout_path, MAX_CRASH_RETRY_STREAM_BYTES)
    stderr_tail = _safe_tail(stderr_path, MAX_CRASH_RETRY_STREAM_BYTES)
    error = str(status.get("error") or "")[:500]
    if not (stdout_tail or stderr_tail or error or state):
        return None, None
    validation_delta: dict[str, Any] | None = None
    validation_manifest_sha256 = ""
    bundle_dir = process_dir / "attempt-artifacts" / request_id
    if bundle_dir.exists():
        try:
            attempt_artifacts.verify_json_bundle(bundle_dir)
            manifest_path = bundle_dir / attempt_artifacts.MANIFEST_FILENAME
            manifest = attempt_artifacts.parse_manifest_json(
                manifest_path.read_text(encoding="utf-8")
            )
            validation_entry = next(
                (entry for entry in manifest.artifacts if entry.role == "validation"),
                None,
            )
            if validation_entry is not None:
                validation_payload = json.loads(
                    (bundle_dir / validation_entry.path).read_text(encoding="utf-8")
                )
                checks = (
                    validation_payload.get("checks")
                    if isinstance(validation_payload, dict)
                    else None
                )
                if isinstance(checks, list):
                    validation_delta = _worker_workspace.validation_failure_delta_packet(
                        row for row in checks if isinstance(row, Mapping)
                    )
                    validation_manifest_sha256 = hashlib.sha256(
                        manifest_path.read_bytes()
                    ).hexdigest()
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            attempt_artifacts.InvalidArtifactError,
            attempt_artifacts.InvalidManifestError,
        ) as exc:
            raise WorkspaceError(
                f"crash_retry_validation_artifact_invalid:{exc}"
            ) from exc

    packet: dict[str, Any] = {
        "schema_id": "aiworkhub.crash_retry_packet.v1",
        "successor_request_id": workspace.request_id,
        "successor_task_id": task_id,
        "predecessor_request_id": request_id,
        "predecessor_task_id": task_id,
        "predecessor_state": state,
        "predecessor_exit_code": returncode,
        "predecessor_error": error,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "stdout_tail_sha256": hashlib.sha256(stdout_tail.encode("utf-8")).hexdigest(),
        "stderr_tail_sha256": hashlib.sha256(stderr_tail.encode("utf-8")).hexdigest(),
        "rework_overlay_sha256": str(
            rework_overlay_packet.get("canonical_digest") or ""
        ),
        "inherited_paths": list(workspace.inherited_rework_paths),
        "validation_failure_delta": validation_delta,
        "validation_manifest_sha256": validation_manifest_sha256,
        "stale_worktree_bytes_authoritative": False,
        "canonical_reread_savings_claimed": False,
    }
    canonical = json.dumps(
        packet,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(canonical) > MAX_CRASH_RETRY_PACKET_BYTES:
        raise WorkspaceError("crash_retry_packet_too_large")
    packet["packet_sha256"] = hashlib.sha256(canonical).hexdigest()
    path = workspace.home / "task_mcp_worker_runtime" / "crash_retry_packet.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json_0600(path, packet)
    return path, packet


def _worker_mcp_live_call_gate(metadata: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Bounded, redacted B833 completion-gate summary.

    Gated for ``task_context_policy.task_type == "code"`` according to the
    repository's two explicit tool-policy switches.  A ``project_context``
    contract must be present to reach that value at all.
    Data-classification and research tasks -- and any task without a
    ``project_context`` contract -- are exempt and never blocked here; they
    keep whatever policy already applied to them (e.g. the immutable input
    shard for data tasks). Fails CLOSED on a gated task: a missing worker_mcp
    runtime record, an unreadable ledger, or zero verified live
    ``source_graph`` calls all resolve to ``satisfied: False``. Source Graph
    must be fresh and non-empty; Session Manager and requested Memory/KB
    sections must have a successful canonical call. A denied malformed or
    out-of-scope tool request remains visible as policy-warning telemetry but
    is not itself terminal once every required canonical call is satisfied:
    the denied request returned no data and granted no capability, so forcing
    a full worker rerun after recovery only discards valid work. A tampered or
    forged ledger line is dropped by ``verify_audit_ledger`` before it ever
    reaches this count, so a worker cannot satisfy the gate by writing text
    that merely looks like an audit entry.
    """
    context_metadata = metadata.get("project_context") or {}
    task_type = str(
        (context_metadata.get("task_context_policy") or {}).get("task_type") or ""
    )
    context_required = context_metadata.get("required") is True
    worker_mcp_meta = metadata.get("worker_mcp") or {}
    sections = context_metadata.get("sections") or []
    tools_policy = dict(repo_policy.DEFAULT_POLICY["tools"])
    policy_error = ""
    authority_repo = worker_mcp_meta.get("authority_repo")
    if isinstance(authority_repo, str) and authority_repo.strip():
        try:
            tools_policy = dict(
                repo_policy.load_policy(Path(authority_repo))["tools"]
            )
        except repo_policy.RepoPolicyError as exc:
            policy_error = f"repo_policy_invalid:{exc}"
    required_tools: list[str] = []
    if task_type == "code" and tools_policy.get("source_graph_required_for_code"):
        required_tools.append("source_graph")
    if (
        task_type == "code"
        and tools_policy.get("session_memory_kb_required_for_nontrivial")
    ):
        for section in sections:
            if not isinstance(section, dict) or not section.get("requested", True):
                continue
            name = str(section.get("name") or "")
            if (
                name in {"session_current_state", "ai_memory", "kb"}
                and name not in required_tools
            ):
                required_tools.append(name)
    # An explicit required project-context contract is stronger than the
    # repository's generic code-task defaults.  Research/read-only cards use
    # this path too, so derive their blocking tools from the exact requested
    # sections instead of silently classifying them as observation-only.
    if context_required:
        for section in sections:
            if not isinstance(section, dict) or not section.get("requested", True):
                continue
            name = str(section.get("name") or "")
            if name in _GATEABLE_CONTEXT_SECTIONS and name not in required_tools:
                required_tools.append(name)
    gated = bool(required_tools) and (task_type == "code" or context_required)
    result: dict[str, Any] = {
        "gated": gated,
        "task_type": task_type,
        "project_context_required": context_required,
        "required_tools": required_tools if gated else [],
        "missing_tools": [],
        "satisfied": True,
        "reason": "",
        "satisfaction_by_tool": {},
        "injected_context_acknowledged": False,
        "observation_only": not gated,
        "telemetry_observed": False,
        "telemetry_reason": "",
        "policy_warning": False,
        "policy_warning_count": 0,
        "warnings": [],
        "tools_policy": {
            "source_graph_required_for_code": bool(
                tools_policy.get("source_graph_required_for_code")
            ),
            "session_memory_kb_required_for_nontrivial": bool(
                tools_policy.get("session_memory_kb_required_for_nontrivial")
            ),
        },
    }
    # Receipt acknowledgement is evidence truth, not merely a code-task gate
    # implementation detail.  Keep it observable for exempt research and
    # data-classification tasks as well, while preserving their non-gated
    # completion semantics.
    injected_acknowledged, injected_tools = _injected_context_satisfaction(metadata)
    result["injected_context_acknowledged"] = injected_acknowledged
    if policy_error:
        result["gated"] = True
        result["satisfied"] = False
        result["reason"] = policy_error
        return result
    ledger_path = worker_mcp_meta.get("audit_ledger_path")
    key_path = worker_mcp_meta.get("audit_hmac_key_path")
    if not ledger_path or not key_path:
        result["telemetry_reason"] = "worker_mcp_runtime_not_provisioned"
        if gated:
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
    result["telemetry_observed"] = bool(verification.get("ok"))
    result["telemetry_reason"] = str(verification.get("reason") or "")
    # Authenticated-ledger numeric decoding fails closed with a named refusal:
    # a malformed count is never an exception escaping this gate boundary.
    policy_violations = _decode_ledger_int(verification.get("policy_violations"))
    live_source_graph_calls = _decode_ledger_int(
        verification.get("live_source_graph_calls")
    )
    successful_raw = verification.get("successful_call_count_by_tool")
    if successful_raw is None:
        successful_raw = {}
    successful: dict[str, int] = {}
    ledger_decode_failure = ""
    if policy_violations is None:
        ledger_decode_failure = "policy_violations"
    elif live_source_graph_calls is None:
        ledger_decode_failure = "live_source_graph_calls"
    elif not isinstance(successful_raw, dict):
        ledger_decode_failure = "successful_call_count_by_tool"
    else:
        for tool_name, raw_count in successful_raw.items():
            decoded_count = _decode_ledger_int(raw_count)
            if decoded_count is None:
                ledger_decode_failure = (
                    "successful_call_count_by_tool:" + str(tool_name)
                )
                break
            successful[str(tool_name)] = decoded_count
    if ledger_decode_failure:
        result["gated"] = True
        result["satisfied"] = False
        result["reason"] = (
            "audit_ledger_numeric_decode_failed:" + ledger_decode_failure
        )
        return result
    result["policy_warning"] = policy_violations > 0
    result["policy_warning_count"] = policy_violations
    if policy_violations:
        result["warnings"] = [
            f"denied_aiworkhub_tool_requests_recovered:{policy_violations}"
        ]
    # A receipt that declares itself blocking is authority to refuse, not a
    # cosmetic flag.  Consult it directly so acceptance can never promote a
    # card over a self-declared-blocking audit receipt, and surface the exact
    # blocker so a manager sees why.  A field asserting authority it does not
    # have is worse than none.
    receipt_conformance = verification.get("receipt_conformance")
    if isinstance(receipt_conformance, dict) and receipt_conformance.get("blocking"):
        blockers = [
            str(item)
            for item in (receipt_conformance.get("blockers") or [])
            if str(item)
        ]
        result["gated"] = True
        result["satisfied"] = False
        result["receipt_conformance_blocking"] = True
        result["reason"] = (
            "receipt_conformance_blocking:" + ",".join(blockers)
            if blockers
            else "receipt_conformance_blocking"
        )
        return result
    if not gated:
        return result
    # ``successful`` was decoded above with fail-closed numeric handling.
    # Injected context accelerates startup but does not prove continuous tool
    # use.  In particular, Source Graph must have a fresh authenticated worker
    # call during execution; an initial hash-receipted bundle alone can never
    # satisfy a code task's discovery gate.
    satisfaction_by_tool: dict[str, str] = {}
    missing: list[str] = []
    stale: list[str] = []
    # Source Graph freshness is invocation truth: an authenticated, successful,
    # non-cached authoritative call satisfies continuous use even when the
    # bounded query returns zero rows.  Result adequacy stays observable through
    # hit_count/zero_hit_calls and may guide re-query/review, but it must not be
    # rewritten into "the tool was never called".  Cached-only, failed,
    # non-authoritative and unverified activity remains fail-closed.
    if live_source_graph_calls > 0:
        satisfaction_by_tool["source_graph"] = "live_worker_call"
    elif injected_acknowledged and "source_graph" in injected_tools:
        satisfaction_by_tool["source_graph"] = "injected_only_not_sufficient"
        missing.append("source_graph_live_call")
    elif int(successful.get("source_graph") or 0) > 0:
        satisfaction_by_tool["source_graph"] = "stale_or_cached"
        stale.append("source_graph")
    else:
        missing.append("source_graph")
    rework_attempt = _is_rework_attempt(metadata)
    for tool in required_tools:
        if tool == "source_graph":
            continue
        if int(successful.get(tool) or 0) > 0:
            satisfaction_by_tool[tool] = "live_worker_call"
        elif not context_required and injected_acknowledged and tool in injected_tools:
            satisfaction_by_tool[tool] = "injected_receipt"
        elif rework_attempt:
            # A rework is a validation-only replay of an already-green
            # predecessor delta; it structurally does not re-issue the context
            # tool calls the predecessor already made.  Honor the predecessor's
            # receipts here instead of discarding green work over a context
            # call this rework path never makes.  Source Graph freshness above
            # is still enforced per attempt.
            satisfaction_by_tool[tool] = "rework_predecessor_receipt"
        else:
            missing.append(tool)
    result["missing_tools"] = missing
    result["stale_tools"] = stale
    result["satisfaction_by_tool"] = satisfaction_by_tool
    if not verification.get("ok") or missing or stale:
        result["satisfied"] = False
        reasons: list[str] = []
        if missing:
            prefix = (
                "worker_mcp_required_tools_missing:"
                if context_required
                else "required_aiworkhub_mcp_calls_missing:"
            )
            reasons.append(prefix + ",".join(missing))
        if stale:
            reasons.append("source_graph_stale_or_cached:" + ",".join(stale))
        result["reason"] = verification.get("reason") or "; ".join(reasons)
    return result


_QUALITY_REVIEW_RECEIPT_TOP_KEYS = frozenset(
    {
        "schema_id",
        "packet_sha256",
        "target",
        "reviewer",
        "report",
        "authority",
        "submission_id",
        "physical_submission_count",
        "logical_submission_count",
    }
)
_QUALITY_REVIEW_TARGET_KEYS = frozenset({"request_id", "task_id", "claim_epoch"})
_QUALITY_REVIEW_REVIEWER_KEYS = frozenset({"request_id", "task_id", "provider"})
_QUALITY_REVIEW_REPORT_KEYS = frozenset(
    {"lens", "provider", "read_only", "can_mutate_repo", "findings"}
)
_QUALITY_REVIEW_AUTHORITY_KEYS = frozenset(
    {"process_identity_verified", "audit_verified", "terminal_state"}
)
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


# One authority for the bool-safe integer rule, owned by the store that binds
# the claim epochs it guards.  A private copy here would silently stop matching
# the store's rule and admit an epoch the store would reject -- visible only as
# a terminal transition bound to the wrong episode.
_is_bool_safe_int = task_store.is_bool_safe_int


def _is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and _SHA256_HEX_RE.fullmatch(value) is not None


def _enforce_quality_review_receipt_schema(
    receipt: dict[str, Any], observed_provider: str
) -> dict[str, Any]:
    """Reject any deviation from the exact production read-only receipt shape."""
    if set(receipt) != _QUALITY_REVIEW_RECEIPT_TOP_KEYS:
        raise WorkspaceError("quality_review_receipt_top_level_keys_invalid")
    if receipt.get("schema_id") != quality_reviewer.RECEIPT_SCHEMA_ID:
        raise WorkspaceError("quality_review_receipt_schema_mismatch")
    packet_sha256 = receipt.get("packet_sha256")
    submission_id = receipt.get("submission_id")
    if not _is_sha256_hex(packet_sha256):
        raise WorkspaceError("quality_review_packet_sha256_invalid")
    if not _is_sha256_hex(submission_id):
        raise WorkspaceError("quality_review_submission_id_invalid")
    target = receipt.get("target")
    reviewer = receipt.get("reviewer")
    report = receipt.get("report")
    authority = receipt.get("authority")
    if not (
        isinstance(target, dict)
        and isinstance(reviewer, dict)
        and isinstance(report, dict)
        and isinstance(authority, dict)
    ):
        raise WorkspaceError("quality_review_receipt_shape_invalid")
    if set(target) != _QUALITY_REVIEW_TARGET_KEYS:
        raise WorkspaceError("quality_review_target_keys_invalid")
    if set(reviewer) != _QUALITY_REVIEW_REVIEWER_KEYS:
        raise WorkspaceError("quality_review_reviewer_keys_invalid")
    if set(report) != _QUALITY_REVIEW_REPORT_KEYS:
        raise WorkspaceError("quality_review_report_keys_invalid")
    if set(authority) != _QUALITY_REVIEW_AUTHORITY_KEYS:
        raise WorkspaceError("quality_review_authority_keys_invalid")
    claim_epoch = target.get("claim_epoch")
    if not _is_bool_safe_int(claim_epoch):
        raise WorkspaceError("quality_review_claim_epoch_invalid")
    if str(reviewer.get("provider") or "") != observed_provider:
        raise WorkspaceError("quality_review_reviewer_provider_mismatch")
    if str(report.get("provider") or "") != observed_provider:
        raise WorkspaceError("quality_review_report_provider_mismatch")
    findings = report.get("findings")
    if not isinstance(findings, list):
        raise WorkspaceError("quality_review_report_findings_invalid")
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise WorkspaceError(f"quality_review_finding_{index}_invalid")
        finding_keys = set(finding)
        if not (
            quality_reviewer.QUALITY_REVIEW_FINDING_REQUIRED_KEYS <= finding_keys
            <= quality_reviewer.QUALITY_REVIEW_FINDING_KEYS
        ):
            raise WorkspaceError(f"quality_review_finding_{index}_keys_invalid")
        if str(finding.get("severity") or "") not in quality_reviewer.FINDING_SEVERITIES:
            raise WorkspaceError(f"quality_review_finding_{index}_severity_invalid")
        if (
            str(finding.get("disposition") or "")
            not in quality_reviewer.FINDING_DISPOSITIONS
        ):
            raise WorkspaceError(f"quality_review_finding_{index}_disposition_invalid")
        if finding.get("actionable") is not (finding.get("disposition") == "defect"):
            raise WorkspaceError(f"quality_review_finding_{index}_actionable_invalid")
    if authority.get("process_identity_verified") is not True:
        raise WorkspaceError("quality_review_authority_process_identity_invalid")
    if authority.get("audit_verified") is not True:
        raise WorkspaceError("quality_review_authority_audit_invalid")
    if authority.get("terminal_state") != "review_ready":
        raise WorkspaceError("quality_review_authority_terminal_state_invalid")
    if report.get("read_only") is not True or report.get("can_mutate_repo") is not False:
        raise WorkspaceError("quality_review_report_not_read_only")
    physical_submission_count = receipt.get("physical_submission_count")
    logical_submission_count = receipt.get("logical_submission_count")
    if (
        not _is_bool_safe_int(physical_submission_count)
        or physical_submission_count != 1
    ):
        raise WorkspaceError("quality_review_physical_submission_count_invalid")
    if not _is_bool_safe_int(logical_submission_count) or logical_submission_count != 1:
        raise WorkspaceError("quality_review_logical_submission_count_invalid")
    return receipt


def _enforce_readonly_retained_workspace(terminal_evidence: dict[str, Any]) -> None:
    """Require a retained reviewer workspace to be provably read-only and empty."""
    changed_paths = terminal_evidence.get("changed_paths")
    changed_path_hashes = terminal_evidence.get("changed_path_hashes")
    if not isinstance(changed_paths, list) or changed_paths:
        raise WorkspaceError("quality_review_changed_paths_not_empty")
    if not isinstance(changed_path_hashes, dict) or changed_path_hashes:
        raise WorkspaceError("quality_review_changed_path_hashes_not_empty")
    workspace_meta = terminal_evidence.get("workspace")
    if not isinstance(workspace_meta, dict):
        raise WorkspaceError("quality_review_workspace_metadata_missing")
    allowed_writes = workspace_meta.get("allowed_writes")
    if not isinstance(allowed_writes, list) or allowed_writes:
        raise WorkspaceError("quality_review_workspace_allowed_writes_not_empty")
    try:
        reconstructed = WorkerWorkspace.from_metadata(dict(workspace_meta))
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise WorkspaceError("quality_review_workspace_reconstruction_failed") from exc
    if reconstructed.allowed_writes:
        raise WorkspaceError("quality_review_reconstructed_workspace_not_read_only")


def _verified_quality_review_receipt(
    metadata: dict[str, Any],
    workspace: WorkerWorkspace,
    request_id: str,
) -> dict[str, Any]:
    """Resolve exactly one authenticated submission for a reviewer process."""

    binding = metadata.get("quality_review")
    if not isinstance(binding, dict):
        raise WorkspaceError("quality_review_binding_missing")
    packet_path_raw = binding.get("packet_path")
    if not isinstance(packet_path_raw, str) or not packet_path_raw:
        raise WorkspaceError("quality_review_packet_path_missing")
    packet_path = Path(packet_path_raw).resolve()
    try:
        packet_path.relative_to(workspace.home.resolve())
    except ValueError as exc:
        raise WorkspaceError("quality_review_packet_outside_home") from exc
    try:
        if packet_path.is_symlink() or not packet_path.is_file():
            raise WorkspaceError("quality_review_packet_invalid")
        if packet_path.stat().st_size > worker_ai_tools_mcp.MAX_QUALITY_REVIEW_PACKET_BYTES:
            raise WorkspaceError("quality_review_packet_too_large")
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("quality_review_packet_unreadable") from exc
    worker_meta = metadata.get("worker_mcp") or {}
    verification = worker_ai_tools_mcp.verify_audit_ledger(
        Path(str(worker_meta.get("audit_ledger_path") or "")),
        Path(str(worker_meta.get("audit_hmac_key_path") or "")),
        task_id=str(metadata.get("task_id") or ""),
        runner=str(metadata.get("runner") or ""),
        topic=str(metadata.get("topic") or ""),
        request_id=request_id,
    )
    payloads = verification.get("verified_payloads") or []
    if not payloads:
        raise WorkspaceError("quality_review_submission_count:0")
    # An immutable read-only receipt requires exactly one physical submission.
    # Identical retries are deduplicated upstream; any additional authenticated
    # payload here is a duplicate and must fail closed rather than collapse
    # silently into a single logical receipt.
    if len(payloads) != 1:
        raise WorkspaceError(f"quality_review_submission_count:{len(payloads)}")
    receipt_payload = payloads[0]
    observed_provider = str(metadata.get("adapter_id") or "")
    target = packet.get("target") if isinstance(packet, dict) else None
    if not isinstance(target, dict):
        raise WorkspaceError("quality_review_packet_target_missing")
    # Independence is a recorded ladder, not a vendor check.  Resolve the rung
    # first: a same-provider (or single-provider) review is no longer discarded
    # here after it has already been launched, run and submitted -- it degrades
    # to same_model_fresh_context and completes.  Refuse only when no rung in the
    # ladder applies at all, naming both providers so the refusal is legible.
    worker_provider_name = str(target.get("worker_provider") or "")
    rung_record = quality_review.resolve_independence_rung(
        worker_provider=worker_provider_name,
        reviewer_provider=observed_provider,
        worker_model=worker_provider_name,
        reviewer_model=observed_provider,
    )
    if rung_record["rung"] not in quality_review.INDEPENDENCE_LADDER:
        raise WorkspaceError(
            "quality_review_provider_not_independent:"
            f"worker_provider={worker_provider_name},"
            f"reviewer_provider={observed_provider}"
        )
    receipt = json.loads(json.dumps(receipt_payload, ensure_ascii=False))
    reviewer = receipt.get("reviewer")
    report = receipt.get("report")
    if not isinstance(reviewer, dict) or not isinstance(report, dict):
        raise WorkspaceError("quality_review_receipt_shape_invalid")
    reviewer["provider"] = observed_provider
    report["provider"] = observed_provider
    entries_tampered = verification.get("entries_tampered")
    if not _is_bool_safe_int(entries_tampered):
        raise WorkspaceError("quality_review_audit_entries_tampered_invalid")
    audit_verified = bool(verification.get("ok")) and entries_tampered == 0
    try:
        verified = quality_reviewer.verify_reviewer_receipt(
            receipt,
            packet=packet,
            expected_reviewer_request_id=request_id,
            expected_reviewer_task_id=str(metadata.get("task_id") or ""),
            observed_provider=observed_provider,
            observed_terminal_state="review_ready",
            audit_verified=audit_verified,
        )
    except quality_reviewer.ReviewerEvidenceError as exc:
        raise WorkspaceError(f"quality_review_receipt_invalid:{exc}") from exc
    if str((verified.get("report") or {}).get("lens") or "") != str(
        binding.get("lens") or ""
    ):
        raise WorkspaceError("quality_review_lens_mismatch")
    verified["submission_id"] = hashlib.sha256(
        json.dumps(
            receipt_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    verified["physical_submission_count"] = 1
    verified["logical_submission_count"] = 1
    return _enforce_quality_review_receipt_schema(verified, observed_provider)


def _verified_accepted_quality_review_receipt(
    latest: dict[str, Any],
    card: dict[str, Any],
    reviewer_request_id: str,
    target_request_id: str,
    target_task_id: str,
) -> dict[str, Any]:
    """Reuse a reviewer receipt only after canonical standalone acceptance.

    Standalone acceptance removes the reviewer's read-only workspace, so the
    original packet/audit files are no longer available.  The receipt remains
    consumable only when the immutable process event and both task-card copies
    agree exactly and retain the authority established during acceptance.
    """

    if str(latest.get("state") or "") != "accepted" or latest.get("accepted") is not True:
        raise WorkspaceError("quality_reviewer_accepted_event_invalid")
    reviewer_task_id = str(latest.get("task_id") or "")
    if not reviewer_task_id or str(card.get("task_id") or "") != reviewer_task_id:
        raise WorkspaceError("quality_reviewer_accepted_task_identity_mismatch")
    if _canonical_task_status(card) != "finished":
        raise WorkspaceError("quality_reviewer_accepted_task_not_finished")
    if str(card.get("accepted_request_id") or "") != reviewer_request_id:
        raise WorkspaceError("quality_reviewer_accepted_request_mismatch")
    if str(card.get("topic") or "") != "quality_review":
        raise WorkspaceError("quality_reviewer_accepted_topic_mismatch")

    terminal_evidence = ((card.get("terminal_review") or {}).get("evidence") or {})
    accept_evidence = card.get("accept_evidence") or {}
    event_receipt = latest.get("quality_review_receipt")
    terminal_receipt = terminal_evidence.get("quality_review_receipt")
    accepted_receipt = accept_evidence.get("quality_review_receipt")
    if not all(
        isinstance(value, dict)
        for value in (event_receipt, terminal_receipt, accepted_receipt)
    ):
        raise WorkspaceError("quality_reviewer_accepted_receipt_missing")
    if event_receipt != terminal_receipt or event_receipt != accepted_receipt:
        raise WorkspaceError("quality_reviewer_accepted_receipt_mismatch")

    receipt = json.loads(json.dumps(event_receipt, ensure_ascii=False))
    target = receipt.get("target")
    reviewer = receipt.get("reviewer")
    report = receipt.get("report")
    authority = receipt.get("authority")
    if not (
        isinstance(target, dict)
        and isinstance(reviewer, dict)
        and isinstance(report, dict)
        and isinstance(authority, dict)
    ):
        raise WorkspaceError("quality_reviewer_accepted_receipt_shape_invalid")
    if (
        str(target.get("request_id") or "") != target_request_id
        or str(target.get("task_id") or "") != target_task_id
    ):
        raise WorkspaceError("quality_reviewer_accepted_target_mismatch")
    if (
        str(reviewer.get("request_id") or "") != reviewer_request_id
        or str(reviewer.get("task_id") or "") != reviewer_task_id
    ):
        raise WorkspaceError("quality_reviewer_accepted_identity_mismatch")
    observed_provider = str(latest.get("adapter_id") or "")
    if not observed_provider:
        raise WorkspaceError("quality_reviewer_accepted_provider_missing")
    # Enforce the exact production receipt schema (top-level/target/reviewer/
    # report/authority key sets, lowercase 64-hex packet/submission hashes,
    # bool-safe claim epoch and submission counts, provider and findings typing,
    # verified authority, terminal review_ready). Malformed, unverified,
    # duplicate, wrong-type/bool or identity-mismatched receipts fail closed
    # here rather than falling through to generic empty-hash equality.
    _enforce_quality_review_receipt_schema(receipt, observed_provider)
    # The retained reviewer workspace must be provably read-only and empty.
    _enforce_readonly_retained_workspace(terminal_evidence)
    # The immutable quality-review binding must pin the exact bool-safe
    # reviewed-parent claim epoch and the current reviewer adapter identity.
    retained_binding = terminal_evidence.get("quality_review")
    if not isinstance(retained_binding, dict):
        raise WorkspaceError("quality_reviewer_retained_binding_missing")
    bound_claim_epoch = retained_binding.get("target_claim_epoch")
    if (
        not _is_bool_safe_int(bound_claim_epoch)
        or bound_claim_epoch != target.get("claim_epoch")
    ):
        raise WorkspaceError("quality_reviewer_claim_epoch_binding_mismatch")
    if str(retained_binding.get("adapter_id") or "") != observed_provider:
        raise WorkspaceError("quality_reviewer_adapter_binding_mismatch")
    # The reviewer's own card must carry an empty writable surface.
    card_allowed_writes = card.get("allowed_writes")
    if not isinstance(card_allowed_writes, list) or card_allowed_writes:
        raise WorkspaceError("quality_reviewer_card_allowed_writes_not_empty")
    return receipt


def _enforce_quality_review_launch_binding(
    topic: str, quality_review_binding: dict[str, Any] | None
) -> None:
    """Keep reviewer launches on the packet-bound authority path.

    A blocked reviewer must be relaunched through ``launch_quality_reviewer``
    against the still-retained target request.  Treating it as an ordinary
    recovered read-only task drops the immutable target packet and can turn an
    ungrounded prose response into apparent review work.
    """

    if topic == "quality_review" and quality_review_binding is None:
        raise LaunchRejected("quality_review_binding_required")
    if topic != "quality_review" and quality_review_binding is not None:
        raise LaunchRejected("quality_review_binding_topic_mismatch")


MAX_OWNER_PROMPT_BYTES = 16 * 1024
MAX_CRASH_RETRY_PACKET_BYTES = 12 * 1024
MAX_CRASH_RETRY_STREAM_BYTES = 2 * 1024
MAX_TASK_CONTRACT_BYTES = 96 * 1024
MAX_REWORK_TASK_CONTRACT_BYTES = 48 * 1024
MAX_WORKER_PROMPT_BYTES = 160 * 1024
MAX_REWORK_WORKER_PROMPT_BYTES = 112 * 1024


def build_worker_prompt(
    *,
    task_id: str,
    runner: str,
    topic: str,
    card: dict[str, Any] | None = None,
    owner_prompt: str = "",
    project_context_bundle: str = "",
    crash_retry_packet: dict[str, Any] | None = None,
    _budget_report: dict[str, Any] | None = None,
) -> str:
    extra = owner_prompt.strip()
    owner_bytes = len(extra.encode("utf-8"))
    if owner_bytes > MAX_OWNER_PROMPT_BYTES:
        raise ValueError("owner_prompt_too_large")
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
        "read_only",
    )
    contract = {
        key: card[key]
        for key in contract_keys
        if card is not None and key in card
    }
    def strip_persistence_envelopes(value: Any, *, depth: int = 0) -> Any:
        if depth > 16:
            raise ValueError("task_contract_too_deep")
        if isinstance(value, dict):
            return {
                str(key): strip_persistence_envelopes(item, depth=depth + 1)
                for key, item in value.items()
                if str(key) != "card_json"
            }
        if isinstance(value, list):
            return [strip_persistence_envelopes(item, depth=depth + 1) for item in value]
        return value

    contract = strip_persistence_envelopes(contract)
    contract.update({"task_id": task_id, "runner": runner, "topic": topic})
    # One-line canonical JSON removes indentation/newline overhead without
    # weakening the exact task contract or its stable identity.
    contract_json = json.dumps(contract, ensure_ascii=False, sort_keys=True)
    contract_bytes = len(contract_json.encode("utf-8"))
    rework = bool(
        card is not None
        and (card.get("rework_predecessor") or card.get("review_feedback"))
    )
    contract_cap = (
        MAX_REWORK_TASK_CONTRACT_BYTES if rework else MAX_TASK_CONTRACT_BYTES
    )
    if contract_bytes > contract_cap:
        raise ValueError("task_contract_too_large")
    bundle_sha256 = hashlib.sha256(project_context_bundle.encode("utf-8")).hexdigest()
    section_count = 0
    if project_context_bundle.strip():
        try:
            payload = json.loads(project_context_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
            section_count = _worker_context_section_count(payload)
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
        }, sort_keys=True, separators=(",", ":"))
        if project_context_bundle.strip()
        else ""
    )
    retry_json = ""
    if crash_retry_packet is not None:
        retry_json = json.dumps(
            crash_retry_packet,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(retry_json.encode("utf-8")) > MAX_CRASH_RETRY_PACKET_BYTES:
            raise ValueError("crash_retry_packet_too_large")
    retry_block = (
        "\n\nTrusted predecessor crash evidence (bounded and coordinator-bound; "
        "do not infer current files from this text):\nCRASH_RETRY_PACKET_JSON:\n"
        + retry_json
        + "\nEND_CRASH_RETRY_PACKET_JSON"
        if retry_json
        else ""
    )
    # Keep every invariant instruction before the first task-specific byte.
    # Provider prefix caches can then reuse this complete policy block across
    # unrelated tasks; the task contract, context receipt, and owner text stay
    # after the stable boundary. This is a structural optimization only --
    # cache savings remain unclaimed until provider telemetry observes them.
    stable_prefix = agent_tool_instructions.render_worker_runtime_policy()
    prompt = (
        stable_prefix
        + "\n\nTASK_CONTRACT_JSON:\n"
        + contract_json
        + "\nEND_TASK_CONTRACT_JSON"
        + context_block
        + retry_block
        + suffix
    )
    prompt_bytes = len(prompt.encode("utf-8"))
    prompt_cap = MAX_REWORK_WORKER_PROMPT_BYTES if rework else MAX_WORKER_PROMPT_BYTES
    if prompt_bytes > prompt_cap:
        raise ValueError("worker_prompt_too_large")
    if _budget_report is not None:
        context_bytes = len(project_context_bundle.encode("utf-8"))
        retry_bytes = len(retry_json.encode("utf-8"))
        static_bytes = max(
            0,
            prompt_bytes - contract_bytes - context_bytes - owner_bytes - retry_bytes,
        )
        _budget_report.update({
            "schema_id": "aiworkhub.worker_prompt_budget.v1",
            "mode": "rework_delta" if rework else "initial",
            "total_bytes": prompt_bytes,
            "max_bytes": prompt_cap,
            "remaining_bytes": prompt_cap - prompt_bytes,
            "utilization_percent": round((prompt_bytes / prompt_cap) * 100.0, 2),
            "sections": {
                "task_contract_bytes": contract_bytes,
                "project_context_bytes": context_bytes,
                "owner_context_bytes": owner_bytes,
                "crash_retry_evidence_bytes": retry_bytes,
                "runtime_instructions_bytes": static_bytes,
            },
            "stable_prefix_bytes": len(stable_prefix.encode("utf-8")),
            "stable_prefix_precedes_task_contract": True,
            "provider_cache_savings_observed": False,
            "byte_labels_are_token_truth": False,
            "delta_rework": rework,
        })
    return prompt


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
    bridge_request: vscode_lm_bridge.BridgeRequest | None = None


class _QualityReviewPrepFlight:
    """Single-flight guard for one ``(target_request_id, target_task_id)`` prep.

    The elected owner runs the heavy packet build; every other concurrent
    caller waits on ``condition`` and reuses the owner's ``result`` (success
    or failure) instead of rebuilding independently.
    """

    __slots__ = ("condition", "done", "result")

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.done = False
        self.result: dict[str, Any] | None = None


class ProcessManager:
    """Thread-safe local process registry with append-only lifecycle events."""

    # Bounded replays allowed while proving a ledger snapshot was read under a
    # single unchanged generation (see ``_latest_by_request_stable``).
    _LEDGER_SNAPSHOT_MAX_ATTEMPTS = 8
    _REVIEWER_TERMINAL_INTENT_SUFFIX = ".reviewer-terminal-intent.json"
    # A committed reviewer whose owner/provider process is proven dead lost its
    # liveness, which is exactly what this terminal substatus states.
    _REVIEWER_TERMINAL_INTENT_SUBSTATUS = "liveness_lost"
    # Dispositions proving the intent is on disk and settlement is guaranteed
    # to be attempted; only these authorize the terminalizing ledger event.
    _DURABLE_TERMINAL_INTENT_DISPOSITIONS = frozenset({"recorded", "already_recorded"})
    # Sibling marker proving one unsettleable intent was already reported, so
    # the diagnostic ledger records it once instead of growing without bound
    # on every reconciliation pass for as long as the operator leaves it there.
    _TERMINAL_INTENT_DIAGNOSED_SUFFIX = ".diagnosed"
    # Sibling marker proving one retired-without-effect intent was already
    # reported.  Distinct from the diagnosed marker so a repaired intent that
    # later meets a final refusal still earns a line under its own reason.
    _TERMINAL_INTENT_RETIRED_SUFFIX = ".retired"
    # Ceiling on diagnostics emitted per settlement pass.  Undiagnosed intents
    # keep their marker unwritten and are reported by a later pass, so the cap
    # bounds one pass rather than silently dropping evidence.
    _TERMINAL_INTENT_DIAGNOSTICS_PER_PASS = 8
    # Store refusal strings carry the observed value after a colon, so the
    # recorded reason is bounded rather than trusting the store's length.
    _TERMINAL_INTENT_DIAGNOSTIC_REASON_MAX = 200
    # Marker proving one proven-dead reservation whose identity could not be
    # bound was already reported.  It sits beside the intent files rather than
    # next to an intent, because the whole point is that no intent exists.
    _IDENTITY_INCOMPLETE_DIAGNOSED_SUFFIX = ".identity-incomplete.diagnosed"
    # Enough digest to separate distinct identity episodes for one request
    # without letting an attacker-chosen field grow the filename without bound.
    _IDENTITY_EPISODE_DIGEST_CHARS = 16
    # Sibling marker naming one settlement pass that failed outright.  Keyed by
    # exception type so a fault repeating on every pass is reported once, while
    # a genuinely different fault is still worth a line of its own.
    _SETTLEMENT_FAILURE_DIAGNOSED_SUFFIX = ".settlement-failed"
    _SETTLEMENT_FAILURE_KIND_MAX = 64
    # Marker naming one ledger segment whose generation can never be described.
    # Keyed by a digest of the segment so the line is emitted once however many
    # launches run against it, while a different bad segment still earns one.
    _UNPROVABLE_LEDGER_DIAGNOSED_SUFFIX = ".unprovable-ledger.diagnosed"

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
        self._collision_guard = collision_guard or core.launch_collision_guard
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
        chmod_fd(fd, 0o600)
        with os.fdopen(fd, "a+", encoding="utf-8") as fh:
            lock_fd(fh.fileno(), blocking=True)
            try:
                yield
            finally:
                unlock_fd(fh.fileno())

    @contextmanager
    def _launch_reservation(self, event: dict[str, Any]):
        """Reserve a cross-process launch slot without serializing setup.

        Worktree creation, runtime provisioning, Source Graph orientation and
        prompt construction can be expensive on large repositories. Holding
        the global registry lock across those operations serialized otherwise
        independent launches and produced 60--90 second queueing. A bounded
        ``starting`` event is the durable reservation observed by every
        ProcessManager; the lock is released before the body runs.
        """

        reservation = {
            **event,
            "state": "starting",
            "reservation_expires_at_epoch": time.time() + 120.0,
        }
        # The stable snapshot may replay the whole ledger up to
        # ``_LEDGER_SNAPSHOT_MAX_ATTEMPTS`` times.  Taking it here, before the
        # cross-process registry lock, keeps that amplified work off every
        # unrelated launch acknowledgement waiting on the same lock.
        snapshot = self._latest_by_request_stable()
        try:
            with self._lock, self._registry_lock():
                # ONE sweep re-proves the handed-in snapshot for this whole
                # critical section.  Everything below decides admission from
                # it and never re-parses: ``_append_event`` does not take this
                # lock, so a plain parse here could be interleaved by another
                # ProcessManager -- or by a supervisor publishing its
                # ``running`` row -- and would simply not see the reservation
                # or duplicate it is about to contradict.  Reconciliation
                # mirrors the rows it retires back into the snapshot, so it
                # keeps describing the ledger exactly across both halves.
                latest, generation = self._resolved_reservation_snapshot(snapshot)
                if generation is None:
                    # No stable generation could be shown, so any row read
                    # here may be one append behind.  Admitting on that could
                    # duplicate a live task or overrun the limit, so the
                    # launch defers instead of guessing.
                    raise LaunchRejected("ledger_snapshot_unproven")
                self._reconcile_expired_starting_reservations(
                    (latest, generation), resolved=True
                )
                if self._active_count(latest) >= _configured_limit():
                    raise LaunchRejected("concurrency_limit_reached")
                self._assert_no_duplicate_task(
                    str(event.get("task_id") or ""), latest
                )
                self._append_event(reservation)
        finally:
            # Reconciliation records terminal *intent* under the lock and
            # settles it here, with the lock released.  A task-store read
            # behind the outer registry lock would make an unrelated
            # reservation acknowledgement wait on SQLite, which is exactly the
            # queueing this reservation boundary exists to prevent.  Contained,
            # because a settlement failure must never displace the
            # ``LaunchRejected`` this block may be unwinding.
            self._settle_reviewer_terminal_intents_contained()
        yield

    @contextmanager
    def _request_lock(self, request_id: str, *, blocking: bool = True):
        """Serialize one request's reconciliation without blocking launches.

        Finalization can run validation, quality gates, usage extraction and
        retained-workspace cleanup. Holding the global launch registry across
        that work caused unrelated Windows launches to hit the bounded
        20-second advisory-lock timeout. A hash-derived, repository-local lock
        keeps duplicate finalizers for the *same* request mutually exclusive
        while independent requests and the short launch registry proceed.
        """

        identity = hashlib.sha256(str(request_id).encode("utf-8")).hexdigest()
        lock_path = self.process_dir / ".request-locks" / f"{identity}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_APPEND | os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
        chmod_fd(fd, 0o600)
        with os.fdopen(fd, "a+", encoding="utf-8") as fh:
            lock_fd(fh.fileno(), blocking=blocking)
            try:
                yield
            finally:
                unlock_fd(fh.fileno())

    @contextmanager
    def _promotion_lock(self):
        """Serialize canonical review promotion without blocking launches.

        Review acceptance may build a combined tree, rerun validations and
        promote files. Those operations must remain cross-task serialized,
        but they must not occupy the short-lived launch registry lock.  A
        dedicated lock preserves atomic promotion while allowing unrelated
        workers and finalizers to continue.
        """

        lock_path = self.process_dir / ".promotion.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_APPEND | os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
        chmod_fd(fd, 0o600)
        with os.fdopen(fd, "a+", encoding="utf-8") as fh:
            lock_fd(fh.fileno(), blocking=True)
            try:
                yield
            finally:
                unlock_fd(fh.fileno())

    def _append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        clean = {
            "schema_id": "aiworkhub.task_mcp.process_event.v1",
            "timestamp": _utcnow(),
            **event,
        }
        process_event_ledger.append_event(self.process_log_path, clean)
        return clean

    def _events(self) -> list[dict[str, Any]]:
        return list(process_event_ledger.iter_events(self.process_log_path))

    def _ledger_generation(self) -> tuple[tuple[str, int, int, int, int], ...] | None:
        """Return the exact per-segment generation of the process ledger.

        ``None`` means the ledger could not be described exactly (a segment
        vanished or is not a regular file mid-read), which is never treated as
        "unchanged".
        """

        signatures: list[tuple[str, int, int, int, int]] = []
        for ledger in process_event_ledger.ledger_paths(self.process_log_path):
            try:
                info = ledger.lstat()
            except OSError:
                return None
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return None
            signatures.append((
                str(ledger),
                int(info.st_dev),
                int(info.st_ino),
                int(info.st_size),
                int(info.st_mtime_ns),
            ))
        return tuple(signatures)

    def _unprovable_ledger_segment_name(self) -> str:
        """Name the segment behind an undescribable generation, if one shows.

        ``_ledger_generation`` refuses a segment that is a symlink or not a
        regular file, and refuses one whose ``lstat`` fails under it.  This
        says WHICH, so the diagnostic sends an operator to a file instead of
        to a directory.  It is naming only -- never the proof of anything --
        so when the cause will not hold still long enough to be named it falls
        back to the active ledger rather than inventing a verdict.
        """

        try:
            segments = process_event_ledger.ledger_paths(self.process_log_path)
        except OSError:
            segments = []
        for ledger in segments:
            try:
                info = ledger.lstat()
            except OSError:
                return ledger.name
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return ledger.name
        return self.process_log_path.name

    def _latest_by_request(self) -> dict[str, dict[str, Any]]:
        """Latest event per request from a single UNPROVEN ledger pass.

        This is a plain read and it carries no anti-hidden-append authority.
        The registry lock does not supply one either: ``_append_event`` never
        takes that lock, so a supervisor publishing its ``running`` row -- or
        any other ProcessManager -- can land a write in the middle of this
        parse, and the row that would contradict the reader is simply not
        seen.  Holding the lock excludes other *lock takers*, not appenders.

        So every caller whose decision a hidden append could falsify -- the
        spawn/attach CAS and anything about to terminalize -- reads through
        the bracketed generation proof (``_latest_by_request_stable``, or the
        one-sweep re-proof in ``_resolved_reservation_snapshot``) and fails
        closed when no stable generation can be shown.  What remains here is
        the reporting path, where a snapshot one append behind is a stale
        number rather than a false verdict.
        """

        latest: dict[str, dict[str, Any]] = {}
        for event in self._events():
            request_id = str(event.get("request_id") or "")
            if request_id:
                latest[request_id] = event
        return latest

    def _latest_by_request_stable(
        self,
    ) -> tuple[dict[str, dict[str, Any]], tuple[Any, ...] | None]:
        """Return the latest event per request plus the generation proving it.

        A snapshot taken while another ProcessManager appends can observe the
        ledger mid-write and silently hide the newer row -- and a hidden append
        is precisely what turns a terminal verdict into a false one.  Bracket
        the read with the exact per-segment generation and replay until the
        pre- and post-read generations are identical.  The second element is
        ``None`` when no stable read was obtained within the bounded attempts;
        callers that terminalize must fail closed on it instead of treating an
        unproven snapshot as evidence.
        """

        latest: dict[str, dict[str, Any]] = {}
        for _attempt in range(self._LEDGER_SNAPSHOT_MAX_ATTEMPTS):
            before = self._ledger_generation()
            if before is None and self._ledger_generation() is None:
                # Two undescribable reads back to back with NO parse between
                # them.  A rotation landing mid-bracket cannot look like that;
                # a segment that is a symlink or not a regular file looks like
                # exactly that, forever.  Replaying the whole attempt budget
                # against a standing condition costs a full parse and two
                # sweeps of the entire ledger per attempt, on every launch,
                # and can never end differently -- so stop at the proof it is
                # standing and name it once.  The unproven ``None`` generation
                # still goes back, so reconciliation stays exactly as
                # fail-closed as it was.
                self._diagnose_unprovable_ledger()
                return latest, None
            latest = self._latest_by_request()
            after = self._ledger_generation()
            if before is not None and before == after:
                return latest, after
        return latest, None

    def _reviewer_terminal_intent_path(self, request_id: str) -> Path:
        identity = hashlib.sha256(str(request_id).encode("utf-8")).hexdigest()
        return self.process_dir / f"{identity}{self._REVIEWER_TERMINAL_INTENT_SUFFIX}"

    def _record_reviewer_terminal_intent(
        self,
        request_id: str,
        event: Mapping[str, Any],
        blocked_reason: str,
    ) -> str:
        """Durably declare a terminal transition before performing it.

        The process ledger and the task store are two independent durable
        stores and the registry lock deliberately does not span the second one.
        Without a recorded intent, a crash between them strands the reviewer
        card in ``processing`` with no evidence of what was meant to happen.
        The intent names the exact task, request and claim epoch, so a later
        pass finishes *that* transition instead of inventing a new one.

        An intent is only recorded when the reservation carries the complete
        identity: an incomplete identity fails closed and is never settled.
        """

        task_id = str(event.get("task_id") or "").strip()
        runner = str(event.get("runner") or "").strip()
        claim_epoch = event.get("reviewer_claim_epoch")
        if not _is_bool_safe_int(claim_epoch):
            return "identity_incomplete"
        if not task_id or not runner or claim_epoch < 1:
            return "identity_incomplete"
        path = self._reviewer_terminal_intent_path(request_id)
        if path.is_file():
            return "already_recorded"
        try:
            write_json_0600(path, {
                "schema_id": REVIEWER_TERMINAL_INTENT_SCHEMA_ID,
                "request_id": str(request_id),
                "task_id": task_id,
                "runner": runner,
                "reviewer_claim_epoch": claim_epoch,
                "substatus": self._REVIEWER_TERMINAL_INTENT_SUBSTATUS,
                "blocked_reason": blocked_reason,
                "recorded_at": _utcnow(),
            })
        except OSError:
            return "record_failed"
        return "recorded"

    def _resolved_reservation_snapshot(
        self,
        snapshot: tuple[
            dict[str, dict[str, Any]], tuple[Any, ...] | None
        ] | None,
    ) -> tuple[dict[str, dict[str, Any]], tuple[Any, ...] | None]:
        """Resolve the stable snapshot one reconciliation pass may act on.

        ``_latest_by_request_stable`` replays until the bracketing generations
        agree, so a busy ledger costs up to ``_LEDGER_SNAPSHOT_MAX_ATTEMPTS``
        full parses and twice as many per-segment sweeps.  Paying that under
        the cross-process registry lock made every unrelated reservation
        acknowledgement queue behind an unbounded-looking read, which is the
        exact serialization the reservation boundary exists to prevent.

        So reservation callers take the snapshot with the lock RELEASED and
        hand it in.  A single ``_ledger_generation`` sweep re-proves it here:
        the snapshot is authority only while it still describes the exact
        ledger this pass is about to append to.  That keeps the authority
        identical to reading it inline -- a concurrent append between the
        snapshot and the lock is seen and defers the pass, exactly as an
        unstable bracketed read does -- while the work done under the lock is
        one sweep and no parse at all.
        """

        if snapshot is None:
            return self._latest_by_request_stable()
        latest, generation = snapshot
        if generation is None or self._ledger_generation() != generation:
            # Either the caller never proved its snapshot, or the ledger moved
            # while it waited for the lock.  Both may hide a row this pass
            # would contradict, so it terminalizes nothing and retries later.
            return latest, None
        return latest, generation

    def _proven_reservation_snapshot(
        self,
        snapshot: tuple[
            dict[str, dict[str, Any]], tuple[Any, ...] | None
        ] | None,
    ) -> tuple[dict[str, dict[str, Any]], tuple[Any, ...]] | None:
        """The one proven snapshot an in-lock decision may be taken from.

        ``snapshot`` was taken with the registry lock RELEASED, so the cheap
        path is the single sweep in ``_resolved_reservation_snapshot`` that
        re-proves it.  That hand-off loses its race whenever a sibling attempt
        appended while this one waited for the lock -- the ordinary case when
        several reviewers commit or terminalize together -- and treating the
        loss as "no authority" would make these one-shot decisions silently do
        nothing at all.  So a lost hand-off falls back to a fresh bracketed
        read, paid for only then, and the decision is still taken from a proven
        generation rather than from a parse a hidden append could falsify.

        ``None`` means no stable generation could be shown at all: an
        undescribable segment, or a ledger churning faster than the bounded
        attempts.  Every caller fails closed on it and decides nothing.
        """

        latest, generation = self._resolved_reservation_snapshot(snapshot)
        if generation is None:
            latest, generation = self._latest_by_request_stable()
        if generation is None:
            return None
        return latest, generation

    def _terminalize_committed_reservation(
        self,
        request_id: str,
        event: Mapping[str, Any],
        blocked_reason: str,
        diagnostics_left: int,
    ) -> tuple[bool, int]:
        """Terminalize one proven-dead committed reservation, intent first.

        The intent is written before the ledger event so that every point after
        this line is recoverable: a crash leaves an exact, replayable record of
        which claim still has to be released.  No task-store I/O happens here --
        the caller may hold the outer registry lock, and SQLite must never be
        reached from under it.

        Returns ``(terminalized, diagnostics_left)``.  ``terminalized`` states
        whether the reservation was terminalized in the ledger, which happens
        only once the intent is durable.  The ``blocked`` event is what
        releases the reservation: appending it with no intent on disk strands
        the reviewer card in ``processing`` with nothing left to finish the
        transition, and it erases the committed row a later pass would need in
        order to try again.  Declining to append keeps the two durable stores
        agreeing and makes a failed intent write retryable.

        ``diagnostics_left`` is the caller's per-pass diagnostic budget,
        threaded through so one pass over many unbindable rows emits a bounded
        number of lines instead of one per row.
        """

        disposition = self._record_reviewer_terminal_intent(
            request_id, event, blocked_reason
        )
        if disposition not in self._DURABLE_TERMINAL_INTENT_DISPOSITIONS:
            if disposition == "identity_incomplete":
                # No intent was written, and appending the terminalizing event
                # without one is exactly what the docstring above forbids, so
                # this reservation moves nowhere.  Every later pass re-derives
                # the same refusal from the same unchanged row: a proven-dead
                # reservation that is permanently unterminalizable, in total
                # silence.  One bounded line keyed to this exact request and
                # identity episode is the only evidence an operator gets that
                # the card needs a hand repair.  ``record_failed`` is
                # deliberately NOT reported here -- that write is transient and
                # a later pass is expected to succeed, so naming it would turn
                # a passing retry into permanent operator noise.
                _recorded, diagnostics_left = (
                    self._diagnose_identity_incomplete_reservation(
                        request_id, event, blocked_reason, diagnostics_left
                    )
                )
            return False, diagnostics_left
        self._append_event({
            "request_id": request_id,
            "task_id": event.get("task_id"),
            "runner": event.get("runner"),
            "topic": event.get("topic"),
            "adapter_id": event.get("adapter_id"),
            "state": "blocked",
            "blocked_reason": blocked_reason,
            "reservation_expires_at_epoch": event.get(
                "reservation_expires_at_epoch"
            ),
            "reviewer_claim_epoch": event.get("reviewer_claim_epoch"),
            "terminal_intent": disposition,
        })
        return True, diagnostics_left

    def _identity_incomplete_marker(
        self, request_id: str, event: Mapping[str, Any], blocked_reason: str
    ) -> Path:
        """Marker naming one exact request AND the identity episode observed.

        Keyed on the request the way the intent file is, plus a digest of the
        identity fields that were actually present.  Keying on the request
        alone would silence a genuinely different episode -- a re-claimed card
        arriving with a new epoch, or a different blocked reason -- behind the
        first line ever written for that request.  Keying on the epoch alone is
        impossible here: a missing epoch is precisely the defect being named.
        """

        identity = hashlib.sha256(str(request_id).encode("utf-8")).hexdigest()
        episode = hashlib.sha256(
            json.dumps(
                [
                    str(event.get("task_id") or ""),
                    str(event.get("runner") or ""),
                    repr(event.get("reviewer_claim_epoch")),
                    str(blocked_reason),
                ],
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[: self._IDENTITY_EPISODE_DIGEST_CHARS]
        return self.process_dir / (
            f"{identity}.{episode}{self._IDENTITY_INCOMPLETE_DIAGNOSED_SUFFIX}"
        )

    def _diagnose_identity_incomplete_reservation(
        self,
        request_id: str,
        event: Mapping[str, Any],
        blocked_reason: str,
        remaining: int,
    ) -> tuple[bool, int]:
        """Name one proven-dead reservation whose identity cannot be bound.

        The reservation is real and its owner is proven dead, but it carries no
        task/runner/claim epoch to bind a terminal transition to, so it is
        never terminalized and never released -- and, without this, never
        mentioned either.  Claiming the marker with ``O_EXCL`` is what bounds
        it: repeated passes and concurrent settlers in other processes find the
        marker present and stay silent, so the ledger gets exactly one line per
        request/episode however long the reservation sits there.  The marker is
        released again if the line itself fails to land, so a transient
        filesystem failure retries instead of suppressing the evidence forever.

        ``remaining`` is the same per-pass ceiling settlement uses.  The marker
        bounds one EPISODE for all time, but a reconciliation pass can meet
        arbitrarily many distinct unbindable rows at once, and each first
        sighting costs a marker plus a ledger line.  An episode left unreported
        keeps its marker unclaimed, so a later pass names it rather than this
        one emitting the whole pile.

        This records evidence only.  It appends no process event and reaches no
        task store, so the caller still fails closed and the reservation is
        left exactly as it was found.

        Returns ``(recorded, remaining)`` the way
        ``_record_intent_diagnostic`` does: ``recorded`` states that
        operator-visible evidence for this exact episode now exists, written
        here or by an earlier pass.
        """

        if remaining <= 0:
            return False, remaining
        marker = self._identity_incomplete_marker(request_id, event, blocked_reason)
        try:
            self.process_dir.mkdir(parents=True, exist_ok=True)
            os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            # Already reported for this exact episode, so the evidence exists
            # and this pass spends none of its budget re-stating it.
            return True, remaining
        except OSError:
            # Nothing here is writable, so nothing could be recorded.
            return False, remaining
        reason = f"identity_incomplete:{blocked_reason}"
        if not self._append_intent_diagnostic({
            "schema_id": REVIEWER_TERMINAL_INTENT_DIAGNOSTIC_SCHEMA_ID,
            "recorded_at": _utcnow(),
            # No intent file exists -- that is the defect -- so the line names
            # the path one WOULD have occupied, which is derived from the exact
            # request and ties the diagnostic back to it.
            "intent_file": self._reviewer_terminal_intent_path(request_id).name,
            "reason": reason[: self._TERMINAL_INTENT_DIAGNOSTIC_REASON_MAX],
            "bytes": -1,
            "sha256": "",
        }):
            unlink_if_regular(marker)
            return False, remaining
        return True, remaining - 1

    @staticmethod
    def _terminal_intent_is_resolved(state: str) -> bool:
        """Return whether no future pass could still move this exact claim.

        The vocabulary belongs to the store that produces these states, so it
        is read from ``task_store`` rather than restated here: a copy would
        silently stop matching the day a new fail-closed state is added, and
        the intent would then be retried forever against a card it may never
        legally move.
        """

        return task_store.terminal_failure_state_is_final(state)

    def _terminal_intent_diagnosed_marker(self, path: Path) -> Path:
        """Sibling marker proving one unusable intent was already reported."""
        return path.with_name(path.name + self._TERMINAL_INTENT_DIAGNOSED_SUFFIX)

    def _terminal_intent_retired_marker(self, path: Path) -> Path:
        """Sibling marker proving one retired intent was already reported.

        Kept distinct from the diagnosed marker so an intent that was once
        unbindable, then repaired, still gets its own line when it later meets
        a final refusal instead of being retired under the stale reason.
        """
        return path.with_name(path.name + self._TERMINAL_INTENT_RETIRED_SUFFIX)

    def _append_intent_diagnostic(self, record: dict[str, Any]) -> bool:
        """Append one line to the terminal-intent diagnostic ledger.

        The single writer for every operator-visible line this settler emits,
        so the ledger cannot grow a second shape.  The record deliberately
        reaches neither the process event ledger nor the task store: it is
        evidence about a claim, never an instruction to move one.  Returns
        whether the line is durable, because callers use that to decide
        whether they may retire the thing they were reporting.
        """

        audit_path = reviewer_terminal_intent_diagnostic_path(self.process_log_path)
        try:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            with audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            return False
        return True

    def _record_intent_diagnostic(
        self, path: Path, reason: str, remaining: int, *, marker_suffix: str
    ) -> tuple[bool, int]:
        """Record one bounded operator-visible line about a single intent.

        Creating the sibling marker with ``O_EXCL`` is what claims the right to
        write the line, so a concurrent settler in another process cannot
        double it, and the marker is released again if the write itself fails.
        ``remaining`` caps how many lines one pass may emit; an intent left
        unreported keeps no marker and is reported by a later pass instead of
        being dropped.

        Returns ``(recorded, remaining)``.  ``recorded`` states that
        operator-visible evidence for this intent now exists -- written here or
        by an earlier pass -- which is what lets a caller retire an intent only
        once the reason it is going is on the record.
        """

        if remaining <= 0:
            return False, remaining
        marker = path.with_name(path.name + marker_suffix)
        try:
            os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            # An earlier pass, or a settler in another process, already put
            # this intent on the record; the evidence exists either way.
            return True, remaining
        except OSError:
            # The directory is unwritable, so nothing could be recorded and the
            # caller must not treat this intent as reported.
            return False, remaining
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            size = path.stat().st_size
        except OSError:
            digest = ""
            size = -1
        recorded = self._append_intent_diagnostic({
            "schema_id": REVIEWER_TERMINAL_INTENT_DIAGNOSTIC_SCHEMA_ID,
            "recorded_at": _utcnow(),
            "intent_file": path.name,
            "reason": str(reason)[: self._TERMINAL_INTENT_DIAGNOSTIC_REASON_MAX],
            "bytes": size,
            "sha256": digest,
        })
        if not recorded:
            # The claim is worthless without the line it was claiming, so give
            # it back rather than suppressing the diagnostic forever.
            unlink_if_regular(marker)
            return False, remaining
        return True, remaining - 1

    def _diagnose_unsettleable_intent(
        self, path: Path, reason: str, remaining: int
    ) -> tuple[bool, int]:
        """Record one bounded line for an intent that can never be settled.

        An intent whose bytes will not parse, whose schema is foreign, whose
        declared substatus is not store vocabulary, or whose identity cannot be
        bound to an exact task/request/claim epoch is deliberately never acted
        on and never deleted -- which also makes it silent.  One line naming
        the file, the reason and the bytes actually on disk is the only
        evidence an operator gets that a reviewer card is waiting on a hand
        repair.
        """

        return self._record_intent_diagnostic(
            path,
            reason,
            remaining,
            marker_suffix=self._TERMINAL_INTENT_DIAGNOSED_SUFFIX,
        )

    def _diagnose_retired_intent(
        self, path: Path, state: str, remaining: int
    ) -> tuple[bool, int]:
        """Record one bounded line for an intent retired having moved no card.

        A final refusal that is not this intent's own completed transition
        proves no later pass could ever move the claim, so the ticket must go.
        It moved nothing, though, so retiring it silently leaves an operator
        unable to tell that outcome from a settlement that worked -- and the
        reviewer card it names keeps whatever state some other authority left
        it in with nothing saying why this intent gave up.
        """

        return self._record_intent_diagnostic(
            path,
            f"final_refusal:{state}",
            remaining,
            marker_suffix=self._TERMINAL_INTENT_RETIRED_SUFFIX,
        )

    def _diagnose_unroutable_callback(
        self, path: Path, reason: str, remaining: int
    ) -> tuple[bool, int]:
        """Record one bounded line for a callback that can never be routed.

        The transition this intent owns really did land, and the callback it
        owes names an identity the store will never enqueue -- an unbound
        origin thread or a provider outside its routing vocabulary.  Retrying
        that forever strands the intent and the claim behind it, so the ticket
        is retired; retiring it in silence would hide a manager wake that is
        genuinely lost, so the truthful reason goes on the record first.

        It shares the retired marker with ``_diagnose_retired_intent`` because
        both describe the same event for one ticket -- this pass is retiring
        it -- and a ticket may only ever be retired once.  ``reason`` comes
        from the store's own fixed vocabulary, never from card content, so the
        line stays bounded.
        """

        return self._record_intent_diagnostic(
            path,
            f"callback_unroutable:{reason}",
            remaining,
            marker_suffix=self._TERMINAL_INTENT_RETIRED_SUFFIX,
        )

    def _diagnose_settlement_pass_failure(self, error: BaseException) -> None:
        """Name a settlement pass that failed outright, once per exception type.

        Every store, filesystem and lock failure is already contained per
        intent, so reaching here means a programming fault, or an
        unavailability no per-intent guard covers.  The caller must still
        return 0 -- its launch outcome may not be replaced by an unrelated
        reservation error -- and 0 is exactly what "no intents were pending"
        returns, so without this line a settler that can never run looks idle
        forever.  One line per exception type keeps a fault that repeats on
        every pass from growing the ledger without bound.
        """

        kind = "".join(
            ch for ch in type(error).__name__ if ch.isalnum() or ch == "_"
        )[: self._SETTLEMENT_FAILURE_KIND_MAX] or "unknown"
        marker = self.process_dir / (
            kind + self._SETTLEMENT_FAILURE_DIAGNOSED_SUFFIX
        )
        try:
            self.process_dir.mkdir(parents=True, exist_ok=True)
            os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except OSError:
            # Already named once, or nothing here is writable.
            return
        if not self._append_intent_diagnostic({
            "schema_id": REVIEWER_TERMINAL_INTENT_DIAGNOSTIC_SCHEMA_ID,
            "recorded_at": _utcnow(),
            "intent_file": "",
            "reason": f"settlement_pass_failed:{kind}",
            "bytes": -1,
            "sha256": "",
        }):
            unlink_if_regular(marker)

    def _diagnose_unprovable_ledger(self) -> None:
        """Name a standing undescribable ledger segment exactly once.

        ``_latest_by_request_stable`` stops replaying the moment the failure
        is proved standing, so without this line every launch would fail
        closed for good against a ledger nobody can repair because nobody is
        told it is broken.  The marker is keyed by a bounded digest of the
        segment, so the single line survives repeated launches while a
        genuinely different unusable segment still earns one of its own.
        """

        segment = self._unprovable_ledger_segment_name()
        digest = hashlib.sha256(segment.encode("utf-8", "surrogatepass")).hexdigest()
        marker = self.process_dir / (
            digest[: self._IDENTITY_EPISODE_DIGEST_CHARS]
            + self._UNPROVABLE_LEDGER_DIAGNOSED_SUFFIX
        )
        try:
            self.process_dir.mkdir(parents=True, exist_ok=True)
            os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except OSError:
            # Already named once, or nothing here is writable.
            return
        if not self._append_intent_diagnostic({
            "schema_id": REVIEWER_TERMINAL_INTENT_DIAGNOSTIC_SCHEMA_ID,
            "recorded_at": _utcnow(),
            "intent_file": segment[: self._TERMINAL_INTENT_DIAGNOSTIC_REASON_MAX],
            "reason": "ledger_generation_unprovable",
            "bytes": -1,
            "sha256": "",
        }):
            unlink_if_regular(marker)

    def _settle_reviewer_terminal_intents(self) -> int:
        """Complete every durable reviewer terminal intent exactly once.

        This is the half of reconciliation that touches SQLite, so it runs with
        no registry lock held.  Each intent is settled under its own request
        lock and bound to the exact task/request/claim epoch it recorded: a
        card that was released and re-claimed since fails closed and is never
        terminalized.  The intent is removed only once the transition AND the
        one callback it owes are both settled, so a transient store failure --
        a locked store above all -- retries instead of stranding a processing
        card or silently swallowing the callback.

        Every store failure is contained per intent.  One contended card must
        not abort settlement of the unrelated intents sitting beside it on
        disk, which is the only reason they are all reached from one pass.

        An intent that can never be bound at all is neither acted on nor
        deleted; it is reported once to the diagnostic ledger so the stranded
        card is visible to an operator rather than silent.  An intent that is
        bindable but meets a final refusal really is retired -- no pass could
        move it -- but never before that refusal is on the same record.
        """

        settled = 0
        diagnostics_left = self._TERMINAL_INTENT_DIAGNOSTICS_PER_PASS
        try:
            intents = sorted(
                self.process_dir.glob("*" + self._REVIEWER_TERMINAL_INTENT_SUFFIX)
            )
        except OSError:
            return 0
        for path in intents:
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                # The settler that won this intent retired it between the glob
                # and this read.  That is the benign race the whole design
                # expects, and it is not evidence that anything is unusable.
                continue
            except OSError:
                # The intent is still THERE and its bytes cannot be read, so
                # no pass will ever settle it while that lasts.  It is kept --
                # never acted on, never deleted -- and named once, because
                # treating it as the retired-by-the-winner case above would
                # strand the card in silence for as long as the condition
                # holds.
                _recorded, diagnostics_left = self._diagnose_unsettleable_intent(
                    path, "unreadable_bytes", diagnostics_left
                )
                continue
            try:
                payload = json.loads(raw)
            except ValueError:
                _recorded, diagnostics_left = self._diagnose_unsettleable_intent(
                    path, "unparseable_json", diagnostics_left
                )
                continue
            if (
                not isinstance(payload, dict)
                or payload.get("schema_id") != REVIEWER_TERMINAL_INTENT_SCHEMA_ID
            ):
                _recorded, diagnostics_left = self._diagnose_unsettleable_intent(
                    path, "foreign_schema", diagnostics_left
                )
                continue
            request_id = str(payload.get("request_id") or "")
            task_id = str(payload.get("task_id") or "")
            runner = str(payload.get("runner") or "")
            claim_epoch = payload.get("reviewer_claim_epoch")
            if (
                not request_id
                or not task_id
                or not runner
                or not _is_bool_safe_int(claim_epoch)
                or claim_epoch < 1
            ):
                # An intent we cannot bind to an exact identity is never acted
                # on and never deleted: it stays as evidence for an operator,
                # and the diagnostic ledger says so out loud exactly once.
                _recorded, diagnostics_left = self._diagnose_unsettleable_intent(
                    path, "identity_unbindable", diagnostics_left
                )
                continue
            substatus = str(
                payload.get("substatus") or self._REVIEWER_TERMINAL_INTENT_SUBSTATUS
            ).strip()
            if substatus not in task_store.MARK_TERMINAL_FAILURE_SUBSTATUSES:
                # The store would refuse this with a final
                # ``unsupported_terminal_failure`` and the ticket would be
                # retired having moved nothing.  A substatus outside the
                # store's vocabulary is a malformed intent, so it is validated
                # here and takes the same never-acted-on, never-deleted path as
                # an unbindable identity rather than reaching SQLite at all.
                # It earns its OWN reason, though: the identity here bound
                # perfectly and only the declared substatus is unusable, so
                # reporting it as ``identity_unbindable`` would send an
                # operator to repair task/request/claim-epoch fields that are
                # already correct.
                _recorded, diagnostics_left = self._diagnose_unsettleable_intent(
                    path, "substatus_unsupported", diagnostics_left
                )
                continue
            owed = False
            try:
                with self._request_lock(request_id, blocking=False):
                    # The intent file IS the claim ticket, and it is re-read,
                    # settled and retired inside this lock.  A settler that
                    # queued behind the winner therefore finds the ticket gone
                    # and transitions nothing, instead of leaning on the
                    # store's CAS to absorb a second attempt.
                    try:
                        held = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if held != payload:
                        continue
                    ok, state = task_store.mark_terminal_failure(
                        self.repo,
                        task_id,
                        runner=runner,
                        substatus=substatus,
                        evidence={
                            "request_id": request_id,
                            "error": str(payload.get("blocked_reason") or ""),
                            "reviewer_claim_epoch": claim_epoch,
                        },
                        request_id=request_id,
                        claim_epoch=claim_epoch,
                    )
                    if not ok and not self._terminal_intent_is_resolved(str(state)):
                        # A transient CAS loss a later pass can still win, so
                        # the ticket is kept rather than the transition dropped.
                        continue
                    # A final refusal can also mean this intent's OWN
                    # transition already landed and only its callback is still
                    # owed -- exactly what a crash, or a failed retire below,
                    # leaves behind.  The store proves that from the card it
                    # wrote rather than the launcher inferring it from a
                    # refusal string.
                    owed = ok or task_store.terminal_failure_already_applied(
                        self.repo,
                        task_id,
                        str(state),
                        runner=runner,
                        substatus=substatus,
                        request_id=request_id,
                        claim_epoch=claim_epoch,
                    )
                    if not owed:
                        # A final refusal that moved no card at all.  The
                        # ticket is genuinely spent, but it may only be retired
                        # once the refusal is durable evidence; when the line
                        # cannot be written the intent is retained so a later
                        # pass reports and retires it instead of it vanishing.
                        recorded, diagnostics_left = self._diagnose_retired_intent(
                            path, str(state), diagnostics_left
                        )
                        if not recorded:
                            continue
                    # The store owns the callback vocabulary, the episode
                    # binding and the containment of its own unavailability, so
                    # this is the single post-transition callback authority
                    # rather than a launcher-private copy of the callback
                    # database, and ``claim_epoch`` names the episode the
                    # transition actually moved.  It runs BEFORE the ticket is
                    # retired: the intent is the only thing that brings a later
                    # pass back to this claim, so retiring first would lose the
                    # callback whenever the enqueue could not be written.
                    if owed and not task_store.enqueue_terminal_callback(
                        self.repo,
                        task_id,
                        substatus=substatus,
                        request_id=request_id,
                        claim_epoch=claim_epoch,
                    ) and not task_store.terminal_callback_already_durable(
                        self.repo,
                        task_id,
                        substatus=substatus,
                        request_id=request_id,
                        claim_epoch=claim_epoch,
                    ):
                        # A refused enqueue is three different worlds, and only
                        # the store can tell them apart.  "Not written yet" --
                        # a locked or unreadable store -- keeps the ticket and
                        # retries.  "Already durable" is what a crash between
                        # a successful enqueue and this retire leaves behind;
                        # every later pass would see the same duplicate
                        # refusal, so reading it as "not written yet" would
                        # strand this intent, and the processing claim behind
                        # it, forever.  The proof is authenticated against the
                        # exact task/request/episode/route this intent binds,
                        # so an unknown or mismatched row never retires it.
                        unroutable = (
                            task_store.terminal_callback_identity_unroutable(
                                self.repo, task_id, substatus=substatus,
                            )
                        )
                        if not unroutable:
                            continue
                        # The third world: the transition IS durable and the
                        # callback identity is one the store will never route,
                        # so no pass could ever write the row and no proof
                        # could ever appear.  Retiring it silently would hide a
                        # manager wake that is genuinely lost, so the truthful
                        # disposition goes on the record first and the ticket
                        # is kept whenever that line cannot be written.
                        recorded, diagnostics_left = (
                            self._diagnose_unroutable_callback(
                                path, unroutable, diagnostics_left
                            )
                        )
                        if not recorded:
                            continue
                    unlink_if_regular(path)
                    # A repaired intent may carry a marker from when it was
                    # still unbindable, and a retired one carries the marker
                    # that claimed its refusal line; retiring the ticket
                    # retires both rather than leaving either behind.
                    unlink_if_regular(self._terminal_intent_diagnosed_marker(path))
                    unlink_if_regular(self._terminal_intent_retired_marker(path))
            except (
                AdvisoryLockTimeout,
                OSError,
                sqlite3.Error,
                task_store.TaskStoreError,
            ):
                # Another settler owns this request, or the store is briefly
                # unavailable or locked.  All retry on the next pass, and
                # containing them per intent is what keeps one contended card
                # from aborting settlement of every other intent on disk.
                continue
            if owed:
                settled += 1
        return settled

    def _settle_reviewer_terminal_intents_contained(self) -> int:
        """Settle terminal intents without ever masking the caller's outcome.

        This is what the launch and reviewer reservation boundaries call from
        their ``finally``.  Store, filesystem and lock failures are already
        contained per intent; this is the last resort for anything else,
        because a failure while settling somebody else's dead reservation has
        nothing to do with the launch the caller is in the middle of, and
        letting it escape a bare ``finally`` would replace an in-flight
        ``LaunchRejected`` -- or an already-built launch receipt -- with an
        unrelated error about a reservation the caller never touched.  The
        intent stays durable, so absorbing the failure defers that settlement
        to the next pass instead of losing it.

        The 0 returned for such a failure is indistinguishable from the 0
        returned for "nothing was pending", so the failure is named once in the
        diagnostic ledger before it is absorbed.
        """

        try:
            return self._settle_reviewer_terminal_intents()
        except Exception as error:
            self._diagnose_settlement_pass_failure(error)
            return 0

    def _reconcile_expired_starting_reservations(
        self,
        snapshot: tuple[
            dict[str, dict[str, Any]], tuple[Any, ...] | None
        ] | None = None,
        *,
        resolved: bool = False,
    ) -> int:
        """Truthfully terminalize pid-null starting reservations that expired.

        A ``starting`` reservation with no pid belongs to a provisioner that
        never reached supervisor spawn.  Once its bounded reservation epoch
        elapses it is no longer live, so it is terminalized (``blocked`` with
        ``reservation_expired``) instead of silently expiring.  A reservation
        that already carries a real pid is only terminalized when pid identity
        evidence proves a mismatch.  A durable ``provider_spawn_committed``
        phase is exact spawn authority that outlives its owner process, so it
        is never terminalized by elapsed or quiet time: only a committed owner
        proven dead (and no provider running event) is truthfully terminalized
        once.  A live provider's liveness follows exact process evidence.  A
        pid-null ``starting`` reservation whose exact owner is still live in the
        ``reviewer_source_graph_prewarm_started`` phase is likewise deferred
        rather than terminalized by its elapsed epoch.

        ``snapshot`` is a stable ledger snapshot the caller already took with
        the registry lock RELEASED; see ``_resolved_reservation_snapshot`` for
        why that hand-off preserves the exact no-hidden-append authority.
        Callers that hold no lock omit it and pay for their own snapshot.

        ``resolved`` states that the caller ALREADY re-proved that snapshot
        with its own sweep, inside this same lock acquisition, and shares the
        dict.  It exists so the admission CAS in ``_launch_reservation`` and
        this pass are proved by ONE sweep between them rather than two: the
        re-proof is a property of the critical section, not of either caller.
        In exchange this pass mirrors every row it terminalizes back into that
        shared dict, so the caller's copy keeps describing the ledger exactly
        without a second parse.

        This pass writes only ledger events and durable terminal intents; the
        task-store half runs in ``_settle_reviewer_terminal_intents`` with no
        registry lock held.  Diagnostics about reservations that can never be
        terminalized share one bounded per-pass budget, so a directory full of
        unbindable rows cannot turn a single pass into an unbounded burst of
        marker and ledger writes.
        """

        now = time.time()
        # The exact requests this pass retired, in ledger order.  It is both
        # the return count and the key set mirrored into ``latest`` below.
        # Named apart from the per-row ``terminalized`` flag the loop rebinds.
        retired: list[str] = []
        diagnostics_left = self._TERMINAL_INTENT_DIAGNOSTICS_PER_PASS
        if resolved and snapshot is not None:
            latest, generation = snapshot
        else:
            latest, generation = self._resolved_reservation_snapshot(snapshot)
        if generation is None:
            # The ledger changed under every bounded read attempt, so this
            # snapshot may hide a concurrent append.  Terminalizing on it could
            # contradict a row that already exists; defer to the next pass.
            return 0
        for request_id, event in latest.items():
            state = event.get("state")
            if state == "provider_spawn_committed":
                provider_pid = int(event.get("provider_pid") or 0)
                if provider_pid:
                    if (
                        _pid_identity_evidence(
                            provider_pid, event.get("provider_pid_start_ticks")
                        ).verdict
                        is not PidIdentityVerdict.MISMATCH
                    ):
                        # A live provider is never terminalized by elapsed or
                        # quiet time, even when its bounded owner is gone.
                        continue
                    terminalized, diagnostics_left = (
                        self._terminalize_committed_reservation(
                            request_id,
                            event,
                            "provider_spawn_committed_provider_dead",
                            diagnostics_left,
                        )
                    )
                    if terminalized:
                        retired.append(request_id)
                    continue
                owner_pid = int(event.get("owner_pid") or 0)
                if owner_pid and _pid_identity_evidence(
                    owner_pid, event.get("owner_pid_start_ticks")
                ).verdict is PidIdentityVerdict.MISMATCH:
                    terminalized, diagnostics_left = (
                        self._terminalize_committed_reservation(
                            request_id,
                            event,
                            "provider_spawn_committed_owner_dead",
                            diagnostics_left,
                        )
                    )
                    if terminalized:
                        retired.append(request_id)
                continue
            if state != "starting":
                continue
            pid = int(event.get("pid") or 0)
            if pid:
                if (
                    _pid_identity_evidence(pid, event.get("pid_start_ticks")).verdict
                    is PidIdentityVerdict.MISMATCH
                ):
                    self._append_event({
                        "request_id": request_id,
                        "task_id": event.get("task_id"),
                        "runner": event.get("runner"),
                        "topic": event.get("topic"),
                        "adapter_id": event.get("adapter_id"),
                        "state": "blocked",
                        "blocked_reason": "reservation_process_false",
                        "reservation_expires_at_epoch": event.get(
                            "reservation_expires_at_epoch"
                        ),
                    })
                    retired.append(request_id)
                continue
            if self._reviewer_source_graph_prewarm_live_event(event):
                # A live owned prewarm keeps extending its own liveness; the
                # bounded reservation epoch alone is never terminal authority
                # while the exact owner is still building the Source Graph.
                continue
            preparation_stall = derive_preparation_stall(
                now_epoch=now,
                preparation_heartbeat_epoch=event.get("preparation_heartbeat_epoch"),
                preparation_phase=event.get("preparation_phase"),
            )
            if preparation_stall["preparation_stalled"]:
                # A frozen preparation heartbeat is invisible to pid-based
                # liveness because a launch that never spawned has no pid. Fail
                # it here with a named reason instead of holding the pid-null
                # reservation alive until its window merely expires.
                self._append_event({
                    "request_id": request_id,
                    "task_id": event.get("task_id"),
                    "runner": event.get("runner"),
                    "topic": event.get("topic"),
                    "adapter_id": event.get("adapter_id"),
                    "state": "blocked",
                    "blocked_reason": preparation_stall["reason"],
                    "preparation_phase": event.get("preparation_phase"),
                    "preparation_heartbeat_epoch": event.get(
                        "preparation_heartbeat_epoch"
                    ),
                    "reservation_expires_at_epoch": event.get(
                        "reservation_expires_at_epoch"
                    ),
                })
                retired.append(request_id)
                continue
            if float(event.get("reservation_expires_at_epoch") or 0.0) >= now:
                continue
            self._append_event({
                "request_id": request_id,
                "task_id": event.get("task_id"),
                "runner": event.get("runner"),
                "topic": event.get("topic"),
                "adapter_id": event.get("adapter_id"),
                "state": "blocked",
                "blocked_reason": "reservation_expired",
                "reservation_expires_at_epoch": event.get(
                    "reservation_expires_at_epoch"
                ),
            })
            retired.append(request_id)
        # Mirror this pass's OWN appends into the snapshot it was proved on.
        # A caller sharing the dict (``resolved=True``) decides admission from
        # it with no second parse, so it has to keep describing the ledger
        # exactly -- otherwise a reservation this pass just retired would still
        # read as live and the CAS would refuse a slot that is genuinely free.
        # Only ``state`` is mirrored: it is the sole field admission reads
        # about a retired row, and the appended event above stays the one and
        # only authority for everything else.
        for request_id in retired:
            latest[request_id] = {**latest[request_id], "state": "blocked"}
        return len(retired)

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
        if adapter_id == "claude_cli":
            status = claude_auth.auth_status()
            if not status.get("launchable"):
                raise LaunchRejected(
                    "claude_authentication_unavailable:"
                    + str(status.get("blocker_reason") or "authentication_required")
                )
            return None, model
        if adapter_id == runtime_adapters.VSCODE_LM_ADAPTER:
            if not isinstance(model, str) or not model.strip():
                raise LaunchRejected("vscode_lm_model_required")
            resolved_model = model.strip()
            readiness = vscode_lm_bridge.bridge_readiness(
                self.repo,
                model=resolved_model,
                adapter_id=runtime_adapters.VSCODE_LM_ADAPTER,
            )
            if not readiness.get("launchable"):
                raise LaunchRejected(
                    "vscode_lm_unavailable:"
                    + str(readiness.get("blocker_reason") or "not_launchable")
                )
            return (
                None,
                str(readiness.get("resolved_model") or "").strip() or resolved_model,
            )
        if adapter_id == runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER:
            resolved_model, model_error = runtime_adapters.resolve_deepseek_model(model)
            if model_error:
                raise LaunchRejected(f"deepseek_model_rejected:{model_error}")
            assert resolved_model is not None
            readiness = vscode_lm_bridge.bridge_readiness(
                self.repo,
                model=resolved_model,
                adapter_id=runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER,
            )
            if not readiness.get("launchable"):
                raise LaunchRejected(
                    "deepseek_vscode_lm_unavailable:"
                    + str(readiness.get("blocker_reason") or "not_launchable")
                )
            return (
                None,
                str(readiness.get("resolved_model") or "").strip() or resolved_model,
            )
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
                self.repo,
                model=resolved_model,
                adapter_id=runtime_adapters.GLM_VSCODE_LM_ADAPTER,
            )
            if not readiness.get("launchable"):
                raise LaunchRejected(
                    "glm_vscode_lm_unavailable:"
                    + str(readiness.get("blocker_reason") or "not_launchable")
                )
            return (
                None,
                str(readiness.get("resolved_model") or "").strip() or resolved_model,
            )
        if adapter_id == runtime_adapters.GROK_KILO_ADAPTER:
            resolved_model, model_error = runtime_adapters.resolve_grok_kilo_model(
                model
            )
            if model_error:
                raise LaunchRejected(f"grok_kilo_model_rejected:{model_error}")
            assert resolved_model is not None
            return None, resolved_model
        else:
            return None, model

    def _preflight_card(
        self,
        task_id: str,
        runner: str,
        topic: str,
        adapter_id: str,
        reserved_request_id: str | None = None,
    ) -> dict[str, Any]:
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
        lifecycle = core._lifecycle_state(card)
        worker_status = str(card.get("worker_status") or "unclaimed")
        claimed_by = str(card.get("claimed_by") or "")
        launch_request_id = str(card.get("launch_request_id") or "")
        if lifecycle == "pending":
            if worker_status != "unclaimed":
                raise LaunchRejected(f"task_not_unclaimed:{worker_status}")
            if claimed_by:
                raise LaunchRejected(f"task_already_claimed:{claimed_by}")
        elif lifecycle == "processing":
            if worker_status != "claimed" or claimed_by != runner:
                raise LaunchRejected(
                    f"task_claim_owner_mismatch:{claimed_by or 'unclaimed'}"
                )
            if launch_request_id and launch_request_id != str(
                reserved_request_id or ""
            ):
                raise LaunchRejected(
                    f"task_launch_already_attached:{launch_request_id[:120]}"
                )
        else:
            raise LaunchRejected(f"task_not_launchable:{lifecycle}")
        _validate_scope(self.repo, card)
        _validate_required_outputs_contract(card)
        path_conflicts = core.task_card_path_conflicts(card)
        if path_conflicts:
            detail = json.dumps(path_conflicts, ensure_ascii=False, separators=(",", ":"))
            raise LaunchRejected("contradictory_task_path_contract:" + detail[:600])
        policy_result = repo_policy.validate_launch(self.repo, card, adapter_id)
        if not policy_result.get("ok"):
            raise LaunchRejected(str(policy_result.get("reason") or "repo_policy_rejected"))
        collision = self._collision_guard(task_id=task_id, print_json=True)
        if collision.get("returncode") != 0:
            raise LaunchRejected("collision_guard_failed")
        return card

    def _active_request_ids(
        self, latest: Mapping[str, Mapping[str, Any]] | None = None
    ) -> set[str]:
        """Requests still live, from ``latest`` when the caller proved one.

        Admission hands in a generation-proven snapshot, because an unproven
        parse can hide the very ``running`` row that would have filled the last
        slot.  Reporting callers omit it and take the plain read.
        """

        dead = [rid for rid, live in self._live.items() if live.process.poll() is not None]
        for rid in dead:
            self._live.pop(rid, None)
        active = {
            rid for rid, live in self._live.items() if live.process.poll() is None
        }
        if latest is None:
            latest = self._latest_by_request()
        for request_id, event in latest.items():
            state = event.get("state")
            if state == "provider_spawn_committed":
                provider_pid = int(event.get("provider_pid") or 0)
                if provider_pid and _pid_identity_evidence(
                    provider_pid, event.get("provider_pid_start_ticks")
                ).verdict is not PidIdentityVerdict.MISMATCH:
                    active.add(request_id)
                    continue
                owner_pid = int(event.get("owner_pid") or 0)
                if owner_pid and _pid_identity_evidence(
                    owner_pid, event.get("owner_pid_start_ticks")
                ).verdict is not PidIdentityVerdict.MISMATCH:
                    active.add(request_id)
                continue
            if state not in ACTIVE_PROCESS_STATES:
                continue
            pid = int(event.get("pid") or 0)
            ticks = event.get("pid_start_ticks")
            if (
                state == "starting"
                and not pid
                and self._reviewer_source_graph_prewarm_live_event(event)
            ):
                active.add(request_id)
                continue
            if (
                state == "starting"
                and not pid
                and float(event.get("reservation_expires_at_epoch") or 0.0)
                > time.time()
            ):
                active.add(request_id)
                continue
            if (
                pid
                and _pid_identity_evidence(pid, ticks).verdict
                is not PidIdentityVerdict.MISMATCH
            ):
                active.add(request_id)
        return active

    def _active_count(
        self, latest: Mapping[str, Mapping[str, Any]] | None = None
    ) -> int:
        return len(self._active_request_ids(latest))

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
        quality_review_binding: dict[str, Any] | None = None,
        reserved_request_id: str | None = None,
        prewarm_progress: Callable[..., None] | None = None,
    ) -> dict[str, Any]:
        isolated_kwargs: dict[str, Any] = {
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "adapter_id": adapter_id,
            "model": model,
            "owner_prompt": owner_prompt,
            "timeout_seconds": timeout_seconds,
        }
        if quality_review_binding is not None:
            isolated_kwargs["quality_review_binding"] = quality_review_binding
        if reserved_request_id is not None:
            isolated_kwargs["reserved_request_id"] = reserved_request_id
        if prewarm_progress is not None:
            isolated_kwargs["prewarm_progress"] = prewarm_progress
        if self.isolation_enabled or reserved_request_id is not None:
            return self._launch_isolated(**isolated_kwargs)
        return self._launch_direct_for_tests(
            task_id=task_id,
            runner=runner,
            topic=topic,
            adapter_id=adapter_id,
            model=model,
            owner_prompt=owner_prompt,
            timeout_seconds=timeout_seconds,
        )

    launch_task = launch

    def _launch_validation_only_replay(
        self,
        *,
        task_id: str,
        runner: str,
        topic: str,
        adapter_id: str,
        model: str | None,
        timeout_seconds: int,
        card: dict[str, Any],
        authorization: dict[str, Any],
    ) -> dict[str, Any]:
        """Run a hash-pinned validation replay without invoking a provider.

        This lane deliberately reuses the canonical isolated-worktree
        finalizer.  It changes only how the successful execution receipt is
        produced: no prompt, credential, adapter plan, worker MCP runtime,
        supervisor, or provider process exists.  The normal finalizer still
        owns exact claim verification, inherited-byte/hash authorization,
        sandboxed validation, review evidence, and the terminal transition.
        """

        request_id = uuid.uuid4().hex
        workspace: WorkerWorkspace | None = None
        claimed = False
        try:
            with self._launch_reservation({
                "request_id": request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": topic,
                "adapter_id": adapter_id,
                "model": model,
                "timeout_seconds": timeout_seconds,
                "authority": "coordinator_validation_only_replay",
                "sandbox_backend": "deterministic_validation",
                "execution_mode": "validation_only_replay",
                "provider_launched": False,
            }):
                self.process_dir.mkdir(parents=True, exist_ok=True)
                chmod_path(self.process_dir, 0o700)
                stdout_path = self.process_dir / f"{request_id}.stdout.log"
                stderr_path = self.process_dir / f"{request_id}.stderr.log"
                status_path = self.process_dir / f"{request_id}.supervisor.json"
                cancel_path = self.process_dir / f"{request_id}.cancel.json"
                metadata_path = self.process_dir / f"{request_id}.request.json"
                _touch_0600(stdout_path)
                _touch_0600(stderr_path)

                workspace = create_workspace(self.repo, request_id, card, adapter_id)
                residual_contract_manifest = build_residual_contract_manifest(
                    workspace, card
                )
                immutable_inputs = [
                    str(path) for path in (card.get("immutable_inputs") or [])
                ]
                immutable_input_manifest = _path_manifest(
                    self.repo, immutable_inputs
                )

                claim = task_engine.claim_start_exact(
                    self.repo, task_id, runner, topic, request_id=request_id
                )
                if not claim.get("ok"):
                    raise LaunchRejected(
                        "claim_start_failed:"
                        + str(claim.get("stderr") or claim.get("stdout") or "")[:300]
                    )
                card = _committed_claim_card(
                    claim,
                    request_id=request_id,
                    task_id=task_id,
                    runner=runner,
                    topic=topic,
                )
                claimed = True

                metadata = {
                    "schema_id": "aiworkhub.task_mcp.isolated_request.v1",
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": topic,
                    "claim_epoch": card["claim_epoch"],
                    "rework_predecessor": dict(card["rework_predecessor"]),
                    "validation_only_replay_authorization": authorization,
                    "execution_mode": "validation_only_replay",
                    "provider_launched": False,
                    "adapter_id": adapter_id,
                    "model": model,
                    "timeout_seconds": timeout_seconds,
                    "token_budget": None,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "supervisor_status_path": str(status_path),
                    "cancel_path": str(cancel_path),
                    "metadata_path": str(metadata_path),
                    "prompt_sha256": hashlib.sha256(b"").hexdigest(),
                    "prompt_budget": {
                        "schema_id": "aiworkhub.worker_prompt_budget.v1",
                        "mode": "validation_only_replay",
                        "total_bytes": 0,
                        "byte_labels_are_token_truth": False,
                    },
                    "project_context": None,
                    "project_context_delivery": {
                        "injected": False,
                        "reason": "deterministic_validation_only_replay",
                    },
                    "worker_mcp": {},
                    "sandbox_backend": "deterministic_validation",
                    "validation": list(card.get("validation") or []),
                    "validation_roles": list(card.get("validation_roles") or []),
                    "work_kind": str(card.get("work_kind") or "generic"),
                    "required_outputs": list(card.get("required_outputs") or []),
                    "read_only": card.get("read_only") is True,
                    "allow_empty_required_outputs": list(
                        card.get("allow_empty_required_outputs") or []
                    ),
                    "allow_unchanged_required_outputs": list(
                        card.get("allow_unchanged_required_outputs") or []
                    ),
                    "immutable_inputs": immutable_inputs,
                    "immutable_input_manifest": immutable_input_manifest,
                    "residual_contract_manifest": residual_contract_manifest,
                    "external_readonly_dirs": [],
                    "workspace": workspace.as_metadata(),
                    "quality_review": None,
                }
                write_json_0600(metadata_path, metadata)
                _write_terminal_authority_grant(
                    self._terminal_authority_grant_path(request_id),
                    self._terminal_authority_key(),
                    repo=self.repo,
                    task_id=task_id,
                    runner=runner,
                    topic=topic,
                    request_id=request_id,
                )
                write_json_0600(
                    status_path,
                    {
                        "state": "exited",
                        "exit_code": 0,
                        "execution_mode": "validation_only_replay",
                        "provider_launched": False,
                        "started_at_epoch": time.time(),
                    },
                )
                started_at = _utcnow()
                self._append_event({
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": topic,
                    "adapter_id": adapter_id,
                    "model": model,
                    "state": "running",
                    "pid": 0,
                    "started_at": started_at,
                    "timeout_seconds": timeout_seconds,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "metadata_path": str(metadata_path),
                    "supervisor_status_path": str(status_path),
                    "workspace_isolated": True,
                    "sandbox_backend": "deterministic_validation",
                    "execution_mode": "validation_only_replay",
                    "provider_launched": False,
                    "shell": False,
                })
                # Validation can legitimately take longer than the MCP
                # request timeout.  Finalize asynchronously just like a real
                # worker while retaining the already-written exited status
                # and metadata: a server restart can reconcile the same
                # request without a provider rerun or a replacement task.
                thread = threading.Thread(
                    target=self._finalize_isolated_request,
                    args=(request_id, 0),
                    name=f"aiworkhub-validation-replay-{request_id[:8]}",
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
                    "state": "running",
                    "terminal": False,
                    "pid": None,
                    "workspace_isolated": True,
                    "sandbox_backend": "deterministic_validation",
                    "execution_mode": "validation_only_replay",
                    "provider_launched": False,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "shell": False,
                }
        except (LaunchRejected, OSError, ValueError, WorkspaceError) as exc:
            if claimed:
                task_engine.mark_launch_failed(
                    self.repo,
                    task_id,
                    runner,
                    reason=f"validation_only_replay_launch_failed:{exc}"[:500],
                    request_id=request_id,
                )
            if workspace is not None and not claimed:
                try:
                    cleanup_workspace(workspace.repo, workspace.path, workspace.home)
                except WorkspaceError:
                    pass
            return self._blocked(
                task_id,
                runner,
                topic,
                adapter_id,
                str(exc),
                request_id=request_id,
                state="launch_failed" if claimed else "blocked",
                diagnostic={
                    "execution_mode": "validation_only_replay",
                    "provider_launched": "false",
                },
            )

    _QUALITY_REVIEW_PREP_LOCK = threading.Lock()
    _QUALITY_REVIEW_PREP_MAX = 8
    # Bounded waiter ceiling for single-flight preparation reuse. A waiter runs
    # under its own per-lens background owner (never the MCP handler) and the
    # elected owner always records a terminal result, so this bound only guards
    # against a deadlocked owner; it never classifies a provider by elapsed time.
    _QUALITY_REVIEW_PREP_WAIT_SECONDS = 600.0
    # The elected owner runs the heavy packet build under a strictly shorter
    # ceiling than waiters so it always publishes a truthful terminal
    # preparation failure (no provider exists yet) and wakes every waiter
    # before any waiter's own ceiling fires. This never classifies a live
    # provider by elapsed time -- there is no process during preparation.
    _QUALITY_REVIEW_PREP_OWNER_SECONDS = 300.0
    # The reviewer's entire pre-provider isolated-launch preparation runs under
    # its own bounded owner.  A launch that outlives the ceiling with no live
    # owned Source Graph prewarm (e.g. a stalled MCP callback) is truthfully
    # terminalized as a pid-null ``quality_review_launch_timeout``.  A live
    # owned ``reviewer_source_graph_prewarm`` keeps extending the owner ceiling
    # so it is never expired by elapsed time; the stale owner becomes
    # ownership-aware and aborts before spawning, so no provider is ever
    # time-limited or killed by elapsed time.  Liveness of a real provider still
    # follows exact process evidence only.
    _QUALITY_REVIEW_LAUNCH_OWNER_SECONDS = 300.0
    _QUALITY_REVIEW_SOURCE_MAX_BYTES = 4_000
    _QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES = 60_000
    _QUALITY_REVIEW_SOURCE_CONTEXT_LINES = 3

    def _quality_review_source_evidence(
        self, workspace: Any, changed_hashes: Mapping[str, str | None]
    ) -> dict[str, dict[str, Any]]:
        """Build bounded source evidence centered on candidate changed ranges."""

        def decode_utf8(raw: bytes) -> str | None:
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return None

        def line_span(start: int, end: int, line_count: int) -> tuple[int, int]:
            anchor_start = start + 1
            anchor_end = max(start + 1, end)
            context = self._QUALITY_REVIEW_SOURCE_CONTEXT_LINES
            return (
                max(1, anchor_start - context),
                min(line_count, anchor_end + context),
            )

        def segment_excerpt(
            lines: list[str], start_line: int, end_line: int, limit: int
        ) -> tuple[str, int, bool]:
            if limit <= 0 or start_line > end_line:
                return "", 0, bool(start_line <= end_line)
            excerpt = "".join(lines[start_line - 1 : end_line])
            encoded = excerpt.encode("utf-8")
            if len(encoded) <= limit:
                return excerpt, len(encoded), False
            body = encoded[:limit]
            return body.decode("utf-8", errors="replace"), len(body), True

        evidence: dict[str, dict[str, Any]] = {}
        remaining = self._QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES
        for path in sorted(changed_hashes):
            candidate = Path(workspace.path) / path
            baseline = Path(self.repo) / path
            row: dict[str, Any] = {
                "candidate_sha256": changed_hashes[path],
                "excerpt": "",
                "excerpt_bytes": 0,
                "source_bytes": 0,
                "truncated": False,
                "segments": [],
            }
            evidence[path] = row
            try:
                if candidate.is_symlink():
                    raise WorkspaceError(
                        f"quality_review_candidate_unreadable:{path}"
                    )
                if not candidate.is_file():
                    if changed_hashes[path] is None:
                        row["omission_reason"] = "candidate_deleted_or_non_file"
                        continue
                    raise WorkspaceError(
                        f"quality_review_candidate_unreadable:{path}"
                    )
                candidate_bytes = candidate.read_bytes()
                baseline_bytes = baseline.read_bytes() if baseline.is_file() else b""
            except OSError as exc:
                raise WorkspaceError(
                    f"quality_review_candidate_unreadable:{path}"
                ) from exc
            row["source_bytes"] = len(candidate_bytes)
            candidate_text = decode_utf8(candidate_bytes)
            baseline_text = decode_utf8(baseline_bytes)
            if candidate_text is None:
                row["omission_reason"] = "candidate_non_utf8"
                row["truncated"] = True
                continue
            if baseline_text is None:
                baseline_text = ""
                row["baseline_omission_reason"] = "baseline_non_utf8"
            candidate_lines = candidate_text.splitlines(keepends=True)
            baseline_lines = baseline_text.splitlines(keepends=True)
            if not candidate_lines and candidate_text:
                candidate_lines = [candidate_text]
            matcher = difflib.SequenceMatcher(None, baseline_lines, candidate_lines)
            chunks: list[str] = []
            omitted_hunks = 0
            path_remaining = self._QUALITY_REVIEW_SOURCE_MAX_BYTES
            header_path = json.dumps(path, ensure_ascii=True)
            for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
                if tag == "equal":
                    continue
                start_line, end_line = line_span(
                    new_start, new_end, len(candidate_lines)
                )
                limit = max(0, min(path_remaining, remaining))
                header = (
                    f"@@ path:{header_path} candidate:{start_line}-{end_line} "
                    f"change:{new_start + 1}-{max(new_start + 1, new_end)} "
                    f"baseline:{old_start + 1}-{max(old_start + 1, old_end)} {tag} @@\n"
                )
                header_bytes = len(header.encode("utf-8"))
                if limit <= header_bytes:
                    omitted_hunks += 1
                    row["truncated"] = True
                    row["segments"].append(
                        {
                            "kind": tag,
                            "candidate_start_line": start_line,
                            "candidate_end_line": end_line,
                            "changed_start_line": new_start + 1,
                            "changed_end_line": max(new_start + 1, new_end),
                            "baseline_start_line": old_start + 1,
                            "baseline_end_line": max(old_start + 1, old_end),
                            "excerpt_bytes": 0,
                            "truncated": True,
                        }
                    )
                    continue
                excerpt, excerpt_bytes, truncated = segment_excerpt(
                    candidate_lines, start_line, end_line, limit - header_bytes
                )
                segment_bytes = header_bytes + excerpt_bytes
                remaining -= segment_bytes
                path_remaining -= segment_bytes
                row["excerpt_bytes"] += segment_bytes
                row["segments"].append(
                    {
                        "kind": tag,
                        "candidate_start_line": start_line,
                        "candidate_end_line": end_line,
                        "changed_start_line": new_start + 1,
                        "changed_end_line": max(new_start + 1, new_end),
                        "baseline_start_line": old_start + 1,
                        "baseline_end_line": max(old_start + 1, old_end),
                        "excerpt_bytes": segment_bytes,
                        "truncated": truncated,
                    }
                )
                chunks.append(header + excerpt)
                if truncated:
                    omitted_hunks += 1
                    row["truncated"] = True
                if remaining <= 0 or path_remaining <= 0:
                    row["truncated"] = True
            if omitted_hunks:
                row["omission_reason"] = f"changed_hunks_omitted:{omitted_hunks}"
            if not chunks and "omission_reason" not in row:
                row["omission_reason"] = "empty_diff"
            row["excerpt"] = "".join(chunks)
        return evidence

    def _prepared_quality_review(
        self,
        target_request_id: str,
        target_task_id: str,
        progress: Any | None = None,
    ) -> dict[str, Any]:
        """Prepare the packet once per exact target, single-flight across lenses.

        Concurrent correctness/security/code_quality reviewers for one target
        previously each observed the same cache miss and rebuilt the heavy
        packet. A per-target single-flight now elects exactly one owner that
        runs ``_build_quality_review_packet``; every other caller waits on a
        bounded condition and reuses the owner's result. The owner's success
        *and* failure propagate truthfully, so a waiter never masks a real
        preparation error with an independent rebuild.

        The elected owner's build is itself bounded: it runs under
        ``_QUALITY_REVIEW_PREP_OWNER_SECONDS`` and a build that outlives the
        ceiling becomes a truthful ``quality_review_preparation_timeout``
        terminal failure instead of a silent pid-null reservation.
        """

        key = (target_request_id, target_task_id)
        with self._QUALITY_REVIEW_PREP_LOCK:
            cache = self.__dict__.setdefault("_quality_review_prepared", {})
            prepared = cache.get(key)
            if prepared is not None:
                return {"ok": True, "prepared": prepared}
            flights = self.__dict__.setdefault("_quality_review_flights", {})
            flight = flights.get(key)
            if flight is None:
                flight = _QualityReviewPrepFlight()
                flights[key] = flight
                owner = True
            else:
                owner = False

        if not owner:
            with flight.condition:
                deadline = time.monotonic() + self._QUALITY_REVIEW_PREP_WAIT_SECONDS
                while not flight.done:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    flight.condition.wait(timeout=remaining)
                result = flight.result if flight.done else None
            if result is None:
                return {"ok": False, "error": "quality_review_preparation_timeout"}
            return result

        result_box: dict[str, dict[str, Any]] = {}

        def _run_owner_build() -> None:
            try:
                result_box["result"] = self._build_quality_review_packet(
                    target_request_id, target_task_id, progress=progress
                )
            except Exception as exc:  # noqa: BLE001 -- propagate owner failure truthfully
                result_box["result"] = {
                    "ok": False,
                    "error": f"quality_review_target_invalid:{exc}",
                }

        builder = threading.Thread(
            target=_run_owner_build,
            name=f"aiworkhub-reviewer-prep-{target_request_id[:8]}",
            daemon=True,
        )
        builder.start()
        builder.join(self._QUALITY_REVIEW_PREP_OWNER_SECONDS)
        if builder.is_alive():
            result = {
                "ok": False,
                "error": "quality_review_preparation_timeout",
            }
        else:
            result = result_box.get("result") or {
                "ok": False,
                "error": "quality_review_preparation_no_result",
            }
        with self._QUALITY_REVIEW_PREP_LOCK:
            if result.get("ok"):
                cache = self.__dict__.setdefault("_quality_review_prepared", {})
                cache[key] = result["prepared"]
                while len(cache) > self._QUALITY_REVIEW_PREP_MAX:
                    cache.pop(next(iter(cache)))
        with flight.condition:
            flight.result = result
            flight.done = True
            flight.condition.notify_all()
        with self._QUALITY_REVIEW_PREP_LOCK:
            flights.pop(key, None)
        return result

    def _build_quality_review_packet(
        self,
        target_request_id: str,
        target_task_id: str,
        progress: Any | None = None,
    ) -> dict[str, Any]:
        """Run the one heavy, uncached preparation for an exact target.

        When ``progress`` is supplied the heavy phases are published as
        reservation progress events so status reads observe forward motion
        instead of a silent pid-null reservation.
        """

        def mark(phase: str) -> None:
            if progress is not None:
                progress(phase)

        mark("packet_build_started")
        events = self._request_events(target_request_id)
        if not events:
            return {"ok": False, "error": "quality_review_target_request_not_found"}
        latest = events[-1]
        if str(latest.get("task_id") or "") != target_task_id:
            return {"ok": False, "error": "quality_review_target_identity_mismatch"}
        if str(latest.get("state") or "") != "review_ready":
            return {
                "ok": False,
                "error": "quality_review_target_not_review_ready",
                "state": latest.get("state"),
            }
        mark("target_events_loaded")
        try:
            try:
                target_envelope = self._show_task(target_task_id)
            except sqlite3.OperationalError as exc:
                # A launch-target read that loses to a finalization writer storm
                # must name the contended task queue, not surface a bare
                # "database is locked" that sends an operator hunting an
                # innocent database (the Source Graph index) for hours.
                if task_store.is_task_queue_lock_error(exc):
                    raise task_store.TaskQueueContended(
                        task_store.task_queue_contention_reason(self.repo, exc)
                    ) from exc
                raise
            card = _parse_card(target_envelope, target_task_id)
            terminal = card.get("terminal_review") or {}
            evidence = terminal.get("evidence") or {}
            workspace = WorkerWorkspace.from_metadata(dict(evidence["workspace"]))
            if workspace.repo != self.repo or workspace.request_id != target_request_id:
                raise WorkspaceError("quality_review_target_workspace_identity_mismatch")
            assert_gc_safe_workspace_shape(
                target_request_id, workspace.path, workspace.home, repo=self.repo
            )
            mark("target_card_loaded")
            changed_hashes = evidence.get("changed_path_hashes")
            if not isinstance(changed_hashes, dict) or not changed_hashes:
                raise WorkspaceError("quality_review_target_hashes_missing")
            current_hashes = _changed_path_hashes(workspace, list(changed_hashes))
            if current_hashes != changed_hashes:
                raise WorkspaceError("quality_review_target_hashes_drifted")
            mark("target_hashes_verified")
            source_evidence = self._quality_review_source_evidence(
                workspace, current_hashes
            )
            initial_gate = evidence.get("quality_gate") or {}
            mark("scope_audits_started")
            scoped_audits = quality_review_scope.build_scoped_audits(
                authority_repo=Path(self.repo),
                candidate_repo=workspace.path,
                task_id=target_task_id,
                packet_seed=target_request_id,
                created_at=str(
                    latest.get("at")
                    or latest.get("updated_at")
                    or latest.get("finished_at")
                    or target_request_id
                ),
                changed_path_hashes=current_hashes,
                source_evidence=source_evidence,
                acceptance=card.get("acceptance") or [],
                forbidden_changes=card.get("forbidden") or [],
                required_outputs=card.get("required_outputs") or [],
                validation=card.get("validation") or [],
                terminal_validation=evidence.get("validation") or [],
                lenses=quality_evidence.JUDGMENT_LENSES,
            )
            mark("scope_audits_complete")
            target_claim_epoch = card.get("claim_epoch")
            if type(target_claim_epoch) is not int or target_claim_epoch < 1:
                raise WorkspaceError("quality_review_target_claim_epoch_invalid")
            packet = quality_reviewer.build_review_packet(
                request_id=target_request_id,
                task_id=target_task_id,
                claim_epoch=target_claim_epoch,
                worker_provider=str(latest.get("adapter_id") or latest.get("runner") or ""),
                changed_path_hashes=current_hashes,
                objective=str(card.get("objective") or ""),
                acceptance=card.get("acceptance") or [],
                required_outputs=card.get("required_outputs") or [],
                validation=card.get("validation") or [],
                terminal_validation=evidence.get("validation") or [],
                mechanical_checks=initial_gate.get("checks") or [],
                source_evidence=source_evidence,
                scoped_audits=scoped_audits,
            )
        except task_store.TaskQueueContended as exc:
            # Distinct from ``quality_review_target_invalid``: the target card is
            # not invalid, the task queue was write-locked by finalization.
            return {"ok": False, "error": f"quality_review_target_contended:{exc}"}
        except (
            KeyError,
            TypeError,
            ValueError,
            LaunchRejected,
            WorkspaceError,
            quality_reviewer.ReviewerEvidenceError,
            quality_review_scope.ReviewScopeBuildError,
        ) as exc:
            return {"ok": False, "error": f"quality_review_target_invalid:{exc}"}

        mark("packet_built")
        prepared = {
            "worker_adapter_id": str(latest.get("adapter_id") or ""),
            "workspace": workspace,
            "changed_hashes": dict(current_hashes),
            "packet": packet,
        }
        return {"ok": True, "prepared": prepared}

    def _reviewer_receipt(
        self,
        request_id: str,
        latest: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return a bounded, truthful receipt for an already-reserved reviewer.

        ``latest`` is the snapshot the admission decision was proven against.
        Reusing it keeps the receipt describing the exact ledger that decision
        saw; a fresh parse here could report a row the decision never had.
        """

        if latest is None:
            latest = self._latest_by_request()
        event = latest.get(request_id) or {}
        return {
            "ok": True,
            "already_reserved": True,
            "launch_implemented": LAUNCH_IMPLEMENTED,
            "request_id": request_id,
            "task_id": event.get("task_id"),
            "runner": event.get("runner"),
            "topic": event.get("topic"),
            "adapter_id": event.get("adapter_id"),
            "model": event.get("model"),
            "state": event.get("state"),
            "pid": event.get("pid"),
            "shell": False,
        }

    def _publish_reviewer_progress(
        self, request_id: str, phase: str, detail: str | None = None
    ) -> None:
        """Append a bounded preparation progress event for a starting reservation.

        The event preserves the reservation's identity fields and unexpired
        epoch so reconciliation and live-receipt admission still see it as a
        live pid-null reservation; it only adds observable preparation phase
        and heartbeat fields. Publishing is a no-op once the reservation has
        been terminalized, so a stale owner can never append progress after a
        truthful terminal state.
        """

        base = self._latest_by_request().get(request_id) or {}
        if base.get("state") != "starting":
            return
        event: dict[str, Any] = {
            "request_id": request_id,
            "task_id": base.get("task_id"),
            "runner": base.get("runner"),
            "topic": base.get("topic") or "quality_review",
            "adapter_id": base.get("adapter_id"),
            "state": "starting",
            "reservation_expires_at_epoch": base.get(
                "reservation_expires_at_epoch"
            ),
            "preparation_phase": phase,
            "preparation_heartbeat_epoch": time.time(),
        }
        if base.get("owner_pid"):
            event["owner_pid"] = base.get("owner_pid")
            event["owner_pid_start_ticks"] = base.get("owner_pid_start_ticks")
        if detail:
            event["preparation_detail"] = str(detail)[:300]
        self._append_event(event)

    def _live_reviewer_receipt(
        self,
        reviewer_task_id: str,
        latest: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        target_request_id: str | None = None,
        target_task_id: str | None = None,
        lens: str | None = None,
    ) -> dict[str, Any] | None:
        """Return a bounded receipt for an already-live reviewer, else ``None``.

        A reviewer that already holds a live starting reservation, a durable
        spawn-committed phase, or a running provider process is returned as a
        bounded receipt referencing the existing request instead of launching a
        duplicate.  Liveness follows the same evidence as every other
        admission check -- an unexpired pid-null reservation, a committed owner
        that is not proven dead, or a real pid whose identity is not a proven
        mismatch -- never elapsed or quiet time against a live provider.

        ``latest`` is the snapshot the caller already proved stable for its
        critical section.  This is the duplicate-admission decision, so a
        hidden append is exactly what would hide the live reviewer it is asked
        about; callers that admit on the answer hand in their proven snapshot
        rather than letting an unproven parse mint a second provider.

        The sealed ``quality_review_attempt`` target request, task and lens
        must match the caller.  A live reviewer for the same
        ``reviewer_task_id`` with a different sealed target is a rejection,
        never a reusable reservation.
        """

        if latest is None:
            latest = self._latest_by_request()

        def _admit(request_id: str) -> dict[str, Any]:
            identity = (target_request_id, target_task_id, lens)
            if identity == (None, None, None):
                return self._reviewer_receipt(request_id, latest)
            if any(value is None for value in identity):
                return {
                    "ok": False,
                    "error": "quality_review_attempt_identity_mismatch",
                }
            event = latest.get(request_id) or {}
            attempt = event.get("quality_review_attempt")
            if not isinstance(attempt, Mapping):
                return {
                    "ok": False,
                    "error": "quality_review_attempt_identity_mismatch",
                }
            if (
                str(attempt.get("target_request_id") or "") != target_request_id
                or str(attempt.get("target_task_id") or "") != target_task_id
                or str(attempt.get("lens") or "") != lens
            ):
                return {
                    "ok": False,
                    "error": "quality_review_attempt_identity_mismatch",
                }
            return self._reviewer_receipt(request_id, latest)

        for live in self._live.values():
            if live.task_id == reviewer_task_id and live.process.poll() is None:
                return _admit(live.request_id)
        for request_id, event in latest.items():
            if event.get("task_id") != reviewer_task_id:
                continue
            state = event.get("state")
            if state == "provider_spawn_committed":
                provider_pid = int(event.get("provider_pid") or 0)
                if provider_pid and _pid_identity_evidence(
                    provider_pid, event.get("provider_pid_start_ticks")
                ).verdict is not PidIdentityVerdict.MISMATCH:
                    return _admit(request_id)
                owner_pid = int(event.get("owner_pid") or 0)
                if owner_pid and _pid_identity_evidence(
                    owner_pid, event.get("owner_pid_start_ticks")
                ).verdict is not PidIdentityVerdict.MISMATCH:
                    return _admit(request_id)
                continue
            if state not in ACTIVE_PROCESS_STATES:
                continue
            pid = int(event.get("pid") or 0)
            if state == "starting" and not pid:
                if self._reviewer_source_graph_prewarm_live_event(event):
                    return _admit(request_id)
                if (
                    float(event.get("reservation_expires_at_epoch") or 0.0)
                    > time.time()
                ):
                    return _admit(request_id)
                continue
            if (
                pid
                and _pid_identity_evidence(
                    pid, event.get("pid_start_ticks")
                ).verdict
                is not PidIdentityVerdict.MISMATCH
            ):
                return _admit(request_id)
        return None

    def _reserve_quality_reviewer_attempt(
        self,
        *,
        reviewer_task_id: str,
        runner: str,
        adapter_id: str,
        target_request_id: str,
        target_task_id: str,
        lens: str,
        model: str | None,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Atomically reserve one exact reviewer attempt before any preparation.

        A concurrent or retried call for the same ``reviewer_task_id``
        reconciles the already-reserved attempt instead of minting a second
        reservation (and therefore a second provider).  The reservation is a
        durable pid-null ``starting`` event, so expiry reconciliation and the
        exact live-pid rules are unchanged.
        """

        # The stable snapshot may replay the whole ledger up to
        # ``_LEDGER_SNAPSHOT_MAX_ATTEMPTS`` times.  Taking it here, before the
        # cross-process registry lock, keeps that amplified work off every
        # unrelated reservation acknowledgement waiting on the same lock.
        snapshot = self._latest_by_request_stable()
        try:
            with self._lock, self._registry_lock():
                # ONE proven snapshot backs this whole critical section, and
                # every decision below is taken from it rather than from a
                # fresh parse.  Reconciliation, the already-reserved check and
                # the concurrency ceiling are each falsified by a single hidden
                # append -- and ``_append_event`` does not take this lock -- so
                # acting on an unproven parse could reserve a second provider
                # for a reviewer that already has one.  When no generation can
                # be shown at all the reservation defers instead of guessing.
                proven = self._proven_reservation_snapshot(snapshot)
                if proven is None:
                    return {"ok": False, "error": "ledger_snapshot_unproven"}
                latest = proven[0]
                self._reconcile_expired_starting_reservations(
                    proven, resolved=True
                )
                existing = self._live_reviewer_receipt(
                    reviewer_task_id,
                    latest,
                    target_request_id=target_request_id,
                    target_task_id=target_task_id,
                    lens=lens,
                )
                if existing is not None:
                    return existing
                if self._active_count(latest) >= _configured_limit():
                    return {"ok": False, "error": "concurrency_limit_reached"}
                request_id = uuid.uuid4().hex
                self._append_event({
                    "request_id": request_id,
                    "task_id": reviewer_task_id,
                    "runner": runner,
                    "topic": "quality_review",
                    "adapter_id": adapter_id,
                    "model": model,
                    "state": "starting",
                    "reservation_expires_at_epoch": (
                        time.time() + QUALITY_REVIEW_ATTEMPT_RESERVATION_SECONDS
                    ),
                    "owner_pid": os.getpid(),
                    "owner_pid_start_ticks": _pid_start_ticks(os.getpid()),
                    "timeout_seconds": timeout_seconds,
                    "quality_review_attempt": {
                        "target_request_id": target_request_id,
                        "target_task_id": target_task_id,
                        "lens": lens,
                    },
                })
                return {
                    "ok": True,
                    "already_reserved": False,
                    "request_id": request_id,
                    "state": "starting",
                }
        finally:
            # Reconciliation above recorded terminal intent only; the SQLite
            # transition and its single manager callback happen here, with the
            # outer registry lock already released.  Contained, so a settlement
            # failure never masks the reviewer receipt this block just built.
            self._settle_reviewer_terminal_intents_contained()

    def _reviewer_reservation_still_held(self, request_id: str) -> bool:
        """True while the exact attempt still owns an unterminalized reservation."""

        latest = self._latest_by_request().get(request_id) or {}
        return latest.get("state") == "starting"

    def _reviewer_provider_committed(self, request_id: str) -> bool:
        """True once a real reviewer provider process exists for the request.

        ``self._live`` is populated only after ``_popen`` returned a real PID,
        so presence is exact process evidence -- never elapsed or quiet time.
        A committed provider is therefore never terminalized by the bounded
        launch owner or reconcile.
        """

        with self._lock:
            return request_id in self._live

    def _reviewer_spawn_transition(
        self,
        request_id: str,
        binding: dict[str, Any] | None = None,
        *,
        reviewer_claim_epoch: Any = None,
    ) -> bool:
        """Atomically advance a still-held reservation to spawn-committed.

        This is the single cross-process registry-lock CAS handoff between the
        pid-null ``starting`` reservation and the durable
        ``provider_spawn_committed`` phase.  The bounded launch owner and
        reconciliation terminalize through the same lock (see
        ``_terminalize_reviewer_attempt`` and
        ``_reconcile_expired_starting_reservations``), so commit and
        terminalization are mutually exclusive across every ProcessManager: a
        timeout or reconcile only ever observes the exact still-preprovider
        state, and once this transition wins the reservation is never
        time-limited or killed by elapsed/quiet time.

        The winning transition persists the exact request/task/packet binding
        once, so a lost-ack/reload re-observing the same committed phase never
        rebinds a different packet and a retry reconciles the original attempt
        instead of minting a duplicate provider.

        ``reviewer_claim_epoch`` is the reviewer card's own claim epoch at the
        moment of commit.  It is the identity a later owner/provider-dead
        reconciliation must bind its terminal intent to: without it a recovery
        pass cannot tell this claim from a subsequent re-claim of the same
        reviewer task, so it fails closed and terminalizes nothing.

        The CAS reads through a bracketed generation proof rather than a plain
        parse: ``_append_event`` never takes the registry lock, so a rival
        commit -- or a supervisor's ``running`` row -- can land mid-parse and
        the row that would lose this CAS is simply not seen.  When no stable
        generation can be shown the transition answers ``False`` and no
        provider is spawned, so an unprovable ledger can never mint a duplicate
        provider for a reservation somebody else already committed.
        """

        snapshot = self._latest_by_request_stable()
        with self._registry_lock():
            proven = self._proven_reservation_snapshot(snapshot)
            if proven is None:
                return False
            latest = proven[0].get(request_id) or {}
            if latest.get("state") == "provider_spawn_committed":
                return True
            if latest.get("state") != "starting":
                return False
            committed: dict[str, Any] = {
                "request_id": request_id,
                "task_id": latest.get("task_id"),
                "runner": latest.get("runner"),
                "topic": latest.get("topic") or "quality_review",
                "adapter_id": latest.get("adapter_id"),
                "model": latest.get("model"),
                "state": "provider_spawn_committed",
                "reservation_expires_at_epoch": latest.get(
                    "reservation_expires_at_epoch"
                ),
                "owner_pid": os.getpid(),
                "owner_pid_start_ticks": _pid_start_ticks(os.getpid()),
            }
            epoch = (
                reviewer_claim_epoch
                if reviewer_claim_epoch is not None
                else latest.get("reviewer_claim_epoch")
            )
            if _is_bool_safe_int(epoch) and int(epoch) >= 1:
                committed["reviewer_claim_epoch"] = int(epoch)
            if latest.get("quality_review_attempt") is not None:
                committed["quality_review_attempt"] = latest["quality_review_attempt"]
            if isinstance(binding, dict):
                committed["packet"] = binding.get("packet")
                committed["target_request_id"] = binding.get("target_request_id")
                committed["target_task_id"] = binding.get("target_task_id")
                committed["lens"] = binding.get("lens")
                committed["target_claim_epoch"] = binding.get("target_claim_epoch")
            self._append_event(committed)
            return True

    def _reviewer_attach_provider_identity(
        self,
        request_id: str,
        *,
        pid: int,
        pid_start_ticks: int,
    ) -> bool:
        """Attach the spawned provider PID identity to a committed reservation.

        This is the exact CAS on ``(pid, pid_start_ticks)``.  Re-attaching the
        identical identity is idempotent (returns ``True`` and appends nothing),
        so a lost-ack/reload that re-observes the same live provider never
        spawns or commits a duplicate.  A different identity proves another
        owner already attached a provider for this exact request/task/packet
        binding: the caller is the losing spawner and must terminate its own
        just-spawned process.  Liveness never depends on elapsed or quiet time.

        The CAS reads through a bracketed generation proof rather than a plain
        parse.  ``_append_event`` never takes the registry lock, so the rival
        owner's own attach can land mid-parse and stay invisible -- and a
        hidden append here is exactly what would let two spawners both believe
        they attached first.  An unprovable snapshot therefore answers
        ``False``, so the caller terminates the process it just spawned: the
        safe half of the ambiguity, never a second live provider.
        """

        snapshot = self._latest_by_request_stable()
        with self._registry_lock():
            proven = self._proven_reservation_snapshot(snapshot)
            if proven is None:
                return False
            latest = proven[0].get(request_id) or {}
            state = latest.get("state")
            if state == "running":
                return (
                    int(latest.get("pid") or 0) == int(pid)
                    and latest.get("pid_start_ticks") == pid_start_ticks
                )
            if state != "provider_spawn_committed":
                return False
            existing_pid = int(latest.get("provider_pid") or 0)
            if existing_pid:
                return (
                    existing_pid == int(pid)
                    and latest.get("provider_pid_start_ticks") == pid_start_ticks
                )
            attached: dict[str, Any] = {
                "request_id": request_id,
                "task_id": latest.get("task_id"),
                "runner": latest.get("runner"),
                "topic": latest.get("topic") or "quality_review",
                "adapter_id": latest.get("adapter_id"),
                "model": latest.get("model"),
                "state": "provider_spawn_committed",
                "reservation_expires_at_epoch": latest.get(
                    "reservation_expires_at_epoch"
                ),
                "owner_pid": latest.get("owner_pid"),
                "owner_pid_start_ticks": latest.get("owner_pid_start_ticks"),
                "provider_pid": int(pid),
                "provider_pid_start_ticks": pid_start_ticks,
            }
            for key in (
                "quality_review_attempt",
                "packet",
                "target_request_id",
                "target_task_id",
                "lens",
                "target_claim_epoch",
                # Attaching the provider identity re-states the committed
                # phase, so the reviewer's own claim epoch has to travel with
                # it.  Dropping it here would leave every provider-dead
                # reservation unbindable and therefore never recoverable.
                "reviewer_claim_epoch",
            ):
                if latest.get(key) is not None:
                    attached[key] = latest[key]
            self._append_event(attached)
            return True

    def _terminalize_reviewer_attempt(
        self,
        request_id: str,
        task_id: str,
        runner: str,
        adapter_id: str,
        *,
        reason: str,
    ) -> None:
        """Terminalize a failed or abandoned reviewer attempt exactly once.

        Under the cross-process registry lock this rereads the latest exact
        event and refuses to terminalize a reservation whose spawn authority
        was durably committed (``provider_spawn_committed``) or whose provider
        process already exists (``running``/``self._live``).  A bounded launch
        owner in a *different* ProcessManager therefore never steals a
        committed spawn and a live provider is never classified by elapsed or
        quiet time.

        That reread is a BRACKETED one.  ``_append_event`` never takes the
        registry lock, so a plain parse under it can be interleaved by the very
        row that forbids this terminalization -- the rival spawn commit, or a
        supervisor publishing ``running`` -- and simply not see it.  The stable
        snapshot is taken with the lock released and re-proved by one sweep
        inside it; when no generation can be shown, this pass terminalizes
        nothing and a later one retries.
        """

        snapshot = self._latest_by_request_stable()
        with self._registry_lock():
            if request_id in self._live:
                return
            proven = self._proven_reservation_snapshot(snapshot)
            if proven is None:
                return
            latest = proven[0].get(request_id) or {}
            if latest.get("state") in ("provider_spawn_committed", "running"):
                return
            if latest.get("state") != "starting":
                return
            self._blocked(
                task_id, runner, "quality_review", adapter_id, reason,
                request_id=request_id,
            )

    def _reviewer_source_graph_prewarm_live_event(
        self, event: dict[str, Any]
    ) -> bool:
        """True when one event is a live, exact-owned Source Graph prewarm.

        A prewarm is live only when its reservation is still ``starting``, its
        latest preparation phase is the started prewarm phase, and its exact
        owner process identity still matches.  Dead, missing, mismatched, or
        unknown-identity owners fail closed, so reconciliation still
        terminalizes them.
        """

        if event.get("state") != "starting":
            return False
        if event.get("preparation_phase") != "reviewer_source_graph_prewarm_started":
            return False
        owner_pid = int(event.get("owner_pid") or 0)
        if owner_pid <= 0:
            return False
        return (
            _pid_identity_evidence(
                owner_pid, event.get("owner_pid_start_ticks")
            ).verdict
            is PidIdentityVerdict.MATCH
        )

    def _reviewer_source_graph_prewarm_live(self, request_id: str) -> bool:
        """True while the exact owned reviewer Source Graph prewarm is still running.

        The launcher thread publishes ``reviewer_source_graph_prewarm_started``
        before the build and ``reviewer_source_graph_prewarm_complete`` after,
        so the latest preparation phase for a still-``starting`` reservation is
        the truthful prewarm liveness signal.  A reservation that already moved
        past the prewarm (or never entered it) is not live here.
        """

        latest = self._latest_by_request().get(request_id) or {}
        return self._reviewer_source_graph_prewarm_live_event(latest)

    def _reviewer_launch_owner_join(
        self, launcher: threading.Thread, request_id: str
    ) -> str:
        """Wait for one bounded reviewer launch owner to finish.

        Returns ``"completed"`` when the launcher thread finished (its result is
        ready), ``"provider_committed"`` when a real provider process already
        exists (never time-limited), or ``"timeout"`` when the still-live owner
        should be terminalized.  A live owned Source Graph prewarm keeps
        extending the owner ceiling: it is never terminalized purely because
        wall time elapsed.
        """

        while launcher.is_alive():
            launcher.join(self._QUALITY_REVIEW_LAUNCH_OWNER_SECONDS)
            if not launcher.is_alive():
                return "completed"
            if self._reviewer_provider_committed(request_id):
                return "provider_committed"
            if self._reviewer_source_graph_prewarm_live(request_id):
                continue
            return "timeout"
        return "completed"

    def _launch_reserved_quality_reviewer(
        self,
        *,
        request_id: str,
        target_request_id: str,
        target_task_id: str,
        reviewer_task_id: str,
        runner: str,
        adapter_id: str,
        lens: str,
        model: str | None,
        timeout_seconds: int,
    ) -> None:
        """Run one reserved reviewer attempt under a single background owner.


        The handler already created, claimed and bound the exact reviewer card
        before acknowledgement.  Preparation and provider start never hold the
        handler.  Every failure path terminalizes the pre-reserved attempt
        exactly once and returns, leaving unrelated MCP calls responsive.
        A live uncapped provider is never cancelled by caller timeout.
        """

        def _fail(reason: str) -> None:
            self._terminalize_reviewer_attempt(
                request_id, reviewer_task_id, runner, adapter_id, reason=reason
            )

        launched: dict[str, Any] | None = None

        def _progress(phase: str, detail: str | None = None) -> None:
            self._publish_reviewer_progress(request_id, phase, detail)

        try:
            if not self._reviewer_reservation_still_held(request_id):
                return
            prep = self._prepared_quality_review(
                target_request_id, target_task_id, progress=_progress
            )
            if not prep.get("ok"):
                _fail(
                    "quality_review_preparation_failed:"
                    + str(prep.get("error") or "unknown")[:500]
                )
                return
            prepared = prep["prepared"]
            worker_adapter_id = str(prepared["worker_adapter_id"] or "")
            # Independence is a recorded ladder, not a vendor check.  Multi-model
            # routing exists to send work to a model by cost/difficulty; a
            # single-provider (or single-model) installation must still be able
            # to complete a review.  Record the best available rung -- best
            # first -- and never refuse on provider identity.  The anti-anchored
            # packet, sealed candidate, separate read-only process and
            # authenticated packet_sha256-bound submission (all enforced below
            # and in the reviewer receipt path) are what make the review
            # independent on every rung.
            independence = quality_review.resolve_independence_rung(
                worker_provider=runtime_adapters.provider_for_adapter(
                    worker_adapter_id
                ),
                reviewer_provider=runtime_adapters.provider_for_adapter(adapter_id),
                worker_model=worker_adapter_id,
                reviewer_model=str(adapter_id or ""),
            )
            _progress("independence_rung_recorded", str(independence["rung"]))
            _progress("packet_prepared")
            binding = {
                "target_request_id": target_request_id,
                "target_task_id": target_task_id,
                "target_claim_epoch": (
                    prepared["packet"].get("target", {}).get("claim_epoch")
                ),
                "adapter_id": adapter_id,
                "source_workspace": prepared["workspace"].as_metadata(),
                "candidate_paths": sorted(prepared["changed_hashes"]),
                "packet": prepared["packet"],
                "lens": lens,
                "independence": independence,
            }
            if not self._reviewer_reservation_still_held(request_id):
                return
            _progress("isolated_launch_started")
            launch_box: dict[str, dict[str, Any]] = {}

            def _run_isolated_launch() -> None:
                try:
                    launch_kwargs: dict[str, Any] = {
                        "task_id": reviewer_task_id,
                        "runner": runner,
                        "topic": "quality_review",
                        "adapter_id": adapter_id,
                        "model": model,
                        "owner_prompt": "",
                        "timeout_seconds": timeout_seconds,
                    }
                    if binding is not None:
                        launch_kwargs["quality_review_binding"] = binding
                    if request_id:
                        launch_kwargs["reserved_request_id"] = request_id
                    if _progress is not None:
                        launch_kwargs["prewarm_progress"] = _progress
                    launch_box["result"] = self.launch_task(**launch_kwargs)
                except Exception as exc:  # noqa: BLE001 -- defensive bounded worker
                    launch_box["result"] = {
                        "ok": False,
                        "error": f"quality_review_launch_failed:{exc}"[:500],
                    }


            launcher = threading.Thread(
                target=_run_isolated_launch,
                name=f"aiworkhub-reviewer-launch-{request_id[:8]}",
                daemon=True,
            )
            launcher.start()
            owner_result = self._reviewer_launch_owner_join(launcher, request_id)
            if owner_result == "provider_committed":
                # A real provider process already exists: never time-limit it.
                # The reservation resolves truthfully through the
                # running/monitor path.
                return
            if owner_result == "timeout":
                # The stale pre-provider owner outlived the ceiling with no
                # live owned prewarm to explain it.  Terminalize the pid-null
                # reservation exactly once and return; the worker thread is
                # never killed, but its ownership-aware checkpoints abort it
                # before it can spawn.
                _fail("quality_review_launch_timeout")
                return
            launched = launch_box.get("result")
        except Exception as exc:
            _fail(f"quality_review_launch_failed:{exc}"[:500])
            return
        if (
            launched is None
            or not launched.get("ok")
            or str(launched.get("request_id") or "") != request_id
        ):
            detail = ""
            if launched is not None:
                detail = str(
                    launched.get("error")
                    or launched.get("blocked_reason")
                    or "non_ok_receipt"
                )
            _fail(f"quality_review_launch_failed:{detail}"[:500])

    _complete_quality_reviewer_launch = _launch_reserved_quality_reviewer

    def _ensure_quality_reviewer_card_bound(
        self,
        *,
        request_id: str,
        target_request_id: str,
        target_task_id: str,
        reviewer_task_id: str,

        runner: str,
        adapter_id: str,
        lens: str,
        target_card: dict[str, Any] | None,
        terminalize_on_failure: bool,
    ) -> dict[str, Any]:
        """Create, claim and bind the exact reviewer card before acknowledgement."""

        worker_adapter_id = str((target_card or {}).get("adapter_id") or "")
        independence = quality_review.resolve_independence_rung(
            worker_provider=runtime_adapters.provider_for_adapter(
                worker_adapter_id
            ),
            reviewer_provider=runtime_adapters.provider_for_adapter(adapter_id),
            worker_model=worker_adapter_id,
            reviewer_model=str(adapter_id or ""),
        )
        created = core.create_task(
            task_id=reviewer_task_id,
            title=f"Independent {lens} review for {target_task_id}"[:300],
            runner=runner,
            topic="quality_review",
            objective=(
                "Review the exact anti-anchored candidate packet and submit "
                f"{lens} findings through the bound reviewer MCP tool."
            ),
            acceptance=[
                "Exactly one authenticated quality_review_submit receipt",
                "No repository mutation",
                quality_review.independence_acceptance_line(independence),
            ],
            allowed_writes=[],
            forbidden=[
                "repository_write",
                "worker_rationale_as_evidence",
                "model_supplied_provider_identity",
            ],
            required_outputs=[],
            validation=[],
            priority="high",
            callback_required=True,
            task_type="research",
            read_only=True,
        )
        create_ok = created.get("ok") is True
        create_structured = (
            created.get("reconciled") is True
            or str(created.get("receipt_state") or "") == "existing_identical"
        )
        create_detail = str(
            created.get("stderr")
            or created.get("stdout")
            or created.get("error")
            or ""
        )
        if not create_ok and not create_structured:
            reason = (
                "quality_review_task_create_failed:" + create_detail[:500]
            )
            if terminalize_on_failure:
                self._terminalize_reviewer_attempt(
                    request_id, reviewer_task_id, runner, adapter_id, reason=reason
                )
            return {"ok": False, "error": reason}

        def _fail(reason: str) -> dict[str, Any]:
            if terminalize_on_failure:
                self._terminalize_reviewer_attempt(
                    request_id, reviewer_task_id, runner, adapter_id, reason=reason
                )
            return {"ok": False, "error": reason}

        def _verify_durable(*, require_launch_request: bool) -> dict[str, Any] | None:
            try:
                card = _parse_card(
                    self._show_task(reviewer_task_id), reviewer_task_id
                )
            except LaunchRejected as exc:
                return _fail(f"quality_review_card_unreadable:{exc}"[:500])
            allowed_writes = card.get("allowed_writes")
            mismatches: list[str] = []
            if card.get("read_only") is not True:
                mismatches.append("read_only")
            if not isinstance(allowed_writes, list) or list(allowed_writes) != []:
                mismatches.append("allowed_writes")
            if card.get("topic") != "quality_review":
                mismatches.append("topic")
            if card.get("runner") != runner:
                mismatches.append("runner")
            if (
                require_launch_request
                and card.get("launch_request_id") != request_id
            ):
                mismatches.append("launch_request_id")
            if mismatches:
                return _fail(
                    "quality_review_card_identity_mismatch:"
                    + ",".join(mismatches)
                )
            return None

        durable_error = _verify_durable(require_launch_request=False)
        if durable_error is not None:
            return durable_error
        claim = task_engine.claim_start_exact(
            self.repo,
            reviewer_task_id,
            runner,
            "quality_review",
            request_id=request_id,
        )
        if not claim.get("ok"):
            reason = (
                "quality_review_claim_failed:"
                + str(claim.get("stderr") or claim.get("stdout") or "")[:500]
            )
            return _fail(reason)
        try:
            _committed_claim_card(
                claim,
                request_id=request_id,
                task_id=reviewer_task_id,
                runner=runner,
                topic="quality_review",
            )
        except LaunchRejected as exc:
            return _fail(f"quality_review_claim_failed:{exc}"[:500])
        durable_error = _verify_durable(require_launch_request=True)
        if durable_error is not None:
            return durable_error
        return {
            "ok": True,
            "request_id": request_id,
            "task_id": reviewer_task_id,
            "target_request_id": target_request_id,
            "launch_request_id": request_id,
        }

    def launch_quality_reviewer(
        self,
        *,
        target_request_id: str,
        target_task_id: str,
        reviewer_task_id: str,
        runner: str,
        adapter_id: str,
        lens: str,
        model: str | None = None,
        timeout_seconds: int = 1800,
    ) -> dict[str, Any]:
        """Create and launch one independent packet-bound reviewer task.

        Acknowledgement is returned only after the exact reviewer card,
        launch_request_id binding and reservation are durably reconcilable.
        Expensive preparation and provider start still run under one background
        owner so a live uncapped provider is never cancelled by caller timeout.
        Retried calls reconcile the same attempt instead of launching a duplicate.
        """

        if lens not in quality_evidence.JUDGMENT_LENSES:
            return {"ok": False, "error": "quality_review_lens_invalid"}
        try:
            _target_card: dict[str, Any] | None = _parse_card(
                self._show_task(target_task_id), target_task_id
            )
        except LaunchRejected:
            _target_card = None
        _target_verdict = quality_review.assess_reviewer_launch_target(
            target_card=_target_card
        )

        def _bind_visible_card(
            request_id: str, *, terminalize_on_failure: bool
        ) -> dict[str, Any]:
            return self._ensure_quality_reviewer_card_bound(
                request_id=request_id,
                target_request_id=target_request_id,
                target_task_id=target_task_id,
                reviewer_task_id=reviewer_task_id,
                runner=runner,
                adapter_id=adapter_id,
                lens=lens,
                target_card=_target_card,
                terminalize_on_failure=terminalize_on_failure,
            )

        existing = self._live_reviewer_receipt(
            reviewer_task_id,
            target_request_id=target_request_id,
            target_task_id=target_task_id,
            lens=lens,
        )
        if existing is not None:
            if existing.get("ok") is not True:
                return existing
            bound = _bind_visible_card(
                str(existing.get("request_id") or ""),
                terminalize_on_failure=False,
            )
            if bound.get("ok") is not True:
                return bound
            return existing
        if not _target_verdict.get("can_launch"):
            return {
                "ok": False,
                "error": "quality_review_target_not_review_ready",
                "reason": _target_verdict.get("reason"),
                "target_substatus": _target_verdict.get("target_substatus"),
                "fails_at_launch": True,
            }
        reservation = self._reserve_quality_reviewer_attempt(
            reviewer_task_id=reviewer_task_id,
            runner=runner,
            adapter_id=adapter_id,
            target_request_id=target_request_id,
            target_task_id=target_task_id,
            lens=lens,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        if reservation.get("ok") is not True:
            return reservation
        request_id = str(reservation["request_id"])
        bound = _bind_visible_card(
            request_id,
            terminalize_on_failure=not bool(reservation.get("already_reserved")),
        )
        if bound.get("ok") is not True:
            return bound
        if reservation.get("already_reserved"):
            return reservation
        threading.Thread(
            target=self._launch_reserved_quality_reviewer,
            kwargs={

                "request_id": request_id,
                "target_request_id": target_request_id,
                "target_task_id": target_task_id,
                "reviewer_task_id": reviewer_task_id,
                "runner": runner,
                "adapter_id": adapter_id,
                "lens": lens,
                "model": model,
                "timeout_seconds": timeout_seconds,
            },
            name=f"aiworkhub-reviewer-{request_id[:8]}",
            daemon=True,
        ).start()
        return {
            "ok": True,
            "launch_implemented": LAUNCH_IMPLEMENTED,
            "launch_enabled": True,
            "request_id": request_id,
            "task_id": reviewer_task_id,
            "runner": runner,
            "topic": "quality_review",
            "adapter_id": adapter_id,
            "model": model,
            "state": "starting",
            "pid": 0,
            "deferred": True,
            "already_reserved": False,
            "launch_request_id": request_id,
            "shell": False,
            **_legacy_timeout_fields(timeout_seconds),
        }

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
        quality_review_binding: dict[str, Any] | None = None,
        reserved_request_id: str | None = None,
        prewarm_progress: Callable[..., None] | None = None,
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
        residual_contract_manifest: list[dict[str, Any]] = []
        claimed = False
        provider_env: dict[str, str] | None = None
        kilo_auth_source: Path | None = None
        kilo_auth_evidence: dict[str, Any] | None = None
        launch_phase = "preflight"

        def _abandon_terminalized_reviewer() -> dict[str, Any]:
            # A stale pre-provider owner discovered its exact ``starting``
            # reservation was already terminalized.  Clean up partial artifacts
            # and return a bounded non-ok receipt WITHOUT appending any event,
            # so terminalization stays exactly-once and a live/terminalized
            # reservation is never stolen.
            if workspace is not None:
                try:
                    cleanup_workspace(workspace.repo, workspace.path, workspace.home)
                except WorkspaceError:
                    pass
            if spec_path is not None:
                unlink_if_regular(spec_path)
            if authority_path is not None:
                unlink_if_regular(authority_path)
            if bridge_request is not None:
                vscode_lm_bridge.cancel_request(bridge_request)
            claimed_task_transition = "not_claimed"
            if claimed:
                # The reservation is somebody else's to terminalize, but the
                # CARD this launch claimed is not: returning here without a
                # transition leaves it ``processing`` under a claim no live
                # owner holds, and nothing else brings a later pass back to
                # it.  The reservation event stays untouched -- this moves the
                # exact claimed task, once, to the truthful failure state.
                claimed_task_transition = "launch_failed"
                failed = task_engine.mark_launch_failed(
                    self.repo,
                    task_id,
                    runner,
                    reason="quality_review_reservation_terminalized",
                    request_id=request_id or reserved_request_id or "",
                )
                if not failed.get("ok"):
                    # Naming the refusal keeps a still-processing card visible
                    # in the receipt instead of the caller reading a bounded
                    # blocked reason as proof the claim was released.
                    claimed_task_transition = (
                        "launch_failure_transition_failed:"
                        + str(failed.get("stderr") or failed.get("stdout") or "")[:200]
                    )
            return {
                "ok": False,
                "launch_implemented": LAUNCH_IMPLEMENTED,
                "launch_enabled": True,
                "request_id": reserved_request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": topic,
                "adapter_id": adapter_id,
                "state": "blocked",
                "blocked_reason": "quality_review_reservation_terminalized",
                "claimed_task_transition": claimed_task_transition,
                "shell": False,
            }

        try:
            if (
                reserved_request_id is not None
                and not self._reviewer_reservation_still_held(reserved_request_id)
            ):
                raise _ReviewerReservationTerminalized(reserved_request_id)
            _validate_adapter_identity(runner, adapter_id)
            # Materialize completed dependencies' promoted (accepted-but-not-yet-
            # committed) outputs into this dependent's isolated worktree by
            # declaring them as immutable inputs before create_workspace and the
            # B919 input-drift snapshot see the card.
            if reserved_request_id is not None:
                card = self._preflight_card(
                    task_id,
                    runner,
                    topic,
                    adapter_id,
                    reserved_request_id=reserved_request_id,
                )
            else:
                card = self._preflight_card(task_id, runner, topic, adapter_id)
            claimed = core._lifecycle_state(card) == "processing"
            _enforce_quality_review_launch_binding(topic, quality_review_binding)

            card = self._with_dependency_inputs(card)
            replay_authorization = _validation_only_replay_authorization(
                card, task_id
            )
            if replay_authorization is not None:
                return self._launch_validation_only_replay(
                    task_id=task_id,
                    runner=runner,
                    topic=topic,
                    adapter_id=adapter_id,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    card=card,
                    authorization=replay_authorization,
                )
            external_readonly_dirs = _external_readonly_dirs(card, adapter_id)
            authority_repo = _task_authority_repo(self.repo, card)
            context_result = _launch_project_context(
                self.repo, card, quality_review_binding
            )
            # Load the BYOK credential (deepseek_copilot_cli) BEFORE claim-start.
            # A missing/invalid credential raises here, leaving the task
            # pending/unclaimed -- never claim on a missing credential.
            provider_env, model = self._resolve_provider_env(adapter_id, model)
            if adapter_id == runtime_adapters.GROK_KILO_ADAPTER:
                try:
                    kilo_auth_source = kilo_auth.resolve_kilo_auth_source(
                        home=Path.home(),
                        xdg_data_home=os.environ.get("XDG_DATA_HOME") or None,
                        platform_name=os.name,
                    )
                except kilo_auth.KiloAuthError as exc:
                    raise LaunchRejected(
                        f"grok_kilo_auth_unavailable:{exc.reason}"
                    ) from exc
            sandbox_backend = _sandbox_backend_for_adapter(adapter_id)
            launch_phase = "workspace_and_runtime_provision"
            request_id = reserved_request_id or uuid.uuid4().hex
            reservation_ctx = (
                nullcontext()
                if reserved_request_id is not None
                else self._launch_reservation({
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": topic,
                    "adapter_id": adapter_id,
                    "model": model,
                    "timeout_seconds": timeout_seconds,
                    "authority": f"coordinator_claim_isolated_worktree_{sandbox_backend}",
                    "sandbox_backend": sandbox_backend,
                    "project_context": (
                        context_result.metadata if context_result is not None else None
                    ),
                })
            )
            with reservation_ctx:
                self.process_dir.mkdir(parents=True, exist_ok=True)
                chmod_path(self.process_dir, 0o700)
                stdout_path = self.process_dir / f"{request_id}.stdout.log"
                stderr_path = self.process_dir / f"{request_id}.stderr.log"
                status_path = self.process_dir / f"{request_id}.supervisor.json"
                cancel_path = self.process_dir / f"{request_id}.cancel.json"
                metadata_path = self.process_dir / f"{request_id}.request.json"
                spec_path = self.process_dir / f"{request_id}.supervisor-spec.json"
                _touch_0600(stdout_path)
                _touch_0600(stderr_path)

                review_packet_path: Path | None = None
                review_workspace_evidence: dict[str, Any] | None = None
                rework_overlay_path: Path | None = None
                rework_overlay_packet: dict[str, Any] | None = None
                crash_retry_packet_path: Path | None = None
                crash_retry_packet: dict[str, Any] | None = None
                if quality_review_binding is not None:
                    source_workspace = WorkerWorkspace.from_metadata(
                        dict(quality_review_binding["source_workspace"])
                    )
                    workspace, review_workspace_evidence = create_quality_review_workspace(
                        source_workspace,
                        request_id,
                        quality_review_binding["candidate_paths"],
                        adapter_id,
                    )
                    review_packet_path = (
                        workspace.home
                        / "task_mcp_worker_runtime"
                        / "quality_review_packet.json"
                    )
                    write_json_0600(
                        review_packet_path,
                        dict(quality_review_binding["packet"]),
                    )
                    launch_phase = "quality_review_source_graph_prewarm"
                    try:
                        worker_ai_tools_mcp.verify_quality_review_prewarm_authority(
                            authority_repo
                        )
                    except worker_ai_tools_mcp.WorkerToolError as exc:
                        raise LaunchRejected(
                            "quality_review_source_graph_authority_unverified:"
                            + str(exc)[:240]
                        ) from exc
                    if prewarm_progress is not None:
                        prewarm_progress("reviewer_source_graph_prewarm_started")
                    try:
                        worker_ai_tools_mcp.prewarm_quality_review_source_graph(
                            review_packet_path,
                            repo=workspace.path,
                            authority_repo=authority_repo,
                        )
                    except worker_ai_tools_mcp.WorkerToolError as exc:
                        # Prewarm prebuilds the reviewer's candidate Source Graph
                        # overlay only to make its queries fast; it is an
                        # optimisation, never a correctness precondition, because
                        # the sealed review packet already carries every
                        # candidate's content.  A path Source Graph *deliberately*
                        # does not index -- an eval artifact, a generated fixture,
                        # an unsupported extension -- is therefore skipped and
                        # recorded, and the reviewer still launches from the
                        # packet alone.  Every other prewarm failure (a hash
                        # mismatch against the sealed packet, an
                        # unreadable-but-indexable file, a path-safety violation,
                        # a clone/backup I/O error) is a genuine problem on a file
                        # that SHOULD be indexable and is re-raised loudly so it
                        # is never swallowed.
                        skip = quality_review.classify_prewarm_error(str(exc))
                        if not skip["tolerated"]:
                            if prewarm_progress is not None:
                                prewarm_progress(
                                    "reviewer_source_graph_prewarm_failed"
                                )
                            raise LaunchRejected(
                                "quality_review_source_graph_prewarm_failed:"
                                + str(exc)[:240]
                            ) from exc
                        if prewarm_progress is not None:
                            prewarm_progress(
                                "reviewer_source_graph_prewarm_skipped_excluded",
                                skip["reason"],
                            )
                    else:
                        if prewarm_progress is not None:
                            prewarm_progress(
                                "reviewer_source_graph_prewarm_complete"
                            )
                    # Any further exception in this block belongs to worker MCP
                    # runtime registration, not the reviewer Source Graph
                    # prewarm this block just completed -- restore the phase so
                    # an unexpected failure there is never misclassified as a
                    # prewarm contract/data failure.
                    launch_phase = "workspace_and_runtime_provision"
                else:
                    workspace = create_workspace(self.repo, request_id, card, adapter_id)
                    residual_contract_manifest = build_residual_contract_manifest(
                        workspace, card
                    )
                    (
                        rework_overlay_path,
                        rework_overlay_packet,
                    ) = _materialize_worker_rework_overlay(
                        workspace,
                        task_id=task_id,
                        card=card,
                    )
                    (
                        crash_retry_packet_path,
                        crash_retry_packet,
                    ) = _materialize_crash_retry_packet(
                        self.process_dir,
                        workspace,
                        task_id=task_id,
                        card=card,
                        rework_overlay_packet=rework_overlay_packet,
                    )
                if adapter_id == runtime_adapters.GROK_KILO_ADAPTER:
                    if kilo_auth_source is None:
                        raise LaunchRejected("grok_kilo_auth_unavailable:source_unresolved")
                    try:
                        projection = kilo_auth.project_xai_auth(
                            kilo_auth_source, workspace.home
                        )
                    except kilo_auth.KiloAuthError as exc:
                        # Never include the coordinator auth path or provider
                        # record in launch evidence.  The typed reason is
                        # sufficient for a bounded pre-claim failure.
                        raise LaunchRejected(
                            f"grok_kilo_auth_unavailable:{exc.reason}"
                        ) from exc
                    kilo_auth_evidence = {
                        "provider": projection.provider,
                        "status": projection.status,
                        "destination_bytes": projection.destination_bytes,
                        "destination_sha256": projection.destination_sha256,
                    }
                worker_source_graph_targets = _worker_mcp_source_graph_targets(context_result)
                worker_session_topic = _worker_mcp_session_topic(context_result, topic)
                worker_mcp_runtime = _provision_worker_mcp_runtime_for_authority(
                    workspace,
                    request_id=request_id,
                    task_id=task_id,
                    runner=runner,
                    topic=topic,
                    backend=sandbox_backend,
                    authority_repo=authority_repo,
                    source_graph_targets=worker_source_graph_targets,
                    allowed_writes=[str(value) for value in card.get("allowed_writes") or []],
                    session_topic=worker_session_topic,
                    quality_review_packet_path=review_packet_path,
                    rework_overlay_path=rework_overlay_path,
                )
                vscode_source_graph_request = _launch_source_graph_request(
                    card, quality_review_binding
                )
                vscode_source_graph_result: dict[str, Any] | None = None
                if (
                    adapter_id in _VSCODE_LM_IN_PROCESS_ADAPTERS
                    and isinstance(vscode_source_graph_request, dict)
                    and vscode_source_graph_request.get("query")
                ):
                    # Execute the mandatory orientation query before the
                    # request enters the editor-host spool.  Calling back over
                    # the coordinator's single MCP stdio connection from four
                    # concurrent VS Code LM workers caused head-of-line
                    # blocking and 90-second bootstrap timeouts.  This uses
                    # the exact same immutable worker identity, target scope,
                    # HMAC ledger and canonical authority as later live tool
                    # calls, so the receipt remains genuine worker-scoped
                    # evidence.  Only the first query is prefetched; every
                    # implementation/review re-query still uses live MCP.
                    source_graph_input = {
                        key: vscode_source_graph_request[key]
                        for key in (
                            "mode", "query", "budget", "target",
                            "bundle_type", "workflow_stage",
                        )
                        if vscode_source_graph_request.get(key) is not None
                    }
                    source_graph_input.setdefault("mode", "focus")
                    source_graph_input.setdefault("workflow_stage", "orientation")
                    prefetch_ctx = worker_ai_tools_mcp.WorkerToolContext(
                        task_id=task_id,
                        runner=runner,
                        topic=topic,
                        request_id=request_id,
                        repo=workspace.path,
                        authority_repo=authority_repo,
                        source_graph_targets=tuple(worker_source_graph_targets),
                        allowed_writes=tuple(
                            str(value) for value in card.get("allowed_writes") or []
                        ),
                        session_topic=worker_session_topic,
                        audit_ledger_path=worker_mcp_runtime.audit_ledger_path,
                        audit_hmac_key_path=worker_mcp_runtime.audit_hmac_key_path,
                        quality_review_packet_path=review_packet_path,
                        rework_overlay_packet=rework_overlay_packet,
                        rework_overlay_packet_path=rework_overlay_path,
                        provenance="prefetch",
                    )
                    vscode_source_graph_result = worker_ai_tools_mcp.source_graph_query(
                        prefetch_ctx,
                        **source_graph_input,
                    )
                    if vscode_source_graph_result.get("ok") is not True:
                        reason = str(
                            vscode_source_graph_result.get("reason")
                            or vscode_source_graph_result.get("error")
                            or "source_graph_result_not_ok"
                        )
                        raise LaunchRejected(
                            "vscode_lm_initial_source_graph_prefetch_failed:"
                            + reason[:300]
                        )
                launch_phase = "prompt_and_adapter_plan"
                if quality_review_binding is not None:
                    private_tool_name = "aiworkhub_worker_quality_review_submit"
                    prompt = quality_review.assemble_reviewer_prompt(
                        quality_review_binding["packet"],
                        lens=str(quality_review_binding["lens"]),
                        adapter_id=adapter_id,
                        submit_tool_name=private_tool_name,
                        packet_path=(
                            str(review_packet_path) if review_packet_path is not None else None
                        ),
                    )
                    prompt_budget = {
                        "schema_id": "aiworkhub.worker_prompt_budget.v1",
                        "mode": "quality_review",
                        "total_bytes": len(prompt.encode("utf-8")),
                        "byte_labels_are_token_truth": False,
                    }
                else:
                    prompt_budget = {}
                    prompt = build_worker_prompt(
                        task_id=task_id,
                        runner=runner,
                        topic=topic,
                        card=card,
                        owner_prompt=owner_prompt,
                        project_context_bundle=(
                            context_result.prompt_bundle if context_result is not None else ""
                        ),
                        crash_retry_packet=crash_retry_packet,
                        _budget_report=prompt_budget,
                    )
                include_partial_messages = (
                    adapter_id == "claude_cli"
                    and isinstance(card.get("token_budget"), dict)
                    and bool(card["token_budget"])
                )
                if adapter_id in _VSCODE_LM_IN_PROCESS_ADAPTERS:
                    bridge_request = vscode_lm_bridge.create_request(
                        repo=self.repo,
                        request_id=request_id,
                        workspace_path=workspace.path,
                        workspace_home=workspace.home,
                        prompt=prompt,
                        model=str(model or runtime_adapters.GLM_DEFAULT_MODEL),
                        allowed_writes=workspace.allowed_writes,
                        workspace_parent_baseline=workspace.parent_baseline,
                        timeout_seconds=timeout_seconds,
                        source_graph_request=vscode_source_graph_request,
                        source_graph_result=vscode_source_graph_result,
                        request_kind=(
                            "quality_review"
                            if quality_review_binding is not None
                            else "worker"
                        ),
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
                    provider_env = _vscode_lm_worker_env(
                        provider_env,
                        worker_mcp_runtime.package_import_root,
                    )
                else:
                    plan = self._build_adapter(
                        adapter_id=adapter_id,
                        prompt=prompt,
                        repo=workspace.path,
                        model=model,
                        outer_sandbox_backend=sandbox_backend,
                        additional_readonly_dirs=external_readonly_dirs,
                        include_partial_messages=include_partial_messages,
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
                # Provision the request-owned temp authority before composing
                # the Landlock command.  sandbox_argv deliberately grants
                # --worker-temp only for an already-provisioned directory;
                # creating TMPDIR later at supervisor spawn leaves provider
                # runtimes unable to create their own nested temp directories.
                launch_env = worker_launch_env(
                    adapter_id,
                    repo=self.repo,
                    request_id=request_id,
                    home=(
                        workspace.home
                        if sandbox_backend in {"landlock", VSCODE_LM_IN_PROCESS_BACKEND}
                        else None
                    ),
                    isolated_task_queue_db=True,
                    provider_env=provider_env,
                )
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

                if (
                    reserved_request_id is not None
                    and not self._reviewer_reservation_still_held(reserved_request_id)
                ):
                    raise _ReviewerReservationTerminalized(reserved_request_id)
                launch_phase = "canonical_claim"
                if claimed and reserved_request_id is not None:
                    if str(card.get("launch_request_id") or "") != str(request_id):
                        raise LaunchRejected(
                            "claim_start_failed:"
                            "card_scoped_claim_start_ineligible:"
                            + str(core._lifecycle_state(card) or "processing")
                        )
                    claim_epoch = card.get("claim_epoch")
                    if type(claim_epoch) is not int or claim_epoch < 1:
                        raise LaunchRejected("claim_receipt_invalid:claim_epoch")
                else:
                    claim = task_engine.claim_start_exact(
                        self.repo, task_id, runner, topic, request_id=request_id
                    )
                    if not claim.get("ok"):
                        raise LaunchRejected(
                            "claim_start_failed:"
                            + str(claim.get("stderr") or claim.get("stdout") or "")[:300]
                        )
                    card = _committed_claim_card(
                        claim,
                        request_id=request_id,
                        task_id=task_id,
                        runner=runner,
                        topic=topic,
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
                    "claim_epoch": card["claim_epoch"],
                    "rework_predecessor": (
                        dict(card["rework_predecessor"])
                        if isinstance(card.get("rework_predecessor"), dict)
                        else None
                    ),
                    "validation_only_replay_authorization": (
                        dict(card["validation_only_replay_authorization"])
                        if isinstance(
                            card.get("validation_only_replay_authorization"), dict
                        )
                        else None
                    ),
                    "adapter_id": adapter_id,
                    "model": model,
                    "provider_stream_mode": (
                        "partial_messages_for_explicit_live_budget"
                        if include_partial_messages
                        else "terminal_events"
                    ),
                    **_legacy_timeout_fields(timeout_seconds),
                    "token_budget": (
                        dict(token_budget_value)
                        if isinstance(
                            token_budget_value := card.get("token_budget"), dict
                        )
                        else None
                    ),
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "supervisor_status_path": str(status_path),
                    "cancel_path": str(cancel_path),
                    "metadata_path": str(metadata_path),
                    "prompt_sha256": prompt_hash,
                    "prompt_budget": prompt_budget,
                    "vscode_lm_bridge": (
                        vscode_lm_bridge.bridge_request_metadata(bridge_request)
                        if bridge_request is not None
                        else None
                    ),
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
                        "kilo_config_path": str(worker_mcp_runtime.kilo_config_path),
                        "kilo_auth": kilo_auth_evidence,
                        "authority_repo": str(authority_repo),
                        "source_graph_targets": worker_source_graph_targets,
                        "allowed_writes": [
                            str(value) for value in card.get("allowed_writes") or []
                        ],
                        "session_topic": worker_session_topic,
                        "source_graph_authority": (
                            {
                                "authority_source": "candidate_overlay",
                                "authority_state": "quality_review_readonly",
                                "target_request_id": str(
                                    quality_review_binding["target_request_id"]
                                ),
                                "target_task_id": str(
                                    quality_review_binding["target_task_id"]
                                ),
                                "packet_sha256": str(
                                    quality_review_binding["packet"]["packet_sha256"]
                                ),
                            }
                            if quality_review_binding is not None
                            else (
                                {
                                    "authority_source": "rework_overlay",
                                    "authority_state": "request_scoped_predecessor",
                                    "packet_path": str(rework_overlay_path),
                                    "target_request_id": str(
                                        rework_overlay_packet.get(
                                            "predecessor_request_id"
                                        )
                                    ),
                                    "target_task_id": str(
                                        rework_overlay_packet.get(
                                            "predecessor_task_id"
                                        )
                                    ),
                                    "packet_sha256": str(
                                        rework_overlay_packet.get(
                                            "canonical_digest"
                                        )
                                    ),
                                }
                                if rework_overlay_packet is not None
                                else {
                                "authority_source": "canonical",
                                "authority_state": "sole_authority",
                                }
                            )
                        ),
                    },
                    "sandbox_backend": sandbox_backend,
                    "validation": list(card.get("validation") or []),
                    "validation_roles": list(card.get("validation_roles") or []),
                    "work_kind": str(card.get("work_kind") or "generic"),
                    "required_outputs": list(card.get("required_outputs") or []),
                    "read_only": card.get("read_only") is True,
                    "allow_empty_required_outputs": list(
                        card.get("allow_empty_required_outputs") or []
                    ),
                    "allow_unchanged_required_outputs": list(
                        card.get("allow_unchanged_required_outputs") or []
                    ),
                    "immutable_inputs": declared_immutable_inputs,
                    "immutable_input_manifest": immutable_input_manifest,
                    "residual_contract_manifest": residual_contract_manifest,
                    "crash_retry_packet": (
                        {
                            "path": str(crash_retry_packet_path),
                            "packet_sha256": str(
                                crash_retry_packet.get("packet_sha256") or ""
                            ),
                            "predecessor_request_id": str(
                                crash_retry_packet.get("predecessor_request_id") or ""
                            ),
                        }
                        if crash_retry_packet is not None
                        else None
                    ),
                    "external_readonly_dirs": external_readonly_dirs,
                    "workspace": workspace.as_metadata(),
                    "quality_review": (
                        {
                            "target_request_id": str(
                                quality_review_binding["target_request_id"]
                            ),
                            "target_task_id": str(
                                quality_review_binding["target_task_id"]
                            ),
                            "target_claim_epoch": quality_review_binding[
                                "target_claim_epoch"
                            ],
                            "adapter_id": str(adapter_id),
                            "lens": str(quality_review_binding["lens"]),
                            "packet_sha256": str(
                                quality_review_binding["packet"]["packet_sha256"]
                            ),
                            "packet_path": str(review_packet_path),
                            "workspace": review_workspace_evidence,
                        }
                        if quality_review_binding is not None
                        else None
                    ),
                }
                launch_phase = "request_metadata"
                write_json_0600(metadata_path, metadata)
                authority_path = self._terminal_authority_grant_path(request_id)
                launch_phase = "terminal_authority"
                _write_terminal_authority_grant(
                    authority_path,
                    self._terminal_authority_key(),
                    repo=self.repo,
                    task_id=task_id,
                    runner=runner,
                    topic=topic,
                    request_id=request_id,
                )
                launch_phase = "supervisor_spec"
                launch_cwd = _worker_launch_cwd(workspace.path)
                write_json_0600(spec_path, {
                    "argv": worker_argv,
                    "cwd": launch_cwd,
                    **_legacy_timeout_fields(timeout_seconds),
                    "status_path": str(status_path),
                    "cancel_path": str(cancel_path),
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "max_output_bytes": 16 * 1024 * 1024,
                    "adapter_id": adapter_id,
                    "token_budget": metadata.get("token_budget"),
                })

                supervisor = _worker_supervisor_script()
                # landlock confines the *real* isolated workspace.home
                # directory directly, so HOME must literally be that path.
                # bubblewrap instead remounts workspace.home onto the string
                # from worker_workspace.bubblewrap_home_env_value() inside
                # its own mount namespace (see sandbox_argv); passing
                # home=None here makes sanitized_env() seed that identical
                # shared string as HOME so the two line up by construction,
                # not by two independently-coincidental Path.home() calls
                # (B314_F004).
                if (
                    reserved_request_id is not None
                    and not self._reviewer_spawn_transition(
                        reserved_request_id,
                        binding=quality_review_binding,
                        # The reviewer card is already claimed at this point, so
                        # the committed phase can carry the exact epoch a later
                        # owner/provider-dead recovery must bind to.
                        reviewer_claim_epoch=metadata.get("claim_epoch"),
                    )
                ):
                    raise _ReviewerReservationTerminalized(reserved_request_id)
                launch_phase = "supervisor_spawn"
                process = self._popen(
                    [sys.executable, str(supervisor), "--spec", str(spec_path)],
                    cwd=launch_cwd,
                    env=launch_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    start_new_session=True,
                )
                started_at = _utcnow()
                launch_phase = "supervisor_pid_identity"
                start_ticks = _pid_start_ticks(process.pid)
                if start_ticks is None:
                    _terminate_process_group(process.pid, grace_seconds=5.0)
                    raise LaunchRejected("supervisor_pid_identity_unavailable")
                if reserved_request_id is not None and not (
                    self._reviewer_attach_provider_identity(
                        reserved_request_id,
                        pid=process.pid,
                        pid_start_ticks=start_ticks,
                    )
                ):
                    # Another owner already attached a different provider pid for
                    # this exact request/task/packet binding: this spawn is the
                    # loser and must never become live or durable.
                    _terminate_process_group(process.pid, grace_seconds=5.0)
                    raise _ReviewerReservationTerminalized(reserved_request_id)
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
                    bridge_request=bridge_request,
                )
                with self._lock:
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
                    "prompt_budget": prompt_budget,
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
                "prompt_budget": prompt_budget,
                "shell": False,
                **_legacy_timeout_fields(timeout_seconds),
            }
        except _ReviewerReservationTerminalized:
            return _abandon_terminalized_reviewer()
        except Exception as exc:  # noqa: BLE001 - return durable launch diagnostics
            if (
                reserved_request_id is not None
                and not self._reviewer_reservation_still_held(reserved_request_id)
                and not self._reviewer_provider_committed(reserved_request_id)
            ):
                # The bounded launch owner terminalized this exact reservation
                # while the stale owner was still preparing and then raised.
                # Avoid a second terminal event; preserve exactly-once
                # terminalization and never steal the terminalized reservation.
                return _abandon_terminalized_reviewer()
            expected = isinstance(
                exc,
                (
                    LaunchRejected,
                    project_context.ProjectContextError,
                    WorkspaceError,
                    OSError,
                    ValueError,
                ),
            )
            diagnostic = (
                None
                if expected
                else _bounded_launch_diagnostic(
                    exc,
                    phase=launch_phase,
                    repo=self.repo,
                )
            )
            reason = (
                str(exc)
                if expected
                else f"unexpected_launch_error:{type(exc).__name__}:{exc}"
            )
            # A pre-claim failure leaves a pending card pending.  Fabricating a
            # claim merely to manufacture a terminal review was the source of
            # false review-ready launch failures.  A card already claimed by
            # auto-pickup, or claimed later in this launch, is instead moved to
            # the truthful blocked/launch_failed state below.
            if claimed:
                blocked_result = task_engine.mark_launch_failed(
                    self.repo,
                    task_id,
                    runner,
                    reason=reason[:500],
                    request_id=request_id or "",
                )
                if not blocked_result.get("ok"):
                    reason += ":launch_failure_transition_failed:" + str(
                        blocked_result.get("stderr") or ""
                    )[:200]
            else:
                blocker_result = task_engine.record_launch_blocker(
                    self.repo,
                    task_id,
                    runner,
                    topic,
                    adapter_id=adapter_id,
                    reason=reason,
                )
                if not blocker_result.get("ok"):
                    reason += ":launch_blocker_record_failed:" + str(
                        blocker_result.get("stderr") or ""
                    )[:200]
            # Cancel the VS Code LM claim BEFORE deleting the request workspace
            # it refers to, so a claim never outlives its workspace.
            for release_error in _release_launch_request_resources(
                bridge_request=bridge_request,
                workspace=workspace,
            ):
                reason += ":" + release_error
            if spec_path is not None:
                unlink_if_regular(spec_path)
            if authority_path is not None:
                unlink_if_regular(authority_path)
            return self._blocked(
                task_id,
                runner,
                topic,
                adapter_id,
                reason,
                request_id=request_id,
                state="launch_failed" if claimed else "blocked",
                diagnostic=diagnostic,
            )

    def _assert_no_duplicate_task(
        self,
        task_id: str,
        latest: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """Refuse a second launch of one task, from a proven ledger read.

        ``latest`` is the generation-proven snapshot admission already holds.
        Re-reading here would be an unproven parse, and the row a concurrent
        append hides is exactly the live duplicate this guard exists to find.
        """

        for live in self._live.values():
            if live.task_id == task_id and live.process.poll() is None:
                raise LaunchRejected(f"duplicate_live_task:{live.request_id}")
        if latest is None:
            latest = self._latest_by_request()
        for event in latest.values():
            if event.get("task_id") != task_id or event.get("state") not in ACTIVE_PROCESS_STATES:
                continue
            pid = int(event.get("pid") or 0)
            ticks = event.get("pid_start_ticks")
            if (
                event.get("state") == "starting"
                and not pid
                and float(event.get("reservation_expires_at_epoch") or 0.0)
                > time.time()
            ):
                raise LaunchRejected(
                    f"duplicate_reserved_task:{event.get('request_id')}"
                )
            if (
                pid
                and _pid_identity_evidence(pid, ticks).verdict
                is not PidIdentityVerdict.MISMATCH
            ):
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
            card = self._preflight_card(task_id, runner, topic, adapter_id)
            external_readonly_dirs = _external_readonly_dirs(card, adapter_id)
            context_result = project_context.collect_project_context(self.repo, card)
            provider_env, model = self._resolve_provider_env(adapter_id, model)
            prompt_budget: dict[str, Any] = {}
            prompt = build_worker_prompt(
                task_id=task_id,
                runner=runner,
                topic=topic,
                card=card,
                owner_prompt=owner_prompt,
                project_context_bundle=(
                    context_result.prompt_bundle if context_result is not None else ""
                ),
                _budget_report=prompt_budget,
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
                    if (
                        event.get("task_id") == task_id
                        and event.get("state") in ACTIVE_PROCESS_STATES
                    ):
                        pid = int(event.get("pid") or 0)
                        # PID identity evidence, consistently with every other
                        # admission check in this module (see
                        # _assert_no_duplicate_task, _active_request_ids,
                        # cancel()). UNKNOWN remains conservatively active;
                        # only a proven mismatch clears the persisted request.
                        if (
                            pid
                            and _pid_identity_evidence(
                                pid, event.get("pid_start_ticks")
                            ).verdict
                            is not PidIdentityVerdict.MISMATCH
                        ):
                            raise LaunchRejected(f"duplicate_persisted_task:{event.get('request_id')}")

                request_id = uuid.uuid4().hex
                self.process_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                chmod_path(self.process_dir, 0o700)
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
                    "prompt_budget": prompt_budget,
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
                # path (see _launch_isolated's Popen call below).  NF430:
                # worker_launch_env wraps that same sanitized allowlist and
                # additionally routes TMPDIR/TMP/TEMP at the request-owned
                # ``.aiworkhub/temp/worker/<request_id>`` authority, so this
                # path no longer silently inherits the shared system temp.
                child_env = worker_launch_env(
                    adapter_id,
                    repo=self.repo,
                    request_id=request_id,
                    provider_env=provider_env,
                )
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
                    "prompt_budget": prompt_budget,
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
                "prompt_budget": prompt_budget,
                "shell": False,
            }
        except (LaunchRejected, project_context.ProjectContextError, OSError, ValueError) as exc:
            # NF430: the direct path owns no isolated worktree, so cleanup_workspace
            # never runs for it -- dispose any worker temp authority provisioned
            # for this request before returning the blocked result so a failed
            # direct launch leaves no orphaned ``.aiworkhub/temp/worker`` root.
            if request_id is not None:
                dispose_worker_temp(self.repo, request_id)
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
        diagnostic: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        event = self._append_event({
            "request_id": request_id or uuid.uuid4().hex,
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "adapter_id": adapter_id,
            "state": state,
            "blocked_reason": reason[:500],
            **({"diagnostic": diagnostic} if diagnostic else {}),
        })
        return {
            "ok": False,
            "launch_implemented": LAUNCH_IMPLEMENTED,
            "launch_enabled": launch_gates_open(),
            "request_id": event["request_id"],
            "task_id": task_id,
            "state": state,
            "blocked_reason": reason[:500],
            **({"diagnostic": diagnostic} if diagnostic else {}),
            "shell": False,
        }

    def _monitor(self, live: _LiveProcess) -> None:
        if live.isolated:
            try:
                live.process.wait()
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
            try:
                self._publish_bridge_cancellation_before_finalization(
                    live.request_id,
                    live,
                )
            except _BridgeCancellationDeferred:
                with self._lock:
                    self._live.pop(live.request_id, None)
                    self._cancelled.discard(live.request_id)
                return
            self._finalize_after_process_exit(live.request_id, live.process.poll())
            with self._lock:
                self._live.pop(live.request_id, None)
                self._cancelled.discard(live.request_id)
            return

        self._monitor_direct_for_tests(live)

    def _bridge_request_for_cancellation(
        self,
        request_id: str,
        live: _LiveProcess | None,
        status: dict[str, Any],
    ) -> vscode_lm_bridge.BridgeRequest | None:
        """Recover the exact bridge receipt before any lifecycle cancellation."""
        if live is not None and live.bridge_request is not None:
            return live.bridge_request
        events = self._request_events(request_id)
        latest = status.get("latest_event")
        if not isinstance(latest, dict) and events:
            latest = events[-1]
        adapter_id = str(
            (latest.get("adapter_id") if isinstance(latest, dict) else "")
            or status.get("adapter_id")
            or ""
        )
        if adapter_id not in _VSCODE_LM_IN_PROCESS_ADAPTERS:
            return None
        metadata_path = self._metadata_from_events(events)
        if metadata_path is None or not metadata_path.is_file():
            raise vscode_lm_bridge.BridgeError(
                "bridge_cancel_metadata_missing"
            )
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise vscode_lm_bridge.BridgeError(
                f"bridge_cancel_metadata_invalid:{type(exc).__name__}:{exc}"
            ) from exc
        return vscode_lm_bridge.bridge_request_from_metadata(
            metadata.get("vscode_lm_bridge"),
            expected_request_id=request_id,
        )

    def _publish_bridge_cancellation_before_finalization(
        self,
        request_id: str,
        live: _LiveProcess | None = None,
    ) -> str:
        """Publish the bridge terminal decision or defer finalization.

        This gate is deliberately independent of the in-memory live-process
        registry. A restarted manager must recover the exact token-bound
        bridge receipt from owner-only request metadata before it can release
        or finalize a dead supervisor's workspace.
        """
        events = self._request_events(request_id)
        if events:
            lineage = self._event_identity(events)
            latest = {**lineage, **events[-1]}
        elif live is not None:
            lineage = {
                "task_id": live.task_id,
                "runner": live.runner,
                "topic": live.topic,
                "adapter_id": live.adapter_id,
            }
            latest = {
                **lineage,
                "request_id": request_id,
                "state": "running",
                "pid": live.process.pid,
                "pid_start_ticks": live.pid_start_ticks,
                "metadata_path": str(live.metadata_path or ""),
                "supervisor_status_path": str(
                    live.supervisor_status_path or ""
                ),
            }
        else:
            return ""
        status = {
            "request_id": request_id,
            "state": latest.get("state"),
            "adapter_id": latest.get("adapter_id"),
            "latest_event": latest,
        }
        errors: list[str] = []
        for attempt, delay in enumerate((0.0, 0.05, 0.2), start=1):
            if delay:
                time.sleep(delay)
            try:
                request = self._bridge_request_for_cancellation(
                    request_id,
                    live,
                    status,
                )
                if request is None:
                    return ""
                return vscode_lm_bridge.cancel_request(request)
            except Exception as exc:  # noqa: BLE001 - fail closed below
                errors.append(
                    f"attempt={attempt}:{type(exc).__name__}:{exc}"[:500]
                )

        error = "bridge_cancel_publication_failed:" + "|".join(errors)
        self._append_event({
            **lineage,
            "request_id": request_id,
            "state": "reconcile_pending",
            "pid": latest.get("pid"),
            "pid_start_ticks": latest.get("pid_start_ticks"),
            "metadata_path": latest.get("metadata_path"),
            "supervisor_status_path": latest.get("supervisor_status_path"),
            "workspace_retained": True,
            "bridge_provider_may_be_active": True,
            "bridge_cancel_status": "failed",
            "reconciliation_deferred": "bridge_cancel_publication_failed",
            "error": error[:500],
        })
        raise _BridgeCancellationDeferred(error[:500])

    def _monitor_direct_for_tests(self, live: _LiveProcess) -> None:
        returncode: int | None
        try:
            returncode = live.process.wait()
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
            self._publish_bridge_cancellation_before_finalization(
                live.request_id,
                live,
            )
        except _BridgeCancellationDeferred:
            with self._lock:
                self._live.pop(live.request_id, None)
                self._cancelled.discard(live.request_id)
            return
        try:
            card = _parse_card(self._show_task(live.task_id), live.task_id)
            task_state = core._lifecycle_state(card)
        except Exception:
            task_state = "unknown"
        with self._lock:
            was_cancelled = live.request_id in self._cancelled
        state = "cancelled" if was_cancelled else "exited"
        if not was_cancelled and returncode == 0 and task_state == "review":
            state = "review_ready"
        elif not was_cancelled and returncode == 0 and task_state != "review":
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
        execution_mode: str = "",
    ) -> tuple[dict[str, Any], bool, str]:
        usage = _usage_from_output(stdout_path, include_samples=True)
        usage["requested_model"] = model
        usage["execution_mode"] = execution_mode or "provider_worker"
        usage["provider_launched"] = execution_mode != "validation_only_replay"
        usage_recorded = False
        usage_error = ""
        total_input = _ledger_input_tokens(usage, adapter_id)
        total_output = _ledger_output_tokens(usage)
        usage["recorded_input_tokens"] = total_input
        usage["recorded_output_tokens"] = total_output
        if execution_mode == "validation_only_replay":
            usage["telemetry_reason"] = "provider_not_invoked_deterministic_replay"
        elif usage.get("usage_observed"):
            usage["telemetry_reason"] = ""
        elif adapter_id in _VSCODE_LM_IN_PROCESS_ADAPTERS:
            # vscode.lm currently exposes the model response stream but no
            # provider-authoritative token/cost usage object. Keep this
            # distinct from a parser miss and never fabricate zero-cost work.
            usage["telemetry_reason"] = "provider_api_usage_unavailable"
        else:
            usage["telemetry_reason"] = "provider_usage_report_not_observed"
        usage_role = "worker"
        note = f"task_mcp_request:{request_id}"
        ledger_model = (
            "deterministic_validation_replay"
            if execution_mode == "validation_only_replay"
            else str(usage.get("observed_model") or model)
        )
        try:
            card = _parse_card(self._show_task(task_id), task_id)
            topic = topic or str(card.get("topic") or "")
            if _card_is_readonly_quality_review(card):
                usage_role = "reviewer"
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
        usage["role"] = usage_role
        args = [
            "usage", task_id,
            "--runner", runner,
            "--model", ledger_model,
            "--requested-model", model,
            "--observed-model", str(usage.get("observed_model") or ""),
            "--role", usage_role,
            "--provider", (
                "deterministic_validation_replay"
                if execution_mode == "validation_only_replay"
                else adapter_id.removesuffix("_cli")
            ),
            "--source", "task_mcp_launcher",
            "--note", note,
            "--input-tokens", str(total_input),
            "--output-tokens", str(total_output),
            "--visible-output-tokens", str(usage["output_tokens"]),
            "--reasoning-output-tokens", str(usage["reasoning_output_tokens"]),
            "--total-tokens", str(total_input + total_output),
            "--cached-input-tokens", str(usage["cached_input_tokens"]),
            "--cache-creation-input-tokens", str(usage["cache_creation_input_tokens"]),
            "--cache-write-input-tokens", str(usage["cache_write_input_tokens"]),
            "--telemetry-reason", str(usage["telemetry_reason"]),
            "--cost-usd", str(
                float(usage["cost_usd"] or 0.0)
                if usage.get("cost_observed")
                else 0.0
            ),
        ]
        if usage.get("usage_observed"):
            args.append("--usage-observed")
        if usage.get("model_observed"):
            args.append("--model-observed")
        if usage.get("cache_metrics_observed"):
            args.append("--cache-metrics-observed")
        if usage.get("cost_observed"):
            args.append("--cost-observed")
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

    def _persist_attempt_artifacts(
        self,
        request_id: str,
        metadata: dict[str, Any],
        workspace: WorkerWorkspace,
        *,
        target_state: str,
        changed_paths: list[str],
        changed_path_hashes: dict[str, Any] | None = None,
        required_outputs: list[dict[str, Any]] | None = None,
        validations: list[dict[str, Any]] | None = None,
        review: dict[str, Any] | None = None,
        quality_gate: dict[str, Any] | None = None,
        worker_mcp_gate: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        """Seal one bounded, replay-verifiable bundle before task transition.

        The bundle intentionally stores structured receipts rather than raw
        prompts, provider output, environment variables, or credentials.
        Request identity and exact candidate hashes are enough to bind later
        replay/review to this attempt without copying sensitive runtime data.
        """

        stdout_path = Path(str(metadata.get("stdout_path") or ""))
        usage = _usage_from_output(stdout_path, include_samples=True)
        usage.update({
            "requested_model": str(
                metadata.get("model") or metadata.get("adapter_id") or ""
            ),
            "execution_mode": str(
                metadata.get("execution_mode") or "provider_worker"
            ),
            "provider_launched": metadata.get("provider_launched") is not False,
        })
        request_identity = {
            "request_id": request_id,
            "task_id": str(metadata.get("task_id") or ""),
            "runner": str(metadata.get("runner") or ""),
            "topic": str(metadata.get("topic") or ""),
        }
        payloads: dict[str, Any] = {
            "metadata": {
                "schema_id": "aiworkhub.attempt_metadata.v1",
                "request_identity": request_identity,
                "adapter_id": str(metadata.get("adapter_id") or ""),
                "model": str(metadata.get("model") or ""),
                "execution_mode": str(
                    metadata.get("execution_mode") or "provider_worker"
                ),
                "sandbox_backend": str(metadata.get("sandbox_backend") or ""),
                "provider_stream_mode": str(
                    metadata.get("provider_stream_mode") or "terminal_events"
                ),
                "workspace": workspace.as_metadata(),
            },
            "diff": {
                "schema_id": "aiworkhub.attempt_diff_index.v1",
                "changed_paths": sorted(set(changed_paths)),
                "changed_path_hashes": changed_path_hashes or {},
                "required_outputs": required_outputs or [],
            },
            "validation": {
                "schema_id": "aiworkhub.attempt_validation.v1",
                "checks": validations or [],
                "quality_gate": quality_gate,
                "worker_mcp_gate": worker_mcp_gate,
            },
            "usage": {
                "schema_id": "aiworkhub.attempt_usage.v1",
                **usage,
            },
            "review": {
                "schema_id": "aiworkhub.attempt_review.v1",
                "target_state": target_state,
                "error": error[:500],
                **(review or {}),
            },
        }
        return attempt_artifacts.persist_json_bundle(
            self.process_dir / "attempt-artifacts" / request_id,
            attempt_id=request_id,
            payloads=payloads,
        )

    def _attempt_evidence_reference(
        self,
        request_id: str,
        receipt: dict[str, Any],
    ) -> str:
        manifest_path = Path(str(receipt.get("manifest_path") or ""))
        try:
            relative = manifest_path.resolve().relative_to(self.repo.resolve())
            return "file:" + relative.as_posix()
        except (OSError, ValueError):
            return f"file:attempt-artifacts/{request_id}/manifest.json"

    def _verify_attempt_artifact_receipt(
        self,
        request_id: str,
        raw: Any,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise WorkspaceError("attempt_artifact_manifest_missing")
        if (
            raw.get("schema_id")
            != "aiworkhub.attempt_artifact_bundle_receipt.v1"
            or str(raw.get("attempt_id") or "") != request_id
            or raw.get("verified") is not True
        ):
            raise WorkspaceError("attempt_artifact_manifest_receipt_invalid")
        expected = (
            self.process_dir
            / "attempt-artifacts"
            / request_id
            / attempt_artifacts.MANIFEST_FILENAME
        )
        observed = Path(str(raw.get("manifest_path") or ""))
        if observed != expected:
            raise WorkspaceError("attempt_artifact_manifest_path_mismatch")
        try:
            verification = attempt_artifacts.verify_json_bundle(expected.parent)
            manifest_sha256 = hashlib.sha256(expected.read_bytes()).hexdigest()
        except (
            OSError,
            attempt_artifacts.InvalidArtifactError,
            attempt_artifacts.InvalidManifestError,
        ) as exc:
            raise WorkspaceError(
                f"attempt_artifact_manifest_verification_failed:{exc}"
            ) from exc
        if (
            verification.get("verified") is not True
            or str(verification.get("attempt_id") or "") != request_id
            or manifest_sha256 != str(raw.get("manifest_sha256") or "")
        ):
            raise WorkspaceError("attempt_artifact_manifest_identity_mismatch")
        return dict(raw)

    def _canonical_outcome_evidence(
        self,
        request_id: str,
        receipt: dict[str, Any],
        *,
        level: evidence_levels.EvidenceLevel,
        message: str,
        verified_by: str | None = None,
    ) -> dict[str, Any]:
        record = evidence_levels.EvidenceRecord(
            evidence_level=level,
            severity="NONE",
            confidence="HIGH",
            reference=self._attempt_evidence_reference(request_id, receipt),
            verified_by=verified_by,
            message=message[:1000],
        )
        return record.to_dict()

    @staticmethod
    def _minimum_acceptance_evidence_level(
        card: dict[str, Any],
        *,
        readonly_quality_review: bool,
        readonly_research: bool,
    ) -> evidence_levels.EvidenceLevel:
        if readonly_research:
            return evidence_levels.EvidenceLevel.OBSERVATION
        if readonly_quality_review:
            return evidence_levels.EvidenceLevel.STATIC_EVIDENCE
        if list(card.get("validation") or []):
            return evidence_levels.EvidenceLevel.TESTED
        return evidence_levels.EvidenceLevel.STATIC_EVIDENCE

    _REQUEST_EVENT_CACHE_LIMIT = 64

    def _request_events(self, request_id: str) -> list[dict[str, Any]]:
        """Return exact request history without replaying an unchanged ledger.

        The worker-tool bridge asks for the same active request on every tool
        turn.  Cache only that request-scoped projection and trust it only while
        every authoritative ledger segment has the same filesystem identity and
        byte length.  New requests are scanned once even when the ledger itself
        is unchanged; an append, rotation, truncation, rewrite, or replacement
        invalidates all projections.
        """
        cache = getattr(self, "_request_event_cache", None)
        if cache is None or cache["path"] != self.process_log_path:
            cache = {
                "path": self.process_log_path,
                "lock": threading.Lock(),
                "fingerprint": None,
                "requests": {},
            }
            self._request_event_cache = cache
        with cache["lock"]:
            fingerprint = self._event_ledger_fingerprint()
            if fingerprint != cache["fingerprint"]:
                cache["fingerprint"] = fingerprint
                cache["requests"].clear()
            requests = cache["requests"]
            if request_id not in requests:
                requests[request_id] = [
                    event
                    for event in self._events()
                    if event.get("request_id") == request_id
                ]
            events = requests.pop(request_id)
            requests[request_id] = events
            while len(requests) > self._REQUEST_EVENT_CACHE_LIMIT:
                requests.pop(next(iter(requests)))
            return [dict(event) for event in events]

    def _event_ledger_fingerprint(self) -> tuple[Any, ...]:
        """Bounded metadata fingerprint for all durable ledger segments."""
        marks: list[tuple[Any, ...]] = []
        for segment in process_event_ledger.ledger_paths(self.process_log_path):
            try:
                info = segment.lstat()
            except OSError:
                return (("unreadable", str(segment)),)
            if not stat.S_ISREG(info.st_mode):
                return (("non_regular", str(segment)),)
            marks.append((
                segment.name,
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ))
        return tuple(marks)

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
        # The queue is authoritative over a still-exiting child.  A manager
        # may deliberately archive/supersede an in-flight task while its
        # supervisor is winding down; that late process result remains useful
        # evidence, but it must never resurrect the canonical card by trying
        # an archived -> review transition.  Detect the durable archive marker
        # immediately before the write, keeping task_store's strict illegal-
        # transition guard intact and leaving ordinary processing outcomes on
        # the normal review path below.
        try:
            card = _parse_card(self._show_task(task_id), task_id)
            archived_at = str(card.get("archived_at") or "").strip()
            lifecycle = core._lifecycle_state(card)
            if archived_at or lifecycle == "archived":
                operation = str(card.get("archive_operation") or "archived").strip().lower()
                disposition = "superseded" if operation == "superseded" else "archived"
                return {
                    "ok": True,
                    "idempotent_noop": True,
                    "canonical_lifecycle": "archived",
                    "terminal_review_disposition": (
                        f"terminal_skipped_already_finalized:{disposition}"
                    ),
                }
        except Exception:
            # A failed defensive read is not authority to suppress review;
            # preserve the existing fail-closed transition behavior.
            pass
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

    def _terminal_failure_exact(
        self,
        metadata: dict[str, Any],
        substatus: str,
        *,
        request_id: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Record a no-candidate failure without resurrecting disposed work."""
        task_id = str(metadata["task_id"])
        runner = str(metadata["runner"])
        try:
            card = _parse_card(self._show_task(task_id), task_id)
            archived_at = str(card.get("archived_at") or "").strip()
            lifecycle = core._lifecycle_state(card)
            if archived_at or lifecycle == "archived":
                operation = str(card.get("archive_operation") or "archived").strip().lower()
                disposition = "superseded" if operation == "superseded" else "archived"
                return {
                    "ok": True,
                    "idempotent_noop": True,
                    "canonical_lifecycle": "archived",
                    "terminal_review_disposition": (
                        f"terminal_skipped_already_finalized:{disposition}"
                    ),
                }
        except Exception:
            pass
        return task_engine.mark_terminal_failure(
            self.repo,
            task_id,
            runner,
            substatus,
            evidence=evidence,
            request_id=request_id,
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
        unknown_streak = 0
        try:
            while True:
                verdict = _pid_identity_evidence(pid, start_ticks).verdict
                if verdict is PidIdentityVerdict.MISMATCH:
                    self._finalize_after_process_exit(request_id)
                    return
                if verdict is PidIdentityVerdict.MATCH:
                    unknown_streak = 0
                else:
                    unknown_streak += 1
                    if unknown_streak >= _PERSISTED_WATCH_UNKNOWN_MAX_CONSECUTIVE:
                        return
                time.sleep(0.2)
        finally:
            with self._lock:
                self._watching.discard(request_id)

    def _finalize_after_process_exit(
        self,
        request_id: str,
        supervisor_returncode: int | None = None,
        *,
        lock_blocking: bool = True,
    ) -> dict[str, Any] | None:
        """Reconcile a dead supervisor without silently stranding its task.

        Finalization crosses filesystem, process-ledger, task-store and
        callback boundaries.  A transient Windows file/SQLite race must not
        kill the daemon monitor thread and leave the canonical card forever
        in ``processing``.  Retry the exact idempotent reconciliation a small
        bounded number of times. Request-lock contention is different: it
        proves another finalizer currently owns the only writer boundary, so
        defer without changing durable state. If the implementation itself
        keeps failing, convert the still-processing card into a truthful
        ``finalize_failed`` terminal outcome and enqueue its manager callback.
        The isolated workspace remains retained for diagnosis.
        """
        finalization_started = time.monotonic()

        errors: list[str] = []
        for attempt, delay in enumerate((0.0, 0.05, 0.2), start=1):
            if delay:
                time.sleep(delay)
            try:
                event = self._finalize_isolated_request(
                    request_id,
                    supervisor_returncode,
                    lock_blocking=lock_blocking,
                )
                if event is not None:
                    return event
                deferred = self._request_events(request_id)
                if deferred:
                    latest = deferred[-1]
                    identity = _pid_identity_evidence(
                        latest.get("pid"), latest.get("pid_start_ticks")
                    )
                    if identity.verdict is PidIdentityVerdict.MATCH:
                        return latest
                    if identity.verdict is PidIdentityVerdict.UNKNOWN:
                        return {
                            **latest,
                            "reconciliation_deferred": "pid_identity_unknown",
                            "workspace_retained": True,
                        }
                errors.append(f"attempt={attempt}:no_terminal_event")
            except _BridgeCancellationDeferred:
                deferred = self._request_events(request_id)
                return deferred[-1] if deferred else None
            except _PidIdentityUnknownDeferred:
                deferred = self._request_events(request_id)
                if not deferred:
                    return None
                return {
                    **deferred[-1],
                    "reconciliation_deferred": "pid_identity_unknown",
                    "workspace_retained": True,
                }
            except AdvisoryLockTimeout:
                deferred = self._request_events(request_id)
                if not deferred:
                    return None
                return {
                    **deferred[-1],
                    "reconciliation_deferred": "request_lock_busy",
                    "workspace_retained": True,
                }
            except OSError as exc:
                if not lock_blocking:
                    raise
                errors.append(
                    f"attempt={attempt}:{type(exc).__name__}:{exc}"[:500]
                )
            except Exception as exc:  # noqa: BLE001 - monitor must remain durable
                errors.append(f"attempt={attempt}:{type(exc).__name__}:{exc}"[:500])

        events = self._request_events(request_id)
        event_identity = self._event_identity(events)
        task_id = str(event_identity.get("task_id") or "")
        runner = str(event_identity.get("runner") or "")
        error = "finalizer_retries_exhausted:" + "|".join(errors)
        release_result: dict[str, Any] = {
            "ok": False,
            "stderr": "request_identity_missing",
            "callback_enqueued": False,
        }
        if task_id and runner:
            release_result = task_engine.mark_terminal_failure(
                self.repo,
                task_id,
                runner,
                "finalize_failed",
                evidence={
                    "request_id": request_id,
                    "error": error[:500],
                    "supervisor_returncode": supervisor_returncode,
                    "finalize_attempts": len(errors),
                },
                request_id=request_id,
            )
        transition_reason = str(
            release_result.get("stderr") or release_result.get("stdout") or ""
        )
        # "retries exhausted" must mean the attempt is over, not that the next
        # reconcile round begins. When the terminal transition fails because the
        # target card is no longer processing -- archived, deleted, or reclaimed
        # -- no future reconcile can move it, so terminalize as
        # ``finalize_abandoned`` (a terminal, non-re-arming state) with a named
        # cause an operator can count. Only a card that may still be processing
        # keeps ``reconcile_pending`` so its legitimate retry survives.
        abandon_cause = (
            None
            if release_result.get("ok")
            else _finalizer_card_not_processing(transition_reason)
        )
        if release_result.get("ok"):
            terminal_state = "finalize_failed"
            error_detail = error
            unlink_if_regular(self._terminal_authority_grant_path(request_id))
        elif abandon_cause is not None:
            terminal_state = "finalize_abandoned"
            error_detail = (
                error + ":finalize_abandoned:" + abandon_cause + ":" + transition_reason
            )
        else:
            terminal_state = "reconcile_pending"
            error_detail = error + ":terminal_transition_failed:" + transition_reason
        return self._append_event({
            **event_identity,
            "request_id": request_id,
            "state": terminal_state,
            "worker_terminal_state": (
                "finalize_abandoned"
                if terminal_state == "finalize_abandoned"
                else "finalize_failed"
            ),
            "exit_code": supervisor_returncode,
            "finished_at": _utcnow(),
            "workspace_retained": True,
            "finalizer_abandoned": terminal_state == "finalize_abandoned",
            "release_transition_ok": bool(release_result.get("ok")),
            "callback_enqueued": bool(release_result.get("callback_enqueued")),
            "finalization_duration_ms": round(
                (time.monotonic() - finalization_started) * 1000.0, 3
            ),
            "error": error_detail[:500],
        })

    def _reconcile_persisted_requests(self) -> dict[str, int]:
        watched = 0
        finalized = 0
        candidates = {
            request_id: event
            for request_id, event in self._latest_by_request().items()
            if event.get("state") in FINALIZATION_PENDING_STATES
            or (
                event.get("state") in ACTIVE_PROCESS_STATES
                and event.get("metadata_path")
            )
        }
        for request_id, event in candidates.items():
            state = event.get("state")
            if state in FINALIZATION_PENDING_STATES:
                finalized_event = self._finalize_after_process_exit(request_id)
                if (
                    finalized_event is not None
                    and finalized_event.get("state") in TERMINAL_PROCESS_STATES
                ):
                    finalized += 1
                continue

            pid = int(event.get("pid") or 0)
            ticks = event.get("pid_start_ticks")
            verdict = _pid_identity_evidence(pid, ticks).verdict
            if verdict is PidIdentityVerdict.UNKNOWN:
                continue
            if verdict is PidIdentityVerdict.MATCH:
                # The passive watcher only finalizes once the PID identity stops
                # matching, so a heartbeat-lost or stalled but still-alive worker
                # would stay "processing" forever. Give the durable escalation
                # path a chance to run while the process still exists: if it
                # returns a terminal state (stall/liveness-lost detected and
                # finalized) count it finalized; otherwise fall through to the
                # watcher unchanged. A healthy worker returns non-terminal here
                # and produces no durable side effect.
                with self._lock:
                    already_tracked = (
                        request_id in self._watching or request_id in self._live
                    )
                if (
                    not already_tracked
                    and str(event.get("supervisor_status_path") or "") not in {"", "."}
                ):
                    try:
                        escalated = self._finalize_after_process_exit(
                            request_id, lock_blocking=False
                        )
                    except Exception:  # noqa: BLE001 - never break the scan
                        escalated = None
                    if (
                        escalated is not None
                        and escalated.get("state") in TERMINAL_PROCESS_STATES
                    ):
                        finalized += 1
                        continue
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
                finalized_event = self._finalize_after_process_exit(request_id)
                if (
                    finalized_event is not None
                    and finalized_event.get("state") in TERMINAL_PROCESS_STATES
                ):
                    finalized += 1
        return {"watched": watched, "finalized": finalized}

    def reconcile(self) -> dict[str, Any]:
        result = self._reconcile_persisted_requests()
        gc_result = self._gc_finalized_workspaces()
        return {"ok": True, **result, **gc_result}

    def abandoned_finalizations(self) -> list[dict[str, Any]]:
        """Operator view of finalizers that terminally gave up.

        Each entry is one request whose retained finalizer reached the terminal
        ``finalize_abandoned`` state because its target card was archived or
        otherwise no longer processing. Counting these lets an operator see how
        many finalizers stopped, and why, instead of watching a
        re-finalization timestamp climb on every poll.
        """
        abandoned: list[dict[str, Any]] = []
        for request_id, event in self._latest_by_request().items():
            if event.get("state") != "finalize_abandoned":
                continue
            abandoned.append({
                "request_id": request_id,
                "task_id": str(event.get("task_id") or ""),
                "reason": str(event.get("error") or ""),
                "finished_at": event.get("finished_at"),
            })
        return abandoned

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
    def _gc_disposition(
        card: dict[str, Any],
        request_id: str,
        *,
        repo: Path | None = None,
    ) -> tuple[bool, str]:
        """Return whether ``request_id`` is no longer a live review surface.

        This deliberately fails closed for processing cards and malformed
        review authority.  A review card may have many historical retained
        worker attempts, but its canonical ``terminal_review`` names exactly
        one current request; only older, different request ids are collected.
        """
        canonical_status = _canonical_task_status(card)
        predecessor = card.get("rework_predecessor")
        pinned_request_id = (
            str(predecessor.get("request_id") or "").strip()
            if isinstance(predecessor, dict)
            else ""
        )
        # A failed successor moves the task from pending/processing to
        # blocked, but the predecessor remains the only hash-pinned reviewed
        # candidate that a manager can recover or retry.  Its retention is an
        # identity invariant, not a pending-status convenience.
        if pinned_request_id == request_id:
            if repo is not None and _worker_workspace.has_verified_rework_delta(
                predecessor, authority_repo=repo
            ):
                return True, "sealed_rework_delta"
            return False, "pinned_rework_predecessor"
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

    def _retained_review_workspace_integrity(
        self,
        card: dict[str, Any],
        request_id: str,
        metadata_workspace: WorkerWorkspace,
    ) -> tuple[bool, str]:
        """Verify that one current review still has actionable exact bytes."""

        terminal = card.get("terminal_review")
        evidence = terminal.get("evidence") if isinstance(terminal, dict) else None
        identity = evidence.get("request_identity") if isinstance(evidence, dict) else None
        workspace_payload = evidence.get("workspace") if isinstance(evidence, dict) else None
        if not isinstance(identity, dict) or str(identity.get("request_id") or "") != request_id:
            return False, "review_request_identity_missing"
        if not isinstance(workspace_payload, dict):
            return False, "review_workspace_evidence_missing"
        try:
            review_workspace = WorkerWorkspace.from_metadata(workspace_payload)
        except (KeyError, TypeError, ValueError) as exc:
            return False, f"review_workspace_evidence_invalid:{exc}"[:200]
        if (
            review_workspace.repo != self.repo
            or review_workspace.request_id != request_id
            or review_workspace.path != metadata_workspace.path
            or review_workspace.home != metadata_workspace.home
        ):
            return False, "review_workspace_identity_mismatch"
        if (
            review_workspace.path.is_symlink()
            or review_workspace.home.is_symlink()
            or not review_workspace.path.is_dir()
            or not review_workspace.home.is_dir()
        ):
            return False, "review_workspace_missing"

        if not isinstance(evidence, dict):
            return False, "review_workspace_evidence_missing"
        stored_hashes = evidence.get("changed_path_hashes")
        changed_paths = evidence.get("changed_paths")
        # A read-only validation failure has no candidate bytes to seal.  The
        # finalizer's mechanically-derived empty ``changed_paths`` list is the
        # complete evidence for that case, so requiring a hash map that cannot
        # contain any entries only lets retention GC replace the real gate
        # failure with ``review_workspace_hashes_missing``.  Keep writable and
        # review-ready candidates on the existing fail-closed hash path.
        if (
            stored_hashes is None
            and changed_paths == []
            and not metadata_workspace.allowed_writes
            and not review_workspace.allowed_writes
            and str(terminal.get("substatus") or "") == "validation_failed"
        ):
            stored_hashes = {}
        if not isinstance(stored_hashes, dict):
            return False, "review_workspace_hashes_missing"
        if not stored_hashes and isinstance(changed_paths, list) and changed_paths:
            return False, "review_workspace_hashes_missing"
        try:
            observed_hashes = _changed_path_hashes(
                review_workspace, [str(path) for path in stored_hashes]
            )
        except OSError as exc:
            return False, f"review_workspace_unreadable:{type(exc).__name__}"
        if observed_hashes != stored_hashes:
            return False, "review_workspace_hash_mismatch"
        return True, "review_workspace_verified"

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
        with self._request_lock(request_id):
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
            eligible, disposition = self._gc_disposition(
                card, request_id, repo=self.repo
            )
            if not eligible:
                if disposition != "current_review_request":
                    return {
                        "request_id": request_id,
                        "gc": False,
                        "reason": disposition,
                    }
                pid = int(latest.get("pid") or 0)
                ticks = latest.get("pid_start_ticks")
                if not _process_proven_dead(pid, ticks):
                    return {
                        "request_id": request_id,
                        "gc": False,
                        "reason": "process_not_proven_dead",
                    }
                try:
                    workspace = WorkerWorkspace.from_metadata(dict(workspace_meta))
                    assert_gc_safe_workspace_shape(
                        meta_request_id, path, home, repo=repo
                    )
                except (KeyError, TypeError, ValueError, WorkspaceError) as exc:
                    return {
                        "request_id": request_id,
                        "gc": False,
                        "reason": f"unsafe_workspace_shape:{exc}"[:200],
                    }
                intact, integrity_reason = self._retained_review_workspace_integrity(
                    card, request_id, workspace
                )
                if intact:
                    return {
                        "request_id": request_id,
                        "gc": False,
                        "reason": disposition,
                    }
                transition = task_engine.mark_review_workspace_missing(
                    self.repo,
                    task_id,
                    runner,
                    request_id,
                    reason=integrity_reason,
                )
                if not transition.get("ok"):
                    return {
                        "request_id": request_id,
                        "gc": False,
                        "reason": (
                            "review_workspace_reconcile_failed:"
                            + str(transition.get("stderr") or "unknown")
                        )[:200],
                    }
                # The "still in review" guard protects verified bytes that
                # EXIST; a workspace that is already gone has nothing to
                # preserve and must not stay unreclaimable forever.  The card is
                # now blocked, so reclaim the retained record through the
                # ordinary idempotent cleanup path.  A cleanup failure stays
                # truthfully retained for a later retry instead of reporting a
                # phantom success, and the removal is recorded in the audit.
                if integrity_reason == "review_workspace_missing":
                    try:
                        cleanup_workspace(repo, path, home)
                    except WorkspaceError as exc:
                        self._append_event({
                            "request_id": request_id,
                            "task_id": task_id,
                            "runner": runner,
                            "topic": latest.get("topic"),
                            "adapter_id": latest.get("adapter_id"),
                            "state": "finalize_failed",
                            "error": f"retained_workspace_cleanup_failed:{exc}"[:500],
                            "workspace_retained": True,
                            "workspace_gc": False,
                            "workspace_gc_at": _utcnow(),
                            "workspace_gc_reason": integrity_reason,
                            "review_transition_ok": True,
                            "callback_enqueued": bool(
                                transition.get("callback_enqueued")
                            ),
                        })
                        return {
                            "request_id": request_id,
                            "gc": False,
                            "reason": f"retained_workspace_cleanup_failed:{exc}"[:200],
                        }
                    record_review_workspace_retention_audit(
                        self.process_log_path,
                        request_id=request_id,
                        task_id=task_id,
                        card_status=_canonical_task_status(card),
                        reason=integrity_reason,
                        action="purge",
                    )
                    self._append_event({
                        "request_id": request_id,
                        "task_id": task_id,
                        "runner": runner,
                        "topic": latest.get("topic"),
                        "adapter_id": latest.get("adapter_id"),
                        "state": "finalize_failed",
                        "error": f"retained_workspace_missing_reclaimed:{integrity_reason}"[:500],
                        "workspace_retained": False,
                        "workspace_gc": True,
                        "workspace_gc_at": _utcnow(),
                        "workspace_gc_reason": integrity_reason,
                        "review_transition_ok": True,
                        "callback_enqueued": bool(
                            transition.get("callback_enqueued")
                        ),
                    })
                    return {
                        "request_id": request_id,
                        "gc": True,
                        "reason": integrity_reason,
                    }
                # A failed integrity check on bytes that still EXIST is not
                # authority to destroy verified work.  Quarantine the workspace
                # so a manager can diff it against the sealed hashes and decide;
                # the card is already blocked with this reason.  Every removal
                # from the live tree is recorded in the retention audit.
                try:
                    quarantine_dir = quarantine_review_workspace(
                        self.process_log_path,
                        request_id=request_id,
                        path=path,
                        home=home,
                    )
                except (OSError, WorkspaceError) as exc:
                    self._append_event({
                        "request_id": request_id,
                        "task_id": task_id,
                        "runner": runner,
                        "topic": latest.get("topic"),
                        "adapter_id": latest.get("adapter_id"),
                        "state": "finalize_failed",
                        "error": f"review_workspace_quarantine_failed:{exc}"[:500],
                        "workspace_retained": True,
                        "workspace_gc": False,
                        "workspace_gc_at": _utcnow(),
                        "workspace_gc_reason": integrity_reason,
                        "review_transition_ok": True,
                        "callback_enqueued": bool(
                            transition.get("callback_enqueued")
                        ),
                    })
                    return {
                        "request_id": request_id,
                        "gc": False,
                        "reason": f"quarantine_failed:{exc}"[:200],
                    }
                record_review_workspace_retention_audit(
                    self.process_log_path,
                    request_id=request_id,
                    task_id=task_id,
                    card_status=_canonical_task_status(card),
                    reason=integrity_reason,
                    action="quarantine",
                    moved_to=str(quarantine_dir),
                )
                self._append_event({
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": latest.get("topic"),
                    "adapter_id": latest.get("adapter_id"),
                    "state": "finalize_failed",
                    "error": f"retained_workspace_quarantined:{integrity_reason}"[:500],
                    "workspace_retained": False,
                    "workspace_gc": False,
                    "workspace_quarantined": True,
                    "workspace_quarantine_path": str(quarantine_dir),
                    "workspace_gc_at": _utcnow(),
                    "workspace_gc_reason": integrity_reason,
                    "review_transition_ok": True,
                    "callback_enqueued": bool(transition.get("callback_enqueued")),
                })
                return {
                    "request_id": request_id,
                    "gc": False,
                    "quarantined": True,
                    "reason": integrity_reason,
                }

            pid = int(latest.get("pid") or 0)
            ticks = latest.get("pid_start_ticks")
            if not _process_proven_dead(pid, ticks):
                return {"request_id": request_id, "gc": False, "reason": "process_not_proven_dead"}
            try:
                assert_gc_safe_workspace_shape(meta_request_id, path, home, repo=repo)
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

            record_review_workspace_retention_audit(
                self.process_log_path,
                request_id=request_id,
                task_id=task_id,
                card_status=_canonical_task_status(card),
                reason=disposition,
                action="purge",
            )
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
        *,
        lock_blocking: bool = True,
    ) -> dict[str, Any] | None:
        finalization_started = time.monotonic()
        with self._request_lock(request_id, blocking=lock_blocking):
            events = self._request_events(request_id)
            if not events:
                return None
            latest = events[-1]
            finalization_retry = bool(latest.get("finalization_retry"))
            if latest.get("state") in TERMINAL_PROCESS_STATES:
                return latest
            with self._lock:
                live = self._live.get(request_id)
            identity = _pid_identity_evidence(
                latest.get("pid"), latest.get("pid_start_ticks")
            )
            status_hint_path = Path(
                str(latest.get("supervisor_status_path") or "")
            )
            status_hint = (
                self._read_supervisor_status(status_hint_path)
                if str(status_hint_path) not in {"", "."}
                else {}
            )
            status_hint_state = str(status_hint.get("state") or "")
            terminal_status_hint = bool(
                status_hint and status_hint_state not in {"starting", "running"}
            )
            if not terminal_status_hint:
                if identity.verdict is PidIdentityVerdict.UNKNOWN:
                    raise _PidIdentityUnknownDeferred("pid_identity_unknown")
                if identity.verdict is PidIdentityVerdict.MATCH and not status_hint:
                    return None
            metadata_path = self._metadata_from_events(events)
            if metadata_path is None or not metadata_path.is_file():
                return None
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                workspace = WorkerWorkspace.from_metadata(metadata["workspace"])
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                if (
                    not terminal_status_hint
                    and identity.verdict is PidIdentityVerdict.MATCH
                ):
                    return None
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
            supervisor_state = str(supervisor_status.get("state") or "")
            terminal_status_artifact = bool(
                supervisor_status and supervisor_state not in {"starting", "running"}
            )
            if not terminal_status_artifact:
                if identity.verdict is PidIdentityVerdict.UNKNOWN:
                    raise _PidIdentityUnknownDeferred("pid_identity_unknown")
                if (
                    identity.verdict is PidIdentityVerdict.MATCH
                    and not supervisor_status
                ):
                    return None
            supervisor_alive = identity.verdict is PidIdentityVerdict.MATCH
            # The supervisor process is spawned before its first atomic status
            # write.  A concurrent reconciler can therefore observe the exact
            # live PID while the status file is still absent for a few
            # milliseconds.  That launch window is active work, not evidence
            # of failure; the PID watcher will call us again after exit.
            liveness_lost = False
            stall_detected = False
            stall_idle_seconds: float | None = None
            stall_error = ""
            terminate_supervisor = False
            if supervisor_status.get("state") in {"starting", "running"} and supervisor_alive:
                liveness = derive_liveness_state(
                    now_epoch=time.time(),
                    supervisor_alive=supervisor_alive,
                    heartbeat_at_epoch=supervisor_status.get("heartbeat_at_epoch"),
                    last_output_change_epoch=supervisor_status.get("last_output_change_epoch"),
                )
                meaningful_at = supervisor_status.get("last_meaningful_progress_epoch")
                if (
                    supervisor_status.get("state") == "running"
                    and liveness["liveness_state"] in {"alive", "quiet"}
                    and isinstance(meaningful_at, (int, float))
                ):
                    stall_idle_seconds = max(0.0, time.time() - float(meaningful_at))
                    # Meaningful-output age is observability only. Providers
                    # can legitimately poll or work silently for an unbounded
                    # interval, so elapsed/quiet time is never terminal
                    # evidence while the exact supervisor identity and its
                    # heartbeat remain live (NF-2026-00176).
                if (
                    not stall_detected
                    and liveness["liveness_state"] in {"alive", "quiet", "unresponsive"}
                ):
                    return None
                # liveness_state == "lost": the heartbeat lease AND bounded
                # recovery grace both elapsed while the exact supervisor PID
                # still exists (a hung/deadlocked supervisor, not merely a
                # slow one). Recheck identity ONE more time immediately
                # before any termination action -- still under this whole
                # call's registry lock -- then terminate ONLY the exact
                # matching supervisor/child process group(s). Never act on a
                # bare "process exists" signal alone.
                if not stall_detected:
                    liveness_lost = True
                    recheck = _pid_identity_evidence(supervisor_pid, supervisor_ticks)
                    if recheck.verdict is PidIdentityVerdict.UNKNOWN:
                        raise _PidIdentityUnknownDeferred("pid_identity_unknown")
                    terminate_supervisor = recheck.verdict is PidIdentityVerdict.MATCH
                    supervisor_alive = False

            if _requires_bridge_cancellation(metadata):
                self._publish_bridge_cancellation_before_finalization(
                    request_id,
                    live,
                )
            if (
                lock_blocking
                and latest.get("state") not in {"finalizing", "cancel_requested"}
            ):
                self._append_event({
                    **self._event_identity(events),
                    "request_id": request_id,
                    "state": "finalizing",
                    "provider_process_alive": False,
                    "finalization_started_at": _utcnow(),
                    "worker_terminal_state": latest.get("worker_terminal_state"),
                })
            if terminate_supervisor:
                _terminate_process_group(supervisor_pid, grace_seconds=5.0)

            # An abruptly lost (or heartbeat-lease-lost) supervisor may leave
            # its child running. Kill only when both PID and proc start time
            # still match the durable status record, preventing PID-reuse
            # termination.
            verified_child_pid = _identity_verified_pid(
                supervisor_status.get("child_pid"),
                supervisor_status.get("child_pid_start_ticks"),
            )
            if (
                not supervisor_alive
                and supervisor_status.get("state") in {"starting", "running"}
                and verified_child_pid
            ):
                _terminate_process_group(verified_child_pid, grace_seconds=5.0)

            exit_code = supervisor_status.get("exit_code")
            error = stall_error or str(supervisor_status.get("error") or "")[:500]
            if liveness_lost and not error:
                error = f"liveness_lost:heartbeat_lease_and_recovery_grace_exceeded:rc={supervisor_returncode}"
            if supervisor_state == "timed_out" and metadata.get("timeout_enforced") is True:
                terminal_state = "timed_out"
                error = error or (
                    "worker_timed_out:timeout_seconds="
                    + str(metadata.get("timeout_seconds") or "unknown")
                    + f":exit_code={exit_code}"
                )
            # NF: The supervisor is uncapped and no longer authorizes a token
            # budget, so it never emits a fresh ``token_budget_exceeded`` state.
            # A legacy supervisor packet that still carries that state must NOT
            # synthesize a new token-cap terminal outcome; with the branch gone
            # it falls through to the infrastructure-failure classification
            # below (``worker_failed`` / ``supervisor_incomplete``). Historical
            # ``token_budget_exceeded`` rows stay readable and diagnosable -- no
            # new token-cap transition is minted here.
            elif supervisor_state == "output_budget_exceeded":
                terminal_state = "output_budget_exceeded"
                budget = supervisor_status.get("output_budget") or {}
                error = error or (
                    "output_budget_exceeded:cap_bytes="
                    + str(budget.get("cap_bytes") or "unknown")
                    + ":observed_bytes="
                    + str(budget.get("observed_bytes") or "unknown")
                )
            elif supervisor_state == "cancelled":
                terminal_state = "cancelled"
                error = error or "worker_cancelled"
            elif latest.get("state") == "cancel_requested":
                # A durable manager cancellation intent outranks the exit
                # shape produced while terminating the exact supervisor or
                # child process.  In particular, SIGTERM/SIGKILL commonly
                # surfaces as ``exited`` with a non-zero code after a manager
                # restart; classifying that as worker_failed loses the user's
                # explicit terminal decision.
                terminal_state = "cancelled"
                error = error or "worker_cancelled"
            elif supervisor_state == "exited" and exit_code == 0:
                terminal_state = "exited"
            elif supervisor_state in {"exited", "spawn_failed", "supervisor_error", "timed_out"}:
                terminal_state = "worker_failed"
                error = error or (
                    f"worker_failed:supervisor_state={supervisor_state}:exit_code={exit_code}"
                )
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
            provider_launch_failure = None
            if terminal_state == "worker_failed":
                provider_output_path = Path(str(metadata["stdout_path"]))
                provider_launch_failure = _provider_auth_failure_from_output(
                    provider_output_path
                )
                if provider_launch_failure is not None:
                    http_status = int(provider_launch_failure["http_status"])
                    # The auth-readiness circuit is a claim about the credential,
                    # so it only trips on an authentication-shaped status (401/
                    # 403). A rate/quota refusal is not evidence of a bad key and
                    # must not re-authenticate the route (NF-2026-00326).
                    if (
                        str(metadata.get("adapter_id") or "") == "claude_cli"
                        and http_status in (401, 403)
                    ):
                        claude_auth.record_runtime_auth_failure(
                            http_status=http_status
                        )
                    terminal_state = "launch_failed"
                    # The recorded blocker reason is the classifier's verdict,
                    # derived from the provider's OWN response body here where it
                    # is still in hand -- not a status-only guess and not the
                    # downstream exit_code=1 (NF-2026-00275, NF-2026-00326).
                    error = str(provider_launch_failure["reason"])

            changed: list[str] = []
            promoted: list[str] = []
            validations: list[dict[str, Any]] = []
            required_output_records: list[dict[str, Any]] = []
            review_result: dict[str, Any] | None = None
            release_result: dict[str, Any] | None = None
            worker_mcp_gate: dict[str, Any] | None = None
            quality_gate: dict[str, Any] | None = None
            research_result: dict[str, Any] | None = None
            residual_contract_result: list[dict[str, Any]] = []
            attempt_artifact_receipt: dict[str, Any] | None = None
            attempt_artifact_error = ""
            outcome_evidence_record: dict[str, Any] | None = None
            cleanup = True
            scope_duration_ms = 0.0
            validation_wall_duration_ms = 0.0

            def _enforce_finalization_scope() -> list[str]:
                nonlocal scope_duration_ms
                phase_started = time.monotonic()
                try:
                    return enforce_scope(
                        workspace,
                        git_phase="worker_finalization",
                        git_timeout=_worker_workspace.finalization_git_timeout_seconds(),
                    )
                finally:
                    scope_duration_ms += (
                        time.monotonic() - phase_started
                    ) * 1000.0

            def _run_finalization_validations() -> list[dict[str, Any]]:
                nonlocal validation_wall_duration_ms
                phase_started = time.monotonic()
                try:
                    return _run_declared_validations(
                        workspace, metadata, metadata
                    )
                finally:
                    validation_wall_duration_ms += (
                        time.monotonic() - phase_started
                    ) * 1000.0

            try:
                if terminal_state != "exited":
                    # A worker that timed out, crashed, or was cancelled did
                    # not produce actionable review work. Preserve its exact
                    # evidence/worktree, but close it in the blocked terminal
                    # bucket so the review queue remains truthful.
                    cleanup = terminal_state == "launch_failed"
                    # A non-exited terminal outcome never promotes/writes and
                    # so never needs the one-task authority grant -- remove
                    # it now so it cannot linger as a stale, unconsumed
                    # artifact for a request that will never reach the
                    # success branch below.
                    unlink_if_regular(self._terminal_authority_grant_path(request_id))
                    if terminal_state == "launch_failed":
                        release_result = task_engine.mark_launch_failed(
                            self.repo,
                            str(metadata["task_id"]),
                            str(metadata["runner"]),
                            reason=error,
                            request_id=request_id,
                        )
                    else:
                        failure_evidence = {
                            "request_id": request_id,
                            "adapter_id": metadata.get("adapter_id"),
                            "model": metadata.get("model"),
                            "error": error[:500],
                            "supervisor_state": supervisor_state,
                            "exit_code": exit_code,
                            "liveness_lost": liveness_lost,
                            "stall_detected": stall_detected,
                            "stall_idle_seconds": stall_idle_seconds,
                            "stall_last_meaningful_phase": supervisor_status.get(
                                "last_meaningful_phase"
                            ),
                            "stall_last_meaningful_progress_epoch": supervisor_status.get(
                                "last_meaningful_progress_epoch"
                            ),
                            "stall_last_progress_sequence": (
                                _meaningful_progress_sequence(supervisor_status)
                            ),
                            "stall_heartbeat_seq": supervisor_status.get("heartbeat_seq"),
                            "stall_stdout_bytes": supervisor_status.get("stdout_bytes"),
                            "stall_stderr_bytes": supervisor_status.get("stderr_bytes"),
                            "stall_supervisor_pid": supervisor_pid,
                            "stall_supervisor_pid_start_ticks": supervisor_ticks,
                            "token_budget": supervisor_status.get("token_budget"),
                            "output_budget": supervisor_status.get("output_budget"),
                            **_declared_failure_denominators(metadata),
                        }
                        # A timed-out worker may still have produced a partial
                        # delta.  Pin it as a rework predecessor so the
                        # successor starts from the work instead of nothing.
                        # This is best-effort evidence enrichment only: it must
                        # never override the true terminal outcome.  A claim
                        # that already moved on (an archived/superseded card, a
                        # lost claim) or a workspace that is gone yields no
                        # predecessor, never a finalize_failed that relabels a
                        # genuine timed_out as a finalization problem.
                        if terminal_state in DELTA_RETAINING_TERMINAL_STATES:
                            timeout_changed: list[str] = []
                            retained: dict[str, Any] = {}
                            try:
                                timeout_changed = _enforce_finalization_scope()
                                retained = retained_rework_candidate_evidence(
                                    terminal_state,
                                    workspace,
                                    metadata,
                                    request_id,
                                    timeout_changed,
                                    self._exact_claim_state(metadata),
                                )
                            except Exception:
                                timeout_changed = []
                                retained = {}
                            if retained:
                                failure_evidence.update(retained)
                                failure_evidence["changed_paths"] = timeout_changed
                        release_result = self._terminal_failure_exact(
                            metadata,
                            terminal_state,
                            evidence=failure_evidence,
                            request_id=request_id,
                        )
                    if not release_result.get("ok"):
                        terminal_state = "release_pending"
                        error = error or (
                            "terminal_failure_transition_failed:"
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
                        if isinstance(metadata.get("quality_review"), dict):
                            changed = _enforce_finalization_scope()
                            if changed:
                                raise WorkspaceError(
                                    "quality_review_workspace_mutated:"
                                    + ",".join(changed[:20])
                                )
                            verified_receipt = _verified_quality_review_receipt(
                                metadata, workspace, request_id
                            )
                            attempt_artifact_receipt = self._persist_attempt_artifacts(
                                request_id,
                                metadata,
                                workspace,
                                target_state="review_ready",
                                changed_paths=[],
                                review={
                                    "kind": "quality_review",
                                    "quality_review_receipt": verified_receipt,
                                    "quality_review": metadata["quality_review"],
                                },
                            )
                            outcome_evidence_record = self._canonical_outcome_evidence(
                                request_id,
                                attempt_artifact_receipt,
                                level=evidence_levels.EvidenceLevel.STATIC_EVIDENCE,
                                message="Quality reviewer produced a sealed read-only report.",
                            )
                            cleanup = False
                            terminal_state = "review_ready"
                            review_result = {"ok": True, "idempotent_noop": True}
                            release_result = self._review_terminal_exact(
                                metadata,
                                "review_ready",
                                request_id=request_id,
                                evidence={
                                    "quality_review_receipt": verified_receipt,
                                    "quality_review": metadata["quality_review"],
                                    "attempt_artifact_manifest": (
                                        attempt_artifact_receipt
                                    ),
                                    "evidence_record": outcome_evidence_record,
                                    "claim_state": claim_state,
                                    "workspace": workspace.as_metadata(),
                                    "changed_paths": [],
                                    "changed_path_hashes": {},
                                    "request_identity": {
                                        "request_id": request_id,
                                        "task_id": str(metadata["task_id"]),
                                        "runner": str(metadata["runner"]),
                                        "topic": str(metadata["topic"]),
                                    },
                                },
                            )
                            if not release_result.get("ok"):
                                terminal_state = "review_pending"
                                error = "review_transition_failed:" + str(
                                    release_result.get("stderr")
                                    or release_result.get("stdout")
                                    or ""
                                )[:300]
                            raise _QualityReviewFinalized
                        changed = _enforce_finalization_scope()
                        residual_contract_result = validate_residual_contract(
                            workspace,
                            list(metadata.get("residual_contract_manifest") or []),
                        )
                        required_output_records = validate_required_outputs(
                            workspace,
                            metadata.get("required_outputs") or [],
                            allow_empty=tuple(
                                metadata.get("allow_empty_required_outputs") or []
                            ),
                            allow_unchanged=tuple(
                                metadata.get("allow_unchanged_required_outputs") or []
                            ),
                            replay_authorization=metadata.get(
                                "validation_only_replay_authorization"
                            ),
                            replay_task_id=str(metadata.get("task_id") or ""),
                            replay_actor=core.CODEX_RUNNER,
                            replay_predecessor_request_id=str(
                                (metadata.get("rework_predecessor") or {}).get(
                                    "request_id"
                                )
                                or ""
                            ),
                            replay_claim_epoch=metadata.get("claim_epoch"),
                        )
                        worker_mcp_gate = _worker_mcp_live_call_gate(metadata, request_id)
                        # Always collect deterministic validation evidence
                        # before enforcing the context/tool-use gate. A
                        # missing MCP call must still block promotion, but it
                        # must not erase the otherwise useful test evidence or
                        # force the coordinator to rerun the worker merely to
                        # learn whether its candidate builds.
                        validations = _run_finalization_validations()
                        if worker_mcp_gate.get("gated") and not worker_mcp_gate.get("satisfied", True):
                            raise WorkspaceError(
                                "validation_required_aiworkhub_mcp_call_missing:"
                                + str(worker_mcp_gate.get("reason") or "")
                            )
                        # An empty validation plan cannot mutate the workspace;
                        # avoid a second Git scan on the read-only/zero-diff hot
                        # path. Commands that did run are followed by the full
                        # post-validation scope check as before.
                        if validations:
                            changed = _enforce_finalization_scope()
                        # B561: union validated required-output exact paths into
                        # the review candidate set.  validate_required_outputs
                        # finds gitignored files that changed_paths (git-diff /
                        # git-ls-files --exclude-standard) silently omits.
                        validated_required_paths = {
                            rec["path"]
                            for rec in required_output_records
                            if not rec.get("unchanged_allowed")
                        }
                        validation_only_replay_records = [
                            rec["replay_evidence"]
                            for rec in required_output_records
                            if rec.get("replay_evidence")
                        ]
                        changed = sorted(set(changed) | validated_required_paths)
                        if not changed:
                            if validation_only_replay_records:
                                # Exact, manager-authorized replay is itself the
                                # intended effect: validations ran against the
                                # hash-pinned inherited candidate, while no
                                # repository delta may be fabricated merely to
                                # satisfy the ordinary code-task no-effect gate.
                                pass
                            elif not _metadata_is_readonly_research(metadata, workspace):
                                raise WorkspaceError("no_effect")
                            else:
                                research_result = _readonly_research_result_evidence(
                                    Path(str(metadata["stdout_path"]))
                                )
                                if not research_result.get("meaningful_output"):
                                    raise WorkspaceError(
                                        str(
                                            research_result.get("reason")
                                            or "research_result_missing"
                                        )
                                    )
                            quality_gate = {
                                "schema_id": "aiworkhub.completion_quality_gate.v1",
                                "applicable": False,
                                "passed": None,
                                "reason": "research_no_repository_change",
                                "changed_paths": [],
                                "checks": [],
                                "blocking_checks": [],
                            }
                        else:
                            quality_gate = quality_evidence.run_completion_quality_gate(
                                workspace.path, changed_paths=changed
                            )
                            if not quality_gate.get("passed"):
                                blockers = quality_gate.get("blocking_checks") or []
                                reason = quality_gate.get("config_error") or ",".join(
                                    str(v) for v in blockers
                                )
                                raise WorkspaceError(
                                    "quality_gate_failed:" + str(reason)[:400]
                                )
                        _enforce_behavioral_gate(
                            metadata,
                            validations,
                            quality_gate,
                        )
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
                        attempt_artifact_receipt = self._persist_attempt_artifacts(
                            request_id,
                            metadata,
                            workspace,
                            target_state="review_ready",
                            changed_paths=changed,
                            changed_path_hashes=changed_path_hashes,
                            required_outputs=required_output_records,
                            validations=validations,
                            review={
                                "kind": "worker_candidate",
                                "research_result": research_result,
                                "residual_contract": residual_contract_result,
                            },
                            quality_gate=quality_gate,
                            worker_mcp_gate=worker_mcp_gate,
                        )
                        if research_result is not None:
                            outcome_level = evidence_levels.EvidenceLevel.OBSERVATION
                            outcome_message = (
                                "Read-only research produced a meaningful, hash-bound result."
                            )
                        elif validations:
                            outcome_level = evidence_levels.EvidenceLevel.TESTED
                            outcome_message = (
                                "Candidate passed its declared deterministic validations."
                            )
                        else:
                            outcome_level = evidence_levels.EvidenceLevel.STATIC_EVIDENCE
                            outcome_message = (
                                "Candidate passed scope, hash, and static quality gates."
                            )
                        outcome_evidence_record = self._canonical_outcome_evidence(
                            request_id,
                            attempt_artifact_receipt,
                            level=outcome_level,
                            message=outcome_message,
                        )
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
                                "validation_only_replay": validation_only_replay_records,
                                "validation": validations,
                                "worker_mcp_gate": worker_mcp_gate,
                                "quality_gate": quality_gate,
                                "research_result": research_result,
                                "claim_state": claim_state,
                                "immutable_inputs": metadata.get("immutable_inputs") or [],
                                "immutable_input_manifest": (
                                    metadata.get("immutable_input_manifest") or {}
                                ),
                                "residual_contract": residual_contract_result,
                                "attempt_artifact_manifest": (
                                    attempt_artifact_receipt
                                ),
                                "evidence_record": outcome_evidence_record,
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
            except _QualityReviewFinalized:
                pass
            except WorkspaceError as exc:
                if isinstance(exc, ValidationRunError):
                    validations = [dict(row) for row in exc.results]
                error = str(exc)
                terminal_state = _terminal_state_for_workspace_error(exc)
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
                    retained_candidate: dict[str, Any] = {}
                    if terminal_state == "validation_failed":
                        try:
                            retained_candidate = _retained_candidate_identity_evidence(
                                workspace,
                                metadata,
                                request_id,
                                changed,
                                claim_state,
                            )
                        except WorkspaceError:
                            # Keep the truthful failure even when these bytes
                            # cannot be safely bound for residual rework.
                            retained_candidate = {}
                    terminal_evidence = {
                        "request_id": request_id,
                        "changed_paths": changed,
                        "promoted_paths": promoted,
                        "required_outputs": required_output_records,
                        "validation": validations,
                        "worker_mcp_gate": worker_mcp_gate,
                        "quality_gate": quality_gate,
                        "residual_contract": residual_contract_result,
                        "workspace": workspace.as_metadata(),
                        "request_identity": {
                            "request_id": request_id,
                            "task_id": str(metadata["task_id"]),
                            "runner": str(metadata["runner"]),
                            "topic": str(metadata["topic"]),
                        },
                        **retained_candidate,
                    }
                    if terminal_state == "validation_failed":
                        rework_delta = _terminal_rework_delta_evidence(
                            workspace,
                            metadata,
                            request_id,
                            changed,
                        )
                        if rework_delta is not None:
                            terminal_evidence["rework_delta"] = rework_delta
                    if terminal_state == "finalize_failed" or (
                        _is_operational_validation_failure(terminal_state, error)
                    ):
                        terminal_evidence["error"] = error[:500]
                        release_result = self._terminal_failure_exact(
                            metadata,
                            terminal_state,
                            request_id=request_id,
                            evidence=terminal_evidence,
                        )
                    else:
                        release_result = self._review_terminal_exact(
                            metadata,
                            terminal_state,
                            request_id=request_id,
                            error=error,
                            evidence=terminal_evidence,
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
                    release_result = self._terminal_failure_exact(
                        metadata,
                        terminal_state,
                        request_id=request_id,
                        evidence={
                            "request_id": request_id,
                            "error": error[:500],
                            "changed_paths": changed,
                            "promoted_paths": promoted,
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
                        terminal_state = "release_pending"

            if attempt_artifact_receipt is None:
                try:
                    failure_hashes = (
                        _changed_path_hashes(workspace, changed) if changed else {}
                    )
                    attempt_artifact_receipt = self._persist_attempt_artifacts(
                        request_id,
                        metadata,
                        workspace,
                        target_state=terminal_state,
                        changed_paths=changed,
                        changed_path_hashes=failure_hashes,
                        required_outputs=required_output_records,
                        validations=validations,
                        review={
                            "kind": "terminal_outcome",
                            "release_transition_ok": bool(
                                release_result and release_result.get("ok")
                            ),
                        },
                        quality_gate=quality_gate,
                        worker_mcp_gate=worker_mcp_gate,
                        error=error,
                    )
                except Exception as exc:  # preserve the truthful terminal outcome
                    attempt_artifact_error = (
                        f"attempt_artifact_persist_failed:{type(exc).__name__}:{exc}"
                    )[:500]

            if (
                outcome_evidence_record is None
                and attempt_artifact_receipt is not None
            ):
                outcome_evidence_record = self._canonical_outcome_evidence(
                    request_id,
                    attempt_artifact_receipt,
                    level=evidence_levels.EvidenceLevel.INCONCLUSIVE,
                    message=(error or f"Attempt ended in {terminal_state}.")[:1000],
                )

            stdout_path = Path(str(metadata["stdout_path"]))
            usage: dict[str, Any] = {}
            usage_recorded = False
            usage_error = ""
            if finalization_retry:
                prior_usage_event = next(
                    (
                        row
                        for row in reversed(events[:-1])
                        if isinstance(row.get("usage"), dict) and row.get("usage")
                    ),
                    {},
                )
                if prior_usage_event.get("usage_recorded"):
                    usage = dict(prior_usage_event.get("usage") or {})
                    usage_recorded = True
                    usage_error = "finalization_retry_reused_prior_usage"
                else:
                    # A release_pending predecessor is a finalization-pending
                    # state, so it never recorded provider spend: the provider
                    # already ran and spent tokens, but usage recording was
                    # deferred with the release transition. Record it now --
                    # ``_record_usage`` is idempotent per request_id, so this
                    # can never double-count -- rather than lose the spend.
                    usage, usage_recorded, usage_error = self._record_usage(
                        request_id,
                        str(metadata["task_id"]),
                        str(metadata["runner"]),
                        str(metadata["adapter_id"]),
                        str(metadata.get("model") or metadata["adapter_id"]),
                        stdout_path,
                        topic=str(metadata["topic"]),
                        execution_mode=str(metadata.get("execution_mode") or ""),
                    )
            elif terminal_state not in FINALIZATION_PENDING_STATES:
                usage, usage_recorded, usage_error = self._record_usage(
                    request_id,
                    str(metadata["task_id"]),
                    str(metadata["runner"]),
                    str(metadata["adapter_id"]),
                    str(metadata.get("model") or metadata["adapter_id"]),
                    stdout_path,
                    topic=str(metadata["topic"]),
                    execution_mode=str(metadata.get("execution_mode") or ""),
                )
            context_ack = _project_context_receipt_from_output(
                stdout_path,
                expected_bundle_sha256=str(
                    (metadata.get("project_context") or {}).get("bundle_sha256") or ""
                ),
            )
            provider_tool_denials = _provider_tool_denials_from_output(stdout_path)
            provider_read_efficiency = _provider_read_efficiency_from_output(stdout_path)
            semantic_edit_evidence = _semantic_edit_evidence_from_output(
                stdout_path, worker_mcp_gate=worker_mcp_gate,
            )
            finalization_duration_ms = round(
                (time.monotonic() - finalization_started) * 1000.0, 3
            )
            validation_duration_ms = round(validation_wall_duration_ms, 3)
            scope_duration_ms = round(scope_duration_ms, 3)
            evidence_transition_duration_ms = round(
                max(
                    0.0,
                    finalization_duration_ms
                    - validation_duration_ms
                    - scope_duration_ms,
                ),
                3,
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
                "execution_mode": metadata.get("execution_mode") or "provider_worker",
                "provider_launched": metadata.get("provider_launched") is not False,
                "finalization_retry": finalization_retry,
                "finalization_retry_provider_launched": False if finalization_retry else None,
                "changed_paths": changed,
                "promoted_paths": promoted,
                "required_outputs": required_output_records,
                "validation": validations,
                "review_transition_ok": bool(review_result and review_result.get("ok")),
                "release_transition_ok": bool(release_result and release_result.get("ok")),
                "terminal_review_disposition": str(
                    (release_result or {}).get("terminal_review_disposition") or ""
                )[:200],
                "canonical_lifecycle": str(
                    (release_result or {}).get("canonical_lifecycle") or ""
                )[:40],
                "liveness_lost": liveness_lost,
                "stall_detected": stall_detected,
                "stall_idle_seconds": stall_idle_seconds,
                "stall_last_meaningful_phase": supervisor_status.get("last_meaningful_phase"),
                "stall_last_meaningful_progress_epoch": supervisor_status.get(
                    "last_meaningful_progress_epoch"
                ),
                "stall_last_progress_sequence": _meaningful_progress_sequence(
                    supervisor_status
                ),
                "stall_heartbeat_seq": supervisor_status.get("heartbeat_seq"),
                "stall_stdout_bytes": supervisor_status.get("stdout_bytes"),
                "stall_stderr_bytes": supervisor_status.get("stderr_bytes"),
                "stall_supervisor_pid": supervisor_pid,
                "stall_supervisor_pid_start_ticks": supervisor_ticks,
                "error": error[:500],
                "usage": usage,
                "usage_recorded": usage_recorded,
                "usage_error": usage_error,
                "project_context": metadata.get("project_context"),
                "project_context_delivery": metadata.get("project_context_delivery"),
                "prompt_budget": metadata.get("prompt_budget"),
                "token_budget": supervisor_status.get("token_budget"),
                "project_context_acknowledgement": context_ack,
                "provider_tool_denials": provider_tool_denials,
                "read_efficiency": provider_read_efficiency,
                "semantic_edit": semantic_edit_evidence,
                "worker_mcp_gate": worker_mcp_gate,
                "quality_gate": quality_gate,
                "research_result": research_result,
                "residual_contract": residual_contract_result,
                "attempt_artifact_manifest": attempt_artifact_receipt,
                "attempt_artifact_error": attempt_artifact_error,
                "evidence_record": outcome_evidence_record,
                "finalization_duration_ms": finalization_duration_ms,
                "finalization_phase_durations_ms": {
                    "workspace_scope": scope_duration_ms,
                    "validation": validation_duration_ms,
                    "evidence_and_transition": evidence_transition_duration_ms,
                },
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
                "supervisor_status_path", "cancel_path", "sandbox_backend", "exit_code",
                "error",
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
        if latest.get("state") not in ACTIVE_PROCESS_STATES | FINALIZATION_PENDING_STATES:
            return {}
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
            "last_meaningful_progress_epoch": supervisor_status.get(
                "last_meaningful_progress_epoch"
            ),
            "last_meaningful_phase": supervisor_status.get("last_meaningful_phase"),
            "last_progress_sequence": supervisor_status.get("last_progress_sequence"),
            "last_meaningful_progress_sequence": _meaningful_progress_sequence(
                supervisor_status
            ),
        }

    def status(self, request_id: str) -> dict[str, Any]:
        events = self._request_events(request_id)
        if not events:
            return {"ok": False, "request_id": request_id, "state": "not_found"}
        # Disposal/GC events intentionally contain only a small lifecycle
        # delta.  Treating that final row as the complete request snapshot
        # drops the request-bound log paths, model, exit code and exact error
        # that were recorded by the preceding terminal event.  Rehydrate the
        # stable request identity from the full request lineage while keeping
        # the final row authoritative for state/disposition.
        lineage = self._event_identity(events)
        latest = {**lineage, **events[-1]}
        if (
            latest.get("state") in ACTIVE_PROCESS_STATES | FINALIZATION_PENDING_STATES
            and latest.get("metadata_path")
        ):
            pid = int(latest.get("pid") or 0)
            ticks = latest.get("pid_start_ticks")
            identity = _pid_identity_evidence(pid, ticks)
            status_path = Path(str(latest.get("supervisor_status_path") or ""))
            supervisor_status = (
                self._read_supervisor_status(status_path)
                if str(status_path) not in {"", "."}
                else {}
            )
            supervisor_state = str(supervisor_status.get("state") or "")
            terminal_status_artifact = bool(
                supervisor_status and supervisor_state not in {"starting", "running"}
            )
            if (
                identity.verdict is PidIdentityVerdict.MISMATCH
                or terminal_status_artifact
            ):
                try:
                    self._finalize_after_process_exit(
                        request_id, lock_blocking=False
                    )
                except OSError:
                    # Status is a read surface. A concurrent finalizer owns
                    # the exact request lock, so report the current durable
                    # snapshot instead of waiting 20 seconds or surfacing a
                    # transport failure. The owner remains the only writer.
                    latest["reconciliation_deferred"] = "request_lock_busy"
                events = self._request_events(request_id)
                lineage = self._event_identity(events)
                latest = {**lineage, **events[-1], **{
                    key: value
                    for key, value in latest.items()
                    if key == "reconciliation_deferred"
                }}
            elif identity.verdict is PidIdentityVerdict.UNKNOWN:
                latest["reconciliation_deferred"] = "pid_identity_unknown"
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
        if latest.get("state") != "starting" or int(latest.get("pid") or 0):
            # A pid-null starting reservation is still under preparation; the
            # reviewer task card may not exist yet and reading it can contend
            # with the preparation owner's own store access. Keep status reads
            # bounded by deriving the card only once a real process identity
            # exists. Preparation progress is still observable via the latest
            # event's preparation_phase/heartbeat fields below.
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
            "preparation_phase": latest.get("preparation_phase"),
            "preparation_heartbeat_epoch": latest.get(
                "preparation_heartbeat_epoch"
            ),
            "runner": latest.get("runner"),
            "topic": latest.get("topic"),
            "adapter_id": latest.get("adapter_id"),
            "model": latest.get("model"),
            "task_state": core._lifecycle_state(card) if card else "unknown",
            "task_card": card,
            "event_count": len(events),
            # A JS consumer reads pid_start_ticks off latest_event; carry it as
            # a lossless string so a >2**53 counter is not silently rounded.
            "latest_event": {**latest, **pid_identity_surface(latest)},
            "liveness": self._liveness_snapshot(latest),
        }

    def invoke_vscode_lm_worker_tool(
        self,
        request_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any]:
        """Run one GLM bridge tool through the exact task-scoped worker authority.

        The VS Code Language Model API can only call tools through the
        extension-owned coordinator MCP connection.  This bridge keeps that
        transport while executing the call with the launched worker's
        immutable identity and HMAC audit ledger, so completion gates observe
        genuine worker tool use instead of an unrelated manager-side call.
        """
        if not re.fullmatch(r"[a-f0-9]{32}", str(request_id or "")):
            return {"ok": False, "reason": "worker_bridge_request_id_invalid"}
        if not isinstance(tool_input, dict):
            return {"ok": False, "reason": "worker_bridge_input_invalid"}
        if len(json.dumps(tool_input, ensure_ascii=False).encode("utf-8")) > 16 * 1024:
            return {"ok": False, "reason": "worker_bridge_input_too_large"}
        # NF389: bind the bridge's authenticated provider-call identity into the
        # exact worker audit context. A PRESENT value -- even an explicit empty
        # string -- must pass the same fail-closed validator the ledger uses;
        # only an ABSENT key retains the backward-compatible empty sentinel.
        # ``provenance`` is provider-call provenance ONLY for the Source Graph
        # dispatch (where ``source_graph_query`` would otherwise default it to
        # ``live``). Every other tool keeps its own write-intent ``provenance``
        # field untouched in tool_input (a distinct concept).
        provider_call_id = ""
        provenance = ""
        is_source_graph = tool_name == "aiworkhub_manager_source_graph_query"
        # NF389 sealed correction: consume (remove) the authenticated identity
        # from the tool input here so it can never leak into the
        # ``source_graph_query(ctx, **tool_input)`` dispatch (which would raise
        # ``TypeError``), while still binding the validated value into the exact
        # worker audit context below.
        if "provider_call_id" in tool_input:
            raw_provider_call_id = tool_input.pop("provider_call_id")
            try:
                provider_call_id = worker_ai_tools_mcp.validate_provider_call_id(
                    raw_provider_call_id
                )
            except worker_ai_tools_mcp.WorkerToolError as exc:
                return {"ok": False, "reason": str(exc)}
        if is_source_graph and "provenance" in tool_input:
            raw_provenance = tool_input.pop("provenance")
            try:
                provenance = worker_ai_tools_mcp.validate_provenance(raw_provenance)
            except worker_ai_tools_mcp.WorkerToolError as exc:
                return {"ok": False, "reason": str(exc)}
        events = self._request_events(request_id)
        if not events:
            return {"ok": False, "reason": "worker_bridge_request_not_found"}
        latest = events[-1]
        vscode_lm_adapters = {
            runtime_adapters.VSCODE_LM_ADAPTER,
            runtime_adapters.GLM_VSCODE_LM_ADAPTER,
            runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER,
        }
        if latest.get("adapter_id") not in vscode_lm_adapters:
            return {"ok": False, "reason": "worker_bridge_adapter_mismatch"}
        if latest.get("state") not in WORKER_BRIDGE_AUTHORIZED_PROCESS_STATES:
            return {"ok": False, "reason": "worker_bridge_request_not_active"}
        metadata_path = self._metadata_from_events(events)
        if metadata_path is None or metadata_path.parent.resolve() != self.process_dir.resolve():
            return {"ok": False, "reason": "worker_bridge_metadata_invalid"}
        try:
            if metadata_path.is_symlink() or not metadata_path.is_file() or metadata_path.stat().st_size > 2 * 1024 * 1024:
                return {"ok": False, "reason": "worker_bridge_metadata_invalid"}
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"ok": False, "reason": "worker_bridge_metadata_unreadable"}
        if (
            str(metadata.get("request_id") or "") != request_id
            or str(metadata.get("adapter_id") or "") not in vscode_lm_adapters
        ):
            return {"ok": False, "reason": "worker_bridge_metadata_identity_mismatch"}
        worker_meta = metadata.get("worker_mcp") or {}
        workspace_meta = metadata.get("workspace") or {}
        rework_overlay_packet: dict[str, Any] | None = None
        rework_overlay_packet_path: Path | None = None
        source_graph_authority = worker_meta.get("source_graph_authority")
        if (
            isinstance(source_graph_authority, dict)
            and source_graph_authority.get("authority_source") == "rework_overlay"
        ):
            try:
                workspace_home = Path(str(workspace_meta["home"])).resolve()
                expected_path = (
                    workspace_home / "task_mcp_worker_runtime" / "rework_overlay.json"
                )
                declared_path = Path(str(source_graph_authority["packet_path"]))
                if os.path.normcase(os.path.abspath(declared_path)) != os.path.normcase(
                    os.path.abspath(expected_path)
                ):
                    return {
                        "ok": False,
                        "reason": "worker_bridge_rework_overlay_path_mismatch",
                    }
                if declared_path.is_symlink():
                    return {
                        "ok": False,
                        "reason": "worker_bridge_rework_overlay_symlink_forbidden",
                    }
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(declared_path, flags)
                try:
                    file_stat = os.fstat(fd)
                    if not stat.S_ISREG(file_stat.st_mode):
                        return {
                            "ok": False,
                            "reason": "worker_bridge_rework_overlay_not_regular",
                        }
                    if file_stat.st_size > worker_ai_tools_mcp.MAX_REWORK_OVERLAY_PACKET_BYTES:
                        return {
                            "ok": False,
                            "reason": "worker_bridge_rework_overlay_too_large",
                        }
                    packet_buffer = bytearray()
                    remaining = file_stat.st_size + 1
                    while remaining > 0:
                        chunk = os.read(fd, min(remaining, 64 * 1024))
                        if not chunk:
                            break
                        packet_buffer.extend(chunk)
                        remaining -= len(chunk)
                finally:
                    os.close(fd)
                packet_bytes = bytes(packet_buffer)
                if len(packet_bytes) != file_stat.st_size:
                    return {
                        "ok": False,
                        "reason": "worker_bridge_rework_overlay_changed_during_read",
                    }
                rework_overlay_packet = json.loads(packet_bytes.decode("utf-8"))
                if not isinstance(rework_overlay_packet, dict):
                    return {
                        "ok": False,
                        "reason": "worker_bridge_rework_overlay_invalid",
                    }
                worker_ai_tools_mcp._verify_rework_overlay_packet(
                    rework_overlay_packet,
                    str(metadata["task_id"]),
                    request_id,
                    str(metadata["runner"]),
                    Path(str(worker_meta["authority_repo"])).resolve(),
                )
                if (
                    str(source_graph_authority.get("target_request_id") or "")
                    != str(rework_overlay_packet.get("predecessor_request_id") or "")
                    or str(source_graph_authority.get("target_task_id") or "")
                    != str(rework_overlay_packet.get("predecessor_task_id") or "")
                    or str(source_graph_authority.get("packet_sha256") or "")
                    != str(rework_overlay_packet.get("canonical_digest") or "")
                ):
                    return {
                        "ok": False,
                        "reason": "worker_bridge_rework_overlay_metadata_mismatch",
                    }
                rework_overlay_packet_path = declared_path
            except KeyError:
                return {
                    "ok": False,
                    "reason": "worker_bridge_rework_overlay_metadata_missing",
                }
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return {
                    "ok": False,
                    "reason": "worker_bridge_rework_overlay_unreadable",
                }
            except worker_ai_tools_mcp.WorkerToolError as exc:
                return {
                    "ok": False,
                    "reason": "worker_bridge_rework_overlay_verification_failed:"
                    + str(exc)[:240],
                }
        try:
            ctx = worker_ai_tools_mcp.WorkerToolContext(
                task_id=str(metadata["task_id"]),
                runner=str(metadata["runner"]),
                topic=str(metadata["topic"]),
                request_id=request_id,
                repo=Path(str(workspace_meta["path"])).resolve(),
                authority_repo=Path(str(worker_meta["authority_repo"])).resolve(),
                source_graph_targets=tuple(str(value) for value in worker_meta.get("source_graph_targets") or []),
                allowed_writes=tuple(str(value) for value in worker_meta.get("allowed_writes") or []),
                session_topic=str(worker_meta.get("session_topic") or metadata["topic"]),
                audit_ledger_path=Path(str(worker_meta["audit_ledger_path"])),
                audit_hmac_key_path=Path(str(worker_meta["audit_hmac_key_path"])),
                quality_review_packet_path=(
                    Path(str((metadata.get("quality_review") or {})["packet_path"]))
                    if isinstance(metadata.get("quality_review"), dict)
                    else None
                ),
                rework_overlay_packet=rework_overlay_packet,
                rework_overlay_packet_path=rework_overlay_packet_path,
                provider_call_id=provider_call_id,
                provenance=provenance,
            )
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "reason": "worker_bridge_context_invalid"}

        if tool_name == "aiworkhub_manager_source_graph_query":
            return worker_ai_tools_mcp.source_graph_query(ctx, **tool_input)
        if tool_name == "aiworkhub_manager_semantic_edit_prepare":
            return worker_ai_tools_mcp.WorkerSemanticEditSession(ctx).prepare(
                # Text-only VS Code LM providers occasionally mirror the final
                # edit envelope's ``path`` field even though the private tool
                # schema calls it ``file_path``.  Both names carry the same
                # repository-relative authority; normalize at the bridge and
                # leave all scope/hash checks to the semantic-edit session.
                file_path=tool_input.get("file_path") or tool_input.get("path", ""),
                start_line=tool_input.get("start_line", 0),
                end_line=tool_input.get("end_line", 0),
            )
        if tool_name == "aiworkhub_manager_session_current_state":
            return worker_ai_tools_mcp.session_current_state(ctx, limit=tool_input.get("limit", 12))
        if tool_name == "aiworkhub_manager_ai_memory_search":
            return worker_ai_tools_mcp.ai_memory_search(
                ctx, query=tool_input.get("query", ""), limit=tool_input.get("limit", 8)
            )
        if tool_name == "aiworkhub_manager_kb_search":
            return worker_ai_tools_mcp.kb_search(
                ctx, query=tool_input.get("query", ""), limit=tool_input.get("limit", 8)
            )
        if tool_name == "aiworkhub_manager_kb_get":
            return worker_ai_tools_mcp.kb_get(ctx, key=tool_input.get("key", ""))
        if tool_name == "aiworkhub_manager_kb_related":
            return worker_ai_tools_mcp.kb_related(ctx, key=tool_input.get("key", ""))
        if tool_name == "aiworkhub_manager_session_write_intent":
            return worker_ai_tools_mcp.session_write_intent(
                ctx,
                action=tool_input.get("action", ""),
                content=tool_input.get("content", ""),
                idempotency_key=tool_input.get("idempotency_key", ""),
                provenance=tool_input.get("provenance", ""),
            )
        if tool_name == "aiworkhub_manager_ai_memory_write_intent":
            return worker_ai_tools_mcp.ai_memory_write_intent(
                ctx,
                action=tool_input.get("action", ""),
                key=tool_input.get("key", ""),
                value=tool_input.get("value", ""),
                tags=tool_input.get("tags", ""),
                scope=tool_input.get("scope", "project"),
                idempotency_key=tool_input.get("idempotency_key", ""),
                provenance=tool_input.get("provenance", ""),
            )
        if tool_name == "aiworkhub_manager_kb_write_intent":
            return worker_ai_tools_mcp.kb_write_intent(
                ctx,
                action=tool_input.get("action", ""),
                key=tool_input.get("key", ""),
                title=tool_input.get("title", ""),
                body=tool_input.get("body", ""),
                category=tool_input.get("category", ""),
                tags=tool_input.get("tags", ""),
                source_refs=tool_input.get("source_refs", ""),
                replacement_key=tool_input.get("replacement_key", ""),
                idempotency_key=tool_input.get("idempotency_key", ""),
                provenance=tool_input.get("provenance", ""),
            )
        if tool_name == "aiworkhub_worker_quality_review_submit":
            findings = tool_input.get("findings", [])
            if not isinstance(findings, list):
                return {"ok": False, "reason": "worker_bridge_findings_invalid"}
            return worker_ai_tools_mcp.quality_review_submit(
                ctx,
                packet_sha256=tool_input.get("packet_sha256", ""),
                lens=tool_input.get("lens", ""),
                findings=findings,
            )
        return {"ok": False, "reason": "worker_bridge_tool_not_allowed"}

    def _context_intent_request(
        self, request_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Resolve one request's immutable worker-MCP ledger binding."""

        if not re.fullmatch(r"[a-f0-9]{32}", str(request_id or "")):
            return None, {"ok": False, "error": "context_intent_request_id_invalid"}
        events = self._request_events(request_id)
        if not events:
            return None, {"ok": False, "error": "context_intent_request_not_found"}
        metadata_path = self._metadata_from_events(events)
        if metadata_path is None or metadata_path.parent.resolve() != self.process_dir.resolve():
            return None, {"ok": False, "error": "context_intent_metadata_invalid"}
        try:
            if metadata_path.is_symlink() or not metadata_path.is_file() or metadata_path.stat().st_size > 2 * 1024 * 1024:
                return None, {"ok": False, "error": "context_intent_metadata_invalid"}
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, {"ok": False, "error": "context_intent_metadata_unreadable"}
        if str(metadata.get("request_id") or "") != request_id:
            return None, {"ok": False, "error": "context_intent_request_identity_mismatch"}
        worker = metadata.get("worker_mcp")
        if not isinstance(worker, dict):
            return None, {"ok": False, "error": "context_intent_worker_runtime_missing"}
        try:
            authority_repo = Path(str(worker["authority_repo"])).resolve()
            ledger_path = Path(str(worker["audit_ledger_path"])).resolve()
            key_path = Path(str(worker["audit_hmac_key_path"])).resolve()
        except (KeyError, TypeError, ValueError):
            return None, {"ok": False, "error": "context_intent_worker_runtime_invalid"}
        if authority_repo != self.repo:
            return None, {"ok": False, "error": "context_intent_authority_repo_mismatch"}
        if (
            ledger_path.is_symlink() or key_path.is_symlink()
            or not ledger_path.is_file() or not key_path.is_file()
            or ledger_path.stat().st_size > 4 * 1024 * 1024
            or key_path.stat().st_size > 4096
        ):
            return None, {"ok": False, "error": "context_intent_ledger_invalid"}
        binding = {
            "request_id": request_id,
            "task_id": str(metadata.get("task_id") or ""),
            "runner": str(metadata.get("runner") or ""),
            "topic": str(metadata.get("topic") or ""),
            "authority_repo": authority_repo,
            "ledger_path": ledger_path,
            "key_path": key_path,
        }
        if not binding["task_id"] or not binding["runner"] or not binding["topic"]:
            return None, {"ok": False, "error": "context_intent_binding_incomplete"}
        return binding, {}

    def _context_write_intent_snapshot(self, request_id: str) -> dict[str, Any]:
        binding, error = self._context_intent_request(request_id)
        if binding is None:
            return error
        try:
            intents = context_write_intents.read_verified_intents(
                ledger_path=binding["ledger_path"],
                key_path=binding["key_path"],
                task_id=binding["task_id"],
                runner=binding["runner"],
                topic=binding["topic"],
                request_id=request_id,
                authority_repo=binding["authority_repo"],
            )
            dispositions = context_write_intents.decisions(self.repo, request_id=request_id)
        except (context_write_intents.ContextWriteIntentError, OSError, sqlite3.Error) as exc:
            return {"ok": False, "error": f"context_intent_read_failed:{type(exc).__name__}"}
        rows: list[dict[str, Any]] = []
        for intent in intents:
            intent_id = str(intent["intent_id"])
            decision = dispositions.get(intent_id)
            rows.append({
                **intent,
                "status": str(decision.get("decision")) if decision else "pending_manager_review",
                "decision": decision,
            })
        pending = [row for row in rows if row["status"] == "pending_manager_review"]
        return {
            "ok": True,
            "schema_id": "aiworkhub.context_write_intent_inbox.v1",
            "request_id": request_id,
            "task_id": binding["task_id"],
            "intents": rows,
            "counts": {
                "total": len(rows),
                "pending": len(pending),
                "accepted": sum(row["status"] == "accepted" for row in rows),
                "rejected": sum(row["status"] == "rejected" for row in rows),
            },
        }

    def context_write_intents(self, request_id: str) -> dict[str, Any]:
        """MANAGER READ: inspect authenticated proposals for one request."""

        route = core.manager_bootstrap()
        if route.get("role") != "manager" or not isinstance(route.get("manager_route"), dict):
            return {"ok": False, "error": "verified_manager_identity_required"}
        route_repo = Path(str(route.get("repo") or self.repo)).resolve()
        if route_repo != self.repo:
            return {"ok": False, "error": "manager_repository_mismatch"}
        return self._context_write_intent_snapshot(request_id)

    def dispose_context_write_intent(
        self, request_id: str, intent_id: str, *, decision: str, reason: str,
    ) -> dict[str, Any]:
        """MANAGER WRITE: accept/reject one exact authenticated proposal."""

        route = core.manager_bootstrap()
        identity = route.get("manager_route") if isinstance(route, dict) else None
        if route.get("role") != "manager" or not isinstance(identity, dict):
            return {"ok": False, "error": "verified_manager_identity_required"}
        if Path(str(route.get("repo") or self.repo)).resolve() != self.repo:
            return {"ok": False, "error": "manager_repository_mismatch"}
        if decision not in {"accepted", "rejected"}:
            return {"ok": False, "error": "invalid_decision"}
        if not core.writes_allowed():
            return {"ok": False, "error": "write_gate_closed"}
        snapshot = self._context_write_intent_snapshot(request_id)
        if not snapshot.get("ok"):
            return snapshot
        try:
            card = _parse_card(self._show_task(str(snapshot["task_id"])), str(snapshot["task_id"]))
        except LaunchRejected as exc:
            return {"ok": False, "error": f"task_lookup_failed:{exc}"}
        if _canonical_task_status(card) != "review":
            return {"ok": False, "error": "context_intent_task_not_in_review"}
        selected = next(
            (row for row in snapshot["intents"] if str(row.get("intent_id")) == intent_id), None,
        )
        if selected is None:
            return {"ok": False, "error": "context_intent_not_found"}
        prior = selected.get("decision")
        if isinstance(prior, dict):
            if str(prior.get("decision")) != decision:
                return {"ok": False, "error": "intent_already_disposed"}
            return {"ok": True, "idempotent": True, **prior}
        provider = str(identity.get("provider") or route.get("provider") or "manager")
        session_id = str(identity.get("thread_id") or identity.get("session_id") or "")
        if not session_id:
            return {"ok": False, "error": "manager_session_identity_missing"}
        result: dict[str, Any] = {"ok": True, "applied": False}
        try:
            if decision == "accepted":
                result = context_write_intents.apply_accepted_intent(
                    self.repo,
                    intent=selected,
                    manager_provider=provider,
                    manager_session_id=session_id,
                )
            recorded = context_write_intents.record_decision(
                self.repo,
                intent=selected,
                decision=decision,  # type: ignore[arg-type]
                reason=reason,
                manager_provider=provider,
                manager_session_id=session_id,
                result=result,
            )
        except (context_write_intents.ContextWriteIntentError, context_writes.ContextWriteError) as exc:
            return {"ok": False, "error": str(exc)[:300]}
        except (OSError, sqlite3.Error) as exc:
            return {"ok": False, "error": f"context_intent_disposition_failed:{type(exc).__name__}"}
        return {
            **recorded,
            "schema_id": context_write_intents.DECISION_SCHEMA_ID,
            "request_id": request_id,
            "task_id": snapshot["task_id"],
        }

    def collect(self, request_id: str, max_log_bytes: int = MAX_LOG_TAIL_BYTES) -> dict[str, Any]:
        status = self.status(request_id)
        if not status.get("ok"):
            return status
        latest = status.get("latest_event") or {}
        stdout_path = Path(str(latest.get("stdout_path") or ""))
        stderr_path = Path(str(latest.get("stderr_path") or ""))
        total_log_limit = max(0, min(int(max_log_bytes), MAX_LOG_TAIL_BYTES))
        stdout_limit = (total_log_limit + 1) // 2
        stderr_limit = total_log_limit // 2
        raw_card = status.get("task_card")
        card: dict[str, Any] = raw_card if isinstance(raw_card, dict) else {}
        card_fields = (
            "task_id", "status", "worker_status", "runner", "topic", "priority",
            "claimed_by", "claim_epoch", "launch_request_id", "terminal_substatus",
        )
        event_fields = (
            "request_id", "task_id", "state", "timestamp", "started_at", "finished_at",
            "pid", "exit_code", "runner", "topic", "adapter_id", "model", "error",
            # Stable public lifecycle evidence used by coordinator/security
            # consumers.  These are scalar paths, not recursive payloads.
            "metadata_path", "workspace_retained",
        )
        card_summary = {key: card.get(key) for key in card_fields if key in card}
        event_summary = {key: latest.get(key) for key in event_fields if key in latest}
        # pid_start_ticks is JS-unsafe above 2**53; expose the same lossless
        # string form the status() surface uses so the two surfaces agree.
        event_summary.update(pid_identity_surface(latest))
        changed_paths = latest.get("changed_paths")
        if isinstance(changed_paths, list):
            event_summary["changed_paths"] = changed_paths[:64]
            event_summary["changed_path_count"] = len(changed_paths)
        promoted_paths = latest.get("promoted_paths")
        if isinstance(promoted_paths, list):
            event_summary["promoted_paths"] = promoted_paths[:64]
            event_summary["promoted_path_count"] = len(promoted_paths)
        card_sha256 = hashlib.sha256(
            json.dumps(card, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest() if card else ""
        event_sha256 = hashlib.sha256(
            json.dumps(latest, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest() if latest else ""
        truncated_fields: list[str] = []
        if set(card) - set(card_summary):
            truncated_fields.append("task_card")
        if set(latest) - set(event_summary):
            truncated_fields.append("latest_event")
        stdout_tail = _safe_tail(stdout_path, stdout_limit) if stdout_limit else ""
        stderr_tail = _safe_tail(stderr_path, stderr_limit) if stderr_limit else ""
        terminal_review = card.get("terminal_review")
        terminal_substatus = (
            str(terminal_review.get("substatus") or "")
            if isinstance(terminal_review, dict)
            else ""
        )
        review_ready = bool(
            status.get("task_state") == "review"
            and terminal_substatus in {"", "review_ready"}
            and str(status.get("state") or "") == "review_ready"
        )
        return {
            "ok": True,
            "request_id": status.get("request_id"),
            "task_id": status.get("task_id"),
            "state": status.get("state"),
            "process_alive": status.get("process_alive"),
            "exit_code": status.get("exit_code"),
            "runner": status.get("runner"),
            "topic": status.get("topic"),
            "adapter_id": status.get("adapter_id"),
            "model": status.get("model"),
            "task_state": status.get("task_state"),
            "event_count": status.get("event_count"),
            "liveness": status.get("liveness"),
            "task_card": card_summary,
            "task_card_sha256": card_sha256,
            "latest_event": event_summary,
            "latest_event_sha256": event_sha256,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "log_bytes_returned": len(stdout_tail.encode("utf-8")) + len(stderr_tail.encode("utf-8")),
            "max_log_bytes": total_log_limit,
            "truncated_fields": truncated_fields,
            "detail_cursor": {"request_id": request_id},
            "review_ready": review_ready,
            "terminal_substatus": terminal_substatus,
            "terminal": status.get("state") in {
                *TERMINAL_PROCESS_STATES,
            },
        }

    def cancel(self, request_id: str, reason: str = "owner_cancelled") -> dict[str, Any]:
        with self._lock:
            live = self._live.get(request_id)
        # Cancellation must inspect the durable lineage without invoking
        # status() reconciliation first. A restarted manager can observe a
        # dead supervisor while the already-claimed editor provider is still
        # running; status() would finalize/release the workspace before the
        # bridge cancellation decision was published.
        initial_events = self._request_events(request_id)
        if not initial_events:
            return {"ok": False, "request_id": request_id, "state": "not_found"}
        initial_lineage = self._event_identity(initial_events)
        initial_latest = {**initial_lineage, **initial_events[-1]}
        status = {
            "ok": True,
            "request_id": request_id,
            "state": initial_latest.get("state"),
            "adapter_id": initial_latest.get("adapter_id"),
            "latest_event": initial_latest,
        }
        if status.get("state") in TERMINAL_PROCESS_STATES:
            return {
                "ok": True,
                "request_id": request_id,
                "state": status.get("state"),
                "idempotent_noop": True,
            }
        bridge_cancel_status = ""
        try:
            bridge_cancel_status = (
                self._publish_bridge_cancellation_before_finalization(
                    request_id,
                    live,
                )
            )
        except _BridgeCancellationDeferred as exc:
            return {
                "ok": False,
                "request_id": request_id,
                "state": "reconcile_pending",
                "blocked_reason": str(exc)[:500],
            }
        if bridge_cancel_status == "completed":
            return {
                "ok": True,
                "request_id": request_id,
                "state": status.get("state"),
                "bridge_cancel_status": "completed",
                "completion_won": True,
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
                "bridge_cancel_status": bridge_cancel_status,
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
            identity = _pid_identity_evidence(pid, ticks)
            if identity.verdict is PidIdentityVerdict.UNKNOWN:
                return {
                    "ok": False,
                    "request_id": request_id,
                    "state": latest.get("state"),
                    "blocked_reason": "pid_identity_unknown",
                    "reconciliation_deferred": "pid_identity_unknown",
                }
            if identity.verdict is PidIdentityVerdict.MISMATCH:
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
                    "bridge_cancel_status": bridge_cancel_status,
                })
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    should_finalize = True

        if should_finalize:
            self._finalize_after_process_exit(request_id)
            refreshed = self.status(request_id)
            return {
                "ok": bool(refreshed.get("ok")),
                "request_id": request_id,
                "state": refreshed.get("state"),
                "blocked_reason": "supervisor_not_alive",
            }
        return {"ok": True, "request_id": request_id, "state": event["state"]}

    def retry_finalization(self, request_id: str, task_id: str) -> dict[str, Any]:
        """Retry retained deterministic finalization without a provider call."""
        if not core.writes_allowed():
            return {
                "ok": False,
                "request_id": request_id,
                "task_id": task_id,
                "error": "write_gate_closed",
            }
        with self._request_lock(request_id):
            events = self._request_events(request_id)
            if not events:
                return {
                    "ok": False,
                    "request_id": request_id,
                    "task_id": task_id,
                    "error": "request_not_found",
                }
            latest = events[-1]
            if str(latest.get("task_id") or "") != task_id:
                return {
                    "ok": False,
                    "request_id": request_id,
                    "task_id": task_id,
                    "error": "request_task_identity_mismatch",
                }
            latest_state = str(latest.get("state") or "")
            latest_error = str(latest.get("error") or "")
            retryable_validation_failure = (
                latest_state == "validation_failed"
                and (
                    latest_error.startswith("validation_exec_scratch_unavailable:")
                    or latest_error.startswith(
                        "validation_failed:validation_exec_scratch_unavailable:"
                    )
                )
            )
            retryable_release_pending = latest_state == "release_pending"
            if (
                latest_state != "finalize_failed"
                and not retryable_validation_failure
                and not retryable_release_pending
            ):
                return {
                    "ok": False,
                    "request_id": request_id,
                    "task_id": task_id,
                    "error": (
                        "request_not_retryable_finalization_failure:"
                        + (latest_state or "missing")
                    ),
                }
            metadata_path = self._metadata_from_events(events)
            if (
                metadata_path is None
                or metadata_path.parent.resolve() != self.process_dir.resolve()
                or metadata_path.is_symlink()
                or not metadata_path.is_file()
                or metadata_path.stat().st_size > 2 * 1024 * 1024
            ):
                return {
                    "ok": False,
                    "request_id": request_id,
                    "task_id": task_id,
                    "error": "finalization_retry_metadata_invalid",
                }
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                workspace = WorkerWorkspace.from_metadata(dict(metadata["workspace"]))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                return {
                    "ok": False,
                    "request_id": request_id,
                    "task_id": task_id,
                    "error": f"finalization_retry_metadata_unreadable:{exc}"[:500],
                }
            runner = str(metadata.get("runner") or "")
            topic = str(metadata.get("topic") or "")
            if (
                str(metadata.get("request_id") or "") != request_id
                or str(metadata.get("task_id") or "") != task_id
                or workspace.repo != self.repo
                or workspace.request_id != request_id
            ):
                return {
                    "ok": False,
                    "request_id": request_id,
                    "task_id": task_id,
                    "error": "finalization_retry_identity_mismatch",
                }
            try:
                assert_gc_safe_workspace_shape(
                    request_id, workspace.path, workspace.home, repo=self.repo
                )
            except WorkspaceError as exc:
                return {
                    "ok": False,
                    "request_id": request_id,
                    "task_id": task_id,
                    "error": f"finalization_retry_workspace_unsafe:{exc}"[:500],
                }
            if workspace.path.is_symlink() or not workspace.path.is_dir():
                return {
                    "ok": False,
                    "request_id": request_id,
                    "task_id": task_id,
                    "error": "finalization_retry_workspace_missing",
                }
            status_path = Path(str(metadata.get("supervisor_status_path") or ""))
            if (
                status_path.parent.resolve() != self.process_dir.resolve()
                or status_path.is_symlink()
                or not status_path.is_file()
            ):
                return {
                    "ok": False,
                    "request_id": request_id,
                    "task_id": task_id,
                    "error": "finalization_retry_supervisor_status_invalid",
                }
            supervisor_status = self._read_supervisor_status(status_path)
            if (
                str(supervisor_status.get("state") or "") != "exited"
                or supervisor_status.get("exit_code") != 0
            ):
                return {
                    "ok": False,
                    "request_id": request_id,
                    "task_id": task_id,
                    "error": "finalization_retry_worker_not_successful",
                }
            if not retryable_release_pending:
                transition = task_engine.retry_finalize_failed(
                    self.repo,
                    task_id,
                    runner,
                    request_id,
                    actor=core.CODEX_RUNNER,
                )
                if not transition.get("ok"):
                    return {
                        "ok": False,
                        "request_id": request_id,
                        "task_id": task_id,
                        "error": (
                            "finalization_retry_transition_failed:"
                            + str(
                                transition.get("stderr")
                                or transition.get("stdout")
                                or ""
                            )
                        )[:500],
                    }
            self._append_event({
                **self._event_identity(events),
                "request_id": request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": topic,
                "adapter_id": metadata.get("adapter_id"),
                "state": "finalizing",
                "metadata_path": str(metadata_path),
                "supervisor_status_path": metadata.get("supervisor_status_path"),
                "pid": latest.get("pid"),
                "pid_start_ticks": latest.get("pid_start_ticks"),
                "provider_process_alive": False,
                "finalization_retry": True,
                "finalization_retry_provider_launched": False,
                "finalization_started_at": _utcnow(),
            })

        event = self._finalize_isolated_request(request_id, 0)
        if event is None:
            return {
                "ok": False,
                "request_id": request_id,
                "task_id": task_id,
                "error": "finalization_retry_no_terminal_event",
            }
        return {
            "ok": str(event.get("state") or "") == "review_ready",
            "request_id": request_id,
            "task_id": task_id,
            "state": event.get("state"),
            "provider_relaunched": False,
            "workspace_retained": event.get("workspace_retained"),
            "error": event.get("error") or "",
        }

    def _candidate_reachability_inputs(
        self, workspace: Any, changed: Iterable[str]
    ) -> dict[str, Any] | None:
        """Best-effort reachability inputs from the candidate's Source Graph.

        Returns the ``changed_symbols``/``call_edges``/``reference_edges``/
        ``entry_points`` the reachability gate consumes, or ``None`` when the
        candidate index is unavailable so the gate records reachability as
        not-evaluated rather than fabricating a verdict.  Never raises:
        reachability is a non-blocking observation (NF-2026-00304) and must
        never break a promotion.
        """

        try:
            workspace_root = Path(workspace.path)
        except (AttributeError, TypeError, ValueError):
            return None
        changed_py = [
            str(rel).replace("\\", "/")
            for rel in (changed or ())
            if str(rel).replace("\\", "/").endswith(".py")
        ]
        if not changed_py:
            return None
        changed_symbols = _candidate_changed_symbols(workspace_root, changed_py)
        if not changed_symbols:
            return None
        try:
            from . import source_graph as _source_graph_mod

            db_path = Path(_source_graph_mod.resolve_db_path(workspace_root))
        except Exception:  # noqa: BLE001 -- reachability never breaks promotion
            return None
        try:
            if not db_path.is_file():
                return None
        except OSError:
            return None
        edges = _read_candidate_short_name_edges(db_path)
        if edges is None:
            return None
        call_edges, reference_edges = edges
        changed_names = {row["symbol"] for row in changed_symbols}
        entry_points = sorted(
            {
                edge["src"]
                for edge in (*call_edges, *reference_edges)
                if edge["src"] and edge["src"] not in changed_names
            }
        )
        return {
            "changed_symbols": changed_symbols,
            "call_edges": call_edges,
            "reference_edges": reference_edges,
            "entry_points": entry_points,
        }

    def _refuse_backwards_version_promotion(
        self, workspace: Any, changed: Iterable[str]
    ) -> dict[str, Any]:
        """Refuse a promotion that would move a version projection backwards.

        Runs at the promotion boundary (NF-2026-00315), BEFORE any file is
        written, comparing every recognised release-version file the promotion
        is about to write against the canonical value already on disk.  Equal is
        silent, ahead is allowed; a backwards (or unverifiable) value raises
        :class:`PromotionVersionRegression` naming the file and both versions.
        """

        projections = _promotion_version_projections(
            self.repo, Path(workspace.path), changed
        )
        return refuse_version_regression(projections)

    def _promote_accepted_candidate(
        self, workspace: Any, changed: list[str]
    ) -> list[str]:
        """Promote the sealed candidate, refusing a backwards version first.

        This is the sole promotion write seam in :meth:`accept_review`: the
        version-regression guard runs BEFORE ``promote`` writes a single byte,
        so a stale-base candidate carrying an older version constant is refused
        rather than promoted and then noticed by hand (NF-2026-00315).
        """

        self._refuse_backwards_version_promotion(workspace, changed)
        return promote(workspace, changed)

    def accept_review(
        self,
        request_id: str,
        task_id: str,
        *,
        confirm_destructive_change: bool = False,
        requested_risk_tier: str = quality_evidence.RISK_LOW,
        risk_signals: list[str] | None = None,
        reviewer_reports: list[dict[str, Any]] | None = None,
        reviewer_request_ids: list[str] | None = None,
        confirm_high_risk: bool = False,
    ) -> dict[str, Any]:
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

        ``requested_risk_tier`` and ``risk_signals`` are manager-owned inputs.
        Medium-and-higher profiles materialize a fresh combined-tree workspace
        and fail closed without the required read-only reviewer reports.
        High/critical profiles additionally require ``confirm_high_risk``.
        """
        # A no-change research/reviewer task never promotes repository bytes.
        # A bounded pre-read selects a per-request lock for that case so it
        # does not wait behind an unrelated writable promotion. Every identity
        # and evidence check is repeated below while the selected lock is held.
        readonly_lock_path = False
        try:
            pre_events = self._request_events(request_id)
            pre_latest = pre_events[-1] if pre_events else {}
            pre_card = _parse_card(self._show_task(task_id), task_id)
            pre_evidence = (pre_card.get("terminal_review") or {}).get("evidence") or {}
            readonly_lock_path = bool(
                str(pre_latest.get("task_id") or "") == task_id
                and _canonical_task_status(pre_card) == "review"
                and (
                    _card_is_readonly_quality_review(pre_card)
                    or _card_is_readonly_research(pre_card)
                )
                and pre_evidence.get("changed_paths") in ([], None)
            )
        except (LaunchRejected, AttributeError, TypeError):
            readonly_lock_path = False
        acceptance_lock = (
            self._request_lock(request_id)
            if readonly_lock_path
            else self._promotion_lock()
        )
        with acceptance_lock:
            if reviewer_reports:
                return {
                    "ok": False,
                    "error": "unverified_reviewer_reports_forbidden",
                    "request_id": request_id,
                    "task_id": task_id,
                }
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
            if deterministic:
                expected_epoch = str(card.get("claim_epoch") or 0)
                observed_epoch = str(deterministic.get("claim_epoch") or 0)
                if observed_epoch != expected_epoch:
                    return {
                        "ok": False,
                        "error": (
                            "deterministic_verification_claim_epoch_mismatch:"
                            f"expected={expected_epoch}:observed={observed_epoch}"
                        ),
                        "request_id": request_id,
                        "task_id": task_id,
                    }
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

            predecessor = card.get("rework_predecessor")
            residual_identities = (
                predecessor.get("residual_identities")
                if isinstance(predecessor, dict)
                else None
            )
            if residual_identities:
                residual_contract = evidence.get("residual_contract")
                if not isinstance(residual_contract, list) or not residual_contract:
                    return {
                        "ok": False,
                        "error": "residual_contract_evidence_missing",
                        "request_id": request_id,
                        "task_id": task_id,
                    }
                if any(
                    not isinstance(row, dict) or row.get("pass") is not True
                    for row in residual_contract
                ):
                    return {
                        "ok": False,
                        "error": "residual_contract_evidence_failed",
                        "request_id": request_id,
                        "task_id": task_id,
                    }

            intent_snapshot = self._context_write_intent_snapshot(request_id)
            if intent_snapshot.get("ok"):
                pending_intents = int((intent_snapshot.get("counts") or {}).get("pending") or 0)
                if pending_intents:
                    return {
                        "ok": False,
                        "error": f"context_write_intents_pending:{pending_intents}",
                        "request_id": request_id,
                        "task_id": task_id,
                        "pending_context_write_intents": [
                            {
                                "intent_id": row.get("intent_id"),
                                "component": row.get("component"),
                                "action": row.get("action"),
                            }
                            for row in intent_snapshot.get("intents") or []
                            if row.get("status") == "pending_manager_review"
                        ],
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
                assert_gc_safe_workspace_shape(
                    request_id, workspace.path, workspace.home, repo=self.repo
                )
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

            readonly_quality_review = _card_is_readonly_quality_review(card)
            readonly_research = (
                _card_is_readonly_research(card) and not readonly_quality_review
            )
            readonly_no_change = readonly_research or readonly_quality_review
            try:
                attempt_artifact_receipt = self._verify_attempt_artifact_receipt(
                    request_id,
                    evidence.get("attempt_artifact_manifest"),
                )
                terminal_evidence_record = evidence_levels.validate_evidence_record(
                    evidence.get("evidence_record")
                )
                minimum_evidence_level = self._minimum_acceptance_evidence_level(
                    card,
                    readonly_quality_review=readonly_quality_review,
                    readonly_research=readonly_research,
                )
                if not evidence_levels.meets_evidence_level(
                    terminal_evidence_record.evidence_level,
                    minimum_evidence_level,
                ):
                    raise WorkspaceError(
                        "evidence_level_below_minimum:"
                        f"observed={terminal_evidence_record}:"
                        f"required={minimum_evidence_level}"
                    )
                expected_reference = self._attempt_evidence_reference(
                    request_id,
                    attempt_artifact_receipt,
                )
                if terminal_evidence_record.reference != expected_reference:
                    raise WorkspaceError("evidence_record_reference_mismatch")
            except (
                WorkspaceError,
                evidence_levels.EvidenceValidationError,
                TypeError,
            ) as exc:
                return {
                    "ok": False,
                    "error": f"evidence_level_gate_failed:{exc}",
                    "request_id": request_id,
                    "task_id": task_id,
                }
            if readonly_lock_path and not readonly_no_change:
                return {
                    "ok": False,
                    "error": "review_lock_class_changed_retry",
                    "request_id": request_id,
                    "task_id": task_id,
                }
            stored_hashes = evidence.get("changed_path_hashes")
            if stored_hashes is None and readonly_no_change:
                stored_hashes = {}
            if not isinstance(stored_hashes, dict) or (
                not stored_hashes and not readonly_no_change
            ):
                return {
                    "ok": False,
                    "error": "evidence_changed_hashes_missing",
                    "request_id": request_id,
                    "task_id": task_id,
                }
            if readonly_no_change and stored_hashes:
                return {
                    "ok": False,
                    "error": (
                        "quality_review_changed_hashes_forbidden"
                        if readonly_quality_review
                        else "research_changed_hashes_forbidden"
                    ),
                    "request_id": request_id,
                    "task_id": task_id,
                }

            if not readonly_no_change:
                # Referential evidence is independently recomputed before the
                # write gate opens and before a single candidate byte is
                # promoted.  Stored prose cannot satisfy this gate.
                from . import evidence_instruments

                evidence_audit = evidence_instruments.review_evidence_audit(
                    self.repo,
                    workspace.path,
                    changed_paths=list(evidence.get("changed_paths") or []),
                    stored_hashes=stored_hashes,
                    required_outputs=list(evidence.get("required_outputs") or []),
                )
                if evidence_audit.get("blocking"):
                    return {
                        "ok": False,
                        "error": "review_evidence_audit_failed:"
                        + ",".join(evidence_audit.get("blockers") or [])[:420],
                        "request_id": request_id,
                        "task_id": task_id,
                        "review_evidence_audit": evidence_audit,
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

            if readonly_quality_review:
                try:
                    if enforce_scope(
                        workspace,
                        git_phase="review_acceptance",
                        git_timeout=_worker_workspace.finalization_git_timeout_seconds(),
                    ):
                        raise WorkspaceError("quality_review_workspace_mutated")
                    if evidence.get("changed_paths") not in ([], None):
                        raise WorkspaceError("quality_review_changed_paths_forbidden")
                    required_output_records = validate_required_outputs(
                        workspace,
                        card.get("required_outputs") or [],
                        allow_empty=(),
                        allow_unchanged=(),
                    )
                    metadata_path = self._metadata_from_events(events)
                    if metadata_path is None:
                        raise WorkspaceError("quality_review_metadata_missing")
                    if (
                        metadata_path.parent.resolve() != self.process_dir.resolve()
                        or metadata_path.is_symlink()
                        or not metadata_path.is_file()
                        or metadata_path.stat().st_size > 2 * 1024 * 1024
                    ):
                        raise WorkspaceError("quality_review_metadata_invalid")
                    try:
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise WorkspaceError("quality_review_metadata_unreadable") from exc
                    if (
                        str(metadata.get("request_id") or "") != request_id
                        or str(metadata.get("task_id") or "") != task_id
                        or str(metadata.get("runner") or "") != runner
                        or str(metadata.get("topic") or "") != topic
                    ):
                        raise WorkspaceError("quality_review_metadata_identity_mismatch")
                    try:
                        metadata_workspace = WorkerWorkspace.from_metadata(
                            dict(metadata["workspace"])
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        raise WorkspaceError("quality_review_workspace_invalid") from exc
                    if metadata_workspace.as_metadata() != workspace.as_metadata():
                        raise WorkspaceError("quality_review_workspace_identity_mismatch")
                    verified_receipt = _verified_quality_review_receipt(
                        metadata, workspace, request_id
                    )
                    stored_receipt = evidence.get("quality_review_receipt")
                    if not isinstance(stored_receipt, dict):
                        raise WorkspaceError("quality_review_receipt_missing")
                    if verified_receipt != stored_receipt:
                        raise WorkspaceError("quality_review_receipt_mismatch")
                    report = verified_receipt.get("report") or {}
                    if (
                        report.get("read_only") is not True
                        or report.get("can_mutate_repo") is not False
                    ):
                        raise WorkspaceError("quality_review_receipt_not_readonly")
                    validations = _run_declared_validations(
                        workspace, card, latest
                    )
                except GitCommandTimeout as exc:
                    return {
                        "ok": False,
                        "error": str(exc)[:500],
                        "request_id": request_id,
                        "task_id": task_id,
                    }
                except WorkspaceError as exc:
                    return {
                        "ok": False,
                        "error": f"revalidation_failed:{exc}",
                        "request_id": request_id,
                        "task_id": task_id,
                    }

                quality_gate = {
                    "schema_id": "aiworkhub.completion_quality_gate.v1",
                    "applicable": False,
                    "passed": None,
                    "reason": "quality_review_no_repository_change",
                    "changed_paths": [],
                    "checks": [],
                    "blocking_checks": [],
                }
                acceptance_evidence_record = self._canonical_outcome_evidence(
                    request_id,
                    attempt_artifact_receipt,
                    level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
                    verified_by=core.CODEX_RUNNER,
                    message=(
                        "Manager reverified and accepted the sealed quality-review outcome."
                    ),
                )
                accept_result = task_engine.accept_review(
                    self.repo,
                    task_id,
                    runner=runner,
                    topic=topic,
                    request_id=request_id,
                    evidence={
                        "promoted_paths": [],
                        "validation": validations,
                        "required_outputs": required_output_records,
                        "quality_gate": quality_gate,
                        "quality_review_receipt": verified_receipt,
                        "source_evidence_record": terminal_evidence_record.to_dict(),
                        "acceptance_evidence_record": acceptance_evidence_record,
                        "attempt_artifact_manifest": attempt_artifact_receipt,
                    },
                )
                if not accept_result.get("ok"):
                    return {
                        "ok": False,
                        "error": (
                            "quality_review_finalize_failed:"
                            + str(
                                accept_result.get("stderr")
                                or accept_result.get("stdout")
                                or ""
                            )
                        )[:500],
                        "request_id": request_id,
                        "task_id": task_id,
                        "promoted_paths": [],
                    }
                cleanup_error = ""
                try:
                    cleanup_workspace(workspace.repo, workspace.path, workspace.home)
                except WorkspaceError as exc:
                    cleanup_error = str(exc)[:500]
                self._append_event({
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": topic,
                    "adapter_id": latest.get("adapter_id"),
                    "state": "accepted",
                    "accepted": True,
                    "promoted_paths": [],
                    "workspace_retained": bool(cleanup_error),
                    "cleanup_error": cleanup_error,
                    "quality_review_receipt": verified_receipt,
                    "acceptance_evidence_record": acceptance_evidence_record,
                    "reviewer_finalization": [],
                    "acceptance_lock_scope": "request",
                    "finished_at": _utcnow(),
                })
                return {
                    "ok": True,
                    "request_id": request_id,
                    "task_id": task_id,
                    "promoted_paths": [],
                    "cleanup_error": cleanup_error,
                    "quality_review_receipt": verified_receipt,
                    "acceptance_evidence_record": acceptance_evidence_record,
                    "reviewer_finalization": [],
                    "acceptance_lock_scope": "request",
                }

            if readonly_research:
                try:
                    if enforce_scope(
                        workspace,
                        git_phase="review_acceptance",
                        git_timeout=_worker_workspace.finalization_git_timeout_seconds(),
                    ):
                        raise WorkspaceError("research_workspace_mutated")
                    if evidence.get("changed_paths") not in ([], None):
                        raise WorkspaceError("research_changed_paths_forbidden")
                    required_output_records = validate_required_outputs(
                        workspace,
                        card.get("required_outputs") or [],
                        allow_empty=(),
                        allow_unchanged=(),
                    )
                    stored_result = evidence.get("research_result")
                    if not isinstance(stored_result, dict):
                        raise WorkspaceError("research_result_evidence_missing")
                    stdout_raw = latest.get("stdout_path")
                    if not stdout_raw:
                        raise WorkspaceError("research_result_path_missing")
                    stdout_path = Path(str(stdout_raw))
                    try:
                        safe_parent = stdout_path.parent.resolve()
                        expected_parent = self.process_dir.resolve()
                    except OSError as exc:
                        raise WorkspaceError("research_result_path_invalid") from exc
                    if (
                        safe_parent != expected_parent
                        or stdout_path.name != f"{request_id}.stdout.log"
                    ):
                        raise WorkspaceError("research_result_path_identity_mismatch")
                    current_result = _readonly_research_result_evidence(stdout_path)
                    if not current_result.get("meaningful_output"):
                        raise WorkspaceError(
                            str(
                                current_result.get("reason")
                                or "research_result_missing"
                            )
                        )
                    identity_keys = (
                        "schema_id",
                        "meaningful_output",
                        "bytes",
                        "sha256",
                        "result_event_count",
                        "result_chars",
                    )
                    if any(
                        current_result.get(key) != stored_result.get(key)
                        for key in identity_keys
                    ):
                        raise WorkspaceError("research_result_evidence_mismatch")
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
                    validations = _run_declared_validations(
                        workspace, card, latest
                    )
                except GitCommandTimeout as exc:
                    return {
                        "ok": False,
                        "error": str(exc)[:500],
                        "request_id": request_id,
                        "task_id": task_id,
                    }
                except WorkspaceError as exc:
                    return {
                        "ok": False,
                        "error": f"revalidation_failed:{exc}",
                        "request_id": request_id,
                        "task_id": task_id,
                    }

                quality_gate = {
                    "schema_id": "aiworkhub.completion_quality_gate.v1",
                    "applicable": False,
                    "passed": None,
                    "reason": "research_no_repository_change",
                    "changed_paths": [],
                    "checks": [],
                    "blocking_checks": [],
                }
                acceptance_evidence_record = self._canonical_outcome_evidence(
                    request_id,
                    attempt_artifact_receipt,
                    level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
                    verified_by=core.CODEX_RUNNER,
                    message=(
                        "Manager reverified and accepted the sealed research outcome."
                    ),
                )
                accept_result = task_engine.accept_review(
                    self.repo,
                    task_id,
                    runner=runner,
                    topic=topic,
                    request_id=request_id,
                    evidence={
                        "promoted_paths": [],
                        "validation": validations,
                        "required_outputs": required_output_records,
                        "quality_gate": quality_gate,
                        "research_result": current_result,
                        "source_evidence_record": terminal_evidence_record.to_dict(),
                        "acceptance_evidence_record": acceptance_evidence_record,
                        "attempt_artifact_manifest": attempt_artifact_receipt,
                    },
                )
                if not accept_result.get("ok"):
                    return {
                        "ok": False,
                        "error": (
                            "research_finalize_failed:"
                            + str(
                                accept_result.get("stderr")
                                or accept_result.get("stdout")
                                or ""
                            )
                        )[:500],
                        "request_id": request_id,
                        "task_id": task_id,
                        "promoted_paths": [],
                    }
                cleanup_error = ""
                try:
                    cleanup_workspace(workspace.repo, workspace.path, workspace.home)
                except WorkspaceError as exc:
                    cleanup_error = str(exc)[:500]
                self._append_event({
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": topic,
                    "adapter_id": latest.get("adapter_id"),
                    "state": "accepted",
                    "accepted": True,
                    "promoted_paths": [],
                    "workspace_retained": bool(cleanup_error),
                    "cleanup_error": cleanup_error,
                    "research_result": current_result,
                    "acceptance_evidence_record": acceptance_evidence_record,
                    "reviewer_finalization": [],
                    "finished_at": _utcnow(),
                })
                return {
                    "ok": True,
                    "request_id": request_id,
                    "task_id": task_id,
                    "promoted_paths": [],
                    "cleanup_error": cleanup_error,
                    "research_result": current_result,
                    "acceptance_evidence_record": acceptance_evidence_record,
                    "reviewer_finalization": [],
                    "acceptance_lock_scope": "request",
                }

            try:
                changed = enforce_scope(
                    workspace,
                    git_phase="review_acceptance",
                    git_timeout=_worker_workspace.finalization_git_timeout_seconds(),
                )
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
                destructive_checks = quality_evidence.run_destructive_diff_checks(
                    self.repo,
                    workspace.path,
                    changed_paths=changed,
                )
                destructive_rows = [check.to_dict() for check in destructive_checks]
                destructive_blockers = [
                    check.check_id
                    for check in destructive_checks
                    if check.status == quality_evidence.STATUS_FAILED
                ]
                if destructive_blockers and not confirm_destructive_change:
                    raise WorkspaceError(
                        "destructive_diff_requires_manager_confirmation:"
                        + ",".join(destructive_blockers)[:300]
                    )
                effective_risk_signals = quality_evidence.derive_risk_signals(
                    card,
                    changed,
                    destructive_checks=destructive_checks,
                )
                effective_risk_signals.extend(risk_signals or [])
                effective_risk_signals = sorted(dict.fromkeys(effective_risk_signals))
                # A candidate that empties or removes its own quality policy must
                # not thereby weaken its own acceptance. The weakening is an
                # observed signal that escalates the tier (medium+ then demands
                # combined-tree and reviewer evidence); it does not silently pass.
                policy_authority = quality_evidence.assess_quality_policy_authority(
                    self.repo, workspace.path
                )
                if policy_authority["weakened"] and policy_authority["escalation_signal"]:
                    effective_risk_signals = sorted(
                        dict.fromkeys(
                            [
                                *effective_risk_signals,
                                str(policy_authority["escalation_signal"]),
                            ]
                        )
                    )
                risk_profile = quality_evidence.resolve_risk_profile(
                    requested_risk_tier,
                    signals=effective_risk_signals,
                )
                combined_tree: dict[str, Any] | None = None
                combined_tree_checks: list[dict[str, Any]] = []
                if risk_profile.get("combined_tree_required"):
                    union_workspace, combined_tree = create_combined_validation_workspace(
                        workspace,
                        card,
                        changed,
                    )
                    try:
                        union_validations = _run_declared_validations(
                            union_workspace, card, latest
                        )
                        union_quality = quality_evidence.run_completion_quality_gate(
                            union_workspace.path,
                            changed_paths=changed,
                            requested_risk_tier=requested_risk_tier,
                            risk_signals=effective_risk_signals,
                            combined_tree_scope=True,
                        )
                        if not union_quality.get("passed"):
                            union_blockers = union_quality.get("blocking_checks") or []
                            raise WorkspaceError(
                                "combined_tree_quality_failed:"
                                + ",".join(str(value) for value in union_blockers)[:300]
                            )
                        combined_tree_checks = [
                            {
                                "check_id": "combined-tree-materialized",
                                "kind": "requirements",
                                "status": quality_evidence.STATUS_PASSED,
                                "provenance": "current canonical tree plus exact candidate delta",
                            },
                            *[
                                {
                                    **dict(row),
                                    "check_id": "combined-tree:" + str(row.get("check_id") or "check"),
                                }
                                for row in union_quality.get("checks") or []
                            ],
                            *[
                                {
                                    "check_id": f"combined-tree:validation:{index}",
                                    "kind": "test",
                                    "status": quality_evidence.STATUS_PASSED,
                                    "provenance": str(row.get("command") or "validation")[:2000],
                                }
                                for index, row in enumerate(union_validations)
                            ],
                        ]
                        combined_tree["validation"] = union_validations
                        combined_tree["quality_gate"] = union_quality
                    finally:
                        cleanup_workspace(
                            union_workspace.repo,
                            union_workspace.path,
                            union_workspace.home,
                        )
                reviewer_ids = list(reviewer_request_ids or [])
                if len(reviewer_ids) > quality_evidence.MAX_REVIEW_REPORTS:
                    raise WorkspaceError("quality_reviewer_request_overflow")
                if len(set(reviewer_ids)) != len(reviewer_ids):
                    raise WorkspaceError("quality_reviewer_request_duplicate")
                verified_reviewer_reports: list[dict[str, Any]] = []
                verified_reviewer_tasks: list[
                    tuple[str, WorkerWorkspace | None, bool]
                ] = []
                for reviewer_request_id in reviewer_ids:
                    reviewer_events = self._request_events(reviewer_request_id)
                    if not reviewer_events:
                        raise WorkspaceError(
                            f"quality_reviewer_request_not_found:{reviewer_request_id}"
                        )
                    reviewer_latest = reviewer_events[-1]
                    reviewer_state = str(reviewer_latest.get("state") or "")
                    if reviewer_state == "accepted":
                        reviewer_task_id = str(reviewer_latest.get("task_id") or "")
                        try:
                            reviewer_card = _parse_card(
                                self._show_task(reviewer_task_id), reviewer_task_id
                            )
                            receipt = _verified_accepted_quality_review_receipt(
                                reviewer_latest,
                                reviewer_card,
                                reviewer_request_id,
                                request_id,
                                task_id,
                            )
                        except (LaunchRejected, WorkspaceError) as exc:
                            raise WorkspaceError(
                                f"quality_reviewer_accepted_invalid:{reviewer_request_id}:{exc}"
                            ) from exc
                        verified_reviewer_reports.append(dict(receipt["report"]))
                        verified_reviewer_tasks.append(
                            (reviewer_task_id, None, True)
                        )
                        continue
                    if reviewer_state != "review_ready":
                        raise WorkspaceError(
                            f"quality_reviewer_not_review_ready:{reviewer_request_id}"
                        )
                    reviewer_metadata_path = self._metadata_from_events(reviewer_events)
                    if reviewer_metadata_path is None:
                        raise WorkspaceError(
                            f"quality_reviewer_metadata_missing:{reviewer_request_id}"
                        )
                    if (
                        reviewer_metadata_path.parent.resolve()
                        != self.process_dir.resolve()
                        or reviewer_metadata_path.is_symlink()
                        or not reviewer_metadata_path.is_file()
                        or reviewer_metadata_path.stat().st_size > 2 * 1024 * 1024
                    ):
                        raise WorkspaceError(
                            f"quality_reviewer_metadata_invalid:{reviewer_request_id}"
                        )
                    try:
                        reviewer_metadata = json.loads(
                            reviewer_metadata_path.read_text(encoding="utf-8")
                        )
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise WorkspaceError(
                            f"quality_reviewer_metadata_unreadable:{reviewer_request_id}"
                        ) from exc
                    reviewer_binding = reviewer_metadata.get("quality_review") or {}
                    if (
                        str(reviewer_binding.get("target_request_id") or "") != request_id
                        or str(reviewer_binding.get("target_task_id") or "") != task_id
                    ):
                        raise WorkspaceError(
                            f"quality_reviewer_target_mismatch:{reviewer_request_id}"
                        )
                    reviewer_workspace = WorkerWorkspace.from_metadata(
                        dict(reviewer_metadata["workspace"])
                    )
                    if enforce_scope(
                        reviewer_workspace,
                        git_phase="review_acceptance",
                        git_timeout=_worker_workspace.finalization_git_timeout_seconds(),
                    ):
                        raise WorkspaceError(
                            f"quality_reviewer_workspace_mutated:{reviewer_request_id}"
                        )
                    receipt = _verified_quality_review_receipt(
                        reviewer_metadata,
                        reviewer_workspace,
                        reviewer_request_id,
                    )
                    verified_reviewer_reports.append(dict(receipt["report"]))
                    verified_reviewer_tasks.append(
                        (
                            str(reviewer_metadata.get("task_id") or ""),
                            reviewer_workspace,
                            False,
                        )
                    )
                quality_gate = quality_evidence.run_completion_quality_gate(
                    workspace.path,
                    changed_paths=changed,
                    requested_risk_tier=requested_risk_tier,
                    risk_signals=effective_risk_signals,
                    reviewer_reports=verified_reviewer_reports,
                    combined_tree_checks=combined_tree_checks,
                    worker_provider=str(latest.get("adapter_id") or runner),
                    human_approval=confirm_high_risk,
                    reachability_inputs=self._candidate_reachability_inputs(
                        workspace, changed
                    ),
                )
                quality_gate["combined_tree"] = combined_tree
                quality_gate["quality_policy_authority"] = policy_authority
                if not quality_gate.get("passed"):
                    blockers = quality_gate.get("blocking_checks") or []
                    reason = quality_gate.get("config_error") or ",".join(str(v) for v in blockers)
                    raise WorkspaceError("quality_gate_failed:" + str(reason)[:400])
                quality_gate["destructive_diff_checks"] = destructive_rows
                quality_gate["destructive_diff_blockers"] = destructive_blockers
                quality_gate["destructive_change_confirmed"] = bool(
                    confirm_destructive_change and destructive_blockers
                )
                validations = _run_declared_validations(
                    workspace, card, latest
                )
                _enforce_behavioral_gate(card, validations, quality_gate)
                current_hashes = _changed_path_hashes(workspace, changed)
                if set(current_hashes) != set(stored_hashes) or any(
                    current_hashes[relative] != stored_hashes.get(relative)
                    for relative in current_hashes
                ):
                    raise WorkspaceError("stored_hash_mismatch")
            except GitCommandTimeout as exc:
                return {
                    "ok": False,
                    "error": str(exc)[:500],
                    "request_id": request_id,
                    "task_id": task_id,
                }
            except WorkspaceError as exc:
                return {
                    "ok": False,
                    "error": f"revalidation_failed:{exc}",
                    "request_id": request_id,
                    "task_id": task_id,
                }

            promoted = self._promote_accepted_candidate(workspace, changed)

            acceptance_evidence_record = self._canonical_outcome_evidence(
                request_id,
                attempt_artifact_receipt,
                level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
                verified_by=core.CODEX_RUNNER,
                message=(
                    "Manager revalidated, promoted, and accepted the exact sealed candidate."
                ),
            )

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
                    "source_evidence_record": terminal_evidence_record.to_dict(),
                    "acceptance_evidence_record": acceptance_evidence_record,
                    "attempt_artifact_manifest": attempt_artifact_receipt,
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

            verified_reviewer_ids = [
                tid for tid, _ws, _accepted in verified_reviewer_tasks
            ]
            disposition_result = task_engine.disposition_reviewer_children(
                self.repo,
                task_id,
                verified_reviewer_task_ids=verified_reviewer_ids,
                parent_request_id=request_id,
                disposition="accepted",
            )
            try:
                disposition_payload = json.loads(
                    str(disposition_result.get("stdout") or "{}")
                )
            except (TypeError, json.JSONDecodeError):
                disposition_payload = {}
            finalized_set = set(disposition_payload.get("finalized") or [])
            reviewer_finalization: list[dict[str, Any]] = []
            for reviewer_task_id, reviewer_workspace, already_accepted in verified_reviewer_tasks:
                row = {
                    "task_id": reviewer_task_id,
                    "finished": already_accepted or reviewer_task_id in finalized_set,
                    "cleanup_error": "",
                }
                if reviewer_workspace is None:
                    reviewer_finalization.append(row)
                    continue
                try:
                    cleanup_workspace(
                        reviewer_workspace.repo,
                        reviewer_workspace.path,
                        reviewer_workspace.home,
                    )
                except WorkspaceError as exc:
                    row["cleanup_error"] = str(exc)[:300]
                reviewer_finalization.append(row)

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
                    "reviewer_finalization": reviewer_finalization,
                    "acceptance_evidence_record": acceptance_evidence_record,
                    "finished_at": _utcnow(),
                })
                return {
                    "ok": True,
                    "request_id": request_id,
                    "task_id": task_id,
                    "promoted_paths": promoted,
                    "cleanup_error": str(exc)[:500],
                    "reviewer_finalization": reviewer_finalization,
                    "acceptance_evidence_record": acceptance_evidence_record,
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
                "reviewer_finalization": reviewer_finalization,
                "acceptance_evidence_record": acceptance_evidence_record,
                "finished_at": _utcnow(),
            })
            return {
                "ok": True,
                "request_id": request_id,
                "task_id": task_id,
                "promoted_paths": promoted,
                "reviewer_finalization": reviewer_finalization,
                "acceptance_evidence_record": acceptance_evidence_record,
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


# Liveness is one function, imported -- never a private copy. The launcher's
# old POSIX branch read EPERM as DEAD, so a worker under another uid was
# declared dead and terminalized while its Windows branch read access-denied as
# ALIVE. ``platform_io.process_is_alive`` gives every entry point the one honest
# answer (EPERM == alive). Kept bound to ``_pid_alive`` so callers and tests
# that reference the launcher's name resolve to the shared implementation.
_pid_alive = process_is_alive


def _pid_start_ticks(pid: int) -> int | None:
    """Read a stable process creation timestamp to guard against PID reuse.

    Cross-platform process identity lives in exactly one place --
    :func:`runtime_temp.process_start_ticks` -- so the launcher, the standalone
    supervisor, and the temp-owner GC can never disagree about what identifies
    a process.  See that function for the per-platform source and units.  On a
    platform that genuinely cannot supply a creation time this returns None;
    every caller here treats None as "identity unknown" and fails closed.
    """
    return runtime_temp.process_start_ticks(pid)


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


class PidIdentityVerdict(Enum):
    """Truthful result of an exact PID plus creation-time identity probe."""

    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PidIdentityEvidence:
    """Immutable evidence captured at the process-identity boundary."""

    verdict: PidIdentityVerdict
    pid: int | None
    expected_start_ticks: int | None
    observed_start_ticks: int | None
    attempts: int
    operation: str
    winerror: int | None = None
    exception: str = ""


_PID_IDENTITY_MAX_ATTEMPTS = 3
_PID_IDENTITY_RETRY_DELAY_SECONDS = 0.01
_WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WINDOWS_ERROR_INVALID_PARAMETER = 87


def _windows_pid_identity_once(
    pid: int,
    expected_start_ticks: int,
    *,
    attempt: int,
) -> PidIdentityEvidence:
    """Perform one Windows identity probe and capture failure provenance."""

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    try:
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        getattr(ctypes, "set_last_error")(0)
        handle = kernel32.OpenProcess(
            _WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror is None:
            winerror = int(getattr(ctypes, "get_last_error")()) or None
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.UNKNOWN,
            pid=pid,
            expected_start_ticks=expected_start_ticks,
            observed_start_ticks=None,
            attempts=attempt,
            operation="OpenProcess",
            winerror=winerror,
            exception=type(exc).__name__,
        )

    if not handle:
        winerror = int(getattr(ctypes, "get_last_error")()) or None
        absent = winerror == _WINDOWS_ERROR_INVALID_PARAMETER
        return PidIdentityEvidence(
            verdict=(
                PidIdentityVerdict.MISMATCH
                if absent
                else PidIdentityVerdict.UNKNOWN
            ),
            pid=pid,
            expected_start_ticks=expected_start_ticks,
            observed_start_ticks=None,
            attempts=attempt,
            operation="OpenProcess",
            winerror=winerror,
            exception="ProcessAbsent" if absent else "OpenProcessFailed",
        )

    creation = _FileTime()
    exit_time = _FileTime()
    kernel = _FileTime()
    user = _FileTime()
    try:
        try:
            getattr(ctypes, "set_last_error")(0)
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if winerror is None:
                winerror = int(getattr(ctypes, "get_last_error")()) or None
            return PidIdentityEvidence(
                verdict=PidIdentityVerdict.UNKNOWN,
                pid=pid,
                expected_start_ticks=expected_start_ticks,
                observed_start_ticks=None,
                attempts=attempt,
                operation="GetProcessTimes",
                winerror=winerror,
                exception=type(exc).__name__,
            )
        if not ok:
            winerror = int(getattr(ctypes, "get_last_error")()) or None
            return PidIdentityEvidence(
                verdict=PidIdentityVerdict.UNKNOWN,
                pid=pid,
                expected_start_ticks=expected_start_ticks,
                observed_start_ticks=None,
                attempts=attempt,
                operation="GetProcessTimes",
                winerror=winerror,
                exception="GetProcessTimesFailed",
            )
        observed = (int(creation.high) << 32) | int(creation.low)
        return PidIdentityEvidence(
            verdict=(
                PidIdentityVerdict.MATCH
                if observed == expected_start_ticks
                else PidIdentityVerdict.MISMATCH
            ),
            pid=pid,
            expected_start_ticks=expected_start_ticks,
            observed_start_ticks=observed,
            attempts=attempt,
            operation="GetProcessTimes",
        )
    finally:
        kernel32.CloseHandle(handle)


def _pid_identity_evidence(pid: Any, expected_start_ticks: Any) -> PidIdentityEvidence:
    """Return bounded, fail-closed PID identity evidence on every platform."""

    try:
        numeric_pid = int(pid or 0)
    except (TypeError, ValueError) as exc:
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.UNKNOWN,
            pid=None,
            expected_start_ticks=None,
            observed_start_ticks=None,
            attempts=0,
            operation="parse_pid",
            exception=type(exc).__name__,
        )
    if numeric_pid <= 0:
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.MISMATCH,
            pid=numeric_pid,
            expected_start_ticks=None,
            observed_start_ticks=None,
            attempts=0,
            operation="pid_absent",
            exception="NonPositivePid",
        )
    if expected_start_ticks in (None, ""):
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.UNKNOWN,
            pid=numeric_pid,
            expected_start_ticks=None,
            observed_start_ticks=None,
            attempts=0,
            operation="parse_expected_start_ticks",
            exception="ExpectedStartTicksMissing",
        )
    try:
        expected = int(expected_start_ticks)
    except (TypeError, ValueError) as exc:
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.UNKNOWN,
            pid=numeric_pid,
            expected_start_ticks=None,
            observed_start_ticks=None,
            attempts=0,
            operation="parse_expected_start_ticks",
            exception=type(exc).__name__,
        )

    if os.name == "nt":
        evidence: PidIdentityEvidence | None = None
        for attempt in range(1, _PID_IDENTITY_MAX_ATTEMPTS + 1):
            evidence = _windows_pid_identity_once(
                numeric_pid,
                expected,
                attempt=attempt,
            )
            if evidence.verdict is not PidIdentityVerdict.UNKNOWN:
                return evidence
            if attempt < _PID_IDENTITY_MAX_ATTEMPTS:
                time.sleep(_PID_IDENTITY_RETRY_DELAY_SECONDS)
        assert evidence is not None
        return evidence

    try:
        os.kill(numeric_pid, 0)
    except ProcessLookupError as exc:
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.MISMATCH,
            pid=numeric_pid,
            expected_start_ticks=expected,
            observed_start_ticks=None,
            attempts=1,
            operation="kill_zero",
            exception=type(exc).__name__,
        )
    except PermissionError as exc:
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.UNKNOWN,
            pid=numeric_pid,
            expected_start_ticks=expected,
            observed_start_ticks=None,
            attempts=1,
            operation="kill_zero",
            exception=type(exc).__name__,
        )
    observed = _pid_start_ticks(numeric_pid)
    if observed is None:
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.UNKNOWN,
            pid=numeric_pid,
            expected_start_ticks=expected,
            observed_start_ticks=None,
            attempts=1,
            operation="_pid_start_ticks",
            exception="StartTicksUnavailable",
        )
    return PidIdentityEvidence(
        verdict=(
            PidIdentityVerdict.MATCH
            if observed == expected
            else PidIdentityVerdict.MISMATCH
        ),
        pid=numeric_pid,
        expected_start_ticks=expected,
        observed_start_ticks=observed,
        attempts=1,
        operation="_pid_start_ticks",
    )


def _identity_verified_pid(pid: Any, ticks: Any) -> int:
    """Return ``pid`` only when its recorded creation timestamp still matches.

    ``_pid_matches`` deliberately reports a match when no start ticks were
    recorded, because bare liveness is good enough for *reporting*.  It is
    never good enough for *termination*: a pid alone is not an identity once
    the OS has recycled it.

    The blast radius is what makes this platform-specific.  On Linux
    ``_terminate_process_group`` calls ``os.killpg``, which fails closed with
    ``ProcessLookupError`` unless the pid really is a process-group leader --
    and workers get their own session via ``start_new_session=True``.  On
    Windows there is no ``killpg``, so the same call becomes
    ``taskkill /PID <pid> /T``, which terminates the pid *and every
    descendant*.  Handed a recycled pid, that silently destroys an unrelated
    process tree -- for example a VS Code extension host and the children it
    owns.  Requiring the creation timestamp closes that hole.
    """

    evidence = _pid_identity_evidence(pid, ticks)
    return (
        int(evidence.pid)
        if evidence.verdict is PidIdentityVerdict.MATCH
        and evidence.pid is not None
        else 0
    )


def _canonical_task_status(card: dict[str, Any]) -> str:
    """Return lifecycle status while preserving archived as a distinct gate."""
    if str(card.get("archived_at") or "").strip():
        return "archived"
    return core._lifecycle_state(card)


def _process_proven_dead(pid: int, ticks: Any) -> bool:
    """Fail closed: a live PID with unknown start ticks is not proven dead."""
    evidence = _pid_identity_evidence(pid, ticks)
    return evidence.verdict is PidIdentityVerdict.MISMATCH


def _process_group_alive(pgid: int) -> bool:
    """True while ANY member of the POSIX process group still exists.

    ``_terminate_process_group`` signals the whole group (``killpg``); verifying
    only the leader let a child outlive its already-dead leader and satisfy the
    early-return, so the SIGKILL escalation never ran -- a narrow check gating a
    wide action. ``killpg(pgid, 0)`` probes the group itself: ``ESRCH`` means the
    group is empty (all members gone), ``EPERM`` means a member exists we may not
    signal, and any other ambiguity fails closed to alive so escalation still
    runs.
    """
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _terminate_process_group(pid: int, grace_seconds: float) -> None:
    if os.name == "nt":
        # Windows has no killpg(). taskkill /T addresses the exact process
        # tree created for the worker without involving a command shell.
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
        )
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.05)
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
        )
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    # Verify the GROUP we signalled, not just the leader: a surviving child
    # keeps the group alive and must still force the SIGKILL escalation below.
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _process_group_alive(pid):
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
