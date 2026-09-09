"""Local, explicitly gated model-process launcher for AIWorkHub task workers.

The authoritative task queue is the selected repository's canonical
``.aiworkhub/tasking/task_queue.sqlite`` task store. This module only owns
process lifecycle evidence: start, observe, collect, timeout, and cancel. It
never selects a task by keywords and it never invokes a shell.
"""

from __future__ import annotations

import ast
import ctypes
import difflib
import fnmatch
from functools import partial
import hashlib
import hmac
import html
import inspect
import json
import math
import os
import re
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
from typing import Any, Callable, Iterable, Mapping, cast

from . import core
from . import agent_tool_instructions
from . import attempt_artifacts
from . import claude_auth
from . import context_write_intents
from . import context_writes
from . import evidence_levels
from . import kilo_auth
from . import learning_commit
from . import needfix_store
from .platform_io import (
    AdvisoryLockTimeout,
    available_memory_bytes as _available_memory_bytes,
    chmod_fd,
    chmod_path,
    is_windows,
    lock_fd,
    probe_process_group,
    process_group_launch_kwargs,
    process_is_alive,
    stat_owned_by_current_user,
    terminate_process_tree,
    unlock_fd,
)
from . import quality_evidence
from . import quality_review_ingest
from . import quality_review_scope
from . import process_event_ledger
from . import storage_retention, terminal_failure_classification
from .process_launcher_acceptance import accepted_outcome_receipt as _accepted_outcome_receipt
from .process_launcher_acceptance import changed_path_hashes as _changed_path_hashes
from .process_launcher_acceptance import finished_acceptance_result as _finished_acceptance_result
from .process_launcher_accept_review import accept_preview as _accept_preview_impl
from .process_launcher_accept_review import accept_review as _accept_review_impl
from .process_launcher_launch_isolated import launch_isolated as _launch_isolated_impl
from .launch_replay_guard import (
    CARD_CONTENT_IDENTITY_KEYS,
    IDENTICAL_RELAUNCH_BLOCKED_REASON,
    TASK_CONTRACT_KEYS,
    TERMINAL_ERROR_HASH_HEX_CHARS,
    bounded_error_hash,
    card_content_identity,
    identical_relaunch_refusal,
    validation_only_replay_authorization,
    review_feedback_identity,
    strip_persistence_envelopes as _strip_persistence_envelopes,
)
from .launch_zero_delta import (
    RUNTIME_NOTICE_EVENT_KIND,
    ZERO_DELTA_DEFAULT_ELAPSED_SHARE,
    ZERO_DELTA_ELAPSED_SHARE_ENV,
    ZERO_DELTA_MAX_SECONDS,
    ZERO_DELTA_MIN_SECONDS,
    ZERO_DELTA_NOTICE,
    ZERO_DELTA_POLL_SECONDS,
    ZERO_DELTA_TERMINAL_REASON,
    ZeroDeltaTripwire,
    changed_allowed_write_paths,
    evaluate_zero_delta_tripwire,
    zero_delta_elapsed_share,
    zero_delta_notice_after_seconds,
)
from .process_launcher_evidence import (
    DELTA_RETAINING_TERMINAL_STATES,
    is_rework_attempt as _is_rework_attempt,
    path_manifest as _path_manifest,
    retained_candidate_identity_evidence as _retained_candidate_identity_evidence,
    retained_rework_candidate_evidence,
)
from . import process_launcher_validation as _launcher_validation
from .process_launcher_read_efficiency import (
    _provider_read_efficiency_from_output,
    _strict_read_command_event,
)
from . import provider_usage
from . import repo_policy
from . import review_orchestrator
from . import runtime_temp
from . import task_engine
from . import task_fsm
from . import task_store
from . import task_templates
from . import toolchain_authority as _toolchain_authority
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
# The receipt schema surface moved to ``quality_review_receipt`` unchanged.
# Re-exported under the original names so every existing reader -- production
# call sites and the tests that pin this contract -- keeps resolving the exact
# same objects from ``process_launcher``.
from .quality_review_receipt import (
    _QUALITY_REVIEW_AUTHORITY_KEYS,
    _QUALITY_REVIEW_FINDING_RECEIPT_REQUIRED_KEYS,
    _QUALITY_REVIEW_RECEIPT_TOP_KEYS,
    _QUALITY_REVIEW_REPORT_KEYS,
    _QUALITY_REVIEW_REVIEWER_KEYS,
    _QUALITY_REVIEW_TARGET_KEYS,
    _SHA256_HEX_RE,
    _enforce_quality_review_receipt_schema,
    _is_sha256_hex,
    _verified_quality_review_receipt,
)
from . import quality_reviewer
from . import reviewer_reservation_recovery
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
    worker_validation_affordance_env,
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

def _fallback_validate_required_outputs(
    workspace: WorkerWorkspace,
    required_outputs: list[str] | tuple[str, ...],
    allow_empty: tuple[str, ...] | None = None,
    allow_unchanged: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Validate the explicit mandatory-change set, never the full write scope.

    Bound as the module-level ``validate_required_outputs`` only when
    ``worker_workspace`` (AppContainer/self-hosting degraded environments
    included) does not already provide one; always callable directly for
    deterministic regression coverage regardless of which branch bound it.

    ``required_outputs`` is expected to already be the narrow,
    explicitly-declared mandatory subset (see
    ``task_templates.expand_template``'s ``mandatory_changed_outputs``), not
    every authorized production/test path. Every entry is checked
    regardless of earlier mismatches so a caller sees the complete picture
    in one pass: missing artifacts, unchanged mandatory outputs and scope
    violations (symlink/non-file/zero-byte/parent-baseline) are collected
    into distinct, named diagnostic buckets alongside the primary
    validation result (the records that passed), instead of failing closed
    on the first mismatch and hiding the rest.
    """
    unchanged_allowed = {str(v).strip().replace("\\", "/") for v in (allow_unchanged or [])}
    records: list[dict[str, Any]] = []
    missing_required_artifacts: list[str] = []
    unchanged_mandatory_outputs: list[str] = []
    scope_violations: list[dict[str, str]] = []
    legacy_error_codes: list[str] = []
    for raw in required_outputs:
        pattern = str(raw or "").strip().replace("\\", "/")
        if not pattern:
            raise WorkspaceError("required_output_invalid")
        matches = sorted(workspace.path.glob(pattern))
        if not matches:
            missing_required_artifacts.append(pattern)
            code = (
                "required_output_no_matches"
                if any(ch in pattern for ch in "*?[")
                else "required_output_missing"
            )
            legacy_error_codes.append(f"{code}:{pattern}")
            continue
        for path in matches:
            relative = path.relative_to(workspace.path).as_posix()
            if path.is_symlink():
                scope_violations.append({"path": relative, "reason": "symlink"})
                legacy_error_codes.append(f"required_output_symlink:{relative}")
                continue
            if not path.is_file():
                scope_violations.append({"path": relative, "reason": "non_file"})
                legacy_error_codes.append(f"required_output_missing:{relative}")
                continue
            size = path.stat().st_size
            if size <= 0 and (allow_empty is None or relative not in allow_empty):
                scope_violations.append({"path": relative, "reason": "zero_bytes"})
                legacy_error_codes.append(f"required_output_zero_bytes:{relative}")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            baseline = workspace.workspace_baseline.get(relative)
            current = f"file:{path.stat().st_mode & 0o777:o}:{digest}"
            is_unchanged = baseline in {digest, current}
            if is_unchanged:
                if relative not in unchanged_allowed:
                    unchanged_mandatory_outputs.append(relative)
                    legacy_error_codes.append(f"required_output_unchanged:{relative}")
                    continue
                if workspace.parent_baseline.get(relative) != current:
                    scope_violations.append(
                        {"path": relative, "reason": "unchanged_parent_mismatch"}
                    )
                    legacy_error_codes.append(
                        f"required_output_unchanged_parent_mismatch:{relative}"
                    )
                    continue
                if size <= 0:
                    unchanged_mandatory_outputs.append(relative)
                    continue
            records.append({
                "path": relative,
                "bytes": size,
                "sha256": current,
                "unchanged_allowed": is_unchanged,
            })
    if missing_required_artifacts or unchanged_mandatory_outputs or scope_violations:
        diagnostics = {
            "missing_required_artifacts": missing_required_artifacts,
            "unchanged_mandatory_outputs": unchanged_mandatory_outputs,
            "scope_violations": scope_violations,
            "primary_validation_result": records,
            "legacy_error_codes": legacy_error_codes,
        }
        raise WorkspaceError(
            "required_output_mismatch:"
            + json.dumps(diagnostics, sort_keys=True, separators=(",", ":"))
        )
    return records


if hasattr(_worker_workspace, "validate_required_outputs"):
    validate_required_outputs = _worker_workspace.validate_required_outputs
else:
    validate_required_outputs = _fallback_validate_required_outputs
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
# Repository-local, non-durable runtime tree: .aiworkhub/runtime/process_logs/.
PROCESS_LOG_DEFAULT_REL = Path(".aiworkhub/runtime/process_logs/process_events.jsonl")
PROCESS_DIR_DEFAULT_REL = Path(".aiworkhub/runtime/process_logs/processes")
DEFAULT_MAX_PROCESSES = 4
MAX_CONFIGURED_PROCESSES = 32
MAX_LOG_TAIL_BYTES = 64 * 1024
MAX_RECEIPT_SCAN_BYTES = 2 * 1024 * 1024
MAX_RESEARCH_RESULT_BYTES = 32 * 1024 * 1024
MAX_WORKER_STREAM_LOG_BYTES = 4 * 1024 * 1024
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
REVIEWER_CLAIM_BOUND_STATE = "reviewer_claim_bound"
_LAUNCH_RESERVATION_ADMISSION_RECOVERY = object()
WORKER_BRIDGE_AUTHORIZED_PROCESS_STATES = {"starting", "running"}
_PERSISTED_WATCH_UNKNOWN_MAX_CONSECUTIVE = 3
FINALIZATION_PENDING_STATES = {
    "finalizing", "release_pending", "review_pending", "reconcile_pending",
}
# The terminal process states the launcher can emit. Owned by ``task_fsm``
# (``LAUNCHER_TERMINAL_SUBSTATUSES``, a named subset of the single terminal
# vocabulary); imported here rather than restated (NF-2026-00339).
TERMINAL_PROCESS_STATES = task_fsm.LAUNCHER_TERMINAL_SUBSTATUSES


def _parse_durable_pid(value: Any) -> tuple[int, bool]:
    """Return a positive durable PID, preserving malformed authority.

    The boolean is true when a persisted value is ambiguous rather than a
    trustworthy absent (zero/null) identity.  Every durable-identity caller
    must fail closed on that signal instead of raising or treating it as dead.
    """

    if value is None or value == "":
        return 0, False
    if isinstance(value, bool):
        return 0, True
    try:
        pid = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0, True
    if pid < 0:
        return 0, True
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return 0, True
    return pid, False


def _reservation_deadline_is_live(value: Any) -> bool:
    """Fail closed unless a finite positive reservation lease has expired."""

    try:
        deadline = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return True
    return not math.isfinite(deadline) or deadline <= 0.0 or deadline > time.time()


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


# Finalizer retry-exhaustion classification lives in
# ``terminal_failure_classification`` -- the module that owns what a terminal
# attempt MEANS -- and is re-exported here under its original private names so
# every existing reader resolves the exact same objects.
_FINALIZER_TRANSIENT_EXCEPTIONS = terminal_failure_classification.FINALIZER_TRANSIENT_EXCEPTIONS
FINALIZER_TRANSIENT_DEFERRAL_BUDGET = terminal_failure_classification.FINALIZER_TRANSIENT_DEFERRAL_BUDGET
FINALIZER_TRANSIENT_DEFERRAL_REASON = terminal_failure_classification.FINALIZER_TRANSIENT_DEFERRAL_REASON
_finalizer_attempt_is_transient = terminal_failure_classification.finalizer_attempt_is_transient


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
    sandbox_backend: str | None = None,
) -> dict[str, str]:
    """Build the sanitized worker env, then route TMPDIR/TMP/TEMP at the exact
    request-owned repository-local temp authority (NF430).

    Both real ProcessManager launch paths -- the isolated supervisor spawn and
    the direct native launch -- call this before spawning a child, so a
    worker-run pytest/tempfile lands in
    ``<repo>/.aiworkhub/temp/worker/<request_id>/tmp`` (provisioned 0700 and
    owner-stamped here) rather than the shared system temp or inside the
    candidate worktree.  The sanitized allowlist, the request-scoped HOME, and
    the explicit BYOK provider env are exactly as ``sanitized_env`` built
    them; the three temp keys are overlaid from the single declaration in
    ``runtime_adapters.WORKER_TEMP_ENV_VARS``, and the read-only validation
    affordances (canonical interpreter and tool paths spelled for
    ``sandbox_backend``, tool caches routed into the worker temp) are added by
    ``worker_validation_affordance_env``.
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
    env.update(
        worker_validation_affordance_env(
            repo,
            env[runtime_adapters.WORKER_TEMP_ENV_VARS[0]],
            sandbox_backend=sandbox_backend,
        )
    )
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
    return _launcher_validation.validation_route_kwargs(
        metadata, _sandbox_backend_for_adapter
    )


_declared_validation_commands = _launcher_validation.declared_validation_commands
_requires_bridge_cancellation = _launcher_validation.requires_bridge_cancellation
_MYPY_DIAGNOSTIC_RE = _launcher_validation.MYPY_DIAGNOSTIC_RE
_exact_schema_mypy_invocation = _launcher_validation.exact_schema_mypy_invocation
_schema_mypy_diagnostics = _launcher_validation.schema_mypy_diagnostics
_baseline_validation_identity = _launcher_validation.baseline_validation_identity
_diagnostic_multiset_digest = _launcher_validation.diagnostic_multiset_digest


def _compare_schema_mypy_baseline(
    workspace: WorkerWorkspace,
    authority: Mapping[str, Any],
    route_metadata: Mapping[str, Any],
    candidate: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return _launcher_validation.compare_schema_mypy_baseline(
        workspace,
        authority,
        route_metadata,
        candidate,
        create_workspace=create_workspace,
        cleanup_workspace=cleanup_workspace,
        run_validations=partial(
            _run_validations_with_toolchain_receipt, authority=authority),
        route_resolver=_validation_route_kwargs,
    )


def _run_declared_validations(
    workspace: WorkerWorkspace,
    authority: Mapping[str, Any],
    route_metadata: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return _launcher_validation.run_declared_validations(
        workspace,
        authority,
        route_metadata,
        run_validations=partial(
            _run_validations_with_toolchain_receipt, authority=authority),
        route_resolver=_validation_route_kwargs,
        baseline_comparer=_compare_schema_mypy_baseline,
    )


def _run_validations_with_toolchain_receipt(
    target: WorkerWorkspace,
    commands: Iterable[str],
    authority: Mapping[str, Any],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    receipt = authority.get(_toolchain_authority.RECEIPT_CARD_KEY)
    if isinstance(receipt, Mapping):
        kwargs.setdefault("toolchain_authority_receipt", receipt)
        kwargs.setdefault("toolchain_authority_card", authority)
    commands = tuple(commands)
    from . import validation_runner as _validation_runner

    def _source_text(relative: str) -> str | None:
        path = target.path / relative
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    needed = any(
        _validation_runner.command_needs_multiprocessing_semlock(
            command, source_text=_source_text
        )
        for command in commands
        if isinstance(command, str)
    )
    probe: dict[str, Any] | None = None
    sandbox_measured = False
    if needed:
        inner = _validation_runner.trusted_semlock_probe_argv(sys.executable)
        backend = kwargs.get("backend")
        selected = backend if isinstance(backend, str) and backend else None
        try:
            argv = sandbox_argv(
                target, str(kwargs.get("adapter_id") or ""), inner, backend=selected
            )
            sandbox_measured = True
        except WorkspaceError:
            argv = inner
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=30, check=False
            )
        except OSError:
            completed = None
        if completed is not None:
            probe = _validation_runner.decode_trusted_semlock_exit(completed.returncode)
        if probe is None:
            command = next((item for item in commands if isinstance(item, str)), "")
            raise ValidationRunError(
                "semlock_capability_probe_harness_failed",
                [
                    {
                        "command": command,
                        "returncode": (
                            None if completed is None else completed.returncode
                        ),
                    }
                ],
            )
    if probe is not None:
        decision = _validation_runner.preflight_semlock_capability(
            commands,
            backend=str(kwargs.get("backend") or ""),
            probe=probe,
            source_text=_source_text,
            sandbox_measured=sandbox_measured,
        )
        if decision.action == "unsupported":
            command = next((item for item in commands if isinstance(item, str)), "")
            raise ValidationEnvironmentBlocked(
                decision.evidence,
                [
                    _validation_runner.attributed_semlock_capability_row(
                        command, probe
                    )
                ],
                restriction=_validation_runner.VALIDATION_UNSUPPORTED_IN_SANDBOX,
            )
    return run_validations(target, commands, **kwargs)


def _run_full_snapshot_validations(
    workspace: WorkerWorkspace, authority: Mapping[str, Any],
    route_metadata: Mapping[str, Any], candidate_changed_paths: Iterable[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return _launcher_validation.run_full_snapshot_validations(
        workspace, authority, route_metadata, candidate_changed_paths,
        create_snapshot=create_combined_validation_workspace,
        cleanup_workspace=cleanup_workspace,
        run_validations=partial(
            _run_validations_with_toolchain_receipt, authority=authority),
        route_resolver=_validation_route_kwargs,
        baseline_comparer=_compare_schema_mypy_baseline,
    )


_enforce_behavioral_gate = _launcher_validation.enforce_behavioral_gate
_is_operational_validation_failure = (
    _launcher_validation.is_operational_validation_failure
)
_VALIDATION_ENVIRONMENT_RESTRICTION_PREFIXES = (
    _launcher_validation.VALIDATION_ENVIRONMENT_RESTRICTION_PREFIXES
)


def _terminal_state_for_workspace_error(exc: WorkspaceError) -> str:
    if str(exc).startswith("validation_unsupported_in_sandbox"):
        return "finalize_failed"
    return _launcher_validation.terminal_state_for_workspace_error(exc)

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
    if not is_windows():
        if not stat_owned_by_current_user(st):
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


# NF-2026-00557 measured the resident extension/MCP/model baseline and launch
# transient. Keep 4 GiB for that host baseline and budget 2 GiB for one new
# heavy agent. These are admission headroom, not a replacement concurrency cap.
MEMORY_RESERVED_HEADROOM_BYTES = 4 * 1024**3
MEMORY_PER_LAUNCH_ESTIMATE_BYTES = 2 * 1024**3
MEMORY_LAUNCH_REQUIRED_BYTES = (
    MEMORY_RESERVED_HEADROOM_BYTES + MEMORY_PER_LAUNCH_ESTIMATE_BYTES
)


def _memory_launch_admission() -> dict[str, Any]:
    available = _available_memory_bytes()
    admitted = available is not None and available >= MEMORY_LAUNCH_REQUIRED_BYTES
    return {
        "admit": admitted,
        "retryable": True,
        "available_bytes": available,
        "required_bytes": MEMORY_LAUNCH_REQUIRED_BYTES,
        "reserved_headroom_bytes": MEMORY_RESERVED_HEADROOM_BYTES,
        "per_launch_estimate_bytes": MEMORY_PER_LAUNCH_ESTIMATE_BYTES,
        "reason": (
            "memory_capacity_available"
            if admitted
            else (
                "memory_probe_unavailable"
                if available is None
                else "memory_capacity_insufficient"
            )
        ),
    }


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
        # Runtime notices (NF-2026-00548) ride the same ledger but are not
        # process rows; merging one here would move the Live Output row's
        # timestamp without any lifecycle change behind it.
        if str(event.get("event_kind") or "") == RUNTIME_NOTICE_EVENT_KIND:
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
    try:
        return validation_only_replay_authorization(card, task_id)
    except ValueError as exc:
        raise LaunchRejected(str(exc)) from None


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


SEMANTIC_EDIT_COVERAGE_SCHEMA_ID = "aiworkhub.semantic_edit_coverage.v1"

# The three exceptions the repository's semantic-edit rule actually allows.
# Kept as data so a declaration carrying anything else is reported as an
# unknown code rather than silently accepted.
SEMANTIC_EDIT_POLICY_EXCEPTIONS = (
    "new_file",
    "spans_most_of_file",
    "adapter_without_tools",
)
_COVERAGE_LIST_CAP = 200


def semantic_edit_path_identifier(relative: str) -> str:
    """The join key shared with the authenticated worker ledger.

    ``sha256`` of the repo-relative POSIX path text -- the identical rule
    ``worker_ai_tools_mcp.verify_audit_ledger`` applies to an apply receipt's
    private ``path``.  Both sides hash the same normalized string, so the
    finalizer can join an apply to a changed path without the ledger ever
    carrying path text.
    """
    text = str(relative or "").strip().replace("\\", "/")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _semantic_edit_coverage(
    changed_paths: Iterable[str],
    *,
    workspace: Any = None,
    worker_mcp_gate: dict[str, Any] | None = None,
    granted_tool_names: Iterable[str] = (),
    runtime_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-attempt semantic-edit coverage over the candidate's changed paths.

    THIS IS A MEASUREMENT, NOT A GATE.  Nothing in acceptance, promotion, the
    quality ratchet or the review decision reads this record; it exists so the
    question "is semantic edit actually mandatory, and does everyone use it?"
    has a durable numeric answer instead of an assertion.  This repository's
    own rule is never to bound what it cannot yet measure, so the measurement
    comes first and any threshold is a separate, later decision made against
    the distribution this record produces.

    Byte labels stay byte labels.  ``coverage_ratio`` is a ratio of BYTES of
    changed files, never of provider tokens and never of money;
    ``token_savings_claimed`` stays False for the same reason it is False on
    the apply receipt itself.

    Absence of evidence is never reported as zero coverage.  An unverifiable
    ledger, an attempt that changed nothing, or receipts written before the
    path identifier existed all resolve to ``measured: False`` with a named
    ``unmeasured_reason`` -- never to ``coverage_ratio: 0.0``.
    """

    record: dict[str, Any] = {
        "schema_id": SEMANTIC_EDIT_COVERAGE_SCHEMA_ID,
        "measured": False,
        "unmeasured_reason": "",
        "changed_paths_count": 0,
        "eligible_paths_count": 0,
        "paths_with_apply": 0,
        "paths_raw_only": [],
        "paths_raw_only_count": 0,
        "paths_new_file": 0,
        "paths_deleted": 0,
        "paths_baseline_unknown": 0,
        "bytes_basis": "changed_file_size_at_finalization",
        "bytes_changed": 0,
        "bytes_via_apply": 0,
        "coverage_ratio": None,
        "declared_exceptions": [],
        "declared_exception_count": 0,
        "derived_exceptions": [],
        "derived_exception_count": 0,
        "undeclared_raw_only": [],
        "undeclared_raw_only_count": 0,
        "apply_receipts_total": 0,
        "apply_receipts_joined": 0,
        "apply_receipts_unjoinable": 0,
        "adapter_semantic_edit_granted": None,
        "token_savings_claimed": False,
        "measurement_only": True,
    }

    changed = sorted({
        str(item).strip().replace("\\", "/")
        for item in (changed_paths or [])
        if str(item).strip()
    })
    record["changed_paths_count"] = len(changed)

    verification = (
        worker_mcp_gate.get("verification")
        if isinstance(worker_mcp_gate, dict) else None
    )
    if not isinstance(verification, dict) or verification.get("ok") is not True:
        # The ledger is the only authenticated statement about tool use.  An
        # adapter that reported nothing, or a ledger that could not be
        # verified, is UNMEASURED -- it is not a worker that used nothing.
        record["unmeasured_reason"] = "ledger_unverified"
        return record
    if not changed:
        record["unmeasured_reason"] = "no_changed_paths"
        return record

    receipts = verification.get("semantic_edit_apply_receipts")
    receipts = [row for row in receipts[:128] if isinstance(row, dict)] if isinstance(
        receipts, list
    ) else []
    record["apply_receipts_total"] = len(receipts)
    applied_digests = {
        str(row.get("path_sha256") or "") for row in receipts
    } - {""}
    if receipts and not applied_digests:
        # Every retained receipt predates the path identifier: the applies are
        # real but cannot be joined to a path, so coverage is unknown rather
        # than zero.
        record["unmeasured_reason"] = "receipts_without_path_identifier"
        return record
    if not receipts and isinstance(runtime_evidence, dict) and runtime_evidence.get(
        "observed"
    ) is True:
        # The authenticated ledger holds no apply, yet the local runtime bridge
        # observed semantic edits for this attempt -- the vscode_lm route
        # reports through ``semantic_edit_metrics`` on its own result stream,
        # not through the worker MCP ledger.  Counting that as 0% would be
        # exactly the "absence of evidence read as zero" defect this record
        # exists to avoid, and folding an unauthenticated stream into the
        # numerator would weaken the ledger.  So: unmeasured, and named.
        record["unmeasured_reason"] = "applies_observed_outside_the_authenticated_ledger"
        return record

    granted = [str(name) for name in (granted_tool_names or [])]
    semantic_edit_granted: bool | None = None
    if granted:
        semantic_edit_granted = any(
            name.endswith("semantic_edit_apply") for name in granted
        )
    record["adapter_semantic_edit_granted"] = semantic_edit_granted

    baselines: dict[str, str | None] = {}
    tree_baseline: dict[str, str | None] | None = None
    workspace_root: Path | None = None
    if workspace is not None:
        baselines = dict(getattr(workspace, "workspace_baseline", None) or {})
        raw_tree = getattr(workspace, "tree_baseline", None)
        tree_baseline = dict(raw_tree) if isinstance(raw_tree, dict) else None
        root = getattr(workspace, "path", None)
        workspace_root = Path(str(root)) if root else None

    def baseline_state(relative: str) -> str:
        """``present`` / ``absent`` / ``unknown`` at workspace creation."""
        if relative in baselines:
            return "present" if baselines[relative] is not None else "absent"
        if tree_baseline is not None:
            # The tree manifest covers EVERY file in the worktree, so absence
            # from it is decisive: the path did not exist before this attempt.
            return "present" if tree_baseline.get(relative) is not None else "absent"
        return "unknown"

    def path_bytes(relative: str) -> tuple[int, bool]:
        if workspace_root is None:
            return 0, False
        candidate = workspace_root / relative
        try:
            if candidate.is_symlink() or not candidate.is_file():
                return 0, False
            return max(0, candidate.stat().st_size), True
        except OSError:
            return 0, False

    declarations_by_digest: dict[str, dict[str, Any]] = {}
    raw_declarations = verification.get("semantic_edit_exception_declarations")
    if isinstance(raw_declarations, list):
        for row in raw_declarations[:128]:
            if not isinstance(row, dict):
                continue
            digest = str(row.get("path_sha256") or "")
            if digest:
                declarations_by_digest[digest] = row

    raw_only: list[str] = []
    new_files: list[str] = []
    derived: list[dict[str, Any]] = []
    declared: list[dict[str, Any]] = []
    joined_digests: set[str] = set()
    bytes_changed = 0
    bytes_via_apply = 0
    eligible = 0

    for relative in changed:
        digest = semantic_edit_path_identifier(relative)
        has_apply = digest in applied_digests
        if has_apply:
            joined_digests.add(digest)
        size, exists = path_bytes(relative)
        state = baseline_state(relative)
        if state == "unknown":
            record["paths_baseline_unknown"] += 1
        if not exists:
            # A removed path was never "written raw": there is no replacement
            # text to have gone through the editor, so it is outside the
            # denominator rather than an uncovered edit.
            record["paths_deleted"] += 1
            continue
        if state == "absent":
            new_files.append(relative)
            derived.append({
                "path": relative,
                "exception": "new_file",
                "basis": "no_baseline_hash_at_workspace_creation",
                "source": "runtime_derivation",
            })
            continue
        eligible += 1
        bytes_changed += size
        if has_apply:
            record["paths_with_apply"] += 1
            bytes_via_apply += size
            continue
        raw_only.append(relative)
        declaration = declarations_by_digest.get(digest)
        covered_by_derivation = False
        if semantic_edit_granted is False:
            derived.append({
                "path": relative,
                "exception": "adapter_without_tools",
                "basis": "semantic_edit_apply_absent_from_granted_tool_names",
                "source": "runtime_derivation",
            })
            covered_by_derivation = True
        if declaration is not None:
            code = str(declaration.get("exception") or "")
            if code not in SEMANTIC_EDIT_POLICY_EXCEPTIONS:
                corroboration = "unknown_exception_code"
                corroborated = False
            elif code == "new_file":
                corroborated = False
                corroboration = "baseline_hash_present_for_path"
            elif code == "adapter_without_tools":
                corroborated = semantic_edit_granted is False
                corroboration = (
                    "granted_tool_names"
                    if semantic_edit_granted is not None
                    else "granted_tool_names_unknown"
                )
            else:
                # "spans most of a file" is the genuinely judgemental case: the
                # runtime never saw the raw write, so it holds no preimage to
                # measure the span against.  The worker's word is recorded and
                # explicitly marked uncorroborated rather than dressed up as a
                # derivation.
                corroborated = False
                corroboration = "no_byte_evidence_for_a_raw_write"
            declared.append({
                "path": relative,
                "exception": code,
                "reason": str(declaration.get("reason") or "")[:200],
                "source": "worker_declaration",
                "corroborated": bool(corroborated),
                "corroboration": corroboration,
            })
        elif not covered_by_derivation:
            record["undeclared_raw_only"].append(relative)

    record["eligible_paths_count"] = eligible
    record["paths_raw_only"] = raw_only[:_COVERAGE_LIST_CAP]
    record["paths_raw_only_count"] = len(raw_only)
    record["paths_new_file"] = len(new_files)
    record["bytes_changed"] = bytes_changed
    record["bytes_via_apply"] = bytes_via_apply
    record["coverage_ratio"] = (
        round(bytes_via_apply / bytes_changed, 4) if bytes_changed > 0 else None
    )
    record["declared_exception_count"] = len(declared)
    record["declared_exceptions"] = declared[:_COVERAGE_LIST_CAP]
    record["derived_exception_count"] = len(derived)
    record["derived_exceptions"] = derived[:_COVERAGE_LIST_CAP]
    record["undeclared_raw_only_count"] = len(record["undeclared_raw_only"])
    record["undeclared_raw_only"] = record["undeclared_raw_only"][:_COVERAGE_LIST_CAP]
    record["apply_receipts_joined"] = len(joined_digests)
    record["apply_receipts_unjoinable"] = max(
        0, len(applied_digests) - len(joined_digests)
    )
    record["measured"] = True
    return record


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
    """Return only provider-authenticated assistant-output payloads.

    The worker prompt contains the complete acknowledgement template.  Raw
    stdout therefore has no acknowledgement authority: a provider that echoes
    its input would otherwise replay that template verbatim.  Supported JSONL
    adapters bind assistant text to a typed output envelope at the
    process/adapter boundary.
    """
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, dict):
        return []

    candidates: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    item = event.get("item")
    if (
        isinstance(item, dict)
        and str(item.get("type") or "") == "agent_message"
    ):
        add(item.get("text"))  # Codex JSONL assistant output

    data = event.get("data")
    if (
        isinstance(data, dict)
        and str(event.get("type") or "") == "assistant.message"
    ):
        add(data.get("content"))  # DeepSeek/Copilot assistant output

    message = event.get("message")
    if (
        isinstance(message, dict)
        and str(message.get("role") or "") == "assistant"
    ):
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
    expected_request_id: str = "",
) -> dict[str, Any]:
    result = {
        "schema_id": project_context.RECEIPT_SCHEMA_ID,
        "acknowledged": False,
        "bundle_sha256": "",
        "prompt_sha256": "",
        "request_id": "",
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
    expected_request = expected_request_id.strip()
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
            receipt_request = str(value.get("request_id") or "").strip()
            section_raw = value.get("section_count") or 0
            section_count = int(section_raw) if str(section_raw).isdigit() else 0
            valid_sha = len(bundle_sha) == 64 and all(ch in "0123456789abcdef" for ch in bundle_sha)
            matches = not expected or bundle_sha == expected
            request_matches = not expected_request or receipt_request == expected_request
            acknowledged = (
                value.get("acknowledged") is True
                and valid_sha
                and matches
                and request_matches
                and section_count > 0
            )
            reason = str(value.get("reason") or "")[:160]
            if not valid_sha:
                reason = "receipt_bundle_sha256_invalid"
            elif not matches:
                reason = "receipt_bundle_sha256_mismatch"
            elif not request_matches:
                reason = "receipt_request_id_mismatch"
            elif section_count <= 0:
                reason = "receipt_section_count_invalid"
            return {
                "schema_id": project_context.RECEIPT_SCHEMA_ID,
                "acknowledged": acknowledged,
                "bundle_sha256": bundle_sha[:80],
                "prompt_sha256": str(value.get("prompt_sha256") or "")[:80],
                "request_id": receipt_request[:160],
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
            "error_code": error_code,
            "session_id": str(
                event.get("session_id")
                or event.get("sessionId")
                or event.get("provider_session_id")
                or ""
            ),
        }
    return None


# Provider-owned envelope shapes that name the MODEL as the thing that does not
# exist for this account.  Kept exact: a bare 400 is usually the caller's
# payload, so a status alone never qualifies.
_MODEL_REJECTION_STATUSES: frozenset[int] = frozenset({400, 404})
_MODEL_REJECTION_ERROR_TYPES: frozenset[str] = frozenset({
    "invalid_request_error", "not_found_error", "invalid_model",
})
_MODEL_REJECTION_ERROR_CODES: frozenset[str] = frozenset({
    "model_not_found", "model_not_supported",
    "unknown_model", "model_not_available",
})


def _provider_model_rejection_from_output(
    path: Path, requested_model: str
) -> dict[str, Any] | None:
    """Return a sealed record when the provider refused the ROUTE, not the work.

    NF-2026-00655 measured 103 launches on ``codex_cli``/``gpt-5.4`` returning
    0 accepts and 0 rejects, 13 of them byte-identical: 725 bytes of stdout and
    143 of stderr carrying

        {"type":"error","status":400,"error":{"type":"invalid_request_error",
         "message":"The 'gpt-5.4' model is not supported when using Codex with
         a ChatGPT account."}}

    ``_provider_auth_failure_from_output`` does not see it -- the envelope is a
    top-level ``error`` object rather than a ``result``/``system`` event, and
    400 is not a refusal status -- so it collapsed into
    ``worker_failed:supervisor_state=exited:exit_code=1`` and killed the CARD
    while leaving the route ready for the next 102 launches.

    Two facts must both hold before this is called a route failure, and
    together they make it unspoofable by model prose: the envelope must be the
    provider's own error object with a model-shaped error type or code, and its
    message must name the exact model THIS launch pinned.  A worker that echoes
    someone else's error text cannot satisfy the second, and a genuine bad
    request about anything other than the model cannot satisfy the first.
    """

    model = str(requested_model or "").strip()
    if not model:
        return None
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
            outer = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(outer, dict):
            continue
        # THE ENVELOPE IS NESTED, AND READING ONLY THE OUTER LINE MATCHED
        # NOTHING.  The Codex CLI forwards the upstream provider's own error
        # body as a QUOTED JSON STRING inside ``message``, so the outer line
        # carries no ``status`` and no ``error`` object at all.  Replayed
        # against the 13 byte-identical 725-byte ``gpt-5.4`` logs this seal was
        # written for, the outer-line-only scan returned ``None`` on every one
        # of them: ``status: 400``, ``error.type: invalid_request_error`` and
        # the message naming the pinned model all live in the nested body.
        #
        # The anti-forgery anchor is unchanged.  A candidate must still be a
        # provider ``error`` envelope with a model-shaped type or code, and its
        # message must still name the exact model THIS launch pinned, so worker
        # prose still cannot mint a route failure.  The unwrapping is shared
        # with ``terminal_failure_classification`` so the boundary detector and
        # the disposition classifier can never disagree about what the provider
        # sent (R4/NF-2026-00646).
        #
        # THE MESSAGE IS UNWRAPPED ONLY FOR A PROVIDER-OWNED ENVELOPE.  An
        # ``assistant`` line's ``message`` is the model's own content, so
        # unwrapping one would let a worker mint this seal by printing the body
        # -- ``test_worker_prose_cannot_forge_a_route_failure`` states exactly
        # that case, and an unrestricted unwrap regressed it.
        nested: tuple[dict[str, Any], ...] = ()
        if (
            str(outer.get("type") or "").strip().lower()
            in terminal_failure_classification.PROVIDER_OWNED_MESSAGE_TYPES
        ):
            nested = tuple(
                terminal_failure_classification.embedded_provider_objects(
                    outer.get("message")
                )
            )
        for event in (outer, *nested):
            if str(event.get("type") or "").strip().lower() != "error":
                continue
            raw_status = event.get("status", event.get("error_status"))
            status = (
                raw_status
                if isinstance(raw_status, int) and not isinstance(raw_status, bool)
                else 0
            )
            if status not in _MODEL_REJECTION_STATUSES:
                continue
            body = event.get("error")
            if not isinstance(body, dict):
                continue
            error_type = str(body.get("type") or "").strip().lower()
            error_code = str(body.get("code") or "").strip().lower()
            if (
                error_type not in _MODEL_REJECTION_ERROR_TYPES
                and error_code not in _MODEL_REJECTION_ERROR_CODES
            ):
                continue
            message = str(body.get("message") or "")
            names_model = error_code in _MODEL_REJECTION_ERROR_CODES or model in message
            if not names_model:
                continue
            sealed = {
                "schema_id": "aiworkhub.provider_route_error.v1",
                "owner": "provider",
                "sealed": True,
                "code": (
                    error_code
                    if error_code in _MODEL_REJECTION_ERROR_CODES
                    else "model_not_supported"
                ),
                "http_status": status,
                "model": model,
                "detail": message[:300],
            }
            return {
                "schema_id": "aiworkhub.provider_launch_failure.v1",
                "reason": f"provider_route_model_unavailable:model={model}",
                "refusal_kind": "model_not_found",
                "recoverable": False,
                "http_status": status,
                "error_code": str(sealed["code"]),
                "session_id": "",
                "provider_error": sealed,
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


def _executed_context_sections(context: dict[str, Any]) -> set[str]:
    """Gateable sections the launcher executed for this request without
    degradation.

    ``hit_count`` is deliberately NOT required to be > 0: an executed section
    that returned zero rows (e.g. AI Memory with no matches) is a valid, real
    result, never a "missing call" (B948/B951 regression). A degraded / stale /
    failed section is not credited, so a live recovery call is still required.
    """
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
    return satisfied


def _coordinator_measured_zero_hit_sections(context: dict[str, Any]) -> set[str]:
    """Gateable sections the launcher executed, without degradation, whose
    canonical query measured exactly zero rows for this request.

    That zero is the coordinator's own measurement, taken before the worker
    started; no model text and no live re-call can change it.
    """
    measured: set[str] = set()
    for section in context.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = str(section.get("name") or "")
        if name not in _GATEABLE_CONTEXT_SECTIONS or not section.get("executed"):
            continue
        if str(section.get("degraded_reason") or "").strip():
            continue
        hit_count = section.get("hit_count")
        if type(hit_count) is int and hit_count == 0:
            measured.add(name)
    return measured


def _coordinator_bound_context_sections(metadata: dict[str, Any]) -> set[str] | None:
    """Sections the coordinator itself proves it injected into this request.

    The launcher recorded, at launch, the collected bundle's ``bundle_sha256``
    (``project_context``) and a delivery receipt naming the sha of the bundle
    it actually wrote into the prompt (``project_context_delivery``). When the
    delivery says ``injected`` and both shas agree, the bundle reached the
    worker by construction; the caller additionally requires the request's
    HMAC audit ledger to verify, which binds this request id. ``None`` means
    the launch record does not prove injection, so nothing is credited here.
    """
    context = metadata.get("project_context") or {}
    delivery = metadata.get("project_context_delivery") or {}
    if not isinstance(context, dict) or not isinstance(delivery, dict):
        return None
    bundle_sha256 = str(context.get("bundle_sha256") or "").strip().lower()
    delivered_sha256 = str(delivery.get("bundle_sha256") or "").strip().lower()
    if (
        delivery.get("injected") is not True
        or len(bundle_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in bundle_sha256)
        or delivered_sha256 != bundle_sha256
    ):
        return None
    return _executed_context_sections(context)


def _injected_context_satisfaction(
    metadata: dict[str, Any],
    request_id: str = "",
) -> tuple[bool, set[str]]:
    """Which required tools a VERIFIED injected project-context section already
    satisfies according to the worker-typed ``PROJECT_CONTEXT_RECEIPT`` line.

    Injection and a live worker call are ALTERNATIVE valid satisfaction sources
    for the same required tool -- the launcher already ran Session Manager / AI
    Memory / KB / Source Graph and injected their results with a hash receipt,
    so a worker need not re-run them by hand.

    This is the optional telemetry path: the primary acknowledgement is derived
    server-side by :func:`_coordinator_bound_context_sections` plus a verified
    audit ledger (``coordinator_prompt_binding``), because the receipt line
    only ever copied a sha the coordinator itself wrote into the prompt.  A
    receipt is credited here only when its ``bundle_sha256`` equals the stored
    one -- that sha binds repository/scope identity (the bundle embeds
    ``repo_identity.scope_root``) -- so a tampered, repo-mismatched, or
    unacknowledged receipt yields ``(False, set())`` from this path.  Sections
    are credited by :func:`_executed_context_sections`.
    """
    context = metadata.get("project_context") or {}
    if not isinstance(context, dict):
        return False, set()
    bundle_sha256 = str(context.get("bundle_sha256") or "").strip()
    stdout_path = str(metadata.get("stdout_path") or "").strip()
    if not bundle_sha256 or not stdout_path:
        return False, set()
    # Preserve the pre-existing bundle-bound acknowledgement contract for
    # Session Manager, AI Memory, and KB. Request binding is evaluated
    # separately at the Source Graph substitution boundary below.
    receipt = _project_context_receipt_from_output(
        Path(stdout_path),
        expected_bundle_sha256=bundle_sha256,
    )
    if not receipt.get("acknowledged"):
        return False, set()
    return True, _executed_context_sections(context)


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
        if card.get("read_only") is True and not (card.get("allowed_writes") or []):
            return
        # Writable templates distinguish authorized scope from an explicitly
        # empty mandatory-change set. Accept that only when canonical template
        # provenance authenticates the exact expanded contract; arbitrary
        # writable cards cannot smuggle an accidental empty list to launch.
        try:
            provenance = task_templates.validate_template_provenance(
                card.get("template_provenance")
            )
            expected_digest = task_templates.expanded_contract_digest(card)
        except task_templates.TaskTemplateError as exc:
            raise LaunchRejected("required_outputs_invalid") from exc
        if provenance["expanded_contract_digest"] != expected_digest:
            raise LaunchRejected("required_outputs_invalid")
        return
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


def adapter_identity_tuple(runner: str) -> tuple[str, ...]:
    """The adapters a runner family may use, in canonical preference order.

    This is the pure function ``_validate_adapter_identity`` has always been:
    the runner PREFIX decides the tuple, nothing else is consulted, and the
    order is the documented preference order (editor-owned bridge first where
    one exists). An empty tuple means the family constrains nothing, and every
    adapter stays acceptable for it -- exactly the ``return`` fall-through the
    validator has always taken.

    Extracted so the launch path can DERIVE an adapter from the card instead of
    requiring the caller to retype one the server already owns, without any
    caller gaining a second, divergent copy of the table.
    """
    if runner == core.CODEX_RUNNER:
        return ()
    if runner.startswith("claude_"):
        return ("vscode_lm", "claude_cli")
    if runner.startswith("codex_"):
        return ("vscode_lm", "codex_cli")
    if runner.startswith("deepseek_"):
        # Prefer the editor-owned VS Code Language Model API authorization.
        # BYOK and manual modes remain explicit compatibility fallbacks.
        return ("vscode_lm", "deepseek_vscode_lm", "deepseek_copilot_cli", "deepseek_manual")
    if runner.startswith("glm_"):
        # Prefer the credential-free VS Code Language Model API bridge.  Keep
        # the explicit BYOK adapter as a backwards-compatible fallback.
        return ("vscode_lm", "glm_vscode_lm", "glm_copilot_cli")
    if runner.startswith("copilot_"):
        # Copilot-owned models are a distinct workforce from first-party
        # Claude Code/Codex subscriptions. They may use only the editor's
        # public VS Code Language Model API bridge.
        return ("vscode_lm",)
    return ()


def _validate_adapter_identity(runner: str, adapter_id: str) -> None:
    if runner == core.CODEX_RUNNER:
        raise LaunchRejected("coordinator_runner_cannot_launch_worker")
    allowed = adapter_identity_tuple(runner)
    if not allowed:
        return
    if adapter_id not in allowed:
        raise LaunchRejected(
            f"runner_adapter_mismatch:runner={runner}:expected={'|'.join(allowed)}:got={adapter_id}"
        )


# ---------------------------------------------------------------------------
# NF-2026-00460: canonical workforce identity table.
#
# The only (runner, adapter_id) pairs this process may resolve to a provider
# reservation/spawn, and the exact canonical model each pair owns. A model is
# never inferred from a display name or editor inventory -- it must equal
# this table's ``model`` (after normalizing only a documented alias below)
# exactly, or the launch is rejected before any provider credential is
# touched and before core.claim_start_exact runs.
_WORKFORCE_MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4.5",
}

_CANONICAL_WORKFORCE: dict[tuple[str, str], dict[str, Any]] = {
    # NF-2026-00549 slice B: every enabled claude-family catalog worker must
    # resolve a pinned-model route here; a catalog-enabled runner absent from
    # this table dies at launch with workforce_route_absent (measured live on
    # claude_opus-5 and claude_haiku, 2026-09-01).
    ("claude_opus-5", "claude_cli"): {
        "model": "claude-opus-5",
        "enabled": True,
        "available": True,
        "risk_tiers": frozenset({"low", "medium", "high", "critical"}),
    },
    ("claude_haiku", "claude_cli"): {
        "model": "claude-haiku-4.5",
        "enabled": True,
        "available": True,
        "risk_tiers": frozenset({"low", "medium"}),
    },
    ("claude_sonnet-5", "claude_cli"): {
        "model": "claude-sonnet-5",
        "enabled": True,
        "available": True,
        "risk_tiers": frozenset({"low", "medium", "high", "critical"}),
    },
    ("claude_sonnet-5", "vscode_lm"): {
        "model": "claude-sonnet-5",
        "enabled": True,
        "available": True,
        "risk_tiers": frozenset({"low", "medium", "high", "critical"}),
    },
    ("claude_haiku-4.5", "claude_cli"): {
        "model": "claude-haiku-4.5",
        "enabled": True,
        "available": True,
        "risk_tiers": frozenset({"low", "medium"}),
    },
    ("claude_haiku-4.5", "vscode_lm"): {
        "model": "claude-haiku-4.5",
        "enabled": True,
        "available": True,
        "risk_tiers": frozenset({"low", "medium"}),
    },
    ("codex_gpt-5.5", "codex_cli"): {
        "model": "gpt-5.5",
        "enabled": True,
        "available": True,
        "risk_tiers": frozenset({"low", "medium", "high", "critical"}),
    },
}


def _normalize_workforce_model(model: str | None) -> str:
    stripped = str(model or "").strip()
    return _WORKFORCE_MODEL_ALIASES.get(stripped, stripped)


def _launch_route_refusal(repo: Path, runner: str, adapter_id: str, model: str) -> str:
    """Return a typed refusal for an extinguished route, or "" to allow it.

    The refusal is worded by ``workforce_catalog``, which owns both the circuit
    and the identity vocabulary it has to quote; this module only decides that
    a launch is what asked.
    """
    from . import workforce_catalog  # local import: cycle-safe (core -> launcher)

    return workforce_catalog.launch_route_refusal_reason(
        repo, runner, adapter_id, model
    )

def validate_workforce_identity(
    runner: str,
    adapter_id: str,
    model: str | None,
    *,
    risk_tier: str | None = None,
    repo: Path | str | None = None,
) -> str | None:
    """Validate an exact, explicitly-pinned (runner, adapter_id, model) tuple
    against the canonical workforce before any provider reservation/spawn.

    Scoped to canonical table rows and to the ``claude_*`` runner family this
    repository already governed. Other non-table runner families keep their
    existing, unchanged identity handling. A launch that does not pin a
    specific model is likewise unaffected: this only guards an explicit, exact
    model pin, never one inferred from a display name or editor inventory.
    Normalizes only a documented workforce alias
    (``_WORKFORCE_MODEL_ALIASES``). Returns the canonical model name (or the
    original ``model`` when this validation does not apply). Raises
    ``LaunchRejected`` with a typed reason for any pinned runner/adapter/model
    combination that does not resolve to exactly one canonical workforce row,
    or for a disabled, unavailable, or risk-incapable route.

    ``repo`` supplies the repository whose retained route evidence decides the
    failure circuit.  It is the launch paths that pass it, because only they
    are about to spend a provider reservation; the pure identity assertions in
    this module's unit surface pass none and keep their exact prior meaning.
    """
    _validate_adapter_identity(runner, adapter_id)
    if repo is not None and str(model or "").strip():
        refusal = _launch_route_refusal(
            Path(repo), runner, adapter_id, str(model).strip()
        )
        if refusal:
            raise LaunchRejected(refusal)
    route = _CANONICAL_WORKFORCE.get((runner, adapter_id))
    if route is None and not runner.startswith("claude_"):
        return model
    if model is None or not str(model).strip():
        return model
    if route is None:
        raise LaunchRejected(
            f"workforce_route_absent:runner={runner}:adapter={adapter_id}"
        )
    canonical_model = _normalize_workforce_model(model)
    if not canonical_model or canonical_model != route["model"]:
        raise LaunchRejected(
            "workforce_model_mismatch:"
            f"runner={runner}:adapter={adapter_id}:expected={route['model']}:got={model}"
        )
    if not route["enabled"]:
        raise LaunchRejected(
            f"workforce_route_disabled:runner={runner}:model={canonical_model}"
        )
    if not route["available"]:
        raise LaunchRejected(
            f"workforce_route_unavailable:runner={runner}:model={canonical_model}"
        )
    normalized_risk_tier = str(risk_tier or "").strip().lower() or None
    if (
        normalized_risk_tier is not None
        and normalized_risk_tier not in route["risk_tiers"]
    ):
        raise LaunchRejected(
            "workforce_route_risk_incapable:"
            f"runner={runner}:model={canonical_model}:risk_tier={normalized_risk_tier}"
        )
    return canonical_model


def derive_launch_identity(
    repo: Path,
    card: Mapping[str, Any],
    *,
    runner: str | None = None,
    topic: str | None = None,
    adapter_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Derive the launch tuple the server already owns, from the card.

    ``aiworkhub_agent_launch_task`` required ``runner``, ``topic`` and
    ``adapter_id`` from the caller, yet none of them was a caller DECISION:
    ``_preflight_card`` refuses unless ``runner`` and ``topic`` EQUAL the
    card's, so they were asserted rather than chosen; ``adapter_id`` was a pure
    function of the runner prefix; and the model is pinned by
    ``_CANONICAL_WORKFORCE``. ``workforce_catalog.rank_task`` has been building
    exactly this ``launch_contract`` all along and this module never read it
    (0 hits), while ``dependency_autolaunch.reconcile`` already launches
    dependents from the card row with no adapter argument at all.

    Determinism is the whole contract: adapter selection walks
    ``adapter_identity_tuple`` in its canonical order and takes the FIRST one
    ``repo_policy.validate_launch`` accepts for this exact card, so two launches
    of one card can never pick different routes. An explicitly supplied value
    always wins over derivation and is returned unchanged; nothing here relaxes
    a check, and every derived value still faces ``validate_workforce_identity``
    /``_validate_adapter_identity`` and the unchanged claim gate afterwards.
    """
    derived_from: dict[str, str] = {}
    resolved_runner = str(runner or "").strip()
    if not resolved_runner:
        resolved_runner = str(card.get("runner") or "").strip()
        derived_from["runner"] = "card"
    resolved_topic = str(topic or "").strip()
    if not resolved_topic:
        resolved_topic = str(card.get("topic") or "").strip()
        derived_from["topic"] = "card"
    if not resolved_runner or not resolved_topic:
        raise LaunchRejected("launch_identity_underivable:card_missing_runner_or_topic")

    resolved_adapter = str(adapter_id or "").strip()
    adapter_candidates = adapter_identity_tuple(resolved_runner)
    adapter_rejections: list[str] = []
    if not resolved_adapter:
        if not adapter_candidates:
            raise LaunchRejected(
                f"launch_adapter_underivable:runner={resolved_runner}:"
                "no adapter tuple for this runner family"
            )
        # A derived adapter must survive the SAME validation an asserted one
        # faces. ``claude_opus-5`` is the measured case: its tuple leads with
        # ``vscode_lm``, which has no canonical workforce row, so deriving the
        # merely-policy-allowed first entry would pin a model the very next
        # check (``validate_workforce_identity``) refuses with
        # ``workforce_route_absent``. When the runner owns ANY canonical row,
        # only an adapter that has one is derivable; a runner the table does not
        # know keeps the plain policy walk. Both passes run the tuple in its
        # canonical order, so the choice stays deterministic either way.
        pinnable = tuple(
            candidate
            for candidate in adapter_candidates
            if (resolved_runner, candidate) in _CANONICAL_WORKFORCE
        )
        for candidate in pinnable or adapter_candidates:
            verdict = repo_policy.validate_launch(repo, card, candidate)
            if verdict.get("ok"):
                resolved_adapter = candidate
                break
            adapter_rejections.append(
                f"{candidate}:{str(verdict.get('reason') or 'repo_policy_rejected')[:80]}"
            )
        if not resolved_adapter:
            raise LaunchRejected(
                "launch_adapter_underivable:runner="
                f"{resolved_runner}:{';'.join(adapter_rejections)[:280]}"
            )
        derived_from["adapter_id"] = (
            "first_pinnable_launchable_in_tuple_order"
            if pinnable
            else "first_launchable_in_tuple_order"
        )

    resolved_model = str(model or "").strip() or None
    if resolved_model is None:
        route = _CANONICAL_WORKFORCE.get((resolved_runner, resolved_adapter))
        if route is not None:
            resolved_model = str(route["model"])
            derived_from["model"] = "canonical_workforce"
        else:
            try:
                from . import workforce_catalog

                identities = workforce_catalog.catalog_launch_identities(repo)
            except Exception:  # noqa: BLE001 - an unreadable catalog pins no model
                identities = {}
            worker = identities.get(resolved_runner)
            if worker is not None and str(worker.get("model") or "").strip():
                resolved_model = str(worker["model"]).strip()
                derived_from["model"] = "catalog_launch_identities"

    return {
        "runner": resolved_runner,
        "topic": resolved_topic,
        "adapter_id": resolved_adapter,
        "model": resolved_model,
        "derived_from": derived_from,
        "adapter_candidates": list(adapter_candidates),
        "adapter_rejections": adapter_rejections,
        "identity_rule": "use_same_runner_for_task_create_and_agent_launch_task",
    }


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
    *,
    captured_entries: list[tuple[str, bytes | None]] | None = None,
) -> dict[str, Any] | None:
    """Seal a complete terminal candidate outside its disposable worktree.

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

    try:
        from .successful_rework_recovery import capture_candidate_paths

        entries = (
            captured_entries if captured_entries is not None
            else capture_candidate_paths(workspace.path, changed)
        )
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
    """Bind bounded predecessor failure evidence to one verified rework overlay.

    The predecessor workspace bytes remain authoritative through the overlay;
    this packet only salvages diagnostics that would otherwise be reread from
    old process logs or re-derived by re-running the declared validation.  A
    crashed predecessor contributes its bounded stream tails.  A predecessor
    that exited cleanly contributes the sealed attempt-artifacts validation
    failure delta instead -- every validation_failed predecessor exits 0, so
    an exit-code gate would drop exactly the runs that hold a measured
    failure -- plus the finalizer's terminal reason when the failure left no
    failed-check receipt (required_output_unchanged, mcp_call_missing, ...).
    Its stream tails are omitted: for an exit-0 run they are the worker's own
    final stream, not diagnostics.  Missing, cross-task, cross-repository, or
    oversized predecessor metadata fails closed by omitting the packet; a
    clean exit that was not measured as a validation failure also omits it,
    because a review-rejected candidate is carried by review_feedback.
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
    clean_exit = state == "exited" and returncode == 0
    validation_delta: dict[str, Any] | None = None
    validation_manifest_sha256 = ""
    terminal_substatus = ""
    terminal_reason = ""
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
            review_entry = next(
                (entry for entry in manifest.artifacts if entry.role == "review"),
                None,
            )
            if review_entry is not None:
                review_payload = json.loads(
                    (bundle_dir / review_entry.path).read_text(encoding="utf-8")
                )
                if isinstance(review_payload, dict):
                    terminal_substatus = str(review_payload.get("target_state") or "")
                    terminal_reason = str(review_payload.get("error") or "")[
                        :MAX_CRASH_RETRY_TERMINAL_REASON_CHARS
                    ]
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
    failed_check_count = (
        int(validation_delta.get("failure_count") or 0)
        if isinstance(validation_delta, dict)
        else 0
    )
    measured_validation_failure = (
        failed_check_count > 0 or terminal_substatus == "validation_failed"
    )
    if clean_exit and not measured_validation_failure:
        return None, None
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    # The packet is JSON, so JSON encoding already neutralises every
    # metacharacter. The HTML-oriented live-output sanitiser escaped and
    # redacted bytes the successor needs verbatim, so carry the predecessor's
    # diagnostics unescaped and unredacted; the tail hashes below then cover
    # exactly the bytes delivered rather than a pre-sanitised original.
    # A clean exit has no crash diagnostics in its streams -- they are the
    # worker's own final output -- so omit them and keep the packet headroom
    # for the validation delta.
    stream_tails_omitted_reason = "predecessor_exited_clean" if clean_exit else ""
    stdout_tail = (
        "" if clean_exit else _safe_tail(stdout_path, MAX_CRASH_RETRY_STREAM_BYTES)
    )
    stderr_tail = (
        "" if clean_exit else _safe_tail(stderr_path, MAX_CRASH_RETRY_STREAM_BYTES)
    )
    error = str(status.get("error") or "")[:500]
    if not (stdout_tail or stderr_tail or error or state):
        return None, None

    packet: dict[str, Any] = {
        "schema_id": "aiworkhub.crash_retry_packet.v1",
        "successor_request_id": workspace.request_id,
        "successor_task_id": task_id,
        "predecessor_request_id": request_id,
        "predecessor_task_id": task_id,
        "predecessor_state": state,
        "predecessor_exit_code": returncode,
        "predecessor_error": error,
        "predecessor_terminal_substatus": terminal_substatus,
        # The finalizer's own reason is the only carrier for a failure that
        # left no failed-check receipt; with receipts present it restates them.
        "predecessor_terminal_reason": (
            terminal_reason if failed_check_count == 0 else ""
        ),
        "stream_tails_omitted_reason": stream_tails_omitted_reason,
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
    inherited_gate_receipt = (metadata.get("worker_mcp") or {}).get(
        "inherited_predecessor_gate"
    )
    if metadata.get("execution_mode") == "validation_only_replay" and isinstance(
        inherited_gate_receipt, dict
    ):
        predecessor = metadata.get("rework_predecessor") or {}
        authorization = metadata.get("validation_only_replay_authorization") or {}
        bindings_match = (
            inherited_gate_receipt.get("task_id") == metadata.get("task_id")
            and inherited_gate_receipt.get("predecessor_request_id")
            == predecessor.get("request_id")
            == authorization.get("predecessor_request_id")
            and inherited_gate_receipt.get("changed_path_hashes")
            == predecessor.get("changed_path_hashes")
            == authorization.get("changed_path_hashes")
            and inherited_gate_receipt.get("next_claim_epoch")
            == metadata.get("claim_epoch")
            == authorization.get("next_claim_epoch")
            and (
                not authorization.get("request_id")
                or (
                    inherited_gate_receipt.get("request_id")
                    == authorization.get("request_id")
                    == metadata.get("request_id")
                    == request_id
                    and inherited_gate_receipt.get("repo")
                    == authorization.get("repo")
                    == (metadata.get("workspace") or {}).get("repo")
                )
            )
        )
        inherited_gate = inherited_gate_receipt.get("worker_mcp_gate")
        inherited_verification = (
            inherited_gate.get("verification")
            if isinstance(inherited_gate, dict)
            else None
        )
        if (
            not bindings_match
            or not isinstance(inherited_gate, dict)
            or inherited_gate.get("gated") is not True
            or inherited_gate.get("satisfied") is not True
            or not isinstance(inherited_verification, dict)
            or inherited_verification.get("ok") is not True
        ):
            return {
                "gated": True,
                "task_type": str(inherited_gate_receipt.get("task_type") or "code"),
                "required_tools": list(
                    inherited_gate_receipt.get("required_tools") or []
                ),
                "missing_tools": [],
                "satisfied": False,
                "reason": "validation_only_replay_predecessor_mcp_receipt_mismatch",
                "observation_only": False,
                "inherited_predecessor_evidence": True,
            }
        result = dict(inherited_gate)
        required_tools = list(result.get("required_tools") or [])
        result.update({
            "gated": True,
            "satisfied": True,
            "observation_only": False,
            "required_tools": required_tools,
            "missing_tools": [],
            "reason": "",
            "inherited_predecessor_evidence": True,
            "fresh_current_request_worker_calls": False,
            "satisfaction_by_tool": {
                tool: "authenticated_predecessor_gate" for tool in required_tools
            },
        })
        return result

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
    # Sections the coordinator executed for this request and measured at zero
    # rows: a fact taken before the worker started, independent of any model
    # text or live re-call.  Degraded sections are never in this set.
    coordinator_zero_hit = (
        _coordinator_measured_zero_hit_sections(context_metadata)
        if isinstance(context_metadata, dict)
        else set()
    )
    gate_required_tools: list[str] = []
    exempted_tools: dict[str, str] = {}
    if task_type == "code" and tools_policy.get("source_graph_required_for_code"):
        gate_required_tools.append("source_graph")
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
                and name not in gate_required_tools
            ):
                if name == "session_current_state" and name in coordinator_zero_hit:
                    # The canonical session query IS the topic-scoped store:
                    # zero rows at launch means the store holds no document
                    # for this card's topic, so no live call can return
                    # anything and requiring one is ceremony.  Record why the
                    # tool is not required; a degraded section never reaches
                    # this branch and stays required.
                    exempted_tools[name] = "session_store_empty_for_topic_at_launch"
                    continue
                gate_required_tools.append(name)
    # An explicit required project-context contract is stronger than the
    # repository's generic code-task defaults.  Research/read-only cards use
    # this path too, so derive their blocking tools from the exact requested
    # sections instead of silently classifying them as observation-only.
    if context_required:
        for section in sections:
            if not isinstance(section, dict) or not section.get("requested", True):
                continue
            name = str(section.get("name") or "")
            if name in _GATEABLE_CONTEXT_SECTIONS and name not in gate_required_tools:
                gate_required_tools.append(name)
    gated = bool(gate_required_tools) and (task_type == "code" or context_required)
    gate_result: dict[str, Any] = {
        "gated": gated,
        "task_type": task_type,
        "project_context_required": context_required,
        "required_tools": gate_required_tools if gated else [],
        "exempted_tools": exempted_tools,
        "missing_tools": [],
        "satisfied": True,
        "reason": "",
        "satisfaction_by_tool": {},
        "injected_context_acknowledged": False,
        "injected_context_acknowledgement_source": "",
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
    injected_acknowledged, injected_tools = _injected_context_satisfaction(
        metadata, request_id
    )
    acknowledgement_source = "worker_receipt" if injected_acknowledged else ""
    gate_result["injected_context_acknowledged"] = injected_acknowledged
    gate_result["injected_context_acknowledgement_source"] = acknowledgement_source
    source_graph_injected_acknowledged = False
    context = metadata.get("project_context") or {}
    if injected_acknowledged and isinstance(context, dict):
        source_graph_receipt = _project_context_receipt_from_output(
            Path(str(metadata.get("stdout_path") or "")),
            expected_bundle_sha256=str(context.get("bundle_sha256") or ""),
            expected_request_id=request_id,
        )
        source_graph_injected_acknowledged = bool(
            source_graph_receipt.get("acknowledged")
        )
    if policy_error:
        gate_result["gated"] = True
        gate_result["satisfied"] = False
        gate_result["reason"] = policy_error
        return gate_result
    ledger_path = worker_mcp_meta.get("audit_ledger_path")
    key_path = worker_mcp_meta.get("audit_hmac_key_path")
    if not ledger_path or not key_path:
        gate_result["telemetry_reason"] = "worker_mcp_runtime_not_provisioned"
        if gated:
            gate_result["satisfied"] = False
            gate_result["reason"] = "worker_mcp_runtime_not_provisioned"
        return gate_result
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
    gate_result["verification"] = {
        k: v for k, v in verification.items() if k != "schema_id"
    }
    gate_result["telemetry_observed"] = bool(verification.get("ok"))
    gate_result["telemetry_reason"] = str(verification.get("reason") or "")
    # Server-derived acknowledgement: the coordinator wrote the bundle into
    # this request's prompt (delivery receipt sha == collected bundle sha) and
    # the HMAC audit ledger verifies for this exact request id.  Both inputs
    # are coordinator facts; the worker-typed receipt line above only ever
    # copied a sha the coordinator itself rendered, so its absence or a typo
    # in it is telemetry, never grounds to discard verified work.
    if not injected_acknowledged and verification.get("ok") is True:
        bound_sections = _coordinator_bound_context_sections(metadata)
        if bound_sections is not None:
            injected_acknowledged = True
            injected_tools = bound_sections
            source_graph_injected_acknowledged = True
            acknowledgement_source = "coordinator_prompt_binding"
            gate_result["injected_context_acknowledged"] = True
            gate_result["injected_context_acknowledgement_source"] = (
                acknowledgement_source
            )
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
        gate_result["gated"] = True
        gate_result["satisfied"] = False
        gate_result["reason"] = (
            "audit_ledger_numeric_decode_failed:" + ledger_decode_failure
        )
        return gate_result
    assert policy_violations is not None
    assert live_source_graph_calls is not None
    gate_result["policy_warning"] = policy_violations > 0
    gate_result["policy_warning_count"] = policy_violations
    if policy_violations:
        gate_result["warnings"] = [
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
        gate_result["gated"] = True
        gate_result["satisfied"] = False
        gate_result["receipt_conformance_blocking"] = True
        gate_result["reason"] = (
            "receipt_conformance_blocking:" + ",".join(blockers)
            if blockers
            else "receipt_conformance_blocking"
        )
        return gate_result
    if not gated:
        return gate_result
    # ``successful`` was decoded above with fail-closed numeric handling.
    # The canonical supervisor-owned Source Graph query is initial-orientation
    # evidence once its exact bundle receipt is acknowledged. A later live,
    # authoritative worker call remains stronger evidence and takes precedence.
    satisfaction_by_tool: dict[str, str] = {}
    missing: list[str] = []
    stale: list[str] = []
    # Source Graph freshness is invocation truth: an authenticated, successful,
    # non-cached authoritative worker call satisfies continuous use even when
    # the bounded query returns zero rows. An acknowledged supervisor section
    # proves the same initial call was already executed before launch. Cached,
    # failed, degraded, non-authoritative, or unverified evidence is excluded
    # from injected_tools and remains fail-closed.
    if live_source_graph_calls > 0:
        satisfaction_by_tool["source_graph"] = "live_worker_call"
    elif (
        verification.get("ok") is True
        and source_graph_injected_acknowledged
        and "source_graph" in injected_tools
    ):
        satisfaction_by_tool["source_graph"] = "supervisor_injected_orientation"
    elif int(successful.get("source_graph") or 0) > 0:
        satisfaction_by_tool["source_graph"] = "stale_or_cached"
        stale.append("source_graph")
    else:
        missing.append("source_graph")
    rework_attempt = _is_rework_attempt(metadata)
    for tool in gate_required_tools:
        if tool == "source_graph":
            continue
        if int(successful.get(tool) or 0) > 0:
            satisfaction_by_tool[tool] = "live_worker_call"
        elif injected_acknowledged and tool in injected_tools:
            satisfaction_by_tool[tool] = (
                "coordinator_prompt_binding"
                if acknowledgement_source == "coordinator_prompt_binding"
                else "injected_receipt"
            )
        elif tool in coordinator_zero_hit:
            # The coordinator ran the canonical query for this request and
            # measured zero rows; that measurement is the answer, independent
            # of a model-typed receipt or a live re-call of the same query.
            satisfaction_by_tool[tool] = "coordinator_zero_hit_measurement"
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
    gate_result["missing_tools"] = missing
    gate_result["stale_tools"] = stale
    gate_result["satisfaction_by_tool"] = satisfaction_by_tool
    if not verification.get("ok") or missing or stale:
        gate_result["satisfied"] = False
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
        gate_result["reason"] = verification.get("reason") or "; ".join(reasons)
    return gate_result


# One authority for the bool-safe integer rule, owned by the store that binds
# the claim epochs it guards.  A private copy here would silently stop matching
# the store's rule and admit an epoch the store would reject -- visible only as
# a terminal transition bound to the wrong episode.
_is_bool_safe_int = task_store.is_bool_safe_int


# Stays with the launcher: it reconstructs a ``WorkerWorkspace``, which is the
# launcher's own collaborator and its established test seam, so the binding it
# resolves must remain this module's.
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
# The sealed attempt review payload already caps the finalizer reason at 500
# characters; the packet never carries more than the bundle holds.
MAX_CRASH_RETRY_TERMINAL_REASON_CHARS = 500
MAX_TASK_CONTRACT_BYTES = 96 * 1024
MAX_REWORK_TASK_CONTRACT_BYTES = 48 * 1024
MAX_WORKER_PROMPT_BYTES = 160 * 1024
MAX_REWORK_WORKER_PROMPT_BYTES = 112 * 1024

def build_worker_prompt(
    *,
    task_id: str,
    runner: str,
    topic: str,
    request_id: str = "",
    card: dict[str, Any] | None = None,
    owner_prompt: str = "",
    project_context_bundle: str = "",
    crash_retry_packet: dict[str, Any] | None = None,
    adapter_id: str | None = None,
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
    contract = {
        key: card[key]
        for key in TASK_CONTRACT_KEYS
        if card is not None and key in card
    }
    contract = _strip_persistence_envelopes(contract)
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
    # No acknowledgement line is requested: the coordinator already knows the
    # bundle sha and the request id it rendered, and binds them to the worker
    # MCP audit ledger at finalization (coordinator_prompt_binding).  Asking
    # the model to copy 265 bytes of the prompt back into stdout added no
    # evidence and its absence used to refuse verified work (receipt_not_found).
    # ``request_id`` stays in the signature for its call sites; the binding
    # is recorded in the request metadata, not in prompt text.
    context_block = (
        "\n\nTrusted project context (bounded, read-only, coordinator-provided; "
        "bound to your request id by the coordinator, no acknowledgement line "
        "is required):\n"
        + project_context_bundle.strip()
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
    # The adapter is passed because six of the nine supported transports carry
    # no launch-time tool deny, and the runtime policy tells only those six that
    # the rule behind it is the audit ledger rather than a provider refusal.
    # This keeps the prefix stable: it varies per adapter, and a provider prefix
    # cache is per provider already, so no cache is split by task.
    stable_prefix = agent_tool_instructions.render_worker_runtime_policy(adapter_id)
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
    claim_epoch: int | None = None
    # Once the zero-delta tripwire has settled (enforcement requested, a real
    # delta observed, or the card exempt) no later observation of this run can
    # change the answer, so the monitor stops re-hashing the workspace.
    zero_delta_tripwire_settled: bool = False


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
        toolchain_authority: _toolchain_authority.ToolchainAuthority | None = None,
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
        self._toolchain_authority = toolchain_authority or (
            _toolchain_authority.ToolchainAuthority(self.repo)
        )
        self._lock = threading.RLock()
        self._live: dict[str, _LiveProcess] = {}
        self._cancelled: set[str] = set()
        self._watching: set[str] = set()
        self._authority_key: bytes | None = None
        if self.process_log_path.is_file() and self.isolation_enabled:
            self._reconcile_persisted_requests()
        self._reconcile_pending_needfix_closures()

    def _default_show_task(self, task_id: str) -> dict[str, Any]:
        """Read from this manager's canonical repository-bound task store."""
        return task_engine.show_task(self.repo, task_id)

    def _load_dependency_card(self, dep_id: str) -> dict[str, Any]:
        """Best-effort repo-bound dependency-card read; failures yield ``{}``."""
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
        unbound_claim_resolutions = self._resolve_unbound_reviewer_claims(snapshot[0])
        try:
            with self._lock, self._registry_lock():
                # Re-prove the handed-in snapshot for this whole critical
                # section.  The cheap path is one generation sweep.  If a
                # sibling appended while this launch waited for the lock, take
                # one fresh bounded bracketed snapshot now that the ledger is
                # serialized instead of rejecting an otherwise healthy launch.
                # Reconciliation mirrors the rows it retires back into the
                # snapshot, so it keeps describing the ledger exactly across
                # both halves.
                proven = self._proven_reservation_snapshot(snapshot)
                if proven is None:
                    # No stable generation could be shown, so any row read
                    # here may be one append behind.  Admitting on that could
                    # duplicate a live task or overrun the limit, so the
                    # launch defers instead of guessing.
                    raise LaunchRejected("ledger_snapshot_unproven")
                latest, generation = proven
                self._reconcile_expired_starting_reservations(
                    (latest, generation),
                    resolved=True,
                    _admission_recovery_authority=(
                        _LAUNCH_RESERVATION_ADMISSION_RECOVERY
                    ),
                    _unbound_claim_resolutions=unbound_claim_resolutions,
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
        # Return what the ledger wrote: a failure-terminal row gains a canonical
        # terminal_reason on the way in, so ``clean`` was an event whose own
        # replay would not compare equal to it.
        return process_event_ledger.append_event(self.process_log_path, clean)

    def _retention_event(self, event: dict[str, Any], *, disposition: str) -> dict[str, Any]:
        """Record terminal retention evidence and enqueue repository hygiene."""
        retained = {"retained_in_place": True, "quarantined": True,
                    "removed": False}[disposition]
        recorded = self._append_event({
            **event, "workspace_disposition": disposition,
            "workspace_retained": retained,
        })
        if str(event.get("state") or "") in {"accepted", "rejected", "archived"}:
            storage_retention.schedule_repository_cleanup(self.repo)
        return recorded

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

        # A runtime notice is an observation, never a lifecycle row: this map
        # REPLACES the row per request, so one landing here would erase the
        # ``state`` every reconciler reads. Both fold options are part of the
        # projection's cache identity, so this view can never be served a merge
        # fold. Folded a raw ``iter_events`` pass until NF-2026-00561 -- 2.19 s
        # per call against 7 ms cached, over ~20 sites and a 30 s reconciler.
        latest = process_event_ledger.latest_events(
            self.process_log_path,
            key_field="request_id",
            skip_event_kinds=(RUNTIME_NOTICE_EVENT_KIND,),
            replace=True,
        )
        # Claim binding is an immutable event in its own right, not a second
        # reservation.  Its payload is a complete snapshot of the reservation
        # it binds so the cached replace projection stays one-pass and bounded;
        # lifecycle consumers interpret that snapshot as the same logical
        # ``starting`` attempt.  Raw ledger readers still see the distinct
        # state, preserving exactly one starting reservation row per attempt.
        for event in latest.values():
            if event.get("state") == REVIEWER_CLAIM_BOUND_STATE:
                event["state"] = "starting"
                event["claim_binding_state"] = REVIEWER_CLAIM_BOUND_STATE
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
        return reviewer_reservation_recovery.record_reviewer_terminal_intent(
            self,
            request_id,
            event,
            blocked_reason,
            schema_id=REVIEWER_TERMINAL_INTENT_SCHEMA_ID,
            is_safe_int=_is_bool_safe_int,
            utcnow=_utcnow,
            write_json=write_json_0600,
        )

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
        return reviewer_reservation_recovery.terminalize_committed_reservation(
            self, request_id, event, blocked_reason, diagnostics_left
        )

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
        # Diagnostics must never turn an already-rejected directory entry into
        # an I/O operation on its target.  In particular, Path.read_bytes()
        # follows symlinks and can block forever when the entry is a FIFO.
        try:
            observed = path.lstat()
        except OSError:
            observed = None
        digest = ""
        size = -1 if observed is None else observed.st_size
        if observed is not None and stat.S_ISREG(observed.st_mode):
            try:
                data, opened = self._read_regular_intent(path)
            except (OSError, UnicodeError):
                pass
            else:
                if (opened.st_dev, opened.st_ino) == (observed.st_dev, observed.st_ino):
                    digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
                    size = opened.st_size
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

    @staticmethod
    def _read_regular_intent(path: Path) -> tuple[str, os.stat_result]:
        """Read one regular intent without following or blocking on a node."""

        max_bytes = 1_048_576
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("nofollow intent reads are unavailable")
        fd = os.open(path, flags | nofollow)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError("terminal intent is not a regular file")
            if opened.st_size > max_bytes:
                raise OSError("terminal intent exceeds read bound")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                raw = handle.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise OSError("terminal intent exceeds read bound")
                return raw, opened
        finally:
            if fd >= 0:
                os.close(fd)

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
        return reviewer_reservation_recovery.settle_reviewer_terminal_intents(
            self,
            schema_id=REVIEWER_TERMINAL_INTENT_SCHEMA_ID,
            is_safe_int=_is_bool_safe_int,
            unlink_regular=unlink_if_regular,
        )

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

    def _resolve_unbound_reviewer_claims(
        self, candidate_latest: dict[str, dict[str, Any]]
    ) -> dict[str, tuple[str, int | None]]:
        """Resolve expired unbound reviewer claims without the registry lock."""
        return reviewer_reservation_recovery.resolve_unbound_reviewer_claims(
            candidate_latest,
            load_task=lambda task_id: task_store.get_task(self.repo, task_id),
            is_safe_int=_is_bool_safe_int,
        )

    def _reconcile_expired_starting_reservations(
        self,
        snapshot: tuple[
            dict[str, dict[str, Any]], tuple[Any, ...] | None
        ] | None = None,
        *,
        resolved: bool = False,
        _admission_recovery_authority: object | None = None,
        _unbound_claim_resolutions: dict[str, tuple[str, int | None]] | None = None,
    ) -> int:
        return reviewer_reservation_recovery.reconcile_expired_starting_reservations(
            self,
            snapshot,
            resolved=resolved,
            _admission_recovery_authority=_admission_recovery_authority,
            _unbound_claim_resolutions=_unbound_claim_resolutions,
            now_epoch=time.time,
            parse_durable_pid=_parse_durable_pid,
            pid_identity_evidence=_pid_identity_evidence,
            identity_mismatch=PidIdentityVerdict.MISMATCH,
            classify_preparation_stall=derive_preparation_stall,
        )

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
        prior_request_id = launch_request_id or card.get("request_id")
        effective_request_id = str(
            reserved_request_id
            or (None if lifecycle == "pending" else prior_request_id)
            or uuid.uuid4().hex
        )
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
        authority_snapshot = self._toolchain_authority.evaluate(card)
        self._toolchain_authority.repair(authority_snapshot)
        if not authority_snapshot.available:
            detail = json.dumps(
                [f"{fact.kind}:{fact.value}" for fact in authority_snapshot.missing],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            raise LaunchRejected(f"task_contract_unwinnable:{detail}")
        card["request_id"] = effective_request_id
        card[_toolchain_authority.RECEIPT_CARD_KEY] = (
            _toolchain_authority.authority_receipt(authority_snapshot, card)
        )
        # ``repo`` lets the guard read the durable terminal history in
        # ``task_events``.  It has to: ``reject_review`` erases the card's own
        # ``terminal_review`` on the transition back to ``pending``, so the
        # repeated-outcome axis has nothing to compare on the card alone.
        relaunch_refusal = identical_relaunch_refusal(
            card, runner=runner, adapter_id=adapter_id, repo=self.repo
        )
        if relaunch_refusal:
            raise LaunchRejected(relaunch_refusal)
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
                provider_pid, provider_pid_ambiguous = _parse_durable_pid(
                    event.get("provider_pid")
                )
                if provider_pid_ambiguous:
                    active.add(request_id)
                    continue
                if provider_pid and _pid_identity_evidence(
                    provider_pid, event.get("provider_pid_start_ticks")
                ).verdict is not PidIdentityVerdict.MISMATCH:
                    active.add(request_id)
                    continue
                owner_pid, owner_pid_ambiguous = _parse_durable_pid(
                    event.get("owner_pid")
                )
                if owner_pid_ambiguous:
                    active.add(request_id)
                    continue
                if owner_pid and _pid_identity_evidence(
                    owner_pid, event.get("owner_pid_start_ticks")
                ).verdict is not PidIdentityVerdict.MISMATCH:
                    active.add(request_id)
                continue
            if state not in ACTIVE_PROCESS_STATES:
                continue
            pid, pid_ambiguous = _parse_durable_pid(event.get("pid"))
            if pid_ambiguous:
                active.add(request_id)
                continue
            ticks = event.get("pid_start_ticks")
            if (
                state == "starting"
                and not pid
                and self._reviewer_source_graph_prewarm_live_event(event)
            ):
                active.add(request_id)
                continue
            if state == "starting" and not pid:
                try:
                    reservation_deadline = float(
                        event.get("reservation_expires_at_epoch") or 0.0
                    )
                except (TypeError, ValueError, OverflowError):
                    # An unreadable lease is ambiguous authority, not proof
                    # that the reservation is dead.  Keep admission bounded
                    # and fail closed while reconciliation preserves it for
                    # explicit repair.
                    active.add(request_id)
                    continue
                if (
                    reservation_deadline <= 0.0
                    or reservation_deadline != reservation_deadline
                    or reservation_deadline > time.time()
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

    def _validation_replay_predecessor_mcp_receipt(
        self,
        card: Mapping[str, Any],
        authorization: Mapping[str, Any],
        task_id: str,
    ) -> dict[str, Any] | None:
        """Return exact authenticated predecessor MCP truth for a code replay."""

        context = card.get("project_context")
        context = context if isinstance(context, dict) else {}
        task_type = str(context.get("task_type") or "")
        if task_type != "code" and context.get("required") is not True:
            return None
        predecessor_request_id = str(
            authorization.get("predecessor_request_id") or ""
        )
        predecessor_event = None
        terminal_seen = False
        for event in reversed(self._events()):
            if (
                event.get("request_id") != predecessor_request_id
                or event.get("task_id") != task_id
                or event.get("state") not in TERMINAL_PROCESS_STATES
            ):
                continue
            terminal_seen = True
            # Compact retention rows must not shadow the evidence-bearing row.
            if isinstance(event.get("worker_mcp_gate"), dict):
                predecessor_event = event
                break
        if not terminal_seen:
            raise LaunchRejected(
                "validation_only_replay_predecessor_terminal_event_missing"
            )
        if predecessor_event is None:
            raise LaunchRejected(
                "validation_only_replay_predecessor_worker_mcp_gate_missing"
            )
        gate = predecessor_event["worker_mcp_gate"]
        verification = gate.get("verification")
        if (
            gate.get("gated") is not True
            or gate.get("satisfied") is not True
            or not isinstance(verification, dict)
            or verification.get("ok") is not True
        ):
            verification_reason = (
                verification.get("reason") if isinstance(verification, dict) else ""
            )
            raise LaunchRejected(
                "validation_only_replay_predecessor_worker_mcp_gate_unsatisfied:"
                + str(gate.get("reason") or verification_reason)[:200]
            )
        required_tools = [str(tool) for tool in (gate.get("required_tools") or [])]
        return {
            "schema_id": "aiworkhub.task_mcp.validation_replay_predecessor_gate.v1",
            "task_id": task_id,
            "predecessor_request_id": predecessor_request_id,
            "changed_path_hashes": dict(
                authorization.get("changed_path_hashes") or {}
            ),
            "next_claim_epoch": authorization.get("next_claim_epoch"),
            "request_id": authorization.get("request_id"),
            "repo": authorization.get("repo"),
            "task_type": str(gate.get("task_type") or task_type),
            "required_tools": required_tools,
            "worker_mcp_gate": {
                "gated": True,
                "task_type": str(gate.get("task_type") or task_type),
                "project_context_required": bool(
                    gate.get("project_context_required")
                ),
                "required_tools": required_tools,
                "missing_tools": [],
                "satisfied": True,
                "reason": "",
                "observation_only": False,
                "verification": dict(verification),
            },
        }

    def launch(
        self,
        *,
        task_id: str,
        runner: str | None = None,
        topic: str | None = None,
        adapter_id: str | None = None,
        model: str | None = None,
        owner_prompt: str = "",
        timeout_seconds: int = 7200,
        quality_review_binding: dict[str, Any] | None = None,
        reserved_request_id: str | None = None,
        prewarm_progress: Callable[..., None] | None = None,
    ) -> dict[str, Any]:
        """Launch one worker; ``runner``/``topic``/``adapter_id`` are optional.

        Omitting them derives the tuple from the card via
        ``derive_launch_identity``: the card already owns runner and topic (and
        ``_preflight_card`` refuses anything else), and the adapter is the first
        one in the family's canonical tuple that ``repo_policy`` accepts for this
        card. Passing them keeps the exact prior behavior -- an explicit value is
        never overridden. Identity remains explicitly validated either way.
        """
        identity_derivation: dict[str, Any] | None = None
        if not (runner and topic and adapter_id):
            try:
                card = _parse_card(self._show_task(task_id), task_id)
                identity_derivation = derive_launch_identity(
                    self.repo,
                    card,
                    runner=runner,
                    topic=topic,
                    adapter_id=adapter_id,
                    model=model,
                )
            except LaunchRejected as exc:
                return self._blocked(
                    task_id,
                    str(runner or ""),
                    str(topic or ""),
                    str(adapter_id or ""),
                    str(exc),
                )
            runner = identity_derivation["runner"]
            topic = identity_derivation["topic"]
            adapter_id = identity_derivation["adapter_id"]
            model = identity_derivation["model"]
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
            result = self._launch_isolated(**isolated_kwargs)
        else:
            result = self._launch_direct_for_tests(
                task_id=task_id,
                runner=runner,
                topic=topic,
                adapter_id=adapter_id,
                model=model,
                owner_prompt=owner_prompt,
                timeout_seconds=timeout_seconds,
            )
        # The receipt records HOW the identity was decided, so a launch that
        # derived its route is auditable against one that asserted it.
        if identity_derivation is not None and isinstance(result, dict):
            result["launch_identity_derivation"] = identity_derivation
        return result

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
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a provider-free replay through the normal finalizer."""

        predecessor_mcp_receipt = self._validation_replay_predecessor_mcp_receipt(
            card, authorization, task_id
        )
        request_id = str(request_id or card.get("request_id") or uuid.uuid4().hex)
        receipt = card.get(_toolchain_authority.RECEIPT_CARD_KEY)
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

                committed_authorization = _validation_only_replay_authorization(
                    card, task_id
                )
                expected_authorization = {
                    **authorization,
                    "next_claim_epoch": card["claim_epoch"],
                    "request_id": request_id,
                    "repo": str(self.repo.resolve()),
                }
                if committed_authorization != expected_authorization:
                    raise LaunchRejected("validation_only_replay_committed_grant_mismatch")
                authorization = committed_authorization
                predecessor_mcp_receipt = self._validation_replay_predecessor_mcp_receipt(
                    card, authorization, task_id
                )

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
                    "project_context": (
                        {
                            "required": bool(
                                predecessor_mcp_receipt["worker_mcp_gate"].get(
                                    "project_context_required"
                                )
                            ),
                            "task_context_policy": {
                                "task_type": predecessor_mcp_receipt["task_type"]
                            },
                            "sections": [
                                {"name": tool, "requested": True}
                                for tool in predecessor_mcp_receipt["required_tools"]
                            ],
                            "inherited_predecessor_evidence": True,
                        }
                        if predecessor_mcp_receipt is not None
                        else None
                    ),
                    "project_context_delivery": {
                        "injected": False,
                        "reason": (
                            "authenticated_predecessor_gate_inherited"
                            if predecessor_mcp_receipt is not None
                            else "deterministic_validation_only_replay"
                        ),
                    },
                    "worker_mcp": (
                        {"inherited_predecessor_gate": predecessor_mcp_receipt}
                        if predecessor_mcp_receipt is not None
                        else {}
                    ),
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
                if isinstance(receipt, Mapping):
                    metadata[_toolchain_authority.RECEIPT_CARD_KEY] = dict(receipt)
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
    _QUALITY_REVIEW_PREP_CAPACITY = threading.Condition(_QUALITY_REVIEW_PREP_LOCK)
    _QUALITY_REVIEW_PREP_ACTIVE_BUILDERS = 0
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
    # The source-evidence budget is sized from the measured change rather than
    # a fixed excerpt.  2026-09-08 reviewer audit over 38 surviving packets:
    # the old 4,000 B per-path / 60,000 B total excerpt with 3 context lines
    # carried 2.0% of the changed bytes (excerpt 12,271 B vs 619,485 B of
    # changed source per candidate) and flagged 67% of rows truncated, so
    # reviewers re-read their own changed files (63.5% of every byte they
    # read) and ran git diff in 38% of runs (p50 26 KB each).  git diff output
    # on those candidates averaged ~35 KB; a 24 KiB per-path and 64 KiB total
    # budget carries the whole change for the typical candidate and names the
    # omission exactly (``diff_complete`` false) for the rest.
    _QUALITY_REVIEW_SOURCE_MAX_BYTES = 24 * 1024
    _QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES = 64 * 1024
    _QUALITY_REVIEW_SOURCE_CONTEXT_LINES = 3
    # Canonical source around every graph-resolved caller line the scoped
    # audit lists (``impact_evidence`` rows of kind ``callers``).  Impact rows
    # sit at the 64-row cap in 38/38 packets; the caller search reviewers ran
    # by hand (110 Agent subagents in 60 claude runs) was looking for exactly
    # these lines, which the coordinator already had.
    _QUALITY_REVIEW_CALLER_CONTEXT_LINES = 5
    _QUALITY_REVIEW_CALLER_CONTEXT_MAX_ROWS = 64
    _QUALITY_REVIEW_CALLER_CONTEXT_ROW_MAX_BYTES = 2_048
    _QUALITY_REVIEW_CALLER_CONTEXT_TOTAL_MAX_BYTES = 16 * 1024

    def _quality_review_source_evidence(
        self, workspace: Any, changed_hashes: Mapping[str, str | None]
    ) -> dict[str, dict[str, Any]]:
        """Build the complete unified diff of each changed path, candidate vs canonical.

        One hunk per non-equal SequenceMatcher opcode: an ``@@`` header naming
        the exact candidate and baseline line ranges, then ``' '`` context
        lines (bounded by the neighbouring equal blocks, so context is never
        another hunk's changed line), ``'-'`` baseline lines removed and
        ``'+'`` candidate lines added.  Hunks are emitted whole while the
        per-path and total byte budgets allow; a hunk that does not fit is cut
        at a line boundary and counted, and every cut or omitted hunk keeps
        its exact ``segments`` row so ``diff_complete`` is false only when
        something is missing and never silently.
        """

        def decode_utf8(raw: bytes) -> str | None:
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return None

        def with_newline(line: str) -> str:
            return line if line.endswith("\n") else line + "\n"

        context_lines = self._QUALITY_REVIEW_SOURCE_CONTEXT_LINES
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
                "diff_complete": False,
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
            opcodes = difflib.SequenceMatcher(
                None, baseline_lines, candidate_lines
            ).get_opcodes()
            chunks: list[str] = []
            omitted_hunks = 0
            path_remaining = self._QUALITY_REVIEW_SOURCE_MAX_BYTES
            header_path = json.dumps(path, ensure_ascii=True)
            for index, (tag, old_start, old_end, new_start, new_end) in enumerate(opcodes):
                if tag == "equal":
                    continue
                # Context is bounded by the adjacent EQUAL blocks: a short
                # equal run between two hunks must not let one hunk's context
                # swallow the other hunk's changed lines as if unchanged.
                before = 0
                if index > 0 and opcodes[index - 1][0] == "equal":
                    before = min(context_lines, opcodes[index - 1][4] - opcodes[index - 1][3])
                after = 0
                if index + 1 < len(opcodes) and opcodes[index + 1][0] == "equal":
                    after = min(context_lines, opcodes[index + 1][4] - opcodes[index + 1][3])
                start_line = new_start - before + 1
                end_line = max(start_line, new_end + after)
                header = (
                    f"@@ path:{header_path} candidate:{start_line}-{end_line} "
                    f"change:{new_start + 1}-{max(new_start + 1, new_end)} "
                    f"baseline:{old_start + 1}-{max(old_start + 1, old_end)} {tag} @@\n"
                )
                body_lines = (
                    [" " + with_newline(line) for line in candidate_lines[new_start - before:new_start]]
                    + ["-" + with_newline(line) for line in baseline_lines[old_start:old_end]]
                    + ["+" + with_newline(line) for line in candidate_lines[new_start:new_end]]
                    + [" " + with_newline(line) for line in candidate_lines[new_end:new_end + after]]
                )
                segment = {
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
                limit = max(0, min(path_remaining, remaining))
                header_bytes = len(header.encode("utf-8"))
                if limit <= header_bytes:
                    omitted_hunks += 1
                    row["segments"].append(segment)
                    continue
                budget = limit - header_bytes
                emitted: list[str] = []
                used = 0
                truncated = False
                for line in body_lines:
                    encoded = len(line.encode("utf-8"))
                    if used + encoded > budget:
                        truncated = True
                        break
                    emitted.append(line)
                    used += encoded
                segment_bytes = header_bytes + used
                remaining -= segment_bytes
                path_remaining -= segment_bytes
                row["excerpt_bytes"] += segment_bytes
                segment["excerpt_bytes"] = segment_bytes
                segment["truncated"] = truncated
                row["segments"].append(segment)
                chunks.append(header + "".join(emitted))
                if truncated:
                    omitted_hunks += 1
            row["diff_complete"] = omitted_hunks == 0
            row["truncated"] = omitted_hunks > 0
            if omitted_hunks:
                row["omission_reason"] = f"changed_hunks_omitted:{omitted_hunks}"
            if not chunks and "omission_reason" not in row:
                row["omission_reason"] = "empty_diff"
            row["excerpt"] = "".join(chunks)
        return evidence

    def _quality_review_caller_context(
        self,
        changed_hashes: Mapping[str, str | None],
        scoped_audits: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Read the canonical source around every graph-resolved caller line.

        The rows come from the scoped audit's ``impact_evidence`` of kind
        ``callers`` -- canonical Source Graph edges into the changed symbols.
        They are lens-independent, so one lens's rows are read.  A caller that
        lives in a changed path is skipped: the diff already carries that file
        in full and the graph line would refer to the canonical bytes anyway.
        Everything read here is canonical-tree bytes at the graph's line;
        nothing is model prose.
        """

        def with_newline(line: str) -> str:
            return line if line.endswith("\n") else line + "\n"

        callers: dict[str, tuple[str, int]] = {}
        for lens in sorted(scoped_audits):
            wrapper = scoped_audits[lens]
            scope = wrapper.get("packet") if isinstance(wrapper, Mapping) else None
            if not isinstance(scope, Mapping):
                continue
            for entry in scope.get("impact_evidence") or []:
                if not isinstance(entry, Mapping) or entry.get("evidence_kind") != "callers":
                    continue
                identity = str(entry.get("identity") or "")
                path = str(entry.get("path") or "")
                line = entry.get("line_start")
                if (
                    not identity
                    or not path
                    or path.startswith("/")
                    or ".." in path.split("/")
                    or type(line) is not int
                    or line < 1
                    or path in changed_hashes
                ):
                    continue
                callers.setdefault(identity, (path, line))
            break
        context = self._QUALITY_REVIEW_CALLER_CONTEXT_LINES
        rows: list[dict[str, Any]] = []
        omitted = 0
        remaining = self._QUALITY_REVIEW_CALLER_CONTEXT_TOTAL_MAX_BYTES
        cache: dict[str, list[str]] = {}
        ordered = sorted(callers, key=lambda key: (callers[key][0], callers[key][1], key))
        for identity in ordered:
            path, line = callers[identity]
            if len(rows) >= self._QUALITY_REVIEW_CALLER_CONTEXT_MAX_ROWS:
                omitted += 1
                continue
            lines = cache.get(path)
            if lines is None:
                source = Path(self.repo) / path
                try:
                    if source.is_symlink() or not source.is_file():
                        lines = []
                    else:
                        lines = source.read_bytes().decode("utf-8").splitlines(keepends=True)
                except (OSError, UnicodeDecodeError):
                    lines = []
                cache[path] = lines
            if line > len(lines):
                omitted += 1
                continue
            start = max(1, line - context)
            end = min(len(lines), line + context)
            text = "".join(with_newline(item) for item in lines[start - 1:end])
            size = len(text.encode("utf-8"))
            if size > self._QUALITY_REVIEW_CALLER_CONTEXT_ROW_MAX_BYTES or size > remaining:
                omitted += 1
                continue
            remaining -= size
            rows.append(
                {
                    "identity": identity,
                    "path": path,
                    "line": line,
                    "line_start": start,
                    "line_end": end,
                    "source": "canonical",
                    "text": text,
                }
            )
        return {"rows": rows, "complete": omitted == 0, "omitted": omitted}

    @staticmethod
    def _quality_review_candidate_delta(
        card: Mapping[str, Any], current_hashes: Mapping[str, str | None]
    ) -> dict[str, Any] | None:
        """Mark which changed paths are byte-identical to the reviewed predecessor.

        Basis: the ``rework_predecessor.changed_path_hashes`` retained on the
        card against the current candidate's hashes.  Measured 2026-09-08:
        80% of lens launches target rework candidates and 34.6% of their
        changed paths are byte-identical to the predecessor.  Predecessor
        worktrees are retired, so this records only hash identity -- never a
        hunk diff against the predecessor and never a prior report.  ``None``
        when the card names no well-formed predecessor.
        """

        predecessor = card.get("rework_predecessor")
        if not isinstance(predecessor, Mapping):
            return None
        request_id = str(predecessor.get("request_id") or "").strip()
        hashes = predecessor.get("changed_path_hashes")
        if not request_id or not isinstance(hashes, Mapping):
            return None
        paths: dict[str, dict[str, Any]] = {}
        for path in sorted(current_hashes):
            prior = hashes.get(path) if path in hashes else None
            if prior is not None and (
                not isinstance(prior, str) or not re.fullmatch(r"[0-9a-f]{64}", prior)
            ):
                return None
            paths[path] = {
                "unchanged_since_reviewed": path in hashes and prior == current_hashes[path],
                "predecessor_sha256": prior,
            }
        return {"predecessor_request_id": request_id, "paths": paths}

    # Bounds on what the prior-review section may cost.  Reviewer children are
    # read for ONE task id, the target's own terminal history for ONE task id,
    # and both are capped, so a target with 30 rework rounds cannot turn packet
    # preparation into a table scan or the packet into a transcript.
    _PRIOR_REVIEW_MAX_REVIEWER_CARDS = 64
    _PRIOR_REVIEW_MAX_TERMINAL_EVENTS = 64

    def _prior_reviewer_receipts(
        self, target_task_id: str, exclude_request_id: str
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, str | None]]]:
        """Read EARLIER reviewer receipts for this task, and the bytes they judged.

        Two bounded reads against the canonical task store, both keyed by one
        task id:

        * the reviewer child cards (``topic='quality_review'``) whose sealed
          ``quality_review`` binding names this target task at some request
          other than the one being packaged now; and
        * this target's own ``terminal_review`` events, which are the only
          durable record of what ``changed_path_hashes`` each earlier request
          actually produced -- the card retains the latest attempt only.

        Sequential by design, and the reason is the shape of the work, not an
        oversight: this is two indexed single-key SQLite reads inside a
        connection that is already open, and the whole call is inside the
        single-flight that prepares one packet.  There is nothing to fan out
        across cores, and a pool here would only compete with the interactive
        MCP server for the same file lock.

        Never raises: a store that cannot be read yields no prior findings,
        which costs a round of re-derivation and breaks nothing.
        """

        reports: list[dict[str, Any]] = []
        hashes_by_request: dict[str, dict[str, str | None]] = {}
        conn: sqlite3.Connection | None = None
        try:
            db_path = task_store.canonical_db_path(self.repo)
            # The canonical store's own read-only opener, not a second way of
            # connecting to it: it applies this repository's busy timeout,
            # ``query_only`` and row factory, so a prior-findings read can never
            # write and never diverge from how everything else reads.
            conn = task_store._connect(Path(db_path), readonly=True)
            rows = conn.execute(
                "SELECT task_id, card_json FROM tasks "
                "WHERE topic='quality_review' AND instr(card_json, ?) > 0 "
                "ORDER BY created_at DESC, task_id DESC LIMIT ?",
                (target_task_id, self._PRIOR_REVIEW_MAX_REVIEWER_CARDS),
            ).fetchall()
            events = conn.execute(
                "SELECT payload_json FROM task_events "
                "WHERE task_id=? AND event='terminal_review' "
                "ORDER BY event_id DESC LIMIT ?",
                (target_task_id, self._PRIOR_REVIEW_MAX_TERMINAL_EVENTS),
            ).fetchall()
        except (sqlite3.Error, task_store.TaskStoreError, OSError, ValueError):
            return [], {}
        finally:
            if conn is not None:
                conn.close()

        for event in events:
            try:
                payload = json.loads(event["payload_json"] or "{}")
            except (TypeError, ValueError):
                continue
            evidence = payload.get("evidence")
            if not isinstance(evidence, Mapping):
                evidence = payload if isinstance(payload, Mapping) else {}
            request_id = str(evidence.get("request_id") or payload.get("request_id") or "")
            changed = evidence.get("changed_path_hashes")
            if not request_id or not isinstance(changed, Mapping):
                continue
            hashes_by_request.setdefault(
                request_id,
                {
                    str(key): (None if value is None else str(value))
                    for key, value in changed.items()
                },
            )

        for row in rows:
            try:
                card = json.loads(row["card_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(card, Mapping):
                continue
            terminal = card.get("terminal_review")
            evidence = terminal.get("evidence") if isinstance(terminal, Mapping) else None
            if not isinstance(evidence, Mapping):
                continue
            binding = evidence.get("quality_review")
            receipt = evidence.get("quality_review_receipt")
            if not isinstance(binding, Mapping) or not isinstance(receipt, Mapping):
                continue
            # Identity, checked here rather than trusted: this receipt must name
            # THIS target task, some OTHER request of it, and a real lens.
            if str(binding.get("target_task_id") or "") != target_task_id:
                continue
            prior_request_id = str(binding.get("target_request_id") or "")
            if not prior_request_id or prior_request_id == exclude_request_id:
                continue
            lens = str(binding.get("lens") or "")
            if lens not in quality_reviewer.REVIEWER_LENSES:
                continue
            report = receipt.get("report")
            target = receipt.get("target")
            if not isinstance(report, Mapping) or not isinstance(target, Mapping):
                continue
            if str(target.get("task_id") or "") != target_task_id:
                continue
            findings = report.get("findings")
            if not isinstance(findings, list):
                continue
            reports.append(
                {
                    "lens": lens,
                    "reviewer_task_id": str(row["task_id"] or ""),
                    "reviewer_request_id": str(
                        (receipt.get("reviewer") or {}).get("request_id") or ""
                    ),
                    "reviewer_provider": str(report.get("provider") or ""),
                    "packet_sha256": str(binding.get("packet_sha256") or ""),
                    "target_request_id": prior_request_id,
                    "findings": [f for f in findings if isinstance(f, Mapping)],
                }
            )
        return reports, hashes_by_request

    def _quality_review_prior_findings(
        self,
        *,
        target_task_id: str,
        target_request_id: str,
        current_hashes: Mapping[str, str | None],
        source_evidence: Mapping[str, Mapping[str, Any]],
        predecessor_request_id: str,
    ) -> dict[str, Any] | None:
        """Carry earlier lens findings forward, with a MECHANICAL line status.

        Measured 2026-09-08: 916 of 1,141 reviewer launches target a successor
        candidate, 34.6% of their changed paths are byte-identical to the
        predecessor, and 45 of 73 accepted targets produced zero findings across
        every lens run they ever paid for.  Every one of those rounds re-derived
        judgments the previous round already made and this repository already
        paid for, because the packet said nothing about them.

        The status attached to each finding is decided by comparing the sha256
        of the cited path in THIS candidate against the sha256 of the same path
        in the candidate that finding was written against -- the exact bytes,
        recovered from that request's own ``terminal_review`` event.  Identical
        bytes mean the cited lines still say exactly what the earlier reviewer
        read, so the line numbers carry over unchanged and the finding is the
        strongest available pointer to where to look first.  Different bytes
        mean the line numbers are not transferable and the status says so.

        Nothing here concludes that a finding was FIXED.  ``lines_changed``
        says the bytes moved, not that the defect went away; ``lines_unchanged``
        says they did not move, not that the finding was right.  Both are
        sha256 comparisons, and the judgment stays with the reviewer.

        Returns ``None`` when no earlier receipt for this task can be read --
        the packet then simply carries no prior-review section, exactly as
        before.
        """

        reports, hashes_by_request = self._prior_reviewer_receipts(
            target_task_id, target_request_id
        )
        if not reports:
            return None

        changed_paths = set(current_hashes)

        def hunk_overlap(path: str, start: int | None, end: int | None) -> bool | None:
            """Does the cited range fall inside a hunk THIS candidate changed?"""
            if start is None:
                return None
            row = source_evidence.get(path)
            segments = row.get("segments") if isinstance(row, Mapping) else None
            if not isinstance(segments, list):
                return None
            last = end if end is not None and end >= start else start
            for segment in segments:
                if not isinstance(segment, Mapping):
                    continue
                low = segment.get("changed_start_line")
                high = segment.get("changed_end_line")
                if not isinstance(low, int) or not isinstance(high, int):
                    continue
                if start <= high and last >= low:
                    return True
            return False

        lenses: dict[str, dict[str, list[dict[str, Any]]]] = {}
        omitted = 0
        for report in reports:
            lens = report["lens"]
            section = lenses.setdefault(lens, {"reports": [], "findings": []})
            if len(section["reports"]) >= quality_reviewer.MAX_PRIOR_REPORTS_PER_LENS:
                omitted += 1
                continue
            prior_hashes = hashes_by_request.get(report["target_request_id"])
            reviewer_request_id = report["reviewer_request_id"]
            if not reviewer_request_id:
                omitted += 1
                continue
            rows: list[dict[str, Any]] = []
            for finding in report["findings"]:
                raw_path = finding.get("path")
                path = str(raw_path) if isinstance(raw_path, str) and raw_path else None
                if path is not None and path not in changed_paths:
                    # The packet binds every path-bearing row to this
                    # candidate's changed set. A citation outside it is carried
                    # without the path rather than dropped, so the judgment
                    # still reaches the reviewer and nothing is silently lost.
                    path = None
                unchanged = (
                    path is not None
                    and isinstance(prior_hashes, Mapping)
                    and path in prior_hashes
                    and prior_hashes[path] is not None
                    and prior_hashes[path] == current_hashes.get(path)
                )

                # Line numbers are carried ONLY when the bytes are identical.
                # On changed bytes they are not merely stale, they are
                # misleading -- a number that points at a line the earlier
                # reviewer never read -- so they are withheld outright.
                cited: list[int | None] = []
                for field in ("line_start", "line_end"):
                    value = finding.get(field)
                    cited.append(
                        value
                        if unchanged
                        and isinstance(value, int)
                        and not isinstance(value, bool)
                        and value >= 1
                        else None
                    )
                line_start, line_end = cited
                rows.append(
                    {
                        "reviewer_request_id": reviewer_request_id,
                        "finding_id": str(finding.get("id") or ""),
                        "severity": str(finding.get("severity") or "low"),
                        "disposition": str(finding.get("disposition") or "observation"),
                        "actionable": finding.get("actionable") is True,
                        "summary": str(finding.get("summary") or ""),
                        "path": path,
                        "line_start": line_start,
                        "line_end": line_end,
                        "status": "lines_unchanged" if unchanged else "lines_changed",
                        "line_mapping": "identity" if unchanged else "unavailable",
                        "path_in_candidate": path is not None,
                        "overlaps_current_hunk": (
                            hunk_overlap(path, line_start, line_end)
                            if unchanged and path is not None
                            else None
                        ),
                    }
                )
            section["reports"].append(
                {
                    "reviewer_request_id": reviewer_request_id,
                    "reviewer_task_id": report["reviewer_task_id"],
                    "reviewer_provider": report["reviewer_provider"],
                    "target_request_id": report["target_request_id"],
                    "packet_sha256": (
                        report["packet_sha256"]
                        if re.fullmatch(r"[0-9a-f]{64}", report["packet_sha256"])
                        else None
                    ),
                    "finding_count": len(rows),
                }
            )
            section["findings"].extend(rows)

        for lens, section in lenses.items():
            # A finding on bytes that did not move is the strongest pointer the
            # packet can carry, so it is kept first when the cap bites; an
            # actionable defect outranks an observation after that.
            section["findings"].sort(
                key=lambda row: (
                    row["status"] != "lines_unchanged",
                    not row["actionable"],
                    {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(
                        row["severity"], 4
                    ),
                    row["finding_id"],
                )
            )
            cap = quality_reviewer.MAX_PRIOR_FINDINGS_PER_LENS
            if len(section["findings"]) > cap:
                omitted += len(section["findings"]) - cap
                section["findings"] = section["findings"][:cap]
            # ``finding_count`` stays the report's TRUE count and is never
            # rewritten to what survived the cap: ``clean`` is derived from it,
            # and a truncated report that reported "clean" would be a lie that
            # tells a reviewer to look away.
        if not any(section["reports"] for section in lenses.values()):
            return None
        return {
            "predecessor_request_id": predecessor_request_id,
            "lenses": lenses,
            "omitted": omitted,
        }

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

        Each caller's wait is bounded. A caller timeout is truthful but never
        retires a still-running builder: that builder remains authoritative,
        publishes its eventual result, and releases its flight and capacity.
        """

        key = (target_request_id, target_task_id)
        capacity_deadline = time.monotonic() + self._QUALITY_REVIEW_PREP_WAIT_SECONDS
        with self._QUALITY_REVIEW_PREP_CAPACITY:
            while True:
                cache = self.__dict__.setdefault("_quality_review_prepared", {})
                prepared = cache.get(key)
                if prepared is not None:
                    return {"ok": True, "prepared": prepared}
                flights = self.__dict__.setdefault("_quality_review_flights", {})
                flight = flights.get(key)
                if flight is not None:
                    owner = False
                    break
                if (
                    self._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS
                    < self._QUALITY_REVIEW_PREP_MAX
                ):
                    flight = _QualityReviewPrepFlight()
                    flights[key] = flight
                    type(self)._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS += 1
                    owner = True
                    break
                remaining = capacity_deadline - time.monotonic()
                if remaining <= 0:
                    return {
                        "ok": False,
                        "error": "quality_review_preparation_timeout",
                    }
                self._QUALITY_REVIEW_PREP_CAPACITY.wait(timeout=remaining)

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

        def _run_owner_build() -> None:
            try:
                result = self._build_quality_review_packet(
                    target_request_id, target_task_id, progress=progress
                )
            except Exception as exc:  # noqa: BLE001 -- propagate owner failure truthfully
                result = {
                    "ok": False,
                    "error": f"quality_review_target_invalid:{exc}",
                }
            with self._QUALITY_REVIEW_PREP_CAPACITY:
                if result.get("ok"):
                    cache = self.__dict__.setdefault("_quality_review_prepared", {})
                    cache[key] = result["prepared"]
                    while len(cache) > self._QUALITY_REVIEW_PREP_MAX:
                        cache.pop(next(iter(cache)))
                with flight.condition:
                    flight.result = result
                    flight.done = True
                    flight.condition.notify_all()
                flights = self.__dict__.setdefault("_quality_review_flights", {})
                if flights.get(key) is flight:
                    flights.pop(key)
                    type(self)._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS -= 1
                    self._QUALITY_REVIEW_PREP_CAPACITY.notify_all()

        builder = threading.Thread(
            target=_run_owner_build,
            name=f"aiworkhub-reviewer-prep-{target_request_id[:8]}",
            daemon=True,
        )
        try:
            builder.start()
        except Exception as exc:  # noqa: BLE001 -- thread launch can fail at runtime
            result = {
                "ok": False,
                "error": f"quality_review_target_invalid:{exc}",
            }
            with self._QUALITY_REVIEW_PREP_CAPACITY:
                with flight.condition:
                    flight.result = result
                    flight.done = True
                    flight.condition.notify_all()
                flights = self.__dict__.setdefault("_quality_review_flights", {})
                if flights.get(key) is flight:
                    flights.pop(key)
                    type(self)._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS -= 1
                    self._QUALITY_REVIEW_PREP_CAPACITY.notify_all()
            return result
        with flight.condition:
            deadline = time.monotonic() + self._QUALITY_REVIEW_PREP_OWNER_SECONDS
            while not flight.done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                flight.condition.wait(timeout=remaining)
            result = flight.result if flight.done else None
        if result is None:
            return {"ok": False, "error": "quality_review_preparation_timeout"}
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
            caller_context = self._quality_review_caller_context(
                current_hashes, scoped_audits
            )
            candidate_delta = self._quality_review_candidate_delta(
                card, current_hashes
            )
            prior_findings = self._quality_review_prior_findings(
                target_task_id=target_task_id,
                target_request_id=target_request_id,
                current_hashes=current_hashes,
                source_evidence=source_evidence,
                predecessor_request_id=str(
                    (candidate_delta or {}).get("predecessor_request_id") or ""
                ),
            )
            target_claim_epoch = card.get("claim_epoch")
            if type(target_claim_epoch) is not int or target_claim_epoch < 1:
                raise WorkspaceError("quality_review_target_claim_epoch_invalid")
            read_only_input_paths = _worker_workspace.quality_review_read_only_input_paths(
                self.repo,
                read_first=card.get("read_first") or [],
                immutable_input_paths=card.get("immutable_inputs") or [],
                candidate_changed_paths=current_hashes,
            )
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
                caller_context=caller_context,
                candidate_delta=candidate_delta,
                prior_findings=prior_findings,
            )
            # Immutable inputs are authenticated reviewer contract context and
            # read-only workspace materialization authority. Keep candidate
            # path gates pure, then bind the same declared paths into the
            # sealed packet contract and refresh its canonical digest.
            contract = packet.get("contract")
            if not isinstance(contract, dict):
                raise quality_reviewer.ReviewerEvidenceError(
                    "review_packet_contract_invalid"
                )
            contract["immutable_inputs"] = list(card.get("immutable_inputs") or [])
            packet_body = {
                key: value for key, value in packet.items() if key != "packet_sha256"
            }
            packet["packet_sha256"] = quality_reviewer._canonical_digest(packet_body)
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
            "read_only_input_paths": read_only_input_paths,
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
        if base.get("reviewer_claim_epoch") is not None:
            event["reviewer_claim_epoch"] = base.get("reviewer_claim_epoch")
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
                provider_pid, provider_pid_ambiguous = _parse_durable_pid(
                    event.get("provider_pid")
                )
                if provider_pid_ambiguous:
                    return _admit(request_id)
                if provider_pid and _pid_identity_evidence(
                    provider_pid, event.get("provider_pid_start_ticks")
                ).verdict is not PidIdentityVerdict.MISMATCH:
                    return _admit(request_id)
                owner_pid, owner_pid_ambiguous = _parse_durable_pid(event.get("owner_pid"))
                if owner_pid_ambiguous:
                    return _admit(request_id)
                if owner_pid and _pid_identity_evidence(
                    owner_pid, event.get("owner_pid_start_ticks")
                ).verdict is not PidIdentityVerdict.MISMATCH:
                    return _admit(request_id)
                continue
            if state not in ACTIVE_PROCESS_STATES:
                continue
            pid, pid_ambiguous = _parse_durable_pid(event.get("pid"))
            if pid_ambiguous:
                return _admit(request_id)
            if state == "starting" and not pid:
                owner_pid, owner_pid_ambiguous = _parse_durable_pid(event.get("owner_pid"))
                if owner_pid_ambiguous:
                    return _admit(request_id)
                owner_identity = (
                    _pid_identity_evidence(
                        owner_pid, event.get("owner_pid_start_ticks")
                    ).verdict
                    if owner_pid > 0
                    else None
                )
                if (
                    owner_pid > 0
                    and owner_identity is not PidIdentityVerdict.MISMATCH
                ):
                    return _admit(request_id)
                if self._reviewer_source_graph_prewarm_live_event(event):
                    return _admit(request_id)
                try:
                    reservation_deadline = float(
                        event.get("reservation_expires_at_epoch") or 0.0
                    )
                except (TypeError, ValueError, OverflowError):
                    # A malformed lease is ambiguous evidence.  Admission must
                    # preserve the reservation instead of raising and allowing
                    # a retry to mint a second provider.
                    return _admit(request_id)
                if not math.isfinite(reservation_deadline):
                    return _admit(request_id)
                if reservation_deadline > time.time():
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
                unbound_claim_resolutions = self._resolve_unbound_reviewer_claims(
                    latest
                )
                self._reconcile_expired_starting_reservations(
                    proven,
                    resolved=True,
                    _unbound_claim_resolutions=unbound_claim_resolutions,
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

    def _bind_reviewer_claim_epoch(
        self, request_id: str, reviewer_claim_epoch: int
    ) -> bool:
        """Durably bind a just-committed reviewer claim to its reservation."""

        snapshot = self._latest_by_request_stable()
        with self._registry_lock():
            proven = self._proven_reservation_snapshot(snapshot)
            if proven is None:
                return False
            latest = proven[0].get(request_id) or {}
            if latest.get("state") != "starting":
                return False
            existing = latest.get("reviewer_claim_epoch")
            if existing is not None:
                return existing == reviewer_claim_epoch
            bound = dict(latest)
            bound.pop("claim_binding_state", None)
            bound["state"] = REVIEWER_CLAIM_BOUND_STATE
            bound["reviewer_claim_epoch"] = reviewer_claim_epoch
            self._append_event(bound)
            return True

    def _retain_ambiguous_reviewer_claim_for_reconciliation(
        self, request_id: str, reviewer_task_id: str, runner: str
    ) -> bool:
        """Persist authority to recover a claim whose commit result is unreadable."""

        with self._registry_lock():
            latest = self._latest_by_request().get(request_id) or {}
            if (
                latest.get("state") != "starting"
                or latest.get("task_id") != reviewer_task_id
                or latest.get("runner") != runner
            ):
                return False
            if latest.get("reviewer_claim_epoch") is not None:
                return True
            if latest.get("claim_recovery_state") == "claim_commit_ambiguous":
                return True
            retained = dict(latest)
            retained["claim_recovery_state"] = "claim_commit_ambiguous"
            retained["claim_recovery_reason"] = (
                "quality_review_claim_receipt_and_canonical_read_failed"
            )
            self._append_event(retained)
            return True

    def _recover_ambiguous_reviewer_claims(self) -> int:
        """Bind exact canonical claim epochs for retryable ambiguous reservations."""

        latest, generation = self._latest_by_request_stable()
        if generation is None:
            return 0
        recovered = 0
        for request_id, event in latest.items():
            if (
                event.get("state") != "starting"
                or event.get("topic") != "quality_review"
                or event.get("claim_recovery_state") != "claim_commit_ambiguous"
                or event.get("reviewer_claim_epoch") is not None
            ):
                continue
            task_id = str(event.get("task_id") or "").strip()
            runner = str(event.get("runner") or "").strip()
            if not task_id or not runner:
                continue
            try:
                card = task_store.get_task(self.repo, task_id)
            except Exception:  # noqa: BLE001 -- transient store failure retries
                continue
            if not isinstance(card, dict):
                continue
            claim_epoch = card.get("claim_epoch")
            if (
                card.get("status") != "processing"
                or card.get("worker_status") != "claimed"
                or card.get("claimed_by") != runner
                or card.get("launch_request_id") != request_id
                or not _is_bool_safe_int(claim_epoch)
                or int(cast(int, claim_epoch)) < 1
            ):
                continue
            if self._bind_reviewer_claim_epoch(
                request_id, int(cast(int, claim_epoch))
            ):
                recovered += 1
        return recovered

    def _retain_reviewer_claim_for_reconciliation(
        self, request_id: str, reviewer_task_id: str, reviewer_claim_epoch: int
    ) -> bool:
        """Bind authenticated claim authority after binding and release both fail."""

        with self._registry_lock():
            latest = self._latest_by_request().get(request_id) or {}
            if (
                latest.get("state") != "starting"
                or latest.get("task_id") != reviewer_task_id
            ):
                return False
            existing = latest.get("reviewer_claim_epoch")
            if existing is not None:
                return existing == reviewer_claim_epoch
            bound = dict(latest)
            bound.pop("claim_binding_state", None)
            bound["state"] = REVIEWER_CLAIM_BOUND_STATE
            bound["reviewer_claim_epoch"] = reviewer_claim_epoch
            bound["claim_recovery_reason"] = (
                "quality_review_claim_binding_and_release_failed"
            )
            self._append_event(bound)
            return True

    def _release_or_retain_reviewer_claim_after_launch_failure(
        self,
        *,
        request_id: str,
        task_id: str,
        runner: str,
        reviewer_claim_epoch: int,
        reason: str,
    ) -> tuple[dict[str, Any], bool]:
        """Release an exact claimed reviewer, or retain it for lease recovery."""

        released = task_engine.mark_launch_failed(
            self.repo,
            task_id,
            runner,
            reason=reason[:500],
            request_id=request_id,
        )
        if released.get("ok"):
            return released, False
        try:
            retained = self._retain_reviewer_claim_for_reconciliation(
                request_id, task_id, reviewer_claim_epoch
            )
        except Exception:  # noqa: BLE001 -- caller must emit terminal diagnostics
            retained = False
        return released, retained

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
            if epoch is not None and _is_bool_safe_int(epoch) and int(epoch) >= 1:
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
        terminal_intent_recorded = False
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
            terminal_intent_recorded = self._record_reviewer_terminal_intent(
                request_id, latest, reason
            ) in {"recorded", "already_recorded"}
            if (
                not terminal_intent_recorded
                and latest.get("reviewer_claim_epoch") is not None
            ):
                return
            self._blocked(
                task_id, runner, "quality_review", adapter_id, reason,
                request_id=request_id,
            )
        if terminal_intent_recorded:
            self._settle_reviewer_terminal_intents_contained()

    def _reviewer_source_graph_prewarm_live_event(
        self, event: Mapping[str, Any]
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
        owner_pid, owner_pid_ambiguous = _parse_durable_pid(event.get("owner_pid"))
        if owner_pid_ambiguous:
            return True
        if not owner_pid:
            return False
        return (
            _pid_identity_evidence(
                owner_pid, event.get("owner_pid_start_ticks")
            ).verdict
            is not PidIdentityVerdict.MISMATCH
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
            # The shared preparation seals every lens's scoped audit into one
            # packet (single-flight per target).  This lens is handed the
            # packet that carries exactly ITS scope and a digest recomputed
            # over that body, so packet_read, submit and the receipt verifier
            # all bind to what this reviewer actually sees -- and the packet
            # fits the inline transport instead of a three-lens file.
            try:
                lens_packet = quality_reviewer.build_lens_packet(
                    prepared["packet"], lens=lens
                )
            except quality_reviewer.ReviewerEvidenceError as exc:
                _fail(f"quality_review_preparation_failed:lens_packet:{exc}"[:500])
                return
            _progress("packet_prepared")
            binding = {
                "target_request_id": target_request_id,
                "target_task_id": target_task_id,
                "target_claim_epoch": (
                    lens_packet.get("target", {}).get("claim_epoch")
                ),
                "adapter_id": adapter_id,
                "source_workspace": prepared["workspace"].as_metadata(),
                "candidate_paths": sorted(prepared["changed_hashes"]),
                "read_only_input_paths": list(
                    prepared.get("read_only_input_paths") or []
                ),
                "packet": lens_packet,
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
            claimed_card = _committed_claim_card(
                claim,
                request_id=request_id,
                task_id=reviewer_task_id,
                runner=runner,
                topic="quality_review",
            )
        except LaunchRejected as receipt_exc:
            try:
                canonical = _parse_card(
                    self._show_task(reviewer_task_id), reviewer_task_id
                )
                claimed_card = _committed_claim_card(
                    {
                        "ok": True,
                        "returncode": 0,
                        "stdout": json.dumps(canonical),
                    },
                    request_id=request_id,
                    task_id=reviewer_task_id,
                    runner=runner,
                    topic="quality_review",
                )
            except LaunchRejected as recovery_exc:
                recovery_retained = False
                try:
                    recovery_retained = (
                        self._retain_ambiguous_reviewer_claim_for_reconciliation(
                            request_id, reviewer_task_id, runner
                        )
                    )
                except Exception:  # noqa: BLE001 -- return stable retry evidence
                    recovery_retained = False
                return {
                    "ok": False,
                    "error": f"quality_review_claim_failed:{receipt_exc}"[:500],
                    "recovery_state": "starting_reservation_retained",
                    "claim_recovery_bound": recovery_retained,
                    "claim_recovery_error": str(recovery_exc)[:500],
                }
        claim_epoch = claimed_card.get("claim_epoch")
        binding_persisted = False
        if type(claim_epoch) is int and claim_epoch >= 1:
            try:
                binding_persisted = self._bind_reviewer_claim_epoch(
                    request_id, claim_epoch
                )
            except Exception:  # noqa: BLE001 -- claim must never be stranded
                binding_persisted = False
        if not binding_persisted:
            # Terminalize the reservation only after the canonical task release
            # succeeds.  If that CAS/store operation fails, preserving the
            # starting reservation keeps it eligible for the periodic reaper.
            released = task_engine.mark_launch_failed(
                self.repo,
                reviewer_task_id,
                runner,
                reason="quality_review_claim_binding_failed",
                request_id=request_id,
            )
            if released.get("ok") is not True:
                recovery_bound = False
                if type(claim_epoch) is int and claim_epoch >= 1:
                    try:
                        recovery_bound = self._retain_reviewer_claim_for_reconciliation(
                            request_id, reviewer_task_id, claim_epoch
                        )
                    except Exception:  # noqa: BLE001 -- report retryable evidence
                        recovery_bound = False
                return {
                    "ok": False,
                    "error": "quality_review_claim_binding_failed",
                    "recovery_state": "starting_reservation_retained",
                    "claim_recovery_bound": recovery_bound,
                    "release_error": str(
                        released.get("stderr")
                        or released.get("stdout")
                        or released.get("error")
                        or ""
                    )[:500],
                }
            return _fail("quality_review_claim_binding_failed")
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
        """Start one worker in an isolated worktree, or refuse without claiming.

        The implementation lives in
        :func:`process_launcher_launch_isolated.launch_isolated`; this method is
        the delegation and holds no logic of its own.  See that module for the
        contract, which is unchanged by the move.
        """
        return _launch_isolated_impl(
            self,
            task_id=task_id,
            runner=runner,
            topic=topic,
            adapter_id=adapter_id,
            model=model,
            owner_prompt=owner_prompt,
            timeout_seconds=timeout_seconds,
            quality_review_binding=quality_review_binding,
            reserved_request_id=reserved_request_id,
            prewarm_progress=prewarm_progress,
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
            pid, pid_ambiguous = _parse_durable_pid(event.get("pid"))
            if pid_ambiguous:
                raise LaunchRejected(
                    f"duplicate_persisted_task:{event.get('request_id')}"
                )
            ticks = event.get("pid_start_ticks")
            if (
                event.get("state") == "starting"
                and not pid
                and _reservation_deadline_is_live(
                    event.get("reservation_expires_at_epoch")
                )
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
            model = validate_workforce_identity(
                runner,
                adapter_id,
                model,
                risk_tier=card.get("risk_tier"),
                repo=self.repo,
            )
            memory_admission = _memory_launch_admission()
            if not memory_admission["admit"]:
                raise LaunchRejected(
                    "memory_launch_capacity_denied:"
                    + json.dumps(
                        memory_admission, sort_keys=True, separators=(",", ":")
                    )
                )
            external_readonly_dirs = _external_readonly_dirs(card, adapter_id)
            context_result = project_context.collect_project_context(self.repo, card)
            provider_env, model = self._resolve_provider_env(adapter_id, model)
            request_id = uuid.uuid4().hex
            prompt_budget: dict[str, Any] = {}
            prompt = build_worker_prompt(
                task_id=task_id,
                runner=runner,
                topic=topic,
                request_id=request_id,
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
                        pid, pid_ambiguous = _parse_durable_pid(event.get("pid"))
                        if pid_ambiguous:
                            raise LaunchRejected(
                                f"duplicate_persisted_task:{event.get('request_id')}"
                            )
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
                        **process_group_launch_kwargs(os.name),
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

    def _remove_live_if_current(self, live: _LiveProcess) -> bool:
        if self._live.get(live.request_id) is not live:
            return False
        self._live.pop(live.request_id, None)
        self._cancelled.discard(live.request_id)
        return True

    def _monitor(self, live: _LiveProcess) -> None:
        if live.isolated:
            try:
                self._await_exit_watching_zero_delta(live)
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
                    self._remove_live_if_current(live)
                return
            self._finalize_after_process_exit(live.request_id, live.process.poll())
            with self._lock:
                self._remove_live_if_current(live)
            return

        self._monitor_direct_for_tests(live)

    def _await_exit_watching_zero_delta(self, live: _LiveProcess) -> None:
        """Wait for the worker exactly as before, observing the delta meanwhile.

        The wait is sliced so the already-running monitor thread can look at
        the isolated workspace it is supervising. A code task that reaches
        its bounded deadline without changing any allowed write is cancelled
        through the normal lifecycle path; real deltas and explicit read-only
        or unchanged-output exemptions settle the observer without action.

        A test double whose ``wait`` takes no ``timeout`` falls back to the
        original blocking call, so the monitor keeps working against any
        process-like object it was already given.
        """

        started = time.monotonic()
        while True:
            try:
                live.process.wait(timeout=ZERO_DELTA_POLL_SECONDS)
                return
            except subprocess.TimeoutExpired:
                pass
            except TypeError:
                live.process.wait()
                return
            notice = self._maybe_emit_zero_delta_notice(
                live, elapsed_seconds=time.monotonic() - started
            )
            if notice is not None and notice.get("enforced") is True:
                self.cancel(live.request_id, reason=ZERO_DELTA_TERMINAL_REASON)

    def _maybe_emit_zero_delta_notice(
        self, live: _LiveProcess, *, elapsed_seconds: float
    ) -> dict[str, Any] | None:
        """Append the one zero-delta notice this run is owed, if it is owed.

        Returns the appended event, or ``None`` when nothing was emitted.  The
        notice carries no lifecycle ``state`` and is tagged as a runtime
        notice, so the reconcilers and reporters that read the ledger for a
        request's state never see it (see ``_latest_by_request``). The monitor
        separately routes an enforcing notice through ``cancel`` so terminal
        state and reason remain canonical lifecycle evidence.
        """

        if live.zero_delta_tripwire_settled or live.metadata_path is None:
            return None
        try:
            metadata = json.loads(live.metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(metadata, dict):
            return None
        workspace_metadata = metadata.get("workspace")
        if not isinstance(workspace_metadata, dict):
            return None
        try:
            workspace = WorkerWorkspace.from_metadata(dict(workspace_metadata))
        except (KeyError, TypeError, ValueError, OSError):
            return None
        try:
            observation = evaluate_zero_delta_tripwire(
                workspace=workspace,
                elapsed_seconds=elapsed_seconds,
                timeout_seconds=live.timeout_seconds,
                required_outputs=metadata.get("required_outputs") or (),
                read_only=metadata.get("read_only") is True,
                allow_unchanged_required_outputs=(
                    metadata.get("allow_unchanged_required_outputs") or ()
                ),
            )
        except OSError:
            # An unreadable workspace is not evidence of an empty run.
            return None
        if observation.settled:
            live.zero_delta_tripwire_settled = True
        if observation.notice is None:
            return None
        return self._append_event({
            "request_id": live.request_id,
            "task_id": live.task_id,
            "runner": live.runner,
            "topic": live.topic,
            "adapter_id": live.adapter_id,
            "model": live.model,
            "event_kind": RUNTIME_NOTICE_EVENT_KIND,
            "pid": live.process.pid,
            **observation.notice,
        })

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
        self._retention_event({
            **lineage,
            "request_id": request_id,
            "state": "reconcile_pending",
            "pid": latest.get("pid"),
            "pid_start_ticks": latest.get("pid_start_ticks"),
            "metadata_path": latest.get("metadata_path"),
            "supervisor_status_path": latest.get("supervisor_status_path"),
            "bridge_provider_may_be_active": True,
            "bridge_cancel_status": "failed",
            "reconciliation_deferred": "bridge_cancel_publication_failed",
            "error": error[:500],
        }, disposition="retained_in_place")
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
                self._remove_live_if_current(live)
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
            claim_authority=None,
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
            **terminal_failure_classification.terminal_event_authority(state=state, exit_code=returncode, error=None, stdout_path=live.stdout_path, stderr_path=live.stderr_path, cancelled=was_cancelled),
        })
        with self._lock:
            self._remove_live_if_current(live)

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
        claim_authority: Mapping[str, Any] | None = None,
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
        except Exception:
            pass
        usage["role"] = usage_role
        event_payload: dict[str, Any] = {
            "runner": runner,
            # Request-time topic: the backfill writer persists it and the
            # ledger attributes by it; omitting it left 31% of input tokens
            # unattributed although every claim carries the topic.
            "topic": str(topic or ""),
            "model": ledger_model,
            "requested_model": model,
            "observed_model": str(usage.get("observed_model") or ""),
            "role": usage_role,
            "provider": (
                "deterministic_validation_replay"
                if execution_mode == "validation_only_replay"
                else adapter_id.removesuffix("_cli")
            ),
            "input_tokens": total_input,
            "output_tokens": total_output,
            "visible_output_tokens": usage["output_tokens"],
            "reasoning_output_tokens": usage["reasoning_output_tokens"],
            "total_tokens": total_input + total_output,
            "cached_input_tokens": usage["cached_input_tokens"],
            "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
            "cache_write_input_tokens": usage["cache_write_input_tokens"],
            "telemetry_reason": str(usage["telemetry_reason"]),
            "cost_usd": (
                float(usage["cost_usd"] or 0.0)
                if usage.get("cost_observed")
                else 0.0
            ),
            "usage_observed": bool(usage.get("usage_observed")),
            "model_observed": bool(usage.get("model_observed")),
            "cache_metrics_observed": bool(usage.get("cache_metrics_observed")),
            "cost_observed": bool(usage.get("cost_observed")),
            "adapter_id": adapter_id,
            "execution_mode": usage["execution_mode"],
            "provider_launched": usage["provider_launched"],
        }
        if claim_authority is None:
            return usage, False, "claim_authority_unavailable"
        claim_request_id = str(claim_authority.get("request_id") or "")
        claimed_by = str(claim_authority.get("claimed_by") or "")
        claim_epoch = claim_authority.get("claim_epoch")
        if claim_request_id != request_id:
            return usage, False, "claim_authority_request_mismatch"
        if claimed_by != runner:
            return usage, False, "claim_authority_claimed_by_mismatch"
        if type(claim_epoch) is not int or claim_epoch < 1:
            return usage, False, "claim_authority_claim_epoch_invalid"
        try:
            usage_recorded, usage_result = task_store.append_live_usage_event(
                self.repo,
                task_id,
                runner,
                request_id=request_id,
                claimed_by=claimed_by,
                claim_epoch=claim_epoch,
                payload=event_payload,
            )
            usage_error = "" if usage_recorded else usage_result
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

    def _request_pid_identity(self, events: list[dict[str, Any]]) -> PidIdentityEvidence:
        """PID identity from the merged request history, never the tail row.

        An advisory runtime notice carries ``pid`` without ``pid_start_ticks``,
        so ``events[-1]`` alone downgraded a decidable MISMATCH to UNKNOWN --
        and UNKNOWN defers, forever, for any worker quiet enough to earn one.
        """
        merged = self._event_identity(events)
        return _pid_identity_evidence(merged.get("pid"), merged.get("pid_start_ticks"))

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

    def _retry_claude_auth_refresh(
        self,
        *,
        request_id: str,
        metadata_path: Path,
        metadata: dict[str, Any],
        workspace: WorkerWorkspace,
        provider_launch_failure: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if str(metadata.get("adapter_id") or "") != "claude_cli":
            return None
        if int(metadata.get("claude_auth_retry_count") or 0) >= 1:
            return None
        classified = claude_auth.classify_runtime_auth_failure(
            http_status=provider_launch_failure.get("http_status"),
            error_code=provider_launch_failure.get("error_code"),
            session_id=provider_launch_failure.get("session_id"),
        )
        if classified is None:
            return None
        worker_argv = metadata.get("worker_argv")
        if not isinstance(worker_argv, list) or not all(
            isinstance(value, str) and value for value in worker_argv
        ):
            return None
        worker_cwd = str(metadata.get("worker_cwd") or "").strip()
        if not worker_cwd:
            return None

        host_auth = claude_auth.refresh_subscription_session_for_retry()
        if host_auth.get("launchable") is not True:
            return None
        projection = _worker_workspace.refresh_claude_credential_projection(
            workspace.home
        )
        status_path = Path(str(metadata["supervisor_status_path"]))
        stdout_path = Path(str(metadata["stdout_path"]))
        stderr_path = Path(str(metadata["stderr_path"]))
        cancel_path = Path(str(metadata["cancel_path"]))
        spec_path = self.process_dir / f"{request_id}.supervisor-spec.json"
        for stale_path in (status_path, stdout_path, stderr_path, cancel_path):
            unlink_if_regular(stale_path)
        _touch_0600(stdout_path)
        _touch_0600(stderr_path)
        timeout_seconds = int(metadata.get("timeout_seconds") or 30)
        write_json_0600(
            spec_path,
            {
                "argv": list(worker_argv),
                "cwd": worker_cwd,
                **_legacy_timeout_fields(timeout_seconds),
                "status_path": str(status_path),
                "cancel_path": str(cancel_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "max_output_bytes": MAX_WORKER_STREAM_LOG_BYTES,
                "adapter_id": "claude_cli",
                "token_budget": metadata.get("token_budget"),
            },
        )
        launch_env = worker_launch_env(
            "claude_cli",
            repo=self.repo,
            request_id=request_id,
            home=workspace.home,
            isolated_task_queue_db=True,
        )
        supervisor = _worker_supervisor_script()
        process = self._popen(
            [sys.executable, str(supervisor), "--spec", str(spec_path)],
            cwd=worker_cwd,
            env=launch_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            **process_group_launch_kwargs(os.name),
        )
        start_ticks = _pid_start_ticks(process.pid)
        if start_ticks is None:
            _terminate_process_group(process.pid, grace_seconds=5.0)
            raise WorkspaceError("supervisor_pid_identity_unavailable")
        updated = {
            **metadata,
            "claude_auth_retry_count": 1,
            "claude_auth_retry": {
                "schema_id": "aiworkhub.claude_auth_retry.v1",
                "http_status": classified["http_status"],
                "session_id_sha256": classified["session_id_sha256"],
                "credential_projection_refreshed": bool(
                    projection.get("refreshed")
                ),
                "credential_projection_sha256": str(
                    projection.get("destination_sha256") or ""
                ),
                "host_auth_refreshed": True,
            },
        }
        write_json_0600(metadata_path, updated)
        started_at = _utcnow()
        live = _LiveProcess(
            request_id=request_id,
            task_id=str(metadata["task_id"]),
            runner=str(metadata["runner"]),
            topic=str(metadata["topic"]),
            adapter_id="claude_cli",
            model=metadata.get("model"),
            process=process,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            isolated=True,
            metadata_path=metadata_path,
            supervisor_status_path=status_path,
            pid_start_ticks=start_ticks,
            claim_epoch=(
                metadata.get("claim_epoch")
                if type(metadata.get("claim_epoch")) is int
                else None
            ),
        )
        with self._lock:
            self._live[request_id] = live
        event = self._append_event({
            "request_id": request_id,
            "task_id": metadata.get("task_id"),
            "runner": metadata.get("runner"),
            "topic": metadata.get("topic"),
            "adapter_id": "claude_cli",
            "model": metadata.get("model"),
            "state": "running",
            "pid": process.pid,
            "pid_start_ticks": start_ticks,
            "started_at": started_at,
            "timeout_seconds": timeout_seconds,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
            "prompt_sha256": metadata.get("prompt_sha256"),
            "claude_auth_retry_count": 1,
            "claude_auth_session_id_sha256": classified["session_id_sha256"],
            "workspace_isolated": True,
            "sandbox_backend": metadata.get("sandbox_backend"),
            "shell": False,
        })
        thread = threading.Thread(
            target=self._monitor,
            args=(live,),
            name=f"aiworkhub-task-{request_id[:8]}-auth-retry",
            daemon=True,
        )
        thread.start()
        return event

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
        defer without changing durable state.

        Exhausting that bounded retry is then classified, not assumed. An
        exhausted run whose every attempt ended on a contended or
        temporarily-unavailable boundary is deferred as ``reconcile_pending``
        for the reconciler to re-arm, up to a finite per-request budget --
        250ms of in-line retry is far narrower than the lock windows those
        boundaries actually have, and a card must not be blocked on that gap.
        An exhausted run that produced no terminal event, or that failed for
        any other reason, is decided: it converts the still-processing card
        into a truthful ``finalize_failed`` terminal outcome and enqueues its
        manager callback. The isolated workspace remains retained for
        diagnosis in every case.
        """
        finalization_started = time.monotonic()

        errors: list[str] = []
        # The cause that ended each exhausted attempt, in order: the exception
        # raised, or ``None`` when the attempt simply produced no terminal
        # event. Exhaustion is classified from these objects, never from the
        # flattened ``errors`` strings, so a provider message that happens to
        # contain an exception type name can never be mistaken for that type.
        attempt_causes: list[BaseException | None] = []
        # The bounded in-line budget stays deliberately short: it exists only
        # to ride out a sub-second write race without blocking the daemon
        # monitor thread. A contended SQLite or filesystem boundary that needs
        # longer is handled by deferring to the reconciler below, not by
        # sleeping here.
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
                    identity = self._request_pid_identity(deferred)
                    if identity.verdict is PidIdentityVerdict.MATCH:
                        return latest
                    if identity.verdict is PidIdentityVerdict.UNKNOWN:
                        return {
                            **latest,
                            "reconciliation_deferred": "pid_identity_unknown",
                            "workspace_retained": True,
                        }
                errors.append(f"attempt={attempt}:no_terminal_event")
                attempt_causes.append(None)
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
                attempt_causes.append(exc)
            except Exception as exc:  # noqa: BLE001 - monitor must remain durable
                errors.append(f"attempt={attempt}:{type(exc).__name__}:{exc}"[:500])
                attempt_causes.append(exc)

        events = self._request_events(request_id)
        event_identity = self._event_identity(events)
        task_id = str(event_identity.get("task_id") or "")
        runner = str(event_identity.get("runner") or "")
        error = "finalizer_retries_exhausted:" + "|".join(errors)

        # Exhausting three attempts inside a 250ms budget is not by itself
        # evidence that the card failed. When every attempt ended on a
        # contended or temporarily-unavailable boundary -- the filesystem and
        # SQLite races this loop's own docstring names -- the reconcile pass
        # thirty seconds from now can still succeed. Record a non-terminal
        # ``reconcile_pending`` deferral and let the reconciler re-arm it
        # instead of a human: ``FINALIZATION_PENDING_STATES`` contains
        # ``reconcile_pending``, so ``_reconcile_persisted_requests`` picks the
        # request up again on its next pass with no operator action at all.
        # ``no_terminal_event`` is deliberately NOT transient -- see
        # ``_finalizer_attempt_is_transient`` for why re-arming it could only
        # loop. The budget bounds the deferral so that a deterministic
        # transient-shaped failure still settles: once it is spent, the
        # original terminal path below runs unchanged.
        deferrals_used = sum(
            1 for prior in events if prior.get("finalizer_transient_deferral")
        )
        if (
            attempt_causes
            and all(_finalizer_attempt_is_transient(c) for c in attempt_causes)
            and deferrals_used < FINALIZER_TRANSIENT_DEFERRAL_BUDGET
        ):
            return self._retention_event({
                **event_identity,
                "request_id": request_id,
                "state": "reconcile_pending",
                "exit_code": supervisor_returncode,
                "finalizer_abandoned": False,
                "finalizer_transient_deferral": True,
                "finalizer_transient_deferrals": deferrals_used + 1,
                "finalizer_transient_deferral_budget": (
                    FINALIZER_TRANSIENT_DEFERRAL_BUDGET
                ),
                "reconciliation_deferred": FINALIZER_TRANSIENT_DEFERRAL_REASON,
                "finalize_attempts": len(errors),
                "release_transition_ok": False,
                "callback_enqueued": False,
                "finalization_duration_ms": round(
                    (time.monotonic() - finalization_started) * 1000.0, 3
                ),
                **terminal_failure_classification.terminal_event_authority(
                    state="reconcile_pending",
                    exit_code=supervisor_returncode,
                    error=error[:500],
                ),
            }, disposition="retained_in_place")

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
        return self._retention_event({
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
            "finalizer_abandoned": terminal_state == "finalize_abandoned",
            "release_transition_ok": bool(release_result.get("ok")),
            "callback_enqueued": bool(release_result.get("callback_enqueued")),
            "finalization_duration_ms": round(
                (time.monotonic() - finalization_started) * 1000.0, 3
            ),
            **terminal_failure_classification.terminal_event_authority(state=terminal_state, exit_code=supervisor_returncode, error=error_detail[:500]),
        }, disposition="retained_in_place")

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

            pid, pid_ambiguous = _parse_durable_pid(event.get("pid"))
            if pid_ambiguous or not pid:
                # Malformed or absent durable identity cannot prove exit.
                continue
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

    def reconcile(self, *, include_gc: bool = True) -> dict[str, Any]:
        """One pass; ``include_gc`` controls the housekeeping half.

        Process finalization and expired reviewer-reservation retirement are
        correctness work and run every pass. The workspace sweep is optional
        housekeeping because re-proving pinned predecessors is comparatively
        expensive.
        """
        ambiguous_claims_recovered = self._recover_ambiguous_reviewer_claims()
        reservations_retired = self._reconcile_expired_starting_reservations()
        # Retirement first records a durable terminal intent while holding only
        # the registry lock. Settle the exact task claim after that lock is
        # released; a transient store lock leaves the intent resumable for the
        # next scan instead of losing or duplicating the terminal transition.
        terminal_intents_settled = (
            self._settle_reviewer_terminal_intents_contained()
        )
        result = self._reconcile_persisted_requests()
        result["ambiguous_claims_recovered"] = ambiguous_claims_recovered
        gc_result: dict[str, int] = (
            self._gc_finalized_workspaces()
            if include_gc
            else {"gc_scanned": 0, "gc_cleaned": 0, "gc_skipped": 0}
        )
        review_result: dict[str, Any] = dict(attempted=0, completed=0, failed=0, pending=0, review_actions={})
        review_db = review_orchestrator.canonical_review_db(self)
        if review_db is None:
            automation_result = dict(automation_retried=0, automation_seeded=0,
                automation_failed=0, automation_failures=[], automation_unavailable=True)
        else:
            automation_result = review_orchestrator.retry_pending_registrations(
                self, db_path=review_db, events=self._latest_by_request())
            driver = review_orchestrator.ReviewOrchestrator(self, db_path=review_db)
            # One action per reconcile pass could not work off an outbox holding
            # 627 chains and 12 actions each: measured, 30 launches completed
            # automatically while 570 were typed by hand. The bound itself is
            # unchanged -- ``drain`` has always clamped to
            # DEFAULT_DRAIN_MAX_ACTIONS -- this call simply stops asking for one
            # twelfth of it.
            review_result = driver.drain().as_dict()
        return {
            "ok": True,
            "reservations_retired": reservations_retired,
            "terminal_intents_settled": terminal_intents_settled,
            **result,
            **gc_result,
            **review_result,
            **automation_result,
        }

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
        repo: Path | None = None, process_state: str | None = None,
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
        # A failed successor moves the task to blocked, but the predecessor
        # remains the only hash-pinned reviewed candidate a manager can recover
        # -- an identity invariant, not a pending-status convenience. It holds
        # only while that recovery is POSSIBLE: recovery fails closed on
        # non-blocked tasks, so an archived card's pin protects a path nobody
        # can take. Measured: 30 of 153 retained worktrees were held that way.
        if pinned_request_id == request_id:
            if repo is not None and _worker_workspace.has_verified_rework_delta(
                predecessor, authority_repo=repo
            ):
                return True, "sealed_rework_delta"
            if canonical_status not in task_fsm.REWORK_RECOVERABLE_STATUSES:
                return True, f"pin_unrecoverable_task_status:{canonical_status}"
            return False, "pinned_rework_predecessor"
        if process_state == "finalize_failed" and canonical_status not in {"finished", "archived"}:
            return False, "retryable_finalize_failed"  # a retry can still recover it
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
            isinstance(terminal, dict)
            and stored_hashes is None
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
        if not event.get("workspace_retained") or event.get("workspace_quarantined"):
            return None
        with self._lock:
            if request_id in self._active_request_ids():
                return None
        with self._request_lock(request_id):
            # Re-read under the lock through the SAME projection the prefilter used; a full _request_events replay per request is a 38-min sweep.
            latest = self._latest_by_request().get(request_id)
            if latest is None:
                return None
            if (
                latest.get("state") not in GC_CANDIDATE_PROCESS_STATES
                or not latest.get("workspace_retained") or latest.get("workspace_quarantined")
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
                card, request_id, repo=self.repo, process_state=str(latest.get("state") or ""))
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
                prior_state = str(latest.get("state") or "")
                retention_state = "finalize_failed" if prior_state in {"review_ready", "exited", "exited_without_review"} else prior_state
                # The "still in review" guard protects bytes that EXIST; bytes already gone have nothing to preserve, so reclaim the retained record through the ordinary idempotent cleanup path (audited).
                if integrity_reason == "review_workspace_missing":
                    try:
                        cleanup_workspace(repo, path, home)
                    except WorkspaceError as exc:
                        self._retention_event({
                            "request_id": request_id,
                            "task_id": task_id,
                            "runner": runner,
                            "topic": latest.get("topic"),
                            "adapter_id": latest.get("adapter_id"),
                            "state": retention_state,
                            "error": f"retained_workspace_cleanup_failed:{exc}"[:500],
                            "workspace_gc": False,
                            "workspace_gc_at": _utcnow(),
                            "workspace_gc_reason": integrity_reason,
                            "review_transition_ok": True,
                            "callback_enqueued": bool(
                                transition.get("callback_enqueued")
                            ),
                        }, disposition="retained_in_place")
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
                    # Bytes already gone: this branch removes (disposition "removed").  GC runs only on a card already in a terminal GC-candidate state, so an adjudicated outcome always exists here -- keep it like the quarantine branch, never mask it.
                    self._retention_event({
                        "request_id": request_id,
                        "task_id": task_id,
                        "runner": runner,
                        "topic": latest.get("topic"),
                        "adapter_id": latest.get("adapter_id"),
                        "state": retention_state,
                        "error": f"retained_workspace_missing_reclaimed:{integrity_reason}"[:500],
                        "workspace_gc": True,
                        "workspace_gc_at": _utcnow(),
                        "workspace_gc_reason": integrity_reason,
                        "review_transition_ok": True,
                        "callback_enqueued": bool(
                            transition.get("callback_enqueued")
                        ),
                    }, disposition="removed")
                    return {
                        "request_id": request_id,
                        "gc": True,
                        "reason": integrity_reason,
                    }
                # A failed integrity check on bytes that still EXIST is not authority to destroy work: quarantine them for a manager (audited).
                try:
                    quarantine_dir = quarantine_review_workspace(
                        self.process_log_path,
                        request_id=request_id,
                        path=path,
                        home=home,
                    )
                except (OSError, WorkspaceError) as exc:
                    self._retention_event({
                        "request_id": request_id,
                        "task_id": task_id,
                        "runner": runner,
                        "topic": latest.get("topic"),
                        "adapter_id": latest.get("adapter_id"),
                        "state": "finalize_failed",
                        "workspace_gc": False,
                        "workspace_gc_at": _utcnow(),
                        "workspace_gc_reason": integrity_reason,
                        "review_transition_ok": True,
                        "callback_enqueued": bool(
                            transition.get("callback_enqueued")
                        ),
                        # ``review_workspace_quarantine_failed`` is now a named control-plane reason, so this no longer degrades to ``finalize_failed:runtime_error``; the ``{exc}`` tail stays caller text on the sanitised channel.
                        **terminal_failure_classification.terminal_event_authority(state="finalize_failed", exit_code=None, error=f"review_workspace_quarantine_failed:{exc}"[:500]),
                    }, disposition="retained_in_place")
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
                self._retention_event({
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": latest.get("topic"),
                    "adapter_id": latest.get("adapter_id"),
                    "state": retention_state,
                    "error": f"retained_workspace_quarantined:{integrity_reason}"[:500],
                    "workspace_gc": False,
                    "workspace_quarantined": True,
                    "workspace_quarantine_path": str(quarantine_dir),
                    "workspace_gc_at": _utcnow(),
                    "workspace_gc_reason": integrity_reason,
                    "review_transition_ok": True,
                    "callback_enqueued": bool(transition.get("callback_enqueued")),
                }, disposition="quarantined")
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
                self._retention_event({
                    "request_id": request_id, "task_id": task_id,
                    "runner": runner, "topic": latest.get("topic"),
                    "adapter_id": latest.get("adapter_id"), "state": latest.get("state"),
                    "error": f"cleanup_failed:{exc}"[:500], "workspace_gc": False,
                    "workspace_gc_at": _utcnow(), "workspace_gc_reason": disposition,
                }, disposition="retained_in_place")
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
            self._retention_event({
                "request_id": request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": latest.get("topic"),
                "adapter_id": latest.get("adapter_id"),
                "state": latest.get("state"),
                "workspace_gc": True,
                "workspace_gc_at": _utcnow(),
                "workspace_gc_reason": disposition,
            }, disposition="removed")
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
            merged_identity = self._event_identity(events)
            identity = self._request_pid_identity(events)
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
                    "finished_at": _utcnow(),
                    # Same split: ``metadata_invalid`` is a named reason now, the ``{exc}`` tail is not and is never copied.
                    **terminal_failure_classification.terminal_event_authority(state="finalize_failed", exit_code=None, error=f"metadata_invalid:{exc}"[:500]),
                })

            status_path = Path(str(metadata["supervisor_status_path"]))
            supervisor_status = self._read_supervisor_status(status_path)
            supervisor_pid = int(merged_identity.get("pid") or 0)
            supervisor_ticks = merged_identity.get("pid_start_ticks")
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
            # The supervisor is spawned before its first status write; a live PID
            # with an absent status file during that launch window is active work.
            liveness_lost = False
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
                    # Elapsed/quiet time is observability only, never terminal
                    # evidence while identity + heartbeat stay live (NF-2026-00176).
                if liveness["liveness_state"] in {"alive", "quiet", "unresponsive"}:
                    return None
                # liveness_state == "lost": lease + recovery grace elapsed
                # while the exact supervisor PID still exists. Recheck
                # identity once more under this call's lock, then terminate
                # only the exact matching process group(s).
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

            exit_code = terminal_failure_classification.normalize_exit_code(supervisor_status.get("exit_code"))
            error = stall_error or str(supervisor_status.get("error") or "")[:500]
            # SPOOFING SURFACE, NAMED AND DELIBERATELY LEFT OPEN FOR STEP 2.
            # This is the one control-plane candidate that comes from OUTSIDE
            # this process: ``supervisor_status`` is JSON a supervisor wrote, so
            # its ``error``/``state`` fields are an OPEN vocabulary. The no-copy
            # invariant still holds -- ``recognised_reason`` re-emits the
            # classifier's own constants and refuses any string carrying a token
            # it does not already own -- but a corrupt or hostile supervisor CAN
            # still SELECT which known reason is reported, by writing a
            # recognised token into that field. Closing that needs a typed wire
            # schema for the status packet, which Step 1 does not change.
            reason = terminal_failure_classification.recognised_reason(error)
            if liveness_lost and not error:
                error = f"liveness_lost:heartbeat_lease_and_recovery_grace_exceeded:rc={supervisor_returncode}"
            if supervisor_state == "timed_out" and metadata.get("timeout_enforced") is True:
                terminal_state = "timed_out"
                error = error or (
                    "worker_timed_out:timeout_seconds="
                    + str(metadata.get("timeout_seconds") or "unknown")
                    + f":exit_code={exit_code}"
                )
            # The supervisor no longer authorizes a token budget, so a legacy
            # packet still carrying token_budget_exceeded falls through to the
            # infrastructure-failure classification below; no new transition.
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
                if not error:
                    # The launcher holds this constant, so it states it TYPED
                    # rather than leaving the ``supervisor_state=`` field to be
                    # recovered downstream from a shape that cannot carry it.
                    reason = terminal_failure_classification.supervisor_failure_reason(supervisor_state, exit_code)
                    error = reason.render()
            else:
                # A missing, malformed, or stale running status is never proof
                # that the worker ran successfully. Cancellation intent is the
                # sole safe special case.
                if latest.get("state") == "cancel_requested":
                    terminal_state = "cancelled"
                else:
                    terminal_state = "worker_failed"
                if not error:
                    reason = terminal_failure_classification.supervisor_incomplete_reason(supervisor_state, supervisor_returncode)
                    error = reason.render()
            provider_launch_failure = None
            if terminal_state == "worker_failed":
                provider_output_path = Path(str(metadata["stdout_path"]))
                provider_launch_failure = _provider_auth_failure_from_output(
                    provider_output_path
                )
                if provider_launch_failure is None:
                    # The route, not the work: the provider named THIS launch's
                    # pinned model as unavailable for this account.  It joins
                    # the same refusal path below, so the card lands on the
                    # retryable ``launch_failed`` substatus instead of dying,
                    # while the sealed error it carries opens the route's
                    # failure circuit after this ONE failure.
                    provider_launch_failure = _provider_model_rejection_from_output(
                        provider_output_path, str(metadata.get("model") or "")
                    )
                if provider_launch_failure is not None:
                    http_status = int(provider_launch_failure["http_status"])
                    retry_event = self._retry_claude_auth_refresh(
                        request_id=request_id,
                        metadata_path=metadata_path,
                        metadata=metadata,
                        workspace=workspace,
                        provider_launch_failure=provider_launch_failure,
                    )
                    if retry_event is not None:
                        return retry_event
                    # The auth-readiness circuit is a claim about the credential,
                    # so it only trips on an authentication-shaped Claude receipt:
                    # a rate/quota refusal must not re-authenticate the route.
                    if (
                        str(metadata.get("adapter_id") or "") == "claude_cli"
                        and claude_auth.record_runtime_auth_failure(
                            http_status=http_status,
                            error_code=provider_launch_failure.get("error_code"),
                            session_id=provider_launch_failure.get("session_id"),
                        )
                    ):
                        error = claude_auth.RUNTIME_AUTH_FAILURE_REASON
                    terminal_state = "launch_failed"
                    # The recorded blocker reason is the classifier's verdict,
                    # derived from the provider's OWN response body here where it
                    # is still in hand -- not a status-only guess and not the
                    # downstream exit_code=1 (NF-2026-00275, NF-2026-00326).
                    if error != claude_auth.RUNTIME_AUTH_FAILURE_REASON:
                        error = str(provider_launch_failure["reason"])
                    # Both branches assigned a constant this repository owns --
                    # the auth circuit's own verdict, or ``classify_provider_
                    # outcome``'s closed refusal vocabulary -- so it is re-minted
                    # typed here and reaches the sink as itself instead of being
                    # flattened to the ``launch_failed:runtime_error`` its
                    # exception-shaped text would otherwise classify as.
                    reason = terminal_failure_classification.recognised_reason(error)
            elif (
                terminal_state == "exited"
                and str(metadata.get("adapter_id") or "") == "claude_cli"
                and int(metadata.get("claude_auth_retry_count") or 0) == 1
            ):
                claude_auth.clear_runtime_auth_failure()

            # R4/NF-2026-00646.  WHAT KIND of failure this is -- transient,
            # credential, defect -- decided only from what the provider or this
            # repository ASSERTED (a typed field, an HTTP status, a refusal kind
            # already established at the boundary above, a control-plane
            # constant).  ``unknown`` is the honest and by far the commonest
            # answer, and it behaves exactly as this path always has.
            #
            # The refusal kind is read from ``provider_launch_failure`` because
            # that detector held the provider's whole response body; a log tail
            # read later is strictly weaker evidence about the same event.
            terminal_disposition = terminal_failure_classification.failure_disposition_from_paths(
                state=terminal_state,
                error=error,
                refusal_kind=(
                    str(provider_launch_failure.get("refusal_kind") or "")
                    if isinstance(provider_launch_failure, dict)
                    else ""
                ),
                stdout_path=metadata.get("stdout_path"),
                stderr_path=metadata.get("stderr_path"),
            )
            failure_class = str(terminal_disposition["failure_class"])

            # NF-2026-00622 V7 rework (temporal-drift fix): terminal_state/
            # error/exit_code can still change below (validation_failed,
            # finalize_failed, review_transition_failed -> review_pending,
            # release_pending), so no call site caches this closure's result
            # across a mutation boundary -- each calls it fresh. ``reason`` is
            # read fresh for the same reason: a later branch may mint one.
            def _settle_terminal_failure_authority() -> dict[str, Any]:
                classification_state = (
                    "liveness_lost"
                    if terminal_state == "worker_failed" and liveness_lost
                    else terminal_state
                )
                return terminal_failure_classification.terminal_event_authority(
                    state=classification_state,
                    exit_code=exit_code,
                    error=error,
                    reason=reason,
                    stdout_path=metadata.get("stdout_path"),
                    stderr_path=metadata.get("stderr_path"),
                )

            changed: list[str] = []
            promoted: list[str] = []
            validations: list[dict[str, Any]] = []
            required_output_records: list[dict[str, Any]] = []
            review_result: dict[str, Any] | None = None
            release_result: dict[str, Any] | None = None
            worker_mcp_gate: dict[str, Any] | None = None
            quality_gate: dict[str, Any] | None = None
            full_validation_snapshot: dict[str, Any] | None = None
            research_result: dict[str, Any] | None = None
            residual_contract_result: list[dict[str, Any]] = []
            attempt_artifact_receipt: dict[str, Any] | None = None
            attempt_artifact_error = ""
            outcome_evidence_record: dict[str, Any] | None = None
            review_automation: dict[str, Any] = {}
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

            def _run_finalization_validations(
                candidate_changed_paths: list[str],
            ) -> list[dict[str, Any]]:
                nonlocal validation_wall_duration_ms, full_validation_snapshot
                phase_started = time.monotonic()
                try:
                    if not candidate_changed_paths:
                        return _run_declared_validations(workspace, metadata, metadata)
                    validations, full_validation_snapshot = (
                        _run_full_snapshot_validations(
                            workspace, metadata, metadata, candidate_changed_paths
                        )
                    )
                    return validations
                finally:
                    validation_wall_duration_ms += (
                        time.monotonic() - phase_started
                    ) * 1000.0

            try:
                if terminal_state != "exited":
                    # A worker that timed out, crashed, or was cancelled produced
                    # no review work: keep its worktree, close it in the blocked
                    # terminal bucket so the review queue remains truthful.
                    #
                    # R4/NF-2026-00646 -- THE ONE LINE THAT COST $46.07.
                    # ``launch_failed`` is the operational landing for "nothing
                    # ran", so it cleans the worktree up, which is right when
                    # nothing ran and catastrophic when a CREDENTIAL expired at
                    # the END of a run that had already done the work.  Measured
                    # on this repository: one card, 190.8M tokens, $46.07, its
                    # entire delta deleted because the Claude subscription
                    # session lapsed on the last call and the launch-failure
                    # branch then swept the workspace.  A credential-class
                    # outcome keeps its workspace, always: the owner has to fix
                    # a credential either way, and the finished work must still
                    # be there when they do.
                    cleanup = terminal_failure_classification.terminal_workspace_cleanup_allowed(
                        terminal_state=terminal_state, failure_class=failure_class,
                    )
                    # A non-exited terminal outcome never promotes/writes, so it
                    # never needs the one-task authority grant -- remove it now so
                    # it cannot linger as a stale artifact past this dead request.
                    unlink_if_regular(self._terminal_authority_grant_path(request_id))
                    transient_requeued = False
                    if (
                        failure_class
                        == terminal_failure_classification.FAILURE_CLASS_TRANSIENT
                    ):
                        # The PROVIDER failed, not the card: a 429, a 5xx, a
                        # route this account cannot use.  Terminalising it
                        # spends a whole card cycle on a condition that clears
                        # by itself, so the card goes back to ``pending`` with
                        # its workspace intact and a bounded retry budget.  The
                        # store refuses once that budget is spent, and the
                        # ordinary terminal path below then runs unchanged --
                        # so a misclassified transient can cost at most
                        # ``TRANSIENT_RETRY_BUDGET`` attempts, never a loop.
                        #
                        # Called on the store directly rather than through
                        # ``task_engine``: this transition is not terminal and
                        # owes the manager no callback, because there is no
                        # decision to make about a card that is back in the
                        # queue.
                        transient_requeued, transient_state = (
                            task_store.mark_transient_retry(
                                self.repo,
                                str(metadata["task_id"]),
                                runner=str(metadata["runner"]),
                                reason=_settle_terminal_failure_authority()["error"],
                                request_id=request_id,
                            )
                        )
                        if transient_requeued:
                            cleanup = False
                            release_result = {
                                "ok": True,
                                "returncode": 0,
                                "command": ["transient-retry", str(metadata["task_id"])],
                                "stdout": json.dumps(
                                    {
                                        "task_id": str(metadata["task_id"]),
                                        "status": transient_state,
                                        "failure_class": failure_class,
                                        "evidence": str(
                                            terminal_disposition["evidence"]
                                        ),
                                    },
                                    ensure_ascii=False,
                                ),
                                "stderr": "",
                                "callback_enqueued": False,
                            }
                    if transient_requeued:
                        # The card is back in the queue with its workspace kept:
                        # there is no terminal transition to make, no terminal
                        # evidence to record, and no manager decision owed.
                        pass
                    elif terminal_state == "launch_failed":
                        release_result = task_engine.mark_launch_failed(
                            self.repo,
                            str(metadata["task_id"]),
                            str(metadata["runner"]),
                            reason=_settle_terminal_failure_authority()["error"],
                            request_id=request_id,
                        )
                    else:
                        failure_evidence = {
                            "request_id": request_id,
                            "adapter_id": metadata.get("adapter_id"),
                            "model": metadata.get("model"),
                            # R4/NF-2026-00646: the class this failure was
                            # placed in, and the exact evidence that placed it.
                            # Both are module constants, so this is durable
                            # without being another untrusted string channel --
                            # and an operator can finally tell "the provider
                            # hiccuped" from "this work is wrong" on the card
                            # itself instead of from a bare exit code.
                            "failure_class": failure_class,
                            "failure_class_evidence": str(
                                terminal_disposition["evidence"]
                            ),
                            # The sealed, provider-owned error object, when one
                            # was read at the boundary.  It is what
                            # ``workforce_catalog._route_failure_kind`` trusts,
                            # so recording it here is what lets a route's
                            # circuit be computed from the 90-day event log
                            # instead of the short-horizon process ledger.
                            **(
                                {"provider_error": provider_launch_failure["provider_error"]}
                                if isinstance(provider_launch_failure, dict)
                                and isinstance(
                                    provider_launch_failure.get("provider_error"), dict
                                )
                                else {}
                            ),
                            "error": _settle_terminal_failure_authority()["diagnostic"],
                            "supervisor_state": supervisor_state,
                            "exit_code": exit_code,
                            "liveness_lost": liveness_lost,
                            "stall_detected": False,
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
                                rework_delta = _terminal_rework_delta_evidence(
                                    workspace,
                                    metadata,
                                    request_id,
                                    timeout_changed,
                                )
                                if rework_delta is not None:
                                    failure_evidence["rework_delta"] = rework_delta
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
                        validated_required_paths = {
                            rec["path"]
                            for rec in required_output_records
                            if not rec.get("unchanged_allowed")
                        }
                        validation_candidate_paths = sorted(
                            set(changed)
                            | validated_required_paths
                            | {
                                rec["path"]
                                for rec in required_output_records
                                if rec.get("replay_evidence")
                            }
                        )
                        worker_mcp_gate = _worker_mcp_live_call_gate(metadata, request_id)
                        validations = _run_finalization_validations(
                            validation_candidate_paths
                        )
                        if worker_mcp_gate.get("gated") and not worker_mcp_gate.get("satisfied", True):
                            raise WorkspaceError(
                                "validation_required_aiworkhub_mcp_call_missing:"
                                + str(worker_mcp_gate.get("reason") or "")
                            )
                        if validations:
                            changed = _enforce_finalization_scope()
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
                            # The canonical card, not ``metadata``: only the card
                            # carries ``task_type``/``project_context``, and
                            # ``derive_risk_signals`` reads both. An unreadable
                            # card degrades to the launch metadata rather than
                            # failing a finalization over risk DESCRIPTION.
                            try:
                                risk_card: dict[str, Any] = _parse_card(
                                    self._show_task(str(metadata["task_id"])),
                                    str(metadata["task_id"]),
                                )
                            except Exception:  # noqa: BLE001 -- description never fails a candidate
                                risk_card = dict(metadata)
                            quality_gate = quality_evidence.run_review_ready_quality_gate(
                                workspace.path,
                                card=risk_card,
                                changed_paths=changed,
                                canonical_repo=self.repo,
                                reachability_inputs=self._candidate_reachability_inputs(
                                    workspace, changed
                                ),
                            )
                            if full_validation_snapshot is not None:
                                quality_gate["full_validation_snapshot"] = full_validation_snapshot
                            if not quality_gate.get("passed"):
                                blockers = quality_gate.get("blocking_checks") or []
                                # Named ``gate_reason``, not ``reason``: the
                                # enclosing finalizer's ``reason`` is the TYPED
                                # terminal reason, and a str assignment here
                                # would silently replace it -- the exact
                                # erosion the non-str reason type exists to
                                # make impossible.
                                gate_reason = quality_gate.get("config_error") or ",".join(
                                    str(v) for v in blockers
                                )
                                raise WorkspaceError(
                                    "quality_gate_failed:" + str(gate_reason)[:400]
                                )
                        _enforce_behavioral_gate(
                            metadata,
                            validations,
                            quality_gate,
                        )
                        # Review-first reconcile retains the isolated workspace
                        # and records every check before coordinator acceptance.
                        # A successful rework publication supersedes its predecessor as
                        # byte authority.  Seal the complete candidate, including paths
                        # inherited unchanged from that predecessor, so generation N+1
                        # never depends on generation N's descriptor remaining present.
                        from .successful_rework_recovery import successful_candidate_evidence

                        successful_candidate_paths, changed_path_hashes, rework_delta = (
                            successful_candidate_evidence(workspace, metadata, request_id, changed)
                        )
                        attempt_artifact_receipt = self._persist_attempt_artifacts(
                            request_id,
                            metadata,
                            workspace,
                            target_state="review_ready",
                            changed_paths=successful_candidate_paths,
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
                                "changed_paths": successful_candidate_paths,
                                "changed_path_hashes": changed_path_hashes,
                                "rework_delta": rework_delta,
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
                                    "repo": str(self.repo.resolve(strict=False)),
                                    "claim_epoch": metadata.get("claim_epoch"),
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
                        else:
                            registration: dict[str, str] = {}
                            try:
                                registration = review_orchestrator.candidate_registration(
                                    metadata=metadata, artifact_receipt=attempt_artifact_receipt,
                                    changed_path_hashes=changed_path_hashes,
                                    quality_gate=quality_gate)
                                review_db = review_orchestrator.canonical_review_db(self)
                                if review_db is None:
                                    raise RuntimeError("review_lifecycle_store_not_ready")
                                chain = review_orchestrator.register_candidate(
                                    self, db_path=review_db, registration=registration)
                            except Exception as exc:
                                review_automation = dict(
                                    state="pending", registration=registration,
                                    error=f"{type(exc).__name__}:{exc}"[:300])
                            else:
                                review_automation = dict(
                                    state="seeded", registration=registration,
                                    chain_identity_sha256=chain.chain_identity_sha256)
            except _QualityReviewFinalized:
                pass
            except WorkspaceError as exc:
                if isinstance(exc, ValidationRunError):
                    validations = [dict(row) for row in exc.results]
                error = str(exc)
                # A workspace error reads ``<constant>:<caller text>``: the
                # prefix is a reason this repository mints, the tail is a path,
                # a card field or an exception. Only the prefix is typed, and
                # the mandatory-output list is re-minted from the CARD's own
                # declared ``required_outputs`` -- never from the validator's
                # observed filenames, which a glob lets a worker choose.
                reason = terminal_failure_classification.workspace_error_reason(error, metadata.get("required_outputs"))
                terminal_state = _terminal_state_for_workspace_error(exc)
                # Keep the isolated candidate intact for coordinator
                # diagnosis/retry on every genuine failure, including a
                # lost-claim race. B863: a claim_ownership_lost read can be a
                # false positive (B860/B861), so cleanup is deferred entirely
                # to _gc_finalized_workspace's canonical-status-gated sweep.
                ownership_lost = error.startswith("claim_ownership_lost")
                cleanup = False
                if not ownership_lost and not promoted:
                    retained_candidate: dict[str, Any] = {}
                    if terminal_state in {
                        "validation_failed",
                        "worker_failed",
                        "finalize_failed",
                    }:
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
                    if terminal_state in {
                        "validation_failed",
                        "worker_failed",
                        "finalize_failed",
                    }:
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
                    failed_candidate: dict[str, Any] = {}
                    try:
                        failed_candidate = _retained_candidate_identity_evidence(
                            workspace, metadata, request_id, changed, claim_state
                        )
                    except WorkspaceError:
                        pass
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
                            **failed_candidate,
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
                    # A release_pending predecessor deferred usage recording with
                    # its release transition. Record the spend now -- _record_usage
                    # is idempotent per request_id, so this can never double-count.
                    usage, usage_recorded, usage_error = self._record_usage(
                        request_id,
                        str(metadata["task_id"]),
                        str(metadata["runner"]),
                        str(metadata["adapter_id"]),
                        str(metadata.get("model") or metadata["adapter_id"]),
                        stdout_path,
                        topic=str(metadata["topic"]),
                        execution_mode=str(metadata.get("execution_mode") or ""),
                        claim_authority={
                            "request_id": request_id,
                            "claimed_by": str(metadata["runner"]),
                            "claim_epoch": metadata.get("claim_epoch"),
                        },
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
                    claim_authority={
                        "request_id": request_id,
                        "claimed_by": str(metadata["runner"]),
                        "claim_epoch": metadata.get("claim_epoch"),
                    },
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
            # Coverage is computed BEFORE the workspace can be purged below:
            # the eligible changed files are still on disk here, so the byte
            # denominator comes from the files the finalizer already stats and
            # hashes rather than from a second diff.  Measurement only -- no
            # branch above or below consults it.
            try:
                semantic_edit_coverage = _semantic_edit_coverage(
                    changed,
                    workspace=workspace,
                    worker_mcp_gate=worker_mcp_gate,
                    granted_tool_names=(
                        (metadata.get("worker_mcp") or {}).get("tool_names") or ()
                    ),
                    runtime_evidence=semantic_edit_evidence,
                )
            except Exception as exc:  # never let a measurement change an outcome
                semantic_edit_coverage = {
                    "schema_id": SEMANTIC_EDIT_COVERAGE_SCHEMA_ID,
                    "measured": False,
                    "unmeasured_reason": (
                        f"coverage_measurement_failed:{type(exc).__name__}"
                    )[:200],
                    "token_savings_claimed": False,
                    "measurement_only": True,
                }
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
            cleanup_error = ""
            if cleanup:
                # Record the purge intent BEFORE the delete: the disposition on
                # the terminal event must be a FACT, not an intention, so it is
                # written delete-then-append -- this durable audit row is then
                # the surviving trace if that later append is ever lost.
                record_review_workspace_retention_audit(
                    self.process_log_path, request_id=request_id,
                    task_id=str(metadata["task_id"]), card_status=terminal_state,
                    reason="finalization_purge", action="purge",
                )
                try:
                    cleanup_workspace(workspace.repo, workspace.path, workspace.home)
                except WorkspaceError as exc:
                    cleanup_error = f"cleanup_failed:{exc}"[:500]
            # Derive fresh, live authority immediately before construction --
            # after every branch above has had its say -- never a cached value.
            final_terminal_failure_authority = _settle_terminal_failure_authority()
            event = self._retention_event({
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
                "review_automation": review_automation,
                "release_transition_ok": bool(release_result and release_result.get("ok")),
                "terminal_review_disposition": str(
                    (release_result or {}).get("terminal_review_disposition") or ""
                )[:200],
                "canonical_lifecycle": str(
                    (release_result or {}).get("canonical_lifecycle") or ""
                )[:40],
                "liveness_lost": liveness_lost,
                "stall_detected": False,
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
                **final_terminal_failure_authority,
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
                "semantic_edit_coverage": semantic_edit_coverage,
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
                "cleanup_error": cleanup_error,
            }, disposition=(
                "removed" if cleanup and not cleanup_error
                else "retained_in_place"
            ))
            return event

    @staticmethod
    def _event_identity(events: list[dict[str, Any]]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        identity_keys = (
            "request_id", "task_id", "runner", "topic", "adapter_id", "model",
            "pid", "pid_start_ticks", "stdout_path", "stderr_path", "metadata_path",
            "supervisor_status_path", "cancel_path", "sandbox_backend", "exit_code",
        )
        for event in events:
            merged.update({k: event[k] for k in identity_keys if k in event})
            if "failure_kind" in event:
                merged["failure_kind"] = event["failure_kind"]
                merged.update({k: event[k] for k in ("diagnostic", "error") if k in event})
            elif "error" in event:
                # Sparse GC/retention/disposal overlay rows never carry failure_kind; an authoritative row always does (even a None verdict), so presence -- not error-value truthiness -- is the marker.
                merged["retention_error" if "error" in merged else "error"] = event["error"]
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
        # Restore the lineage's error/retention_error over an overlay row.
        latest.update({key: lineage[key] for key in ("error", "retention_error") if key in lineage})
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
        card_read = "read"
        if latest.get("state") == "starting" and not int(latest.get("pid") or 0):
            # Bounded during preparation (owner contention) -- but not absent.
            card_read = "deferred_pid_null_starting_reservation"
        else:
            try:
                card = _parse_card(self._show_task(task_id), task_id)
            except Exception as exc:
                card_read = f"read_failed:{type(exc).__name__}"  # not absent
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
            "task_card_read": card_read,
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
        # A read on the way IN, so the reviewer prompt's ban on submission
        # tools does not reach it.  The packet path is bound server-side from
        # the request metadata above; the prompt tells the reviewer to call
        # this with no arguments and forbids supplying a path or identity
        # (quality_reviewer.py:578-582), so ``tool_input`` is deliberately NOT
        # forwarded -- a provider can neither redirect the read nor break it
        # by mirroring a stray field.
        if tool_name == "aiworkhub_worker_quality_review_packet_read":
            return worker_ai_tools_mcp.quality_review_packet_read(ctx)
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
            "metadata_path", "workspace_retained", "workspace_disposition", "failure_kind", "diagnostic", "retention_error",
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
            "task_card_read": status.get("task_card_read"),
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
            # Merged, because this pid also gets SIGTERM: an advisory notice
            # carries `pid` with no ticks, so the tail row would signal a pid
            # nothing had verified.
            merged_identity = self._event_identity(events)
            pid = int(merged_identity.get("pid") or 0)
            ticks = merged_identity.get("pid_start_ticks")
            identity = self._request_pid_identity(events)
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

    def _close_accepted_task_needfix(
        self, task_id: str, request_id: str
    ) -> dict[str, Any]:
        """Return explicit recoverable state without revising acceptance truth."""
        try:
            return needfix_store.close_for_accepted_task(
                self.repo, task_id, accepted_request_id=request_id
            )
        except Exception as exc:
            closure_id = hashlib.sha256(
                (
                    f"needfix-accepted-task-closure\0{task_id}"
                    f"\0{request_id}"
                ).encode("utf-8")
            ).hexdigest()
            pending = {
                "state": "pending_recovery",
                "task_id": task_id,
                "accepted_request_id": request_id,
                "closure_id": closure_id,
                "recoverable": True,
                "error": str(exc)[:500],
            }
            self._append_event({
                "request_id": request_id,
                "task_id": task_id,
                "state": "needfix_closure_pending",
                "needfix_closure": pending,
                "recorded_at": _utcnow(),
            })
            return pending

    def _reconcile_pending_needfix_closures(self) -> None:
        """Retry durable accepted-task closures without model/operator action."""
        events = self._events()
        completed = {
            (str(event.get("task_id") or ""), str(event.get("request_id") or ""))
            for event in events
            if event.get("state") == "needfix_closure_reconciled"
        }
        pending: dict[tuple[str, str], dict[str, Any]] = {}
        for event in events:
            if event.get("state") != "needfix_closure_pending":
                continue
            identity = (
                str(event.get("task_id") or ""),
                str(event.get("request_id") or ""),
            )
            if all(identity) and identity not in completed:
                pending[identity] = event
        for (task_id, request_id), _event in pending.items():
            try:
                card = _parse_card(self._show_task(task_id), task_id)
                if (
                    _canonical_task_status(card) != "finished"
                    or str(card.get("accepted_request_id") or "") != request_id
                ):
                    continue
                closure = needfix_store.close_for_accepted_task(
                    self.repo, task_id, accepted_request_id=request_id
                )
            except Exception:
                continue
            self._append_event({
                "request_id": request_id,
                "task_id": task_id,
                "state": "needfix_closure_reconciled",
                "needfix_closure": closure,
                "recorded_at": _utcnow(),
            })

    def accept_review(
        self,
        request_id: str,
        task_id: str,
        *,
        confirm_destructive_change: bool = False,
        requested_risk_tier: str | None = None,
        risk_signals: list[str] | None = None,
        reviewer_reports: list[dict[str, Any]] | None = None,
        reviewer_request_ids: list[str] | None = None,
        confirm_high_risk: bool = False,
    ) -> dict[str, Any]:
        """Coordinator/write-gated acceptance of one ``review_ready`` request.

        The implementation lives in
        :func:`process_launcher_accept_review.accept_review`; this method is the
        delegation and holds no logic of its own.  See that module for the
        contract, which is unchanged by the move.
        """
        return _accept_review_impl(
            self,
            request_id,
            task_id,
            confirm_destructive_change=confirm_destructive_change,
            requested_risk_tier=requested_risk_tier,
            risk_signals=risk_signals,
            reviewer_reports=reviewer_reports,
            reviewer_request_ids=reviewer_request_ids,
            confirm_high_risk=confirm_high_risk,
        )

    def accept_preview(
        self,
        request_id: str,
        task_id: str,
        **overrides: Any,
    ) -> dict[str, Any]:
        """READ-ONLY preview of the cheap blocker fold ``accept_review`` runs.

        Measured 2026-09-08: 49 of 159 accept attempts (31%) failed on a
        parameter or timing blocker -- a lens still running, a target not yet
        review_ready, an approval not given -- AFTER the combined tree had been
        materialized and two validation runs had been paid for. Those blockers
        are all knowable before any of that work, so they are now foldable on
        their own.

        The implementation lives in
        :func:`process_launcher_accept_review.accept_preview`; this method is
        the delegation and holds no logic of its own. It writes nothing and it
        is not an acceptance: a clear preview only means nothing cheap is
        refusing yet, and the expensive half can still refuse.
        """
        return _accept_preview_impl(self, request_id, task_id, **overrides)

    def reject_review(
        self,
        task_id: str,
        reason: str,
        *,
        to: str = "pending",
    ) -> dict[str, Any]:
        """Coordinator-gated rejection of one ``review`` card, back to rework.

        The transition itself belongs to :func:`core.reject_review` and none of
        it is re-implemented here: it re-reads the live card, refuses a
        disposition it does not know, moves the row atomically and only
        ``WHERE worker_status='review'``, writes the ``reject_review`` event,
        finalizes the quality-review children bound to the rejected request,
        and names the learning duty the rejection incurs.

        Three preconditions are checked before delegating.  ``core`` resolves
        the card through its *module-global* repository binding, while this
        manager is bound to ``self.repo`` -- so a manager pointed somewhere
        else must never reach it: it would reject a same-named card in the
        wrong tree and destroy a completed review there.  And a rejection with
        no reason, or no task, is an unexplained state transition, which this
        repository does not permit.  Every refusal returns without touching
        any card, exactly as a failed precondition in ``accept_review`` leaves
        the canonical repository untouched with the reason returned.

        The core result is returned intact -- ``ok``/``returncode``/``stdout``
        plus ``reviewer_finalization``, ``learning_commit_owed`` and any
        ``rework_delta_recovery`` -- with ``task_id``/``to`` echoed and, on
        failure, ``error`` set from ``stderr`` so one field reads either way.
        """
        refusal: dict[str, Any] = {"ok": False, "task_id": task_id, "to": to}
        target = str(task_id or "").strip()
        if not target:
            return {**refusal, "error": "task_id_required"}
        bounded_reason = str(reason or "").strip()
        if not bounded_reason:
            return {**refusal, "error": "reject_reason_required"}
        try:
            authority = core.repo_root().resolve()
        except Exception as exc:  # noqa: BLE001 -- an unresolvable root refuses
            return {
                **refusal,
                "error": f"repo_authority_unavailable:{type(exc).__name__}",
            }
        if authority != self.repo:
            return {
                **refusal,
                "error": "repo_authority_mismatch",
                "manager_repo": str(self.repo),
                "core_repo": str(authority),
            }
        result = core.reject_review(target, bounded_reason, to=to)
        if not isinstance(result, dict):
            return {**refusal, "error": "reject_review_result_invalid"}
        result.setdefault("task_id", target)
        result.setdefault("to", to)
        if result.get("ok") is not True:
            result["error"] = str(result.get("stderr") or "reject_review_failed")
        return result

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


# Exact process identity (PID reuse refusal) lives in ``process_identity``:
# one subject, one module, and the size ratchet is descending by design.
# Re-exported here because callers and tests reach for these launcher names.
from .process_identity import (  # noqa: E402
    PidIdentityEvidence,
    PidIdentityVerdict,
    _identity_verified_pid,
    _pid_alive,
    _pid_identity_evidence,
    _pid_matches,
    _pid_start_ticks,
    _process_proven_dead,
)


def _canonical_task_status(card: dict[str, Any]) -> str:
    """Return lifecycle status while preserving archived as a distinct gate."""
    if str(card.get("archived_at") or "").strip():
        return "archived"
    return core._lifecycle_state(card)


def _process_group_alive(pgid: int) -> bool:
    """True while any POSIX group member exists; ambiguity fails closed."""
    return probe_process_group(pgid, platform_name="posix")


def _terminate_process_group(pid: int, grace_seconds: float) -> None:
    terminate_process_tree(
        pid,
        platform_name=os.name,
        timeout=grace_seconds,
        probe=_pid_alive if is_windows(os.name) else _process_group_alive,
    )


_DEFAULT_MANAGER: ProcessManager | None = None
_DEFAULT_MANAGER_LOCK = threading.Lock()


def default_manager() -> ProcessManager:
    global _DEFAULT_MANAGER
    with _DEFAULT_MANAGER_LOCK:
        if _DEFAULT_MANAGER is None:
            _DEFAULT_MANAGER = ProcessManager()
            storage_retention.schedule_repository_cleanup(_DEFAULT_MANAGER.repo)
        return _DEFAULT_MANAGER
