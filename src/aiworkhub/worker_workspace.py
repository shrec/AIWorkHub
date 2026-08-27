"""Isolated worktree and fail-closed sandbox helpers for Task MCP workers.

The parent repository is never the model process working directory. A worker
receives a detached Git worktree and a minimal HOME containing only the selected
adapter credential. Bubblewrap is preferred when usable; Landlock confines
writes when unprivileged user namespaces are blocked. Changes are promoted only
after scope, validation, and parent-content checks.
"""

from __future__ import annotations

import argparse
import ast
import copy
import ctypes
import errno
import base64
import fnmatch
import hashlib
import hmac
import importlib.util
import json
import os
import re
import shlex
import shutil
import site
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

try:
    from .platform_io import (
        atomic_replace,
        chmod_fd,
        chmod_path,
        posix_path_modes_supported,
    )
except ImportError:  # direct-script Landlock entrypoint
    def atomic_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        os.replace(source, destination)

    def chmod_fd(fd: int, mode: int) -> None:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(fd, mode)

    def chmod_path(path: str | os.PathLike[str], mode: int) -> None:
        if os.name != "nt":
            os.chmod(path, mode)

    def posix_path_modes_supported(platform_name: str | None = None) -> bool:
        return (platform_name or os.name) != "nt"


# ``VALIDATION_FAILED`` / ``VALIDATION_ENVIRONMENT_BLOCKED`` name the two terminal
# states this module's ``ValidationRunError`` hierarchy carries at class-definition
# time.  ``validation_runner`` is their authoritative source of truth, but this
# module MUST also load as a BARE SCRIPT: it is executed directly as the Landlock
# wrapper (``<python> src/aiworkhub/worker_workspace.py --landlock-exec ...``),
# loaded by file location with NO package context, so ``from .validation_runner
# import ...`` has no package to resolve AND ``from validation_runner import ...``
# has no sys.path entry for the sibling either (the module's own directory is not
# on sys.path in that mode -- exactly what
# ``tests/test_runtime_temp.py::test_worker_workspace_direct_script_resolves_sibling_runtime_temp``
# reproduces, which caught the previous sibling-import fallback).  Unlike
# ``runtime_temp`` -- a whole module resolved by file location because its
# behaviour is needed -- only these two immutable string constants are needed here,
# so a sibling-import dependency that cannot exist in bare-script mode is avoided
# entirely by re-stating the literals.  ``tests/test_worker_workspace.py`` asserts
# they stay byte-identical to ``validation_runner``'s definitions, so this
# deliberate duplication cannot silently drift.  The live classifier
# (``classify_validation_results``) is imported package-relatively at its one call
# site below, which only runs under the package-imported entrypoint.
VALIDATION_FAILED = "validation_failed"
VALIDATION_ENVIRONMENT_BLOCKED = "validation_environment_blocked"
# Worker-side (sandbox-selection) restriction. The sandbox backend itself could
# not be provisioned BEFORE any command was built or run, so this is outside
# ``validation_runner``'s per-row classification authority (the candidate never
# executed). It blocks acceptance exactly like ``VALIDATION_ENVIRONMENT_BLOCKED``
# but is recovered by re-running in a corrected sandbox -- never reported as
# ``VALIDATION_FAILED``, and always preserving the retained workspace/hashes for
# a provider-free ``retry_finalization``.
VALIDATION_UNSUPPORTED_IN_SANDBOX = "validation_unsupported_in_sandbox"
OUTER_VALIDATION_AUTHORITY_SCHEMA = "aiworkhub.outer_validation_authority.v1"
OUTER_VALIDATION_AUTHORITY_RELATIVE = (
    ".aiworkhub/outer_validation_authority.v1.json"
)
_OUTER_VALIDATION_AUTHORITY_KIND = "coordinator_outer_validation"
_OUTER_VALIDATION_HMAC_KEY = b"aiworkhub.outer_validation_authority.v1.landlock"
NESTED_LANDLOCK_AUTHORITY_LOCATOR_SCHEMA = (
    "aiworkhub.nested_landlock_authority_locator.v1"
)
NESTED_LANDLOCK_AUTHORITY_LOCATOR_RELATIVE = (
    ".aiworkhub/nested_landlock_authority_locator.v1.json"
)
NESTED_LANDLOCK_AUTHORITY_LOCATOR_ANCHOR_RELATIVE = (
    ".aiworkhub/nested_landlock_authority_locator.v1.anchor"
)
_NESTED_LANDLOCK_AUTHORITY_LOCATOR_KIND = "coordinator_nested_landlock_locator"
_NESTED_LANDLOCK_AUTHORITY_LOCATOR_MAX_ANCESTORS = 16


def _load_runtime_temp():
    """Resolve the runtime_temp authority module deterministically.

    ``worker_workspace.py`` is both package-imported (``from aiworkhub import
    worker_workspace``) and executed directly as the Landlock wrapper
    (``<python> src/aiworkhub/worker_workspace.py --landlock-exec ...``). In the
    direct-script case the package-relative import fails and the installed
    ``aiworkhub.runtime_temp`` is not guaranteed to be importable inside the
    retained workspace. Resolve the authenticated sibling file beside this
    module first; fall back to the package import only when the sibling is not
    a regular file (installed/bundled layouts). A missing, symlinked, or
    unloadable sibling fails closed.
    """
    try:
        from . import runtime_temp
        return runtime_temp
    except ImportError:
        pass
    sibling = Path(__file__).resolve().parent / "runtime_temp.py"
    try:
        is_regular = sibling.is_file() and not sibling.is_symlink()
    except OSError as exc:
        raise ImportError(f"runtime_temp sibling unreadable: {sibling}") from exc
    if not is_regular:
        raise ImportError(
            "runtime_temp unavailable: no package import and no regular "
            f"sibling at {sibling}"
        )
    spec = importlib.util.spec_from_file_location("aiworkhub.runtime_temp", sibling)
    if spec is None or spec.loader is None:
        raise ImportError(f"runtime_temp sibling has no loader: {sibling}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["aiworkhub.runtime_temp"] = module
    spec.loader.exec_module(module)
    return module


runtime_temp = _load_runtime_temp()


def bubblewrap_home_env_value() -> str:
    """Single source of truth for the bubblewrap HOME string.

    ``sandbox_argv``'s bubblewrap branch binds ``workspace.home`` (the real,
    isolated per-request home) onto this exact path inside the sandbox mount
    namespace, so a process running under bubblewrap that reads ``$HOME``
    transparently lands in the isolated directory even though the string
    itself looks like the real host home. ``sanitized_env(..., home=None)``
    must seed the identical string as the child's ``HOME`` env var for that
    remap to line up -- previously both call sites independently called
    ``Path.home()`` and only *happened* to agree; routing both through this
    one function makes the two impossible to silently diverge (B314_F004).
    """
    return str(Path.home())


WORKTREE_ROOT_ENV = "AIWORKHUB_WORKTREE_ROOT"
RUNTIME_ROOT_ENV = "AIWORKHUB_RUNTIME_ROOT"
TEMP_ROOT_ENV = runtime_temp.TEMP_ROOT_ENV
BWRAP_ENV = "AIWORKHUB_BWRAP"
SANDBOX_BACKEND_ENV = "AIWORKHUB_SANDBOX_BACKEND"
VSCODE_LM_IN_PROCESS_BACKEND = "vscode_lm_in_process"
_VSCODE_LM_IN_PROCESS_ADAPTERS = frozenset(
    {"vscode_lm", "glm_vscode_lm", "deepseek_vscode_lm"}
)
SANDBOX_WORKSPACE = "/workspace"
# B834: the coordinator-owned host repository (``workspace.repo``) bound
# read-only for worker MCP authority lookups (Source Graph / Session Manager
# / AI Memory / KB), distinct from the isolated writable worktree above.
# Under bubblewrap this is the ONLY sandbox-visible alias for that host path
# -- the real host path itself is absent inside the mount namespace, so any
# code that embeds a host path string into adapter/MCP config would silently
# reference a path the sandboxed process cannot see.
SANDBOX_AUTHORITY_REPO = "/authority-repo"
# B870 V2: a SEPARATE, dedicated read-only alias for the AIWorkHub Python
# package's own import root (the directory that must be on PYTHONPATH for
# ``import aiworkhub`` to resolve). This is never derived from -- or bound
# alongside -- SANDBOX_AUTHORITY_REPO: the package import root may live in a
# standalone ``<repo>/src/aiworkhub`` checkout, nested inside a monorepo
# beneath authority_repo, or bundled/installed entirely OUTSIDE
# authority_repo, so it needs its own independent host-path binding rather
# than being expressed as an offset inside the authority_repo alias.
SANDBOX_PACKAGE_IMPORT_ROOT = "/aiworkhub-package-root"
MAX_SEED_FILES = 20_000
_VALIDATION_WORKER_PACKAGE_SUPPORT = (
    # Keep pytest configuration inside the sparse worktree.  Without this
    # anchor pytest walks up through ``.aiworkhub/runtime/worktrees`` and
    # discovers the canonical repository's pyproject.toml, so ``pythonpath =
    # [\"src\"]`` resolves to canonical code instead of the retained candidate.
    "pyproject.toml",
    "src/aiworkhub/__init__.py",
    "src/aiworkhub/_version.py",
    "src/aiworkhub/platform_io.py",
    "src/aiworkhub/runtime_temp.py",
    "src/aiworkhub/validation_runner.py",
)
_NPM_SUPPORT_EXCLUDED_DIRS = frozenset(
    {
        "node_modules",
        "dist",
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
MAX_REWORK_OVERLAY_FILES = 512
MAX_REWORK_OVERLAY_CONTENT_BYTES = 8 * 1024 * 1024
MAX_VALIDATION_COMMANDS = 32
MAX_VALIDATION_SECONDS = 1_800
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# B753: validation commands previously inherited TMPDIR/HOME from a mount that
# may be noexec (e.g. a hardened /tmp), so a validation command that compiles
# and then executes a native binary failed with rc=126 even though the
# product itself was correct (B751). Every isolated validation run now gets
# its own private, request-unique, explicitly exec-probed scratch directory
# instead of trusting the ambient tmp mount. Admins may pin an exact root
# (must itself be exec-capable, no silent fallback) via
# AIWORKHUB_VALIDATION_EXEC_SCRATCH_ROOT; otherwise a small fixed
# candidate list is probed in order and the first root that is BOTH
# exec-capable AND honours the chmod/metadata semantics git init needs wins
# (a hardened /dev/shm that execs but rejects chmod is skipped, not forced).
# Fails closed (raises WorkspaceError) if nothing usable is found -- never
# chmods or remounts a shared filesystem to force it to work.
VALIDATION_EXEC_SCRATCH_ROOT_ENV = "AIWORKHUB_VALIDATION_EXEC_SCRATCH_ROOT"
_DEFAULT_EXEC_SCRATCH_ROOTS: tuple[Path, ...] = (Path("/dev/shm"), Path(tempfile.gettempdir()))
SANDBOX_VALIDATION_EXEC_SCRATCH = "/validation-exec-scratch"
_EXEC_SCRATCH_NAME_PREFIX = "aiworkhub_validation_exec_"
_EXEC_PROBE_SCRIPT = b"#!/bin/sh\nexit 0\n"

# B328: AITools/taskdb.py's DEFAULT_DB resolves relative to its own
# ``__file__``. Inside an isolated git worktree that file is the WORKTREE's
# own checked-out copy, so DEFAULT_DB silently points at a worktree-local
# path that (a) is outside workspace.allowed_writes, so Landlock refuses to
# create it (sqlite3.OperationalError: unable to open database file -- the
# B313 live-canary failure, reproduced at
# AITools/taskdb.py:65/open_db), and (b) even when creation succeeds
# (bubblewrap, which binds the whole worktree read-write), taskctl's
# from-empty auto-seed (``_ensure_db_seeded``) immediately tries to write
# back into the worktree's tracked machine_task_cards_v1.jsonl/manifest --
# Landlock refuses that too (PermissionError), and bubblewrap would silently
# succeed and corrupt the later git-diff-based scope check with a mutation
# the worker never made. ``provision_isolated_task_queue_db`` pre-seeds a
# disposable, non-authoritative copy under ``workspace.home`` on the HOST
# side (never inside the sandbox, never touching the parent's live DB for
# anything but a read) so the sandboxed process's own ``_ensure_db_seeded``
# always finds a non-empty DB and never takes the seed-and-export branch.
TASK_QUEUE_ISOLATED_RELATIVE = Path("task_mcp_isolated_queue/task_queue_isolated.sqlite")
_ISOLATED_QUEUE_PLACEHOLDER_TASK_ID = "AIWORKHUB_ISOLATED_QUEUE_PLACEHOLDER_B328"

# Large production repositories can legitimately need several minutes to
# materialize a detached worktree on cold storage.  The generic command
# timeout is intentionally shorter; provisioning gets its own bounded budget.
WORKTREE_CREATE_TIMEOUT_SECONDS = 600
FINALIZATION_GIT_TIMEOUT_ENV = "AIWORKHUB_FINALIZATION_GIT_TIMEOUT_SECONDS"
DEFAULT_FINALIZATION_GIT_TIMEOUT_SECONDS = 5.0
MIN_FINALIZATION_GIT_TIMEOUT_SECONDS = 0.25
MAX_FINALIZATION_GIT_TIMEOUT_SECONDS = 120.0
_FINALIZATION_PROBE_CACHE_SECONDS = 300.0
_FINALIZATION_PROBE_LOCK = threading.Lock()
_FINALIZATION_PROBE_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_FINALIZATION_PROBE_FAILURES: dict[
    tuple[str, str, str], tuple[float, dict[str, Any]]
] = {}
_FINALIZATION_PROBE_ACTIVE: dict[
    tuple[str, str, str], tuple[float, threading.Thread]
] = {}

# Landlock uses the generic syscall numbers on the Linux architectures Python
# supports in this deployment. Unsupported kernels return ENOSYS and launch is
# rejected instead of silently running without confinement.
_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_LL_WRITE_FILE = 1 << 1
_LL_REMOVE_DIR = 1 << 4
_LL_REMOVE_FILE = 1 << 5
_LL_MAKE_CHAR = 1 << 6
_LL_MAKE_DIR = 1 << 7
_LL_MAKE_REG = 1 << 8
_LL_MAKE_SOCK = 1 << 9
_LL_MAKE_FIFO = 1 << 10
_LL_MAKE_BLOCK = 1 << 11
_LL_MAKE_SYM = 1 << 12
_LL_REFER = 1 << 13
_LL_TRUNCATE = 1 << 14
_LL_MUTATE_ABI1 = (
    _LL_WRITE_FILE
    | _LL_REMOVE_DIR
    | _LL_REMOVE_FILE
    | _LL_MAKE_CHAR
    | _LL_MAKE_DIR
    | _LL_MAKE_REG
    | _LL_MAKE_SOCK
    | _LL_MAKE_FIFO
    | _LL_MAKE_BLOCK
    | _LL_MAKE_SYM
)
_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000
_SECCOMP_DENIED_SYSCALLS = (
    "chmod",
    "fchmod",
    "fchmodat",
    "fchmodat2",
    "chown",
    "fchown",
    "lchown",
    "fchownat",
    "setxattr",
    "lsetxattr",
    "fsetxattr",
    "removexattr",
    "lremovexattr",
    "fremovexattr",
    "utime",
    "utimes",
    "futimesat",
    "utimensat",
    "ptrace",
    "process_vm_writev",
    "pidfd_getfd",
    "open_by_handle_at",
)


class WorkspaceError(RuntimeError):
    """A fail-closed workspace, sandbox, scope, or promotion error."""


class GitCommandTimeout(WorkspaceError):
    """A bounded Git child exceeded its phase deadline and was reaped."""

    def __init__(
        self,
        *,
        phase: str,
        argv: list[str],
        cwd: Path,
        timeout: float,
        pid: int,
        tree_terminated: bool,
    ) -> None:
        token = {
            "workspace_provision": "workspace_provision_git_timeout",
            "worker_finalization": "worker_finalization_timeout",
            "review_acceptance": "review_acceptance_git_probe_timeout",
            "preflight_finalization": "preflight_finalization_git_timeout",
            "workspace_cleanup": "workspace_cleanup_git_timeout",
        }.get(phase, "git_command_timeout")
        command = " ".join(argv[:8])
        super().__init__(
            f"{token}:phase={phase}:command={command}:cwd={cwd}:"
            f"timeout={timeout:g}:pid={pid}:tree_terminated={str(tree_terminated).lower()}"
        )
        self.phase = phase
        self.argv = tuple(argv)
        self.cwd = cwd
        self.timeout = timeout
        self.pid = pid
        self.tree_terminated = tree_terminated


class ValidationRunError(WorkspaceError):
    """A bounded validation batch failed with structured rows retained.

    The default terminal disposition is ``validation_failed``: the candidate
    failed its gate and must not be accepted. ``retry_terminal``/``accept_review``
    /``mark_done`` refuse it (a supersede is the only way out), which is the
    correct, unchanged behaviour for a genuine failure.
    """

    # Single source of truth for the terminal state a caught ValidationRunError
    # maps to. A subclass overrides it; the finalizer should read
    # ``getattr(exc, "terminal_state", ...)`` rather than hardcoding the string.
    terminal_state: str = VALIDATION_FAILED
    recoverable: bool = False
    requires_supersede: bool = True
    restriction: str | None = None

    def __init__(self, message: str, results: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.results = [dict(row) for row in results]


class ValidationEnvironmentBlocked(ValidationRunError):
    """The validation command could not run in this sandbox (NF-2026-00271/298).

    A distinct, *additional* terminal state -- never a relabelling of a real
    failure. It names the exact restriction (a forbidden spawn, a refused
    chmod, an absent interpreter, a missing validator package) and is
    operationally recoverable: re-running in a corrected environment clears it,
    so unlike ``validation_failed`` it does not require a supersede.

    Subclasses ``ValidationRunError`` on purpose: existing ``except
    ValidationRunError`` finalizer paths still catch it (they never crash), but
    a finalizer that reads ``terminal_state`` routes it to the recoverable
    state instead of the acceptance-blocking one.
    """

    terminal_state = VALIDATION_ENVIRONMENT_BLOCKED
    recoverable = True
    requires_supersede = False

    def __init__(
        self,
        message: str,
        results: list[dict[str, Any]],
        *,
        restriction: str,
        restrictions: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message, results)
        self.restriction = restriction
        self.restrictions = restrictions or (restriction,)


@dataclass(frozen=True, slots=True)
class WorkerWorkspace:
    request_id: str
    repo: Path
    path: Path
    home: Path
    allowed_writes: tuple[str, ...]
    parent_baseline: dict[str, str | None]
    workspace_baseline: dict[str, str | None]
    # Complete worktree manifest used only when the bounded Git detector is
    # unavailable.  Unlike ``workspace_baseline`` this covers every file, so a
    # fallback cannot silently miss an out-of-scope modification or new file.
    tree_baseline: dict[str, str | None] | None = None
    provisioning_timings_ms: dict[str, float] | None = None
    inherited_rework_paths: tuple[str, ...] = ()
    # Commit OID the isolated worktree was detached at when it was created.
    # ``changed_paths`` diffs against this pinned base, not the live symbolic
    # ``HEAD``, so a worker that commits inside its own worktree cannot make its
    # work invisible by moving ``HEAD``.  ``None`` only for legacy metadata that
    # predates the pin, in which case the symbolic ``HEAD`` fallback is used.
    base_oid: str | None = None

    def as_metadata(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "repo": str(self.repo),
            "path": str(self.path),
            "home": str(self.home),
            "allowed_writes": list(self.allowed_writes),
            "parent_baseline": dict(self.parent_baseline),
            "workspace_baseline": dict(self.workspace_baseline),
            "tree_baseline": dict(self.tree_baseline or {}),
            "provisioning_timings_ms": dict(self.provisioning_timings_ms or {}),
            "inherited_rework_paths": list(self.inherited_rework_paths),
            "base_oid": self.base_oid,
        }

    @classmethod
    def from_metadata(cls, payload: dict[str, Any]) -> "WorkerWorkspace":
        return cls(
            request_id=str(payload["request_id"]),
            repo=Path(payload["repo"]).resolve(),
            path=Path(payload["path"]).resolve(),
            home=Path(payload["home"]).resolve(),
            allowed_writes=tuple(str(v) for v in payload["allowed_writes"]),
            parent_baseline={
                str(k): (None if v is None else str(v))
                for k, v in dict(payload["parent_baseline"]).items()
            },
            workspace_baseline={
                str(k): (None if v is None else str(v))
                for k, v in dict(payload.get("workspace_baseline") or {}).items()
            },
            tree_baseline={
                str(k): (None if v is None else str(v))
                for k, v in dict(payload.get("tree_baseline") or {}).items()
            } or None,
            provisioning_timings_ms={
                str(k): float(v)
                for k, v in dict(payload.get("provisioning_timings_ms") or {}).items()
            } or None,
            inherited_rework_paths=tuple(
                _relative_repo_path(v)
                for v in payload.get("inherited_rework_paths") or ()
            ),
            base_oid=(
                str(payload["base_oid"])
                if payload.get("base_oid") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class TrustedValidationExecutable:
    path: Path
    root: Path


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def _terminate_git_process_tree(process: subprocess.Popen[str]) -> bool:
    """Terminate the exact process group created for one bounded Git call."""
    if process.poll() is not None:
        return True
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(process.pid), "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        import signal

        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return False
    return process.poll() is not None


def _git_environment() -> dict[str, str]:
    """Return a noninteractive Git environment without ambient redirection."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float = 120,
    phase: str = "workspace_git",
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    popen_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "text": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "shell": False,
        "env": _git_environment(),
    }
    if input_text is not None:
        popen_kwargs["stdin"] = subprocess.PIPE
    if os.name == "nt":
        # ``CREATE_NEW_PROCESS_GROUP`` is Windows-only and absent from POSIX
        # typeshed/runtime modules. Keep the documented Win32 flag local while
        # preserving importability and type-checking on every other platform.
        popen_kwargs["creationflags"] = 0x00000200
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(argv, **popen_kwargs)
    try:
        if input_text is None:
            stdout, stderr = process.communicate(timeout=timeout)
        else:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        tree_terminated = _terminate_git_process_tree(process)
        raise GitCommandTimeout(
            phase=phase,
            argv=argv,
            cwd=cwd,
            timeout=timeout,
            pid=process.pid,
            tree_terminated=tree_terminated,
        ) from exc
    return subprocess.CompletedProcess(
        argv,
        int(process.returncode or 0),
        stdout,
        stderr,
    )


def _sparse_checkout_pattern(relative: str) -> str:
    """Return one exact non-cone sparse-checkout pattern."""
    if "\n" in relative or "\r" in relative:
        raise WorkspaceError("sparse_checkout_path_contains_line_break")
    escaped = relative.replace("\\", "\\\\")
    for token in ("*", "?", "["):
        escaped = escaped.replace(token, f"\\{token}")
    return f"/{escaped}"


def _prepare_sparse_worktree(
    path: Path,
    seeded: list[str],
    allowed: tuple[str, ...],
    *,
    timeout: float,
) -> None:
    """Materialize only declared tracked files while preserving Git truth."""
    patterns = sorted({_sparse_checkout_pattern(relative) for relative in seeded})
    # Allowed patterns must also be present in the sparse definition so a new
    # output can be staged without ``git add --sparse``. Unlike seeded paths,
    # their glob metacharacters are intentional and retain card semantics.
    patterns.extend(f"/{relative}" for relative in allowed)
    patterns = sorted(set(patterns))
    if not patterns:
        patterns = ["/__AIWORKHUB_EMPTY_SPARSE_WORKTREE__"]
    commands: tuple[tuple[list[str], str | None], ...] = (
        (["git", "sparse-checkout", "init", "--no-cone"], None),
        (
            ["git", "sparse-checkout", "set", "--no-cone", "--stdin"],
            "\n".join(patterns) + "\n",
        ),
        (["git", "read-tree", "-mu", "HEAD"], None),
    )
    for argv, input_text in commands:
        result = _run(
            argv,
            cwd=path,
            timeout=timeout,
            phase="workspace_provision",
            input_text=input_text,
        )
        if result.returncode != 0:
            raise WorkspaceError(
                f"workspace_sparse_checkout_failed:{argv[1]}:{result.stderr[-500:]}"
            )


def _read_git_control_file(path: Path, *, label: str) -> str:
    """Read one bounded Git administrative file without spawning Git."""
    if path.is_symlink() or not path.is_file():
        raise WorkspaceError(f"{label}_missing")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise WorkspaceError(f"{label}_unreadable:{type(exc).__name__}") from exc
    if not payload or len(payload) > 4096 or b"\x00" in payload:
        raise WorkspaceError(f"{label}_invalid")
    try:
        value = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise WorkspaceError(f"{label}_invalid_encoding") from exc
    if not value or "\n" in value or "\r" in value:
        raise WorkspaceError(f"{label}_invalid")
    return value


def _gitdir_pointer(marker: Path, *, label: str) -> Path:
    value = _read_git_control_file(marker, label=label)
    prefix = "gitdir: "
    if not value.startswith(prefix):
        raise WorkspaceError(f"{label}_invalid")
    raw = value[len(prefix):].strip()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = marker.parent / candidate
    try:
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"{label}_target_unavailable") from exc


def _common_git_dir(repo: Path) -> Path:
    marker = repo / ".git"
    if marker.is_symlink():
        raise WorkspaceError("repository_git_marker_symlink_forbidden")
    if marker.is_dir():
        return marker.resolve(strict=True)
    git_dir = _gitdir_pointer(marker, label="repository_git_marker")
    common_marker = git_dir / "commondir"
    if not common_marker.exists():
        return git_dir
    raw = _read_git_control_file(common_marker, label="repository_commondir")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = git_dir / candidate
    try:
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("repository_commondir_unavailable") from exc


def _isolated_worktree_base_oid(repo: Path, path: Path) -> str:
    """Verify a new detached worktree from bounded Git metadata only.

    ``git worktree add --detach`` has already created an administrative record.
    Re-spawning ``git symbolic-ref`` and two ``git rev-parse`` probes added no
    authority, but on Windows one of those tiny children could inherit a bad
    launcher handle and hold the interactive launch call for 120 seconds. The
    worktree pointer, its round-trip backlink, ``commondir`` and detached HEAD
    encode the same facts without another process or an unbounded wait.
    """
    marker = path / ".git"
    if marker.is_symlink() or not marker.is_file():
        raise WorkspaceError("worktree_is_not_detached_and_isolated")
    admin_dir = _gitdir_pointer(marker, label="worktree_git_marker")
    common_dir = _common_git_dir(repo)
    try:
        admin_dir.relative_to(common_dir / "worktrees")
    except ValueError as exc:
        raise WorkspaceError("worktree_gitdir_outside_repository") from exc

    backlink_raw = _read_git_control_file(
        admin_dir / "gitdir", label="worktree_gitdir_backlink"
    )
    backlink = Path(backlink_raw)
    if not backlink.is_absolute():
        backlink = admin_dir / backlink
    if os.path.normcase(os.path.abspath(backlink)) != os.path.normcase(
        os.path.abspath(marker)
    ):
        raise WorkspaceError("worktree_gitdir_backlink_mismatch")

    common_raw = _read_git_control_file(
        admin_dir / "commondir", label="worktree_commondir"
    )
    linked_common = Path(common_raw)
    if not linked_common.is_absolute():
        linked_common = admin_dir / linked_common
    try:
        linked_common = linked_common.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("worktree_commondir_unavailable") from exc
    if os.path.normcase(str(linked_common)) != os.path.normcase(str(common_dir)):
        raise WorkspaceError("worktree_repository_identity_mismatch")

    head = _read_git_control_file(admin_dir / "HEAD", label="worktree_head")
    if head.startswith("ref:"):
        raise WorkspaceError("worktree_is_not_detached_and_isolated")
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head) is None:
        raise WorkspaceError("worktree_base_oid_unavailable")
    return head


def _read_packed_ref_oid(git_dir: Path, ref: str) -> str:
    """Resolve one branch ref from the bounded ``packed-refs`` table."""
    path = git_dir / "packed-refs"
    if path.is_symlink() or not path.is_file():
        raise WorkspaceError("repository_head_oid_unavailable")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise WorkspaceError("repository_head_oid_unavailable") from exc
    if not payload or len(payload) > 1_048_576 or b"\x00" in payload:
        raise WorkspaceError("repository_head_oid_unavailable")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceError("repository_head_oid_unavailable") from exc
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "^")):
            continue
        oid, separator, name = line.partition(" ")
        if separator and name == ref and re.fullmatch(r"[0-9a-f]{40,64}", oid):
            return oid
    raise WorkspaceError("repository_head_oid_unavailable")


def _repository_head_oid(repo: Path) -> str:
    """Resolve HEAD from bounded Git metadata without spawning a subprocess."""
    git_dir = _common_git_dir(repo)
    head = _read_git_control_file(git_dir / "HEAD", label="repository_head")
    if head.startswith("ref:"):
        ref = head[4:].strip()
        if (
            not ref
            or ref.startswith(("/", "\\"))
            or "\\" in ref
            or "\x00" in ref
            or any(part in {"", ".", ".."} for part in ref.split("/"))
        ):
            raise WorkspaceError("repository_head_oid_unavailable")
        ref_path = git_dir / ref
        if ref_path.is_symlink() or not ref_path.is_file():
            head = _read_packed_ref_oid(git_dir, ref)
        else:
            head = _read_git_control_file(ref_path, label="repository_head_ref")
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head) is None:
        raise WorkspaceError("repository_head_oid_unavailable")
    return head


def _relative_repo_path(raw: str) -> str:
    if not isinstance(raw, str):
        raise WorkspaceError(f"invalid_repo_path_type:{type(raw).__name__}")
    value = raw.strip().replace("\\", "/")
    if not value or value.startswith("/") or "\x00" in value:
        raise WorkspaceError(f"invalid_repo_path:{raw!r}")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise WorkspaceError(f"unsafe_repo_path:{raw}")
    return str(path)


def _require_beneath(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError(f"path_escapes_workspace:{candidate}") from exc
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise WorkspaceError(f"symlink_path_component_forbidden:{cursor}")
    resolved = candidate.resolve(strict=False)
    if resolved == root or root in resolved.parents:
        return resolved
    raise WorkspaceError(f"path_escapes_workspace:{candidate}")


def _static_prefix(pattern: str) -> str:
    indexes = [i for i in (pattern.find("*"), pattern.find("?"), pattern.find("[")) if i >= 0]
    return pattern[: min(indexes)] if indexes else pattern


def _segment_glob_matches(pattern_parts: list[str], path_parts: list[str]) -> bool:
    """Match repo-relative path segments with per-segment wildcards.

    A single ``*`` (and ``?`` / ``[...]``) is confined to one path segment via
    :func:`fnmatch.fnmatchcase`; only an explicit ``**`` segment spans zero or
    more separators.  This mirrors the single-segment semantics that
    :meth:`pathlib.Path.glob` already applies when the workspace seeds and
    validates the same patterns, so scope checks cannot admit a nested path a
    lone ``*`` was never meant to reach (for example ``docs/*.md`` must not
    match ``docs/private/secret.md``).
    """
    if not pattern_parts:
        return not path_parts
    head, *tail = pattern_parts
    if head == "**":
        for split in range(len(path_parts) + 1):
            if _segment_glob_matches(tail, path_parts[split:]):
                return True
        return False
    if not path_parts:
        return False
    if fnmatch.fnmatchcase(path_parts[0], head):
        return _segment_glob_matches(tail, path_parts[1:])
    return False


def _matches(path: str, patterns: Iterable[str]) -> bool:
    normalized = _relative_repo_path(path)
    path_parts = normalized.split("/")
    for raw in patterns:
        pattern = _relative_repo_path(raw)
        if normalized == pattern:
            return True
        if _segment_glob_matches(pattern.split("/"), path_parts):
            return True
    return False


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _hash_path(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        return "symlink:" + os.readlink(path)
    if not path.is_file():
        raise WorkspaceError(f"non_file_promotion_target:{path}")
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    mode = stat.S_IMODE(path.stat().st_mode)
    return f"file:{mode:o}:{digest.hexdigest()}"


def finalization_git_timeout_seconds() -> float:
    """Return the bounded finalization Git budget configured for this host."""
    raw = os.environ.get(FINALIZATION_GIT_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_FINALIZATION_GIT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise WorkspaceError("invalid_finalization_git_timeout") from exc
    if not MIN_FINALIZATION_GIT_TIMEOUT_SECONDS <= value <= MAX_FINALIZATION_GIT_TIMEOUT_SECONDS:
        raise WorkspaceError("finalization_git_timeout_out_of_range")
    return value


def _worktree_manifest_paths(root: Path) -> list[tuple[str, Path]]:
    """List every mechanically relevant worktree file without following links."""
    rows: list[tuple[str, Path]] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise WorkspaceError(f"workspace_manifest_scan_failed:{directory}:{exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            # A linked worktree's .git pointer is Git-owned administrative
            # metadata, not candidate content and never an allowed output.
            if relative == ".git":
                continue
            if entry.is_symlink():
                rows.append((_relative_repo_path(relative), path))
            elif entry.is_dir(follow_symlinks=False):
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                rows.append((_relative_repo_path(relative), path))
            else:
                raise WorkspaceError(f"workspace_manifest_special_file:{relative}")
    rows.sort(key=lambda item: item[0])
    return rows


def _worktree_manifest(root: Path) -> dict[str, str | None]:
    """Hash the complete worktree using bounded CPU-derived IO parallelism."""
    rows = _worktree_manifest_paths(root)
    if not rows:
        return {}
    # Hashing independent files releases the GIL while reading.  Keep one core
    # free for the interactive MCP server and never use a hard-coded pool size.
    worker_count = min(len(rows), max(1, (os.cpu_count() or 2) - 1))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        hashes = list(pool.map(lambda item: _hash_path(item[1]), rows))
    return {relative: digest for (relative, _path), digest in zip(rows, hashes)}


def _manifest_changed_paths(workspace: WorkerWorkspace, *, git_phase: str) -> list[str]:
    baseline = workspace.tree_baseline
    if baseline is None:
        raise WorkspaceError(f"{git_phase}_git_fallback_unavailable:baseline_missing")
    current = _worktree_manifest(workspace.path)
    rows = {
        relative
        for relative in set(baseline) | set(current)
        if baseline.get(relative) != current.get(relative)
    }
    # Rework predecessor bytes are intentionally present at workspace creation
    # yet still form part of the candidate delta against the canonical parent.
    for relative in workspace.inherited_rework_paths:
        current_hash = current.get(relative)
        if current_hash != workspace.parent_baseline.get(relative):
            rows.add(_relative_repo_path(relative))
    return sorted(rows)


PYTHON_CANDIDATE_AUTHORITY_ENV = "AIWORKHUB_PYTHON_CANDIDATE_AUTHORITY"


def _python_candidate_authority_rows(
    workspace: WorkerWorkspace,
) -> list[dict[str, str]]:
    """Describe the exact retained Python delta without importing candidate code."""
    # A real isolated workspace created by ``create_workspace`` always carries
    # this manifest.  Lightweight unit/portability harnesses intentionally do
    # not; they have no retained candidate delta to authorize.
    if workspace.tree_baseline is None:
        return []
    current = _worktree_manifest(workspace.path)
    rows: list[dict[str, str]] = []
    inherited = set(workspace.inherited_rework_paths)
    for relative in sorted(set(workspace.tree_baseline) | set(current) | inherited):
        if not relative.endswith(".py"):
            continue
        # Retained predecessor bytes are applied while the successor workspace
        # is provisioned, before ``tree_baseline`` is recorded.  Compare those
        # exact paths to the canonical parent baseline or they disappear from
        # Python import authority during provider-free validation replays.
        before = (
            workspace.parent_baseline.get(relative)
            if relative in inherited
            else workspace.tree_baseline.get(relative)
        )
        after = current.get(relative)
        if before == after:
            continue
        state = "added" if before is None else "deleted" if after is None else "modified"
        row = {"path": _relative_repo_path(relative), "state": state}
        if after is not None:
            target = workspace.path / relative
            if target.is_symlink() or not target.is_file():
                raise WorkspaceError(
                    f"python_candidate_authority_path_invalid:{relative}"
                )
            row["bytes_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        rows.append(row)
    return rows


def python_candidate_authority(workspace: WorkerWorkspace) -> dict[str, Any]:
    """Return deterministic path/state/bytes authority for Python candidate files."""
    rows = _python_candidate_authority_rows(workspace)
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row["path"].encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(row["state"].encode("ascii"))
        digest.update(b"\x1f")
        digest.update(row.get("bytes_sha256", "").encode("ascii"))
        digest.update(b"\x1e")
    return {
        "schema_id": "aiworkhub.python_candidate_authority.v1",
        "digest": digest.hexdigest() if rows else "",
        "sources": rows,
    }


def _expand_declared(repo: Path, declared: Iterable[str]) -> list[str]:
    """Expand declared paths from the live parent filesystem.

    ``Path.glob("tree/**")`` yields directory entries rather than every file
    below ``tree`` on supported Python versions.  Terminal ``/**`` therefore
    needs an explicit filesystem walk so declared untracked and gitignored
    input artifacts are hydrated into the isolated worktree as well.
    """
    rows: set[str] = set()
    for raw in declared:
        pattern = _relative_repo_path(raw)
        if any(ch in pattern for ch in "*?["):
            if pattern.endswith("/**"):
                root = repo / pattern[:-3]
                if root.is_symlink():
                    rows.add(root.relative_to(repo).as_posix())
                elif root.is_dir():
                    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                        directory = Path(dirpath)
                        for name in list(dirnames):
                            candidate = directory / name
                            if candidate.is_symlink():
                                rows.add(candidate.relative_to(repo).as_posix())
                                dirnames.remove(name)
                        for name in filenames:
                            candidate = directory / name
                            if candidate.is_file() or candidate.is_symlink():
                                rows.add(candidate.relative_to(repo).as_posix())
            else:
                for match in repo.glob(pattern):
                    if match.is_file() or match.is_symlink():
                        rows.add(match.relative_to(repo).as_posix())
        else:
            rows.add(pattern)
    if len(rows) > MAX_SEED_FILES:
        raise WorkspaceError(f"seed_file_limit_exceeded:{len(rows)}")
    return sorted(rows)

def _extract_pytest_test_files(repo: Path, validation_rows: Iterable[str]) -> tuple[str, ...]:
    """Extract test file paths from pytest validation commands.

    Parse validation commands to find pytest invocations and extract the test
    file arguments. Returns paths relative to the repository root.
    """
    test_files: set[str] = set()

    for cmd in validation_rows:
        parts = shlex.split(cmd)
        if not parts:
            continue

        is_pytest = False
        skip_next = False

        for i, part in enumerate(parts):
            if skip_next:
                skip_next = False
                continue
            if part in ("pytest", "python", "python3"):
                is_pytest = part == "pytest"
                continue
            if part == "-m" and i + 1 < len(parts) and parts[i + 1] == "pytest":
                is_pytest = True
                skip_next = True
                continue

            if is_pytest and part and not part.startswith("-"):
                # A pytest node ID authenticates its file component; the
                # ``::class::test`` suffix is selection metadata, not a path.
                relative = _relative_repo_path(part.split("::", 1)[0])
                candidate = repo / relative
                if candidate.is_file() and relative.endswith(".py"):
                    test_files.add(relative)

    return tuple(sorted(test_files))


def _resolve_local_python_imports(repo: Path, seeded: Iterable[str]) -> tuple[str, ...]:
    """Return the bounded static package-local import closure for Python seeds.

    Sparse validation must import candidate modules from the detached worktree,
    not fall through to the installed/canonical package.  Follow only imports
    that resolve to regular repository files; third-party and stdlib imports
    remain external.  Relative imports are unambiguously repository-local and
    therefore fail closed when their target cannot be resolved.
    """

    repo = repo.resolve()
    initial = sorted({_relative_repo_path(value) for value in seeded})
    roots: set[Path] = {repo}
    src_root = repo / "src"
    if src_root.is_dir() and not src_root.is_symlink():
        roots.add(src_root)

    def package_context(relative: str) -> tuple[Path, tuple[str, ...]]:
        current = (repo / relative).parent
        parts: list[str] = []
        while current != repo and (current / "__init__.py").is_file():
            parts.insert(0, current.name)
            current = current.parent
        if parts:
            roots.add(current)
        return current, tuple(parts)

    for relative in initial:
        if relative.endswith(".py") and (repo / relative).is_file():
            package_context(relative)

    def module_file(
        parts: tuple[str, ...], preferred_root: Path | None = None
    ) -> tuple[Path, Path] | None:
        if not parts:
            return None
        ordered_roots = sorted(roots, key=lambda value: value.as_posix())
        if preferred_root is not None:
            ordered_roots = [preferred_root] + [
                root for root in ordered_roots if root != preferred_root
            ]
        for root in ordered_roots:
            module = root.joinpath(*parts).with_suffix(".py")
            package = root.joinpath(*parts, "__init__.py")
            for candidate in (module, package):
                _require_beneath(repo, candidate)
                if candidate.is_symlink():
                    raise WorkspaceError(
                        "validation_python_import_symlink:"
                        + candidate.relative_to(repo).as_posix()
                    )
                if candidate.is_file():
                    return root, candidate
        return None

    def add_module(
        parts: tuple[str, ...],
        pending: list[str],
        rows: set[str],
        preferred_root: Path | None = None,
    ) -> bool:
        resolved = module_file(parts, preferred_root)
        if resolved is None:
            return False
        root, candidate = resolved
        additions = [candidate]
        parent = candidate.parent
        while parent != root:
            package_init = parent / "__init__.py"
            if not package_init.is_file():
                break
            additions.append(package_init)
            parent = parent.parent
        for path in additions:
            relative = _relative_repo_path(path.relative_to(repo).as_posix())
            if relative not in rows:
                rows.add(relative)
                pending.append(relative)
                if len(rows) > MAX_SEED_FILES:
                    raise WorkspaceError(
                        f"seed_file_limit_exceeded:{len(rows)}"
                    )
        return True

    rows = set(initial)
    pending = [
        relative
        for relative in initial
        if relative.endswith(".py") and (repo / relative).is_file()
    ]
    parsed: set[str] = set()
    while pending:
        relative = pending.pop()
        if relative in parsed:
            continue
        parsed.add(relative)
        source_path = repo / relative
        if source_path.is_symlink() or not source_path.is_file():
            continue
        try:
            tree = ast.parse(source_path.read_bytes(), filename=relative)
        except (OSError, SyntaxError, ValueError) as exc:
            raise WorkspaceError(
                f"validation_python_import_parse_failed:{relative}"
            ) from exc
        source_root, package_parts = package_context(relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add_module(
                        tuple(alias.name.split(".")), pending, rows, source_root
                    )
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            module_parts = tuple((node.module or "").split(".")) if node.module else ()
            if node.level:
                if not package_parts or node.level > len(package_parts):
                    raise WorkspaceError(
                        f"validation_python_relative_import_unresolved:{relative}:"
                        f"level={node.level}"
                    )
                base = package_parts[: len(package_parts) - node.level + 1] + module_parts
                if module_parts and not add_module(
                    base, pending, rows, source_root
                ):
                    raise WorkspaceError(
                        f"validation_python_relative_import_unresolved:{relative}:"
                        + ".".join(base)
                    )
            else:
                base = module_parts
                add_module(base, pending, rows, source_root)
            for alias in node.names:
                if alias.name == "*":
                    continue
                add_module(
                    base + tuple(alias.name.split(".")),
                    pending,
                    rows,
                    source_root,
                )
    return tuple(sorted(rows))


# NF-2026-00448: a declared JS validation entrypoint may ``require('./x')`` a
# sibling module the task card never listed, mirroring the Python relative-
# import closure above. Only relative requires (``./x``, ``../x``) are
# followed -- they are unambiguously repository-local; a bare specifier
# (``require('vscode')``, ``require('fs')``) is a Node builtin or an
# ``npm``-managed dependency, and ``_npm_validation_support`` already requires
# a dependency-free ``node_modules`` tree, so there is nothing local left to
# seed for it.
_JS_LOCAL_REQUIRE_RE = re.compile(r"""require\(\s*['"](\.\.?/[^'"]*)['"]\s*\)""")


def _resolve_one_local_js_require(
    repo: Path, including_dir: Path, target: str
) -> Path | None:
    """Bounded Node-local resolution for one relative ``require`` target.

    Deliberately narrower than Node's full resolver: an explicit ``.js``/
    ``.json`` target resolves to exactly that file (so ``require('../package
    .json')`` -- used by canonical fixtures such as ``vscode-extension/test/
    stable-runtime-upgrade.test.js`` -- seeds the JSON file itself instead of
    being mangled into a nonexistent ``package.json.js``); an extensionless
    target tries ``<target>.js``, then ``<target>.json``, then
    ``<target>/index.js``, then ``<target>/index.json``, mirroring Node's own
    file-before-directory order. Every candidate still passes through
    ``_require_beneath`` (beneath + symlink enforcement), so this only widens
    which filenames are considered -- never where they may resolve.
    """
    base = including_dir / PurePosixPath(target)
    if base.suffix in (".js", ".json"):
        candidates = (base,)
    else:
        candidates = (
            base.parent / f"{base.name}.js",
            base.parent / f"{base.name}.json",
            base / "index.js",
            base / "index.json",
        )
    for candidate in candidates:
        resolved = _require_beneath(repo, candidate)
        if resolved.is_symlink():
            raise WorkspaceError(
                "validation_js_require_symlink:"
                + resolved.relative_to(repo).as_posix()
            )
        if resolved.is_file():
            return resolved
    return None


def _resolve_local_js_requires(repo: Path, seeded: Iterable[str]) -> tuple[str, ...]:
    """Return the bounded static local ``require('./x')`` closure for JS seeds.

    Sparse validation must run a candidate's own CommonJS sibling modules from
    the retained worktree, not fail with a bare ``MODULE_NOT_FOUND`` for a
    sibling the task card never declared (NF-2026-00448/458, the GLM
    ``extension.js`` -> ``./runtime-retention`` -> ``./runtime-language-model-
    bridge`` -> ``./runtime-provider-boundary`` reproduction). An unresolved
    relative require fails closed, exactly like the Python closure's relative
    imports, since it is unambiguously repository-local.
    """
    repo = repo.resolve()
    rows = set(seeded)
    pending = [relative for relative in rows if relative.endswith(".js")]
    parsed: set[str] = set()
    while pending:
        relative = pending.pop()
        if relative in parsed:
            continue
        parsed.add(relative)
        source_path = repo / relative
        if source_path.is_symlink() or not source_path.is_file():
            continue
        try:
            text = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        including_dir = source_path.parent
        for match in _JS_LOCAL_REQUIRE_RE.finditer(text):
            target = match.group(1)
            candidate = _resolve_one_local_js_require(repo, including_dir, target)
            if candidate is None:
                raise WorkspaceError(
                    f"validation_js_require_unresolved:{relative}:{target}"
                )
            candidate_relative = candidate.relative_to(repo).as_posix()
            if candidate_relative not in rows:
                rows.add(candidate_relative)
                pending.append(candidate_relative)
                if len(rows) > MAX_SEED_FILES:
                    raise WorkspaceError(f"seed_file_limit_exceeded:{len(rows)}")
    return tuple(sorted(rows))


def _npm_validation_prefixes(commands: Iterable[str]) -> tuple[str, ...]:
    """Return exact repository-relative ``npm --prefix`` validation roots."""
    prefixes: set[str] = set()
    for command in commands:
        tokens, _components, _tmpdir, cd_relative = _parse_validation_command_detailed(
            command
        )
        if not tokens or Path(tokens[0]).name.lower() not in {"npm", "npm.cmd"}:
            continue
        raw_prefix = ""
        for index, token in enumerate(tokens[1:], start=1):
            if token == "--prefix" and index + 1 < len(tokens):
                raw_prefix = tokens[index + 1]
                break
            if token.startswith("--prefix="):
                raw_prefix = token.split("=", 1)[1]
                break
        if not raw_prefix:
            continue
        prefix = _relative_repo_path(raw_prefix)
        if cd_relative:
            prefix = _relative_repo_path(
                (PurePosixPath(cd_relative) / PurePosixPath(prefix)).as_posix()
            )
        prefixes.add(prefix)
    return tuple(sorted(prefixes))


def _regular_support_files(root: Path, relative_root: str) -> set[str]:
    """Enumerate a bounded immutable validation-support subtree."""
    base = root / relative_root
    _require_beneath(root, base)
    if base.is_symlink() or not base.is_dir():
        raise WorkspaceError(f"validation_npm_support_missing:{relative_root}")
    rows: set[str] = set()
    for directory, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _NPM_SUPPORT_EXCLUDED_DIRS
        )
        current = Path(directory)
        for name in sorted(filenames):
            if name.endswith(".vsix"):
                continue
            candidate = current / name
            relative = _relative_repo_path(candidate.relative_to(root).as_posix())
            if candidate.is_symlink() or not candidate.is_file():
                raise WorkspaceError(f"validation_npm_support_invalid:{relative}")
            rows.add(relative)
            if len(rows) > MAX_SEED_FILES:
                raise WorkspaceError("validation_npm_support_file_limit_exceeded")
    return rows


def _npm_validation_support(repo: Path, commands: Iterable[str]) -> tuple[str, ...]:
    """Resolve immutable files needed by exact sparse ``npm --prefix`` gates.

    The dependency tree is deliberately not copied.  Projects with external
    dependencies must provide a separately verified read-only tree; silently
    hydrating or downloading ``node_modules`` during validation would make the
    candidate non-reproducible and reintroduce the large-copy bottleneck.
    """
    rows: set[str] = set()
    for prefix in _npm_validation_prefixes(commands):
        package_path = repo / prefix / "package.json"
        lock_path = repo / prefix / "package-lock.json"
        for required in (package_path, lock_path):
            _require_beneath(repo, required)
            if required.is_symlink() or not required.is_file():
                raise WorkspaceError(
                    f"validation_npm_support_missing:"
                    f"{required.relative_to(repo).as_posix()}"
                )
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"validation_npm_manifest_invalid:{prefix}") from exc
        packages = lock.get("packages")
        if not isinstance(package, dict) or not isinstance(packages, dict):
            raise WorkspaceError(f"validation_npm_manifest_invalid:{prefix}")
        dependency_rows = [key for key in packages if str(key).startswith("node_modules/")]
        if dependency_rows:
            raise WorkspaceError(
                f"validation_npm_dependency_tree_unbound:{prefix}:"
                f"{len(dependency_rows)}"
            )
        rows.update(_regular_support_files(repo, prefix))
        # AIWorkHub's extension suite has explicit static/package assertions
        # against these repository-owned roots.  Seed their committed bytes as
        # immutable support rather than broadening worker write authority.
        if package.get("name") == "aiworkhub" and prefix == "vscode-extension":
            rows.update(_regular_support_files(repo, "src/aiworkhub"))
            for relative in (
                "README.md",
                "scripts/aiworkhub-app-server-mux",
                "scripts/aiworkhub-app-server-mux.cmd",
            ):
                candidate = repo / relative
                if candidate.is_symlink() or not candidate.is_file():
                    raise WorkspaceError(f"validation_npm_support_missing:{relative}")
                rows.add(relative)
    return tuple(sorted(rows))


# ---- local quoted-include dependency preflight (B664) -----------------------
_HEADER_FILE_SUFFIXES = frozenset(
    {".h", ".hpp", ".hxx", ".hh", ".inl", ".cuh", ".c", ".cpp", ".cu", ".cc", ".cxx"}
)
_QUOTED_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+"([^"]+)"')
_DEFAULT_INCLUDE_ROOTS: tuple[str, ...] = (".",)
_INCLUDE_ROOT_CARD_KEYS = (
    "include_roots",
    "local_include_roots",
    "project_include_roots",
)


def _resolve_local_quoted_includes(
    repo: Path,
    seeded: list[str],
    include_roots: tuple[str, ...] = _DEFAULT_INCLUDE_ROOTS,
) -> list[str]:
    """Resolve repository-local quoted ``#include`` dependencies for C/CUDA files.

    For every C / CUDA / header file already in *seeded*, scan for ``#include
    "..."`` directives (angle-bracket ``<...>`` system includes are never
    followed), resolve each quoted path using compiler-style rules (current-file
    directory first, then each configured repository include root), and
    recursively collect the transitive closure of real regular files reachable
    from the declared inputs.

    Unresolvable quoted includes are *fail-closed*: the function raises
    ``WorkspaceError`` with an exact, bounded missing-dependency list so the
    coordinator can reject launch before a worker model spends tokens.

    Returns the augmented, sorted, deduplicated seed list.  Callers must still
    respect ``MAX_SEED_FILES``, symlink rejection, and beneath-root checks.
    """
    if not seeded:
        return []

    normalized_roots = _normalize_include_roots(repo, include_roots)

    resolved: dict[str, str] = {}  # repo-relative path -> including relative (provenance)
    missing: dict[str, str] = {}    # unresolved include string -> first including relative
    pending: list[str] = list(seeded)
    seen: set[str] = set()

    while pending:
        relative = pending.pop()
        if relative in seen:
            continue
        seen.add(relative)

        full_path = repo / relative
        suffix = full_path.suffix.lower()
        if suffix not in _HEADER_FILE_SUFFIXES:
            continue
        if full_path.is_symlink() or not full_path.is_file():
            continue

        try:
            text = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        including_dir = (repo / relative).parent
        for line in text.splitlines():
            match = _QUOTED_INCLUDE_RE.match(line)
            if match is None:
                continue
            include_target = match.group(1)
            # Resolve: current-file directory first, then each include root.
            candidate = _resolve_one_quoted_include(
                repo, including_dir, include_target, normalized_roots
            )
            if candidate is None:
                if include_target not in missing:
                    missing[include_target] = relative
                continue
            candidate_relative = candidate.relative_to(repo).as_posix()
            if candidate.is_symlink():
                continue
            if not candidate.is_file():
                if include_target not in missing:
                    missing[include_target] = relative
                continue
            if candidate_relative not in resolved:
                resolved[candidate_relative] = relative
                if candidate_relative not in seen:
                    pending.append(candidate_relative)

    if missing:
        items = sorted(missing.items(), key=lambda kv: kv[0])[:32]
        detail = "; ".join(f"{inc} (from {src})" for inc, src in items)
        raise WorkspaceError(f"local_quoted_include_unresolved:{detail}")

    augmented = sorted(set(seeded) | set(resolved.keys()))
    if len(augmented) > MAX_SEED_FILES:
        raise WorkspaceError(f"seed_file_limit_exceeded:{len(augmented)}")
    return augmented


def _normalize_include_roots(repo: Path, roots: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in roots:
        if raw == ".":
            norm = "."
            candidate = repo
        else:
            candidate = _safe_include_candidate(repo, repo, raw)
            if candidate is None:
                raise WorkspaceError(f"include_root_not_directory:{raw}")
            norm = candidate.relative_to(repo).as_posix()
        if candidate.is_symlink() or not candidate.is_dir():
            raise WorkspaceError(f"include_root_not_directory:{raw}")
        key = "." if norm == "." else norm
        if key not in seen:
            seen.add(key)
            normalized.append(key)
    return tuple(normalized)


def _include_roots_from_card(card: Mapping[str, Any]) -> tuple[str, ...]:
    roots: list[str] = ["."]
    for key in _INCLUDE_ROOT_CARD_KEYS:
        value = card.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            roots.append(value)
            continue
        if isinstance(value, Iterable):
            roots.extend(root for root in value if isinstance(root, str))
    return tuple(roots)


def _resolve_one_quoted_include(
    repo: Path,
    including_dir: Path,
    target: str,
    include_roots: tuple[str, ...],
) -> Path | None:
    """Try to find *target* using compiler-style lookup.

    1. Relative to the including file's directory (``including_dir / target``).
    2. Relative to each configured repository include root.
    """
    # Rule 1: current-file directory.
    direct = _safe_include_candidate(repo, including_dir, target)
    if direct is not None and direct.exists():
        return direct

    # Rule 2: configured include roots.
    for root_raw in include_roots:
        base = repo if root_raw == "." else repo / root_raw
        candidate = _safe_include_candidate(repo, base, target)
        if candidate is None:
            continue
        if candidate.exists():
            return candidate

    return None


def _safe_include_candidate(repo: Path, base: Path, target: str) -> Path | None:
    target_value = target.strip().replace("\\", "/")
    if (
        not target_value
        or target_value.startswith("/")
        or "\x00" in target_value
    ):
        return None
    candidate = Path(os.path.abspath(base / PurePosixPath(target_value)))
    try:
        _require_beneath(repo, candidate.parent)
        resolved = candidate.resolve(strict=False)
        _require_beneath(repo, resolved)
    except WorkspaceError:
        return None
    return candidate
# ---- end local quoted-include dependency preflight (B664) -------------------


def _copy_one(source: Path, destination: Path) -> None:
    # lstat source; silently return on FileNotFoundError
    try:
        source_lstat = os.lstat(source)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WorkspaceError(f"lstat_source_failed:{source}:{exc}") from exc
    if stat.S_ISLNK(source_lstat.st_mode):
        raise WorkspaceError(f"symlink_seed_forbidden:{source}")
    if not stat.S_ISREG(source_lstat.st_mode):
        raise WorkspaceError(f"non_regular_seed_forbidden:{source}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise WorkspaceError(f"open_source_failed:{source}:{exc}") from exc

    temp_fd = None
    temp_path = None
    replaced = False
    try:
        source_fstat = os.fstat(source_fd)
        if not stat.S_ISREG(source_fstat.st_mode):
            raise WorkspaceError(f"source_not_regular_after_open:{source}")
        if not os.path.samestat(source_lstat, source_fstat):
            raise WorkspaceError(f"source_stat_mismatch:{source}")

        source_mode = stat.S_IMODE(source_fstat.st_mode)

        # Ensure destination parent directory exists (nested mkdir)
        destination.parent.mkdir(parents=True, exist_ok=True)

        temp_fd, temp_path = tempfile.mkstemp(dir=str(destination.parent))

        # Copy all bytes with explicit complete-write loop
        while True:
            data = os.read(source_fd, 65536)
            if not data:
                break
            written = 0
            while written < len(data):
                w = os.write(temp_fd, data[written:])
                if w == 0:
                    raise OSError("write returned 0")
                written += w

        # chmod temp fd to ordinary permission bits only
        chmod_fd(temp_fd, source_mode & 0o777)

        # reject an existing destination symlink
        if destination.is_symlink():
            raise WorkspaceError(f"destination_symlink_forbidden:{destination}")

        # Windows refuses to rename a mkstemp file while this process still
        # owns its CRT handle (WinError 32). Close our writer before publish;
        # the finally block sees None and cannot double-close it.
        os.close(temp_fd)
        temp_fd = None

        # Atomic replacement; hardlink-safe. The shared helper performs one
        # bounded retry window for transient Windows sharing violations (for
        # example an editor/AV reader holding AGENTS.md) while POSIX remains a
        # single replace call. Source policy files are never modified.
        atomic_replace(temp_path, destination)
        replaced = True
    finally:
        # Single ownership cleanup: close each fd at most once,
        # unlink temp exactly once unless os.replace succeeded.
        os.close(source_fd)
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_path is not None and not replaced:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def _touch_placeholder(worktree: Path, relative: str) -> None:
    target = worktree / relative
    _require_beneath(worktree, target)
    if target.exists() or target.is_symlink():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    chmod_fd(fd, 0o644)
    os.close(fd)


def _materialize_rework_predecessor(
    repo: Path,
    worktree: Path,
    card: dict[str, Any],
    allowed_writes: tuple[str, ...],
) -> list[str]:
    """Overlay one hash-pinned reviewed candidate into a successor worktree.

    Use the retained worktree while it exists. Once it is absent, only a
    sealed content-addressed changed/deleted-file delta may seed the successor.
    """
    predecessor = card.get("rework_predecessor")
    if predecessor is None:
        return []
    if not isinstance(predecessor, dict):
        raise WorkspaceError("rework_predecessor_invalid")
    request_id = str(predecessor.get("request_id") or "").strip()
    workspace_payload = predecessor.get("workspace")
    hashes = predecessor.get("changed_path_hashes")
    if (
        not _REQUEST_ID_RE.fullmatch(request_id)
        or not isinstance(workspace_payload, dict)
        or not isinstance(hashes, dict)
        or not hashes
    ):
        raise WorkspaceError("rework_predecessor_invalid")
    try:
        source_workspace = WorkerWorkspace.from_metadata(workspace_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspaceError(f"rework_predecessor_workspace_invalid:{exc}") from exc
    if source_workspace.repo != repo or source_workspace.request_id != request_id:
        raise WorkspaceError("rework_predecessor_identity_mismatch")
    assert_gc_safe_workspace_shape(
        request_id, source_workspace.path, source_workspace.home, repo=repo
    )
    if not source_workspace.path.is_symlink() and source_workspace.path.is_dir():
        return _materialize_rework_predecessor_from_worktree(
            worktree, source_workspace, hashes, allowed_writes
        )
    if predecessor.get("delta_artifact") is None:
        raise WorkspaceError("rework_predecessor_workspace_missing")
    return materialize_rework_delta_artifact(
        artifact=predecessor["delta_artifact"],
        authority_repo=repo,
        request_id=request_id,
        task_id=str(predecessor.get("task_id") or ""),
        claim_epoch=predecessor.get("claim_epoch"),
        worktree=worktree,
        expected_path_hashes=hashes,
        allowed_writes=allowed_writes,
    )


def _materialize_rework_predecessor_from_worktree(
    worktree: Path,
    source_workspace: WorkerWorkspace,
    hashes: dict[str, Any],
    allowed_writes: tuple[str, ...],
) -> list[str]:
    """Copy the hash-pinned predecessor delta from the retained worktree."""

    seeded: list[str] = []
    for raw_relative, raw_expected in sorted(hashes.items()):
        relative = _relative_repo_path(str(raw_relative))
        if not _matches(relative, allowed_writes):
            raise WorkspaceError(f"rework_predecessor_outside_scope:{relative}")
        if raw_expected is not None and (
            not isinstance(raw_expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw_expected)
        ):
            raise WorkspaceError(f"rework_predecessor_hash_invalid:{relative}")
        source = source_workspace.path / relative
        destination = worktree / relative
        _require_beneath(source_workspace.path, source)
        _require_beneath(worktree, destination)
        if source.is_symlink() or not source.is_file():
            observed = None
        else:
            observed = hashlib.sha256(source.read_bytes()).hexdigest()
        if observed != raw_expected:
            raise WorkspaceError(f"rework_predecessor_hash_mismatch:{relative}")
        if raw_expected is None:
            if destination.is_symlink() or destination.is_file():
                destination.unlink()
            elif destination.exists():
                raise WorkspaceError(f"rework_predecessor_delete_non_file:{relative}")
        else:
            _copy_one(source, destination)
        seeded.append(relative)
    return seeded


REWORK_DELTA_ARTIFACT_SCHEMA_ID = "aiworkhub.rework_delta_artifact.v2"


def _rework_delta_canonical_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _rework_delta_normalize_path(raw_path: object) -> str:
    if not isinstance(raw_path, str) or not raw_path:
        raise WorkspaceError(f"rework_delta_invalid_path:{raw_path!r}")
    relative = _relative_repo_path(raw_path)
    posix = PurePosixPath(relative)
    if (
        not relative
        or posix.is_absolute()
        or any(part in {"", ".", ".."} for part in posix.parts)
        or posix.parts[0] == ".git"
    ):
        raise WorkspaceError(f"rework_delta_invalid_path:{raw_path!r}")
    return relative


def seal_rework_delta_artifact(
    authority_repo: Path,
    task_id: str,
    request_id: str,
    claim_epoch: int,
    file_entries: Iterable[tuple[str, bytes | None]],
    artifact_dir: Path,
) -> dict[str, str]:
    """Atomically seal exact predecessor bytes as one bounded delta artifact."""
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise WorkspaceError(f"rework_delta_invalid_request_id:{request_id!r}")
    if not task_id or type(claim_epoch) is not int or claim_epoch < 1:
        raise WorkspaceError("rework_delta_identity_missing")
    entries = list(file_entries)
    if not entries:
        raise WorkspaceError("rework_delta_artifact_empty")
    if len(entries) > MAX_REWORK_OVERLAY_FILES:
        raise WorkspaceError("rework_delta_file_count_exceeds_limit")
    files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    total_content_bytes = 0
    for raw_path, content in entries:
        relative = _rework_delta_normalize_path(raw_path)
        if relative in seen_paths:
            raise WorkspaceError(f"rework_delta_duplicate_path:{relative}")
        seen_paths.add(relative)
        entry: dict[str, Any] = {"path": relative}
        if content is None:
            entry["deleted"] = True
        else:
            total_content_bytes += len(content)
            if total_content_bytes > MAX_REWORK_OVERLAY_CONTENT_BYTES:
                raise WorkspaceError("rework_delta_content_exceeds_limit")
            entry["sha256"] = hashlib.sha256(content).hexdigest()
            entry["content_base64"] = base64.b64encode(content).decode("ascii")
        files.append(entry)
    files.sort(key=lambda item: item["path"])
    payload = {
        "schema_id": REWORK_DELTA_ARTIFACT_SCHEMA_ID,
        "authority_repo": str(authority_repo.resolve(strict=False)),
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
        "files": files,
    }
    packet = {**payload, "canonical_digest": _rework_delta_canonical_digest(payload)}
    encoded = json.dumps(packet, indent=2, ensure_ascii=True).encode("utf-8")
    artifact_digest = hashlib.sha256(encoded).hexdigest()
    artifact_dir = artifact_dir.resolve(strict=False)
    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    chmod_path(artifact_dir, 0o700)
    artifact_path = artifact_dir / f"{artifact_digest}.json"
    fd, temp_name = tempfile.mkstemp(
        dir=artifact_dir, prefix=".rework-delta-", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        chmod_path(temp_path, 0o600)
        atomic_replace(temp_path, artifact_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return {"path": str(artifact_path), "digest": artifact_digest}


def has_verified_rework_delta(
    predecessor: Any,
    *,
    authority_repo: Path,
) -> bool:
    """Return whether a predecessor has a durable, identity-bound delta.

    This is the retention fence shared by process GC and the storage planner.
    A worktree may be released only after the exact descriptor projected by
    ``reject_review`` still names an intact content-addressed artifact beneath
    this repository's runtime root.  Any malformed, missing, moved, symlinked,
    oversized, or tampered artifact fails closed and keeps the worktree pinned.
    """
    if not isinstance(predecessor, dict):
        return False
    descriptor = predecessor.get("rework_delta")
    artifact = predecessor.get("delta_artifact")
    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != {
            "schema_id",
            "sealed",
            "authority_repo",
            "task_id",
            "request_id",
            "claim_epoch",
            "artifact_path",
            "artifact_sha256",
        }
        or not isinstance(artifact, dict)
        or set(artifact) != {"path", "digest"}
    ):
        return False
    authority_repo = authority_repo.resolve(strict=False)
    request_id = str(predecessor.get("request_id") or "").strip()
    task_id = str(predecessor.get("task_id") or "").strip()
    claim_epoch = predecessor.get("claim_epoch")
    digest = descriptor.get("artifact_sha256")
    raw_path = descriptor.get("artifact_path")
    if (
        descriptor.get("schema_id") != "aiworkhub.rework_delta_descriptor.v1"
        or descriptor.get("sealed") is not True
        or str(descriptor.get("authority_repo") or "") != str(authority_repo)
        or str(descriptor.get("request_id") or "") != request_id
        or str(descriptor.get("task_id") or "") != task_id
        or type(claim_epoch) is not int
        or claim_epoch < 1
        or descriptor.get("claim_epoch") != claim_epoch
        or not _REQUEST_ID_RE.fullmatch(request_id)
        or not task_id
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or artifact != {"path": raw_path, "digest": digest}
    ):
        return False
    artifact_path = Path(str(raw_path or ""))
    artifact_root = (configured_runtime_root(authority_repo) / "rework_deltas").resolve(
        strict=False
    )
    resolved = artifact_path.resolve(strict=False)
    if (
        resolved.parent != artifact_root
        or resolved.name != f"{digest}.json"
        or artifact_path.is_symlink()
        or not resolved.is_file()
    ):
        return False
    try:
        if resolved.stat().st_size > 64 * 1024 * 1024:
            return False
        return hashlib.sha256(resolved.read_bytes()).hexdigest() == digest
    except OSError:
        return False


def materialize_rework_delta_artifact(
    artifact: Any,
    authority_repo: Path,
    request_id: str,
    task_id: str,
    claim_epoch: int,
    worktree: Path,
    expected_path_hashes: dict[str, Any],
    allowed_writes: tuple[str, ...],
) -> list[str]:
    """Verify and materialize one sealed changed/deleted-file delta."""
    if not isinstance(artifact, dict):
        raise WorkspaceError("rework_delta_artifact_invalid")
    raw_path = str(artifact.get("path") or "")
    digest = str(artifact.get("digest") or "")
    if not raw_path or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise WorkspaceError("rework_delta_artifact_invalid")
    artifact_path = Path(raw_path)
    if (
        artifact_path.name != f"{digest}.json"
        or artifact_path.is_symlink()
        or not artifact_path.is_file()
    ):
        raise WorkspaceError("rework_delta_artifact_missing")
    encoded = artifact_path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise WorkspaceError("rework_delta_artifact_tampered")
    try:
        packet = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WorkspaceError("rework_delta_artifact_invalid") from exc
    if not isinstance(packet, dict):
        raise WorkspaceError("rework_delta_artifact_invalid")
    canonical_digest = str(packet.pop("canonical_digest", "") or "")
    if (
        packet.get("schema_id") != REWORK_DELTA_ARTIFACT_SCHEMA_ID
        or not re.fullmatch(r"[0-9a-f]{64}", canonical_digest)
        or _rework_delta_canonical_digest(packet) != canonical_digest
    ):
        raise WorkspaceError("rework_delta_artifact_tampered")
    if (
        str(packet.get("authority_repo")) != str(authority_repo.resolve(strict=False))
        or str(packet.get("request_id")) != request_id
        or str(packet.get("task_id")) != task_id
        or type(packet.get("claim_epoch")) is not int
        or packet.get("claim_epoch") != claim_epoch
    ):
        raise WorkspaceError("rework_delta_identity_mismatch")
    files = packet.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_REWORK_OVERLAY_FILES:
        raise WorkspaceError("rework_delta_artifact_incomplete")
    expected = {
        _relative_repo_path(str(key)): value
        for key, value in expected_path_hashes.items()
    }
    planned: list[tuple[str, bytes | None]] = []
    seen_paths: set[str] = set()
    total_content_bytes = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise WorkspaceError("rework_delta_artifact_invalid")
        relative = _rework_delta_normalize_path(entry.get("path"))
        if relative in seen_paths:
            raise WorkspaceError(f"rework_delta_duplicate_path:{relative}")
        seen_paths.add(relative)
        if relative not in expected:
            raise WorkspaceError(f"rework_delta_unexpected_path:{relative}")
        raw_expected = expected[relative]
        if raw_expected is not None and (
            not isinstance(raw_expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw_expected)
        ):
            raise WorkspaceError(f"rework_predecessor_hash_invalid:{relative}")
        if not _matches(relative, allowed_writes):
            raise WorkspaceError(f"rework_predecessor_outside_scope:{relative}")
        _require_beneath(worktree, worktree / relative)
        if entry.get("deleted") is True:
            if (
                raw_expected is not None
                or entry.get("sha256") is not None
                or entry.get("content_base64") is not None
            ):
                raise WorkspaceError(f"rework_delta_delete_conflict:{relative}")
            planned.append((relative, None))
            continue
        file_sha = str(entry.get("sha256") or "")
        content_base64 = entry.get("content_base64")
        if not re.fullmatch(r"[0-9a-f]{64}", file_sha) or not isinstance(
            content_base64, str
        ):
            raise WorkspaceError(f"rework_delta_artifact_incomplete:{relative}")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise WorkspaceError(f"rework_delta_artifact_invalid:{relative}") from exc
        if hashlib.sha256(content).hexdigest() != file_sha:
            raise WorkspaceError(f"rework_delta_content_hash_mismatch:{relative}")
        if file_sha != raw_expected:
            raise WorkspaceError(f"rework_predecessor_hash_mismatch:{relative}")
        total_content_bytes += len(content)
        if total_content_bytes > MAX_REWORK_OVERLAY_CONTENT_BYTES:
            raise WorkspaceError("rework_delta_content_exceeds_limit")
        planned.append((relative, content))
    if seen_paths != set(expected):
        raise WorkspaceError("rework_delta_artifact_incomplete")
    seeded: list[str] = []
    for relative, content in planned:
        destination = worktree / relative
        if content is None:
            if destination.is_symlink() or destination.is_file():
                destination.unlink()
            elif destination.exists():
                raise WorkspaceError(f"rework_predecessor_delete_non_file:{relative}")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        seeded.append(relative)
    return sorted(seeded)


def _json_pointer_parts(pointer: str) -> tuple[str, ...]:
    if not pointer.startswith("/") or len(pointer) > 1000:
        raise WorkspaceError(f"residual_pointer_invalid:{pointer[:120]}")
    return tuple(
        part.replace("~1", "/").replace("~0", "~")
        for part in pointer[1:].split("/")
    )


def _mask_json_pointer(document: Any, pointer: str) -> None:
    parts = _json_pointer_parts(pointer)
    current = document
    for part in parts[:-1]:
        if isinstance(current, list):
            if not part.isdigit() or int(part) >= len(current):
                raise WorkspaceError(f"residual_pointer_missing:{pointer}")
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise WorkspaceError(f"residual_pointer_missing:{pointer}")
    leaf = parts[-1]
    sentinel = {"__aiworkhub_residual__": pointer}
    if isinstance(current, list):
        if not leaf.isdigit() or int(leaf) >= len(current):
            raise WorkspaceError(f"residual_pointer_missing:{pointer}")
        current[int(leaf)] = sentinel
    elif isinstance(current, dict) and leaf in current:
        current[leaf] = sentinel
    else:
        raise WorkspaceError(f"residual_pointer_missing:{pointer}")


def _masked_json_hash(path: Path, pointers: list[str]) -> str:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceError(f"residual_artifact_missing:{path.name}")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise WorkspaceError(f"residual_artifact_too_large:{path.name}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"residual_artifact_invalid_json:{path.name}") from exc
    masked = copy.deepcopy(document)
    decoded = [(pointer, _json_pointer_parts(pointer)) for pointer in pointers]
    for index, (pointer, parts) in enumerate(decoded):
        for other, other_parts in decoded[index + 1:]:
            shorter, longer = (parts, other_parts) if len(parts) <= len(other_parts) else (other_parts, parts)
            if longer[:len(shorter)] == shorter:
                raise WorkspaceError(
                    f"residual_pointer_overlap:{pointer}:{other}"[:300]
                )
    for pointer in sorted(pointers):
        _mask_json_pointer(masked, pointer)
    canonical = json.dumps(
        masked, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _bounded_residual_file_hash(path: Path) -> str:
    """Hash one non-JSON residual artifact without claiming pointer precision."""

    if path.is_symlink() or not path.is_file():
        raise WorkspaceError(f"residual_artifact_missing:{path.name}")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise WorkspaceError(f"residual_artifact_too_large:{path.name}")
    value = _hash_path(path)
    if not isinstance(value, str) or not value.startswith("file:"):
        raise WorkspaceError(f"residual_artifact_invalid:{path.name}")
    return value


def build_residual_contract_manifest(
    workspace: WorkerWorkspace, card: dict[str, Any]
) -> list[dict[str, Any]]:
    """Snapshot non-residual JSON content after predecessor materialization."""
    predecessor = card.get("rework_predecessor")
    if not isinstance(predecessor, dict):
        return []
    identities = predecessor.get("residual_identities")
    if identities in (None, []):
        return []
    if not isinstance(identities, list) or len(identities) > 256:
        raise WorkspaceError("invalid_residual_identities")
    grouped: dict[str, list[str]] = {}
    for row in identities:
        if not isinstance(row, dict):
            raise WorkspaceError("invalid_residual_identities")
        relative = _relative_repo_path(str(row.get("path") or ""))
        pointer = str(row.get("pointer") or "").strip()
        if not _matches(relative, workspace.allowed_writes):
            raise WorkspaceError(f"residual_artifact_outside_scope:{relative}")
        grouped.setdefault(relative, []).append(pointer)
    manifest: list[dict[str, Any]] = []
    for relative, pointers in sorted(grouped.items()):
        unique = sorted(set(pointers))
        path = workspace.path / relative
        if path.suffix.lower() == ".json":
            manifest.append({
                "path": relative,
                "pointers": unique,
                "scope": "json_pointer",
                "non_residual_sha256": _masked_json_hash(path, unique),
            })
        else:
            manifest.append({
                "path": relative,
                "pointers": unique,
                "scope": "whole_file",
                "predecessor_file_hash": _bounded_residual_file_hash(path),
            })
    return manifest


def validate_residual_contract(
    workspace: WorkerWorkspace, manifest: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Fail when a rework changes content outside declared JSON pointers."""
    results: list[dict[str, Any]] = []
    for row in manifest:
        relative = _relative_repo_path(str(row.get("path") or ""))
        pointers = [str(value) for value in row.get("pointers") or []]
        scope = str(row.get("scope") or "json_pointer")
        if scope == "whole_file":
            expected_file_hash = str(row.get("predecessor_file_hash") or "")
            if not re.fullmatch(r"file:[0-7]{3,4}:[0-9a-f]{64}", expected_file_hash):
                raise WorkspaceError(
                    f"residual_contract_file_hash_invalid:{relative}"
                )
            observed_file_hash = _bounded_residual_file_hash(
                workspace.path / relative
            )
            if observed_file_hash == expected_file_hash:
                raise WorkspaceError(f"residual_contract_file_unchanged:{relative}")
            results.append({
                "path": relative,
                "pointers": pointers,
                "scope": scope,
                "predecessor_file_hash": expected_file_hash,
                "observed_file_hash": observed_file_hash,
                "pass": True,
            })
            continue
        if scope != "json_pointer":
            raise WorkspaceError(f"residual_contract_scope_invalid:{relative}")
        expected = str(row.get("non_residual_sha256") or "")
        observed = _masked_json_hash(workspace.path / relative, pointers)
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or observed != expected:
            raise WorkspaceError(f"residual_contract_non_residual_changed:{relative}")
        results.append({
            "path": relative,
            "pointers": pointers,
            "scope": scope,
            "non_residual_sha256": expected,
            "pass": True,
        })
    return results


def _credential_home(home: Path, adapter_id: str, project_root: Path | None = None) -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    chmod_path(home, 0o700)
    temp_home = home / "tmp"
    temp_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    chmod_path(temp_home, 0o700)
    source_home = Path.home()
    if adapter_id == "claude_cli":
        refresh_claude_credential_projection(home)
        # Claude Code ignores the repository's permissions allowlist until the
        # project trust dialog has been accepted. Isolated workers have no TTY,
        # so a fresh minimal HOME would otherwise wait forever before the first
        # tool call. Seed only the trust bit for this exact parent project; do
        # not copy the user's general Claude configuration or MCP credentials.
        if project_root is not None:
            trust_config = home / ".claude.json"
            trust_config.write_text(
                json.dumps(
                    {
                        "projects": {
                            str(project_root.resolve()): {
                                "hasTrustDialogAccepted": True,
                                "projectOnboardingSeenCount": 1,
                            }
                        }
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            chmod_path(trust_config, 0o600)
    elif adapter_id == "codex_cli":
        source = source_home / ".codex" / "auth.json"
        if source.is_file():
            destination = home / ".codex" / "auth.json"
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, destination)
            chmod_path(destination, 0o600)


def _copy_regular_file_atomic(source: Path, destination: Path) -> None:
    source_info = source.lstat()
    if not stat.S_ISREG(source_info.st_mode):
        raise WorkspaceError("claude_credential_source_not_regular")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        chmod_path(destination.parent, 0o700)
    except OSError:
        pass
    if destination.parent.is_symlink() or destination.is_symlink():
        raise WorkspaceError("claude_credential_destination_symlink_forbidden")
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        read_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            write_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(read_fd, "rb") as source_stream:
                    read_fd = -1
                    with os.fdopen(write_fd, "wb") as destination_stream:
                        write_fd = -1
                        shutil.copyfileobj(source_stream, destination_stream)
                        destination_stream.flush()
                        os.fsync(destination_stream.fileno())
                atomic_replace(temporary, destination)
                try:
                    chmod_path(destination, 0o600)
                except OSError:
                    pass
            finally:
                if write_fd >= 0:
                    os.close(write_fd)
        finally:
            if read_fd >= 0:
                os.close(read_fd)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise WorkspaceError(f"claude_credential_projection_failed:{type(exc).__name__}") from exc


def refresh_claude_credential_projection(home: Path) -> dict[str, Any]:
    """Refresh only Claude's narrow request-local credential projection."""

    selected_home = _verify_owner_private_directory(home, "claude_projection_home")
    source_root_path = Path.home() / ".claude"
    if source_root_path.exists() or source_root_path.is_symlink():
        source_root = _verify_owner_private_directory(
            source_root_path, "claude_credential_source_home"
        )
    else:
        source_root = source_root_path
    source = source_root / ".credentials.json"
    destination = selected_home / ".claude" / ".credentials.json"
    if source.is_symlink():
        raise WorkspaceError("claude_credential_source_symlink_forbidden")
    if not source.is_file():
        try:
            destination.unlink(missing_ok=True)
        except OSError as exc:
            raise WorkspaceError(
                f"claude_credential_projection_remove_failed:{type(exc).__name__}"
            ) from exc
        return {"refreshed": False, "source_present": False}
    _copy_regular_file_atomic(source, destination)
    return {
        "refreshed": True,
        "source_present": True,
        "destination_bytes": destination.stat().st_size,
        "destination_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
    }


def _load_repo_taskdb_module(repo: Path) -> Any:
    """Dynamically load ``<repo>/AITools/taskdb.py`` as a private module.

    Loaded by explicit file path (not ``sys.path`` + ``import taskdb``) so
    this always reads the exact ``repo`` passed in -- the parent repo for a
    real launch, or a fixture repo in tests -- and never collides with any
    ``taskdb`` module already present in ``sys.modules``. Not registered in
    ``sys.modules``: ``taskdb.py`` is self-contained (stdlib only, no
    relative imports), so nothing else needs to resolve it by name.
    """
    module_path = (repo / "AITools" / "taskdb.py").resolve()
    spec = importlib.util.spec_from_file_location(
        f"_aiworkhub_taskdb_ro_{uuid.uuid4().hex}", module_path
    )
    if spec is None or spec.loader is None:
        raise WorkspaceError(f"taskdb_module_unloadable:{module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _placeholder_queue_card() -> dict[str, Any]:
    return {
        "task_id": _ISOLATED_QUEUE_PLACEHOLDER_TASK_ID,
        "runner": "NONE_isolated_placeholder",
        "topic": "task_mcp",
        "mode": "review_only",
        "status": "finished",
        "worker_status": "done",
        "priority": "low",
        "objective": "Synthetic placeholder row so an isolated worktree's "
        "disposable task-queue DB copy is never empty (B328). Not a real "
        "task; never claimed, never routed.",
        "allowed_writes": ["eval/_b328_isolated_queue_placeholder_never_written.json"],
        "forbidden": ["placeholder_task_never_claimable"],
    }


def provision_isolated_task_queue_db(repo: Path, home: Path) -> Path:
    """Best-effort pre-seed a disposable task-queue DB copy under ``home``.

    Read-only against the parent's live queue (never writes to it). Runs on
    the coordinator/host side only -- ``create_workspace`` calls this before
    the sandboxed process is ever spawned, so the isolated copy already
    exists (non-empty) by the time bubblewrap/Landlock start the worker or a
    later validation command runs ``AITools/taskctl.py`` inside the sandbox.
    Never raises: any failure degrades to an empty-schema DB seeded with one
    synthetic placeholder card, which still guarantees ``task_count() > 0``
    so ``taskctl._ensure_db_seeded()``'s from-empty seed-and-export branch
    (the second B313 failure mode) never fires inside the sandbox.
    """
    destination = (home / TASK_QUEUE_ISOLATED_RELATIVE).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    chmod_path(destination.parent, 0o700)
    cards: list[dict[str, Any]] = []
    taskdb: Any = None
    try:
        taskdb = _load_repo_taskdb_module(repo)
    except Exception:
        taskdb = None
    if taskdb is not None:
        try:
            parent_conn = taskdb.open_db(taskdb.DEFAULT_DB)
            try:
                taskdb.init_db(parent_conn)
                cards = taskdb.load_cards(parent_conn)
            finally:
                parent_conn.close()
        except Exception:
            cards = []
        if not cards:
            try:
                fallback = repo / "bitnnv2" / "data" / "tasking" / "machine_task_cards_v1.jsonl"
                cards = taskdb.read_jsonl(fallback)
            except Exception:
                cards = []
    if not cards:
        cards = [_placeholder_queue_card()]
    try:
        if taskdb is None:
            taskdb = _load_repo_taskdb_module(repo)
        iso_conn = taskdb.open_db(destination)
        try:
            taskdb.init_db(iso_conn)
            if taskdb.task_count(iso_conn) == 0:
                taskdb.import_cards(iso_conn, cards, preserve_lifecycle=True)
        finally:
            iso_conn.close()
    except Exception:
        # Absolute last resort: a bare, empty, schema-only sqlite file. Still
        # strictly better than the pre-B328 crash (path never existed / was
        # unwritable) -- taskctl.py verify will legitimately report an
        # empty-DB FAIL instead of raising an uncaught traceback, and never
        # touches the worktree's tracked queue files to get there.
        try:
            fd_conn = __import__("sqlite3").connect(destination)
            fd_conn.close()
        except Exception:
            pass
    return destination


def provision_worker_mcp_runtime(
    workspace: "WorkerWorkspace",
    *,
    request_id: str,
    task_id: str,
    runner: str,
    topic: str,
    backend: str,
    source_graph_targets: list[str] | tuple[str, ...],
    session_topic: str,
    allowed_writes: list[str] | tuple[str, ...] = (),
    quality_review_packet_path: Path | None = None,
    rework_overlay_path: Path | None = None,
) -> Any:
    """B834: provision this request's isolated worker MCP config + audit ledger.

    Thin, additive wrapper around ``worker_ai_tools_mcp.generate_worker_mcp_runtime``
    so ``process_launcher.py`` can provision the per-request MCP surface from
    the same import block it already uses for workspace helpers. Runs on the
    HOST side, before the sandboxed adapter process starts, so it may write
    freely under ``workspace.home``.

    Derives BOTH repository bindings from ``workspace`` itself (the caller no
    longer passes a bare ``repo`` value) and rewrites them for the sandbox
    backend that will actually run the worker: under bubblewrap only the
    bound sandbox aliases (``SANDBOX_WORKSPACE``, ``SANDBOX_AUTHORITY_REPO``)
    are visible inside the mount namespace, so embedding the real host paths
    there would silently reference something the sandboxed process cannot
    read (see ``sandbox_argv``'s bubblewrap branch and
    ``resolve_validation_pythonpath`` for the same backend-aware pattern).
    Landlock confines writes only, never reads, so the real host paths are
    used directly there.

    B870 V2: the AIWorkHub package's own import root is resolved separately
    from ``repo``/``authority_repo`` via
    ``worker_ai_tools_mcp.resolve_host_package_import_root()`` -- never as an
    offset beneath either of them -- and, only under bubblewrap, rewritten to
    the dedicated ``SANDBOX_PACKAGE_IMPORT_ROOT`` alias. ``sandbox_argv`` (see
    below) binds that same real host directory read-only at that exact alias
    in the SAME mount namespace this request's adapter process launches
    under, so the two stay in lockstep without either function guessing the
    other's repository-layout assumptions.
    """

    if backend not in ("landlock", "bubblewrap", VSCODE_LM_IN_PROCESS_BACKEND):
        raise WorkspaceError(f"unsupported_sandbox_backend:{backend}")
    if not workspace.repo.is_dir():
        raise WorkspaceError(f"authority_repo_not_directory:{workspace.repo}")
    if backend == "bubblewrap":
        # Bubblewrap-allocated sandbox aliases are POSIX paths that exist only
        # inside the sandbox's mount namespace. Use PurePosixPath so their
        # string representation stays POSIX-shaped (e.g. "/aiworkhub-package-root")
        # even on Windows, where Path("/") would otherwise normalize to a
        # drive-relative form ("\\aiworkhub-package-root"). These values are
        # emitted verbatim into the worker MCP config/env and are never used
        # to touch the host filesystem directly.
        worker_repo = PurePosixPath(SANDBOX_WORKSPACE)
        authority_repo = PurePosixPath(SANDBOX_AUTHORITY_REPO)
    else:
        worker_repo = workspace.path
        authority_repo = workspace.repo

    from . import worker_ai_tools_mcp

    host_package_import_root = worker_ai_tools_mcp.resolve_host_package_import_root()
    if not host_package_import_root.is_dir():
        raise WorkspaceError(
            f"package_import_root_not_directory:{host_package_import_root}"
        )
    package_import_root = (
        PurePosixPath(SANDBOX_PACKAGE_IMPORT_ROOT) if backend == "bubblewrap" else host_package_import_root
    )

    worker_review_packet_path: Path | None = None
    if quality_review_packet_path is not None:
        host_packet = quality_review_packet_path.resolve()
        _require_beneath(workspace.home, host_packet)
        relative_packet = host_packet.relative_to(workspace.home)
        worker_review_packet_path = (
            PurePosixPath(bubblewrap_home_env_value()) / PurePosixPath(*relative_packet.parts)
            if backend == "bubblewrap"
            else host_packet
        )

    worker_rework_overlay_path: Path | None = None
    if rework_overlay_path is not None:
        host_overlay = rework_overlay_path.resolve()
        _require_beneath(workspace.home, host_overlay)
        if host_overlay.is_symlink() or not host_overlay.is_file():
            raise WorkspaceError("rework_overlay_packet_invalid")
        relative_overlay = host_overlay.relative_to(workspace.home)
        worker_rework_overlay_path = (
            PurePosixPath(bubblewrap_home_env_value())
            / PurePosixPath(*relative_overlay.parts)
            if backend == "bubblewrap"
            else host_overlay
        )

    try:
        return worker_ai_tools_mcp.generate_worker_mcp_runtime(
            home=workspace.home,
            request_id=request_id,
            task_id=task_id,
            runner=runner,
            topic=topic,
            repo=worker_repo,
            authority_repo=authority_repo,
            source_graph_targets=source_graph_targets,
            allowed_writes=allowed_writes,
            session_topic=session_topic,
            package_import_root=package_import_root,
            quality_review_packet_path=worker_review_packet_path,
            rework_overlay_path=worker_rework_overlay_path,
        )
    except worker_ai_tools_mcp.WorkerToolError as exc:
        # Provisioning/config-injection failure must reject the launch, not
        # silently degrade to a worker without a working tool surface --
        # WorkspaceError is already in process_launcher's caught-and-rejected
        # exception tuple, WorkerToolError is not.
        raise WorkspaceError(f"worker_mcp_runtime_provisioning_failed:{exc}") from exc


def configured_runtime_root(repo: Path | None = None) -> Path:
    """Return the runtime root without creating it.

    An explicit runtime root may live on a system/ephemeral volume.  With an
    exact repository, the default is the repository-owned, git-ignored
    ``.aiworkhub/runtime`` boundary.  Callers without repository identity keep
    the historical system-temp fallback rather than guessing authority.
    """
    override = os.environ.get(RUNTIME_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    selected_repo = Path(repo).resolve() if repo is not None else None
    if selected_repo is None:
        env_repo = (
            os.environ.get("AIWORKHUB_REPO_ROOT", "").strip()
            or os.environ.get("AIWORKHUB_REPO", "").strip()
        )
        if env_repo:
            candidate = Path(env_repo).expanduser().resolve()
            if (candidate / ".aiworkhub" / "project.json").is_file():
                selected_repo = candidate
        else:
            candidate = Path.cwd().resolve()
            if (candidate / ".aiworkhub" / "project.json").is_file():
                selected_repo = candidate
    if selected_repo is not None:
        hub = selected_repo / ".aiworkhub"
        runtime = hub / "runtime"
        if hub.is_symlink() or runtime.is_symlink():
            raise WorkspaceError("repo_runtime_symlink_forbidden")
        return runtime.resolve(strict=False)
    return (Path(tempfile.gettempdir()) / "aiworkhub-runtime").resolve()


def configured_worktree_root(repo: Path | None = None) -> Path:
    """Return the single configured root for isolated worker workspaces."""
    authorized_scratch = _nested_landlock_exec_scratch_for_repo(repo)
    override = os.environ.get(WORKTREE_ROOT_ENV, "").strip()
    if override:
        resolved = Path(override).expanduser().resolve()
        if authorized_scratch is None or _path_is_relative_to(
            resolved, authorized_scratch
        ):
            return resolved
        return (authorized_scratch / "nested-worktrees").resolve()
    if repo is None and not os.environ.get(RUNTIME_ROOT_ENV, "").strip():
        env_repo = (
            os.environ.get("AIWORKHUB_REPO_ROOT", "").strip()
            or os.environ.get("AIWORKHUB_REPO", "").strip()
        )
        if not env_repo and not (Path.cwd() / ".aiworkhub" / "project.json").is_file():
            fallback = (Path(tempfile.gettempdir()) / "aiworkhub-worktrees").resolve()
            if authorized_scratch is None or _path_is_relative_to(
                fallback, authorized_scratch
            ):
                return fallback
            return (authorized_scratch / "nested-worktrees").resolve()
    default = (configured_runtime_root(repo) / "worktrees").resolve()
    if authorized_scratch is None or _path_is_relative_to(default, authorized_scratch):
        return default
    return (authorized_scratch / "nested-worktrees").resolve()


def configured_temp_root(repo: Path | None = None) -> Path:
    """Return the repository-owned disposable temp root (``.aiworkhub/temp``).

    Validation exec scratch and per-request worker tmp live under this
    authority.  Never a shared system temp location; symlink/escape fails
    closed via :func:`aiworkhub.runtime_temp.temp_root`.
    """
    return runtime_temp.temp_root(repo)


# ── Request-owned worker temp authority (NF430) ────────────────────────────
# Worker-run pytest/tempfile artifacts must live in the exact request-owned
# repository-local ``.aiworkhub/temp/worker/<request_id>`` authority -- outside
# the candidate worktree -- so they never surface as out-of-scope changed_paths
# and never collide across concurrent requests.  The runtime_temp module is the
# single owner of the on-disk layout, the PID/start-time owner manifests, and
# the dead-owner GC; these thin helpers give the launcher and the Landlock
# planner one vocabulary for that ``worker`` namespace without duplicating any
# of that policy here.
WORKER_TEMP_NAMESPACE = runtime_temp.WORKER_NAMESPACE


def worker_temp_root(repo: Path, request_id: str) -> Path:
    """Return ``<repo>/.aiworkhub/temp/worker/<request_id>`` (never provisioned).

    Pure path resolution through the runtime_temp authority: it validates the
    request identity and fails closed on symlink/escape without touching the
    filesystem, so a Landlock/sandbox planner can ask "does this request own a
    temp root yet?" without creating one as a side effect.
    """
    return runtime_temp.request_dir(
        Path(repo).resolve(), request_id, runtime_temp.WORKER_NAMESPACE
    )


def provision_worker_temp(repo: Path, request_id: str) -> "runtime_temp.RequestTemp":
    """Create (or reuse) the 0700, owner-stamped worker temp layout for a request.

    Delegates to the runtime_temp authority so the ``tmp`` subdirectory, the
    PID/start-time owner manifest (consumed by the sole dead-owner GC), and the
    per-repo quota accounting all stay single-sourced.  Collision-free by
    construction: the layout is namespaced by request identity.
    """
    return runtime_temp.provision_request_temp(
        Path(repo).resolve(), request_id, namespace=runtime_temp.WORKER_NAMESPACE
    )


def worker_temp_environment(
    repo: Path,
    request_id: str,
    *,
    provision: bool = True,
) -> dict[str, str]:
    """Return the ``TMPDIR``/``TMP``/``TEMP`` mapping for a request's worker temp.

    Every value points at ``<repo>/.aiworkhub/temp/worker/<request_id>/tmp``.
    With ``provision`` true (the real-launch default) the owner-stamped 0700
    layout is created first so the child can write immediately; the candidate
    worktree is never widened because this root lives outside it.  The three
    keys cover POSIX, macOS and Windows temp semantics with one shell-free
    mapping and mirror ``runtime_adapters.WORKER_TEMP_ENV_VARS``.
    """
    if provision:
        tmp = provision_worker_temp(repo, request_id).tmp
    else:
        tmp = worker_temp_root(repo, request_id) / runtime_temp.TMP_SUBDIR
    value = str(tmp)
    return {"TMPDIR": value, "TMP": value, "TEMP": value}


def dispose_worker_temp(repo: Path, request_id: str) -> bool:
    """Best-effort immediate removal of a request's worker temp authority.

    Fail-closed and never raises for an absent/foreign/pinned root: returns
    True only when the exact request-owned directory was removed.  Stale/
    dead-owner and review/rework-pinned lifecycles remain the responsibility of
    the sole runtime_temp GC authority; this is the accepted/discarded/cleanup
    disposition path.
    """
    try:
        root = worker_temp_root(repo, request_id)
    except runtime_temp.RuntimeTempError:
        return False
    try:
        return runtime_temp.dispose_request_temp(
            root, repo=Path(repo).resolve(), expected_request_id=request_id
        )
    except (runtime_temp.RuntimeTempError, OSError):
        return False


def _outer_validation_authority_mac(
    payload: Mapping[str, str],
    *,
    identity: os.stat_result,
) -> str:
    material = "\n".join(
        f"{key}={payload[key]}" for key in sorted(payload)
    ).encode("utf-8")
    material += f"\ndev={identity.st_dev}\nino={identity.st_ino}".encode("utf-8")
    return hmac.new(_OUTER_VALIDATION_HMAC_KEY, material, hashlib.sha256).hexdigest()


def _coordinator_owned_regular_file(path: Path) -> os.stat_result | None:
    try:
        status = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        return None
    if posix_path_modes_supported():
        if status.st_uid != os.geteuid():
            return None
        if stat.S_IMODE(status.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
            return None
    return status


def _write_authenticated_document(
    path: Path, payload: dict[str, str]
) -> os.stat_result:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise WorkspaceError("outer_validation_authority_symlink")
    if path.exists():
        path.unlink()
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        identity = os.fstat(fd)
        document = {
            **payload,
            "mac": _outer_validation_authority_mac(payload, identity=identity),
        }
        os.write(
            fd,
            json.dumps(document, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            ),
        )
        try:
            chmod_fd(fd, 0o444)
        except PermissionError:
            pass
    finally:
        os.close(fd)
    return identity


def _directory_write_denied_by_landlock(directory: Path) -> bool:
    try:
        status = directory.stat()
    except OSError:
        return False
    if not stat.S_ISDIR(status.st_mode):
        return False
    if status.st_uid != os.geteuid():
        return False
    if stat.S_IMODE(status.st_mode) & 0o200 == 0:
        return False
    probe = directory / f".aiworkhub_outer_validation_probe_{os.getpid()}"
    try:
        fd = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except PermissionError:
        return True
    except OSError:
        return False
    os.close(fd)
    try:
        os.unlink(probe)
    except OSError:
        pass
    return False


def plant_outer_validation_authority(
    workspace: Path,
    *,
    exec_scratch: Path | None = None,
) -> Path:
    """Write the coordinator-only outer validation authority file."""
    workspace = Path(workspace).resolve()
    path = workspace / OUTER_VALIDATION_AUTHORITY_RELATIVE
    payload = {
        "schema_id": OUTER_VALIDATION_AUTHORITY_SCHEMA,
        "kind": _OUTER_VALIDATION_AUTHORITY_KIND,
        "workspace": str(workspace),
    }
    if exec_scratch is None:
        _write_authenticated_document(path, payload)
        return path
    scratch = Path(exec_scratch).resolve()
    payload["exec_scratch"] = str(scratch)
    locator_path = scratch / NESTED_LANDLOCK_AUTHORITY_LOCATOR_RELATIVE
    anchor_path = workspace / NESTED_LANDLOCK_AUTHORITY_LOCATOR_ANCHOR_RELATIVE
    locator_path.parent.mkdir(parents=True, exist_ok=True)
    anchor_path.parent.mkdir(parents=True, exist_ok=True)
    if locator_path.is_symlink():
        raise WorkspaceError("nested_landlock_locator_symlink")
    if anchor_path.is_symlink():
        raise WorkspaceError("nested_landlock_locator_anchor_symlink")
    if locator_path.exists():
        locator_path.unlink()
    if anchor_path.exists():
        anchor_path.unlink()
    locator_fd = os.open(
        str(locator_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
    )
    try:
        locator_identity = os.fstat(locator_fd)
        payload["locator_dev"] = str(locator_identity.st_dev)
        payload["locator_ino"] = str(locator_identity.st_ino)
        payload["locator_anchor"] = str(anchor_path.resolve())
        locator_payload = {
            "schema_id": NESTED_LANDLOCK_AUTHORITY_LOCATOR_SCHEMA,
            "kind": _NESTED_LANDLOCK_AUTHORITY_LOCATOR_KIND,
            "authority": str(path.resolve()),
            "exec_scratch": str(scratch),
            "workspace": str(workspace),
        }
        document = {
            **locator_payload,
            "mac": _outer_validation_authority_mac(
                locator_payload, identity=locator_identity
            ),
        }
        os.write(
            locator_fd,
            json.dumps(document, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            ),
        )
        try:
            chmod_fd(locator_fd, 0o444)
        except PermissionError:
            pass
        try:
            os.link(str(locator_path), str(anchor_path))
        except OSError as exc:
            raise WorkspaceError("nested_landlock_locator_anchor_link") from exc
        _write_authenticated_document(path, payload)
    finally:
        os.close(locator_fd)
    return path


def verify_outer_validation_authority_file(path: Path) -> dict[str, str] | None:
    """Return the verified payload or None. Env and prose cannot mint this."""
    status = _coordinator_owned_regular_file(path)
    if status is None or status.st_nlink != 1:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    schema_id = str(raw.get("schema_id") or "")
    kind = str(raw.get("kind") or "")
    workspace = str(raw.get("workspace") or "")
    exec_scratch = str(raw.get("exec_scratch") or "")
    locator_dev = str(raw.get("locator_dev") or "")
    locator_ino = str(raw.get("locator_ino") or "")
    locator_anchor = str(raw.get("locator_anchor") or "")
    mac = str(raw.get("mac") or "")
    if (
        schema_id != OUTER_VALIDATION_AUTHORITY_SCHEMA
        or kind != _OUTER_VALIDATION_AUTHORITY_KIND
        or not workspace
        or not re.fullmatch(r"[0-9a-f]{64}", mac)
    ):
        return None
    payload = {
        "schema_id": schema_id,
        "kind": kind,
        "workspace": workspace,
    }
    if exec_scratch:
        payload["exec_scratch"] = exec_scratch
    if locator_dev:
        payload["locator_dev"] = locator_dev
    if locator_ino:
        payload["locator_ino"] = locator_ino
    if locator_anchor:
        payload["locator_anchor"] = locator_anchor
    expected = _outer_validation_authority_mac(payload, identity=status)
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        resolved_file = path.resolve()
        resolved_workspace = Path(workspace).resolve()
    except OSError:
        return None
    if not _path_is_relative_to(resolved_file, resolved_workspace):
        return None
    if not _directory_write_denied_by_landlock(resolved_file.parent):
        return None
    return payload


def verify_nested_landlock_authority_locator(
    path: Path,
) -> dict[str, str] | None:
    """Return the re-verified authority payload, or None."""
    status = _coordinator_owned_regular_file(path)
    if status is None or status.st_nlink != 2:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    schema_id = str(raw.get("schema_id") or "")
    kind = str(raw.get("kind") or "")
    authority = str(raw.get("authority") or "")
    exec_scratch = str(raw.get("exec_scratch") or "")
    workspace = str(raw.get("workspace") or "")
    mac = str(raw.get("mac") or "")
    if (
        schema_id != NESTED_LANDLOCK_AUTHORITY_LOCATOR_SCHEMA
        or kind != _NESTED_LANDLOCK_AUTHORITY_LOCATOR_KIND
        or not authority
        or not exec_scratch
        or not workspace
        or not re.fullmatch(r"[0-9a-f]{64}", mac)
    ):
        return None
    payload = {
        "schema_id": schema_id,
        "kind": kind,
        "authority": authority,
        "exec_scratch": exec_scratch,
        "workspace": workspace,
    }
    expected = _outer_validation_authority_mac(payload, identity=status)
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        resolved_locator = path.resolve()
        resolved_scratch = Path(exec_scratch).resolve()
        resolved_authority = Path(authority).resolve()
        resolved_workspace = Path(workspace).resolve()
    except OSError:
        return None
    if not _path_is_relative_to(resolved_locator, resolved_scratch):
        return None
    if not _path_is_relative_to(resolved_authority, resolved_workspace):
        return None
    verified = verify_outer_validation_authority_file(resolved_authority)
    if verified is None:
        return None
    if verified.get("exec_scratch") != str(resolved_scratch):
        return None
    if verified.get("workspace") != str(resolved_workspace):
        return None
    if verified.get("locator_dev") != str(status.st_dev):
        return None
    if verified.get("locator_ino") != str(status.st_ino):
        return None
    anchor_text = str(verified.get("locator_anchor") or "")
    if not anchor_text:
        return None
    try:
        resolved_anchor = Path(anchor_text).resolve()
        expected_anchor = (
            resolved_workspace / NESTED_LANDLOCK_AUTHORITY_LOCATOR_ANCHOR_RELATIVE
        ).resolve()
    except OSError:
        return None
    if resolved_anchor != expected_anchor:
        return None
    if not _path_is_relative_to(resolved_anchor, resolved_workspace):
        return None
    anchor_status = _coordinator_owned_regular_file(Path(anchor_text))
    if (
        anchor_status is None
        or anchor_status.st_nlink != 2
        or (anchor_status.st_dev, anchor_status.st_ino)
        != (status.st_dev, status.st_ino)
    ):
        return None
    return verified


def authenticated_outer_validation_context() -> dict[str, str] | None:
    """Return the coordinator outer-validation context when it is authentic.

    Candidate environment variables and output text cannot create this
    context. The reserved authority file must verify HMAC and live in a
    directory this process owns and that is mode-writable, yet Landlock
    still denies creating a sibling. Nested Landlock lookups use a bounded
    cwd-ancestor locator and re-verify that authority.
    """
    current = Path.cwd().resolve()
    seen: set[Path] = set()
    for _ in range(_NESTED_LANDLOCK_AUTHORITY_LOCATOR_MAX_ANCESTORS + 1):
        if current in seen:
            break
        seen.add(current)
        locator = current / NESTED_LANDLOCK_AUTHORITY_LOCATOR_RELATIVE
        located = verify_nested_landlock_authority_locator(locator)
        if located is not None:
            try:
                if current == Path(located["exec_scratch"]).resolve():
                    return located
            except OSError:
                pass
        candidate = current / OUTER_VALIDATION_AUTHORITY_RELATIVE
        verified = verify_outer_validation_authority_file(candidate)
        if verified is not None:
            try:
                if current == Path(verified["workspace"]).resolve():
                    return verified
            except OSError:
                pass
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _nested_landlock_exec_scratch_for_repo(repo: Path | None) -> Path | None:
    context = authenticated_outer_validation_context()
    if context is None or repo is None:
        return None
    scratch_text = str(context.get("exec_scratch") or "")
    workspace_text = str(context.get("workspace") or "")
    if not scratch_text or not workspace_text:
        return None
    try:
        scratch = Path(scratch_text).resolve()
        workspace = Path(workspace_text).resolve()
        resolved_repo = Path(repo).resolve()
    except OSError:
        return None
    if resolved_repo != workspace:
        return None
    return scratch


def nested_sandbox_requires_host_boundary() -> bool:
    """True only inside the authenticated coordinator validation sandbox."""
    return authenticated_outer_validation_context() is not None


def _legacy_worktree_root() -> Path:
    """Exact pre-repo-runtime root retained only for upgrade-time GC."""
    return (Path(tempfile.gettempdir()) / "aiworkhub-worktrees").resolve()


def create_workspace(
    repo: Path,
    request_id: str,
    card: dict[str, Any],
    adapter_id: str,
) -> WorkerWorkspace:
    repo = repo.resolve()
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise WorkspaceError("invalid_request_id")
    raw_allowed = card.get("allowed_writes")
    if raw_allowed is None:
        # Key absent -> genuinely under-specified (distinct from an intentional
        # readonly empty list, which a canary/no-output card may legitimately
        # declare -- no NO_WRITES sentinel required).
        raise WorkspaceError("allowed_writes_missing")
    allowed = tuple(_relative_repo_path(v) for v in raw_allowed)
    if not allowed and (card.get("required_outputs") or []):
        raise WorkspaceError("allowed_writes_empty")
    if any(PurePosixPath(pattern).parts[0] == ".git" for pattern in allowed):
        raise WorkspaceError("git_metadata_write_forbidden")
    root = configured_worktree_root(repo)
    canonical_repo_root = (repo / ".aiworkhub" / "runtime" / "worktrees").resolve(
        strict=False
    )
    if (root == repo or repo in root.parents) and root != canonical_repo_root:
        raise WorkspaceError(f"worktree_root_inside_parent_repo:{root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    chmod_path(root, 0o700)
    # NF-2026-00285: never seed while a promotion is mutating the parent tree.
    # This up-front check alone is check-then-act: a promotion whose in-flight
    # marker appears AFTER it but during the seed window below would be missed
    # (raised on review). The seed is re-checked once more after it completes
    # (see below), so a promotion overlapping the window is caught rather than
    # returned as a half-promoted, inconsistent tree.
    _refuse_if_promotion_in_flight(repo)
    path = root / request_id / "worktree"
    home = root / request_id / "home"
    if (path == repo or repo in path.parents) and root != canonical_repo_root:
        raise WorkspaceError("worker_path_is_parent_worktree")
    if path.exists() or home.exists():
        raise WorkspaceError(f"workspace_exists:{request_id}")
    path.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
    provisioning_started = time.monotonic()
    provisioning_timings_ms: dict[str, float] = {}

    declared = list(card.get("read_first") or []) + list(card.get("immutable_inputs") or []) + list(allowed)
    live_seeded = _expand_declared(repo, declared)
    live_seeded = _resolve_local_quoted_includes(
        repo,
        live_seeded,
        include_roots=_include_roots_from_card(card),
    )
    validation_rows = tuple(
        row
        for row in (card.get("validation") or [])
        if isinstance(row, str) and row.strip()
    )
    support_seeded: tuple[str, ...] = ()
    if validation_rows and any(
        relative.startswith("src/aiworkhub/") for relative in live_seeded
    ):
        support_seeded = tuple(
            relative
            for relative in _VALIDATION_WORKER_PACKAGE_SUPPORT
            if relative not in live_seeded
        )
    if validation_rows:
        test_files = _extract_pytest_test_files(repo, validation_rows)
        seeds_for_python_closure = (*live_seeded, *support_seeded, *test_files)
        python_seeded = _resolve_local_python_imports(
            repo, seeds_for_python_closure
        )
        support_seeded = tuple(
            sorted(set(support_seeded) | (set(python_seeded) - set(live_seeded)))
        )
        js_seeded = _resolve_local_js_requires(repo, (*live_seeded, *support_seeded))
        support_seeded = tuple(
            sorted(set(support_seeded) | (set(js_seeded) - set(live_seeded)))
        )
    npm_support_seeded = tuple(
        relative
        for relative in _npm_validation_support(repo, validation_rows)
        if relative not in live_seeded
    )
    support_seeded = tuple(sorted(set(support_seeded) | set(npm_support_seeded)))
    seeded = sorted(set(live_seeded) | set(support_seeded))
    # Git ignore rules participate in changed-path truth. Materialize the root
    # rule file and rules in ancestors of declared files without checking out
    # the rest of a potentially multi-gigabyte repository.
    ignore_candidates = {".gitignore"}
    for relative in seeded:
        parent = PurePosixPath(relative).parent
        while str(parent) not in {"", "."}:
            ignore_candidates.add((parent / ".gitignore").as_posix())
            parent = parent.parent
    ignore_seeded = {
            relative
            for relative in ignore_candidates
            if (repo / relative).is_file() and not (repo / relative).is_symlink()
        }
    live_seeded = sorted(set(live_seeded) | ignore_seeded)
    seeded = sorted(set(live_seeded) | set(support_seeded))

    try:
        phase_started = time.monotonic()
        result = _run(
            [
                "git",
                "worktree",
                "add",
                "--detach",
                "--no-checkout",
                str(path),
                "HEAD",
            ],
            cwd=repo,
            timeout=WORKTREE_CREATE_TIMEOUT_SECONDS,
            phase="workspace_provision",
        )
        provisioning_timings_ms["worktree_register"] = round(
            (time.monotonic() - phase_started) * 1000.0, 3
        )
        if result.returncode == 0:
            phase_started = time.monotonic()
            _prepare_sparse_worktree(
                path,
                seeded,
                allowed,
                timeout=WORKTREE_CREATE_TIMEOUT_SECONDS,
            )
            provisioning_timings_ms["sparse_checkout"] = round(
                (time.monotonic() - phase_started) * 1000.0, 3
            )
        provisioning_timings_ms["worktree_create"] = round(
            sum(
                provisioning_timings_ms.get(name, 0.0)
                for name in ("worktree_register", "sparse_checkout")
            ),
            3,
        )
    except Exception:
        # ``git worktree add`` can raise (for example a timeout that kills git
        # mid-checkout) after it has already written a partial checkout and a
        # ``.git/worktrees`` registration. Use the same exact, process-free
        # reciprocal cleanup path; a wedged Git process must not force another
        # Git process into the failure path.
        cleanup_workspace(repo, path, home)
        raise
    if result.returncode != 0:
        cleanup_workspace(repo, path, home)
        # Git reports the actionable cause (for example ENOSPC) at the end,
        # after a long checkout progress stream. Preserve that tail.
        raise WorkspaceError(f"git_worktree_add_failed:{result.stderr[-1000:]}")
    try:
        phase_started = time.monotonic()
        # Declared inputs and their imported support/dependency closure must
        # reflect ONE coherent current-canonical generation.  Overlaying the
        # support closure from the live parent tree (NF-2026-00423) prevents a
        # candidate whose allowed production file imports a current-canonical
        # dependency from validating against a stale detached-HEAD copy of that
        # dependency.  Only the exact resolved closure is copied -- never an
        # arbitrary dirty parent file -- and support paths stay outside
        # ``allowed_writes``, so their post-overlay bytes seed
        # ``workspace_baseline`` below and they remain read-only, never enter
        # the candidate delta, and can never be promoted.
        for relative in live_seeded:
            destination = path / relative
            _require_beneath(path, destination)
            _require_beneath(repo, repo / relative)
            _copy_one(repo / relative, destination)
        for relative in support_seeded:
            source = repo / relative
            support_path = path / relative
            _require_beneath(repo, source)
            _require_beneath(path, support_path)
            if source.is_symlink() or not source.is_file():
                raise WorkspaceError(
                    f"validation_worker_support_missing:{relative}"
                )
            _copy_one(source, support_path)
            if support_path.is_symlink() or not support_path.is_file():
                raise WorkspaceError(
                    f"validation_worker_support_missing:{relative}"
                )
        # Landlock can grant file-specific write rights without exposing the
        # worktree's .git pointer. Precreating exact new outputs makes those
        # file-specific rules possible; unchanged placeholders are filtered by
        # workspace_baseline below.
        for relative in allowed:
            if not any(ch in relative for ch in "*?["):
                _touch_placeholder(path, relative)
        rework_seeded = _materialize_rework_predecessor(
            repo, path, card, allowed
        )
        seeded = sorted(set(seeded) | set(rework_seeded))
        provisioning_timings_ms["declared_seed"] = round(
            (time.monotonic() - phase_started) * 1000.0, 3
        )
        phase_started = time.monotonic()
        baseline: dict[str, str | None] = {}
        for relative in _expand_declared(repo, allowed):
            baseline[relative] = _hash_path(repo / relative)
        workspace_baseline = {
            relative: _hash_path(path / relative)
            for relative in sorted(set(seeded) | set(_expand_declared(path, allowed)))
        }
        provisioning_timings_ms["declared_baseline"] = round(
            (time.monotonic() - phase_started) * 1000.0, 3
        )
        phase_started = time.monotonic()
        tree_baseline = _worktree_manifest(path)
        provisioning_timings_ms["tree_baseline"] = round(
            (time.monotonic() - phase_started) * 1000.0, 3
        )
        phase_started = time.monotonic()
        _credential_home(home, adapter_id, repo)
        provisioning_timings_ms["credential_home"] = round(
            (time.monotonic() - phase_started) * 1000.0, 3
        )
        phase_started = time.monotonic()
        provision_isolated_task_queue_db(repo, home)
        provisioning_timings_ms["task_queue"] = round(
            (time.monotonic() - phase_started) * 1000.0, 3
        )
        # NF-2026-00285 (check-then-act closure): re-check at the end of the seed
        # window. The declared inputs were just copied from the LIVE parent tree;
        # if a promotion began writing into that tree at any point during the
        # copy loop above and is still in flight now, this seed may have captured
        # a half-promoted mix, so refuse it (and clean up via the except below)
        # rather than hand back an inconsistent tree. ``promote`` holds its
        # marker across its entire parent-write loop, so an overlapping promotion
        # is still marked here.
        _refuse_if_promotion_in_flight(repo)
    except Exception:
        cleanup_workspace(repo, path, home)
        raise
    try:
        phase_started = time.monotonic()
        base_oid = _isolated_worktree_base_oid(repo, path)
        provisioning_timings_ms["base_oid"] = round(
            (time.monotonic() - phase_started) * 1000.0, 3
        )
    except WorkspaceError:
        cleanup_workspace(repo, path, home)
        raise
    return WorkerWorkspace(
        request_id=request_id,
        repo=repo,
        path=path,
        home=home,
        allowed_writes=allowed,
        parent_baseline=baseline,
        workspace_baseline=workspace_baseline,
        tree_baseline=tree_baseline,
        provisioning_timings_ms={
            **provisioning_timings_ms,
            "total": round((time.monotonic() - provisioning_started) * 1000.0, 3),
        },
        inherited_rework_paths=tuple(sorted(set(rework_seeded))),
        base_oid=base_oid,
    )


def _registered_worktree_admin_dir(repo: Path, path: Path) -> Path | None:
    """Return the exact reciprocal Git worktree registration, if present."""
    repo_marker = repo / ".git"
    if not repo_marker.exists() and not repo_marker.is_symlink():
        # Validation fixtures and already-detached retained workspaces may be
        # exact request-owned directories without any Git registration. There
        # is no administrative state to touch; directory cleanup remains safe.
        return None
    common = _common_git_dir(repo)
    admin_root = common / "worktrees"
    if admin_root.is_symlink():
        raise WorkspaceError("workspace_cleanup_admin_root_symlink")
    marker = path / ".git"
    expected_marker = marker.resolve(strict=False)
    candidates: list[Path] = []
    if marker.is_file() and not marker.is_symlink():
        candidates.append(_gitdir_pointer(marker, label="workspace_git_marker"))
    elif admin_root.is_dir():
        try:
            entries = list(os.scandir(admin_root))
        except OSError as exc:
            raise WorkspaceError("workspace_cleanup_admin_scan_failed") from exc
        if len(entries) > MAX_SEED_FILES:
            raise WorkspaceError("workspace_cleanup_admin_scan_limit_exceeded")
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            candidates.append(Path(entry.path))
    matches: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved.parent != admin_root.resolve(strict=False):
            if marker.is_file():
                raise WorkspaceError("workspace_cleanup_admin_escape")
            continue
        gitdir_file = resolved / "gitdir"
        if gitdir_file.is_symlink() or not gitdir_file.is_file():
            if marker.is_file():
                # A killed ``git worktree add`` may have published the bounded
                # forward marker before its reverse pointer. The marker still
                # gives exact authority for this one admin directory.
                matches.append(resolved)
            continue
        reverse = _read_git_control_file(
            gitdir_file, label="workspace_cleanup_reverse_pointer"
        )
        reverse_path = Path(reverse)
        if not reverse_path.is_absolute():
            reverse_path = resolved / reverse_path
        if reverse_path.resolve(strict=False) == expected_marker:
            matches.append(resolved)
    unique = sorted(set(matches))
    if len(unique) > 1:
        raise WorkspaceError("workspace_cleanup_registration_ambiguous")
    if marker.is_file() and not unique:
        raise WorkspaceError("workspace_cleanup_registration_mismatch")
    return unique[0] if unique else None


def _rmtree_workspace_owned(root: Path) -> None:
    """Remove one already-authorized workspace/admin tree, repairing mode only."""
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise WorkspaceError("workspace_cleanup_root_invalid")

    def repair_and_retry(function: Any, target: str, _exc: BaseException) -> None:
        try:
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            function(target)
        except OSError as exc:
            raise WorkspaceError(
                f"workspace_cleanup_remove_failed:{Path(target).name}:"
                f"{type(exc).__name__}"
            ) from exc

    try:
        shutil.rmtree(root, onexc=repair_and_retry)
    except TypeError:  # pragma: no cover - Python <3.12 compatibility
        shutil.rmtree(root, onerror=repair_and_retry)
    except OSError as exc:
        raise WorkspaceError(
            f"workspace_cleanup_remove_failed:{root.name}:{type(exc).__name__}"
        ) from exc


def cleanup_workspace(
    repo: Path,
    path: Path,
    home: Path,
    *,
    git_timeout: float = 120,
) -> None:
    repo = repo.resolve()
    path = path.resolve()
    home = home.resolve()
    if path.name != "worktree" or home.name != "home" or path.parent != home.parent:
        raise WorkspaceError("refusing_unsafe_workspace_cleanup")
    canonical_repo_root = (repo / ".aiworkhub" / "runtime" / "worktrees").resolve(
        strict=False
    )
    workspace_root = path.parent.parent
    if (
        path == repo or repo in path.parents
    ) and workspace_root != canonical_repo_root:
        raise WorkspaceError("refusing_to_cleanup_parent_worktree")
    # Cleanup must remain serviceable even when Git subprocesses are the thing
    # that is wedged (the Windows NF356 failure). Resolve the one registration
    # by its reciprocal gitdir pointers, remove the request-owned tree, then
    # remove only that exact administrative directory. No global prune and no
    # subprocess are needed.
    _ = git_timeout  # retained API compatibility; cleanup is process-free.
    admin_dir = _registered_worktree_admin_dir(repo, path)
    _rmtree_workspace_owned(path.parent)
    if path.exists() or home.exists() or path.parent.exists():
        raise WorkspaceError("workspace_cleanup_directory_retained")
    if admin_dir is not None:
        _rmtree_workspace_owned(admin_dir)
        if admin_dir.exists():
            raise WorkspaceError("workspace_cleanup_registration_retained")
    # NF430: the request-owned worker temp authority shares this workspace's
    # lifecycle.  Whenever the isolated worktree is torn down (accepted,
    # cancelled, provider-crashed, or reclaimed) its
    # ``.aiworkhub/temp/worker/<request_id>`` root is disposed too.  The
    # request id is exactly ``path.parent.name`` by the workspace-shape
    # contract asserted above.  Best-effort and fail-closed: review/rework/
    # validation-failed candidates retain the workspace so this never runs for
    # them (their temp stays pinned), and a dead-owner root is still swept by
    # the sole runtime_temp GC authority.
    try:
        dispose_worker_temp(repo, path.parent.name)
    except Exception:  # noqa: BLE001 - cleanup must never fail on temp disposal
        pass


def _finalization_probe_key(repo: Path, adapter_id: str) -> tuple[str, str, str]:
    repo = repo.resolve()
    try:
        head_oid = _repository_head_oid(repo)
    except WorkspaceError:
        head_oid = "unresolved"
    return str(repo), str(adapter_id), head_oid


def finalization_preflight_probe(
    repo: Path,
    adapter_id: str,
    *,
    cache_seconds: float = _FINALIZATION_PROBE_CACHE_SECONDS,
) -> dict[str, Any]:
    """Exercise the real isolated zero-diff path with a short-lived cache."""
    repo = repo.resolve()
    key = _finalization_probe_key(repo, adapter_id)
    head_oid = key[2]
    now = time.monotonic()
    with _FINALIZATION_PROBE_LOCK:
        cached = _FINALIZATION_PROBE_CACHE.get(key)
        if cached and now - cached[0] <= cache_seconds:
            return {**cached[1], "cache_hit": True}

    request_id = f"preflight-finalization-{uuid.uuid4().hex[:16]}"
    workspace: WorkerWorkspace | None = None
    started = time.monotonic()
    cleanup_ms = 0.0
    try:
        workspace = create_workspace(
            repo,
            request_id,
            {
                "allowed_writes": [],
                "required_outputs": [],
                "read_first": [],
                "immutable_inputs": [],
            },
            adapter_id,
        )
        changed = enforce_scope(
            workspace,
            git_phase="preflight_finalization",
            git_timeout=finalization_git_timeout_seconds(),
        )
        if changed:
            raise WorkspaceError("preflight_finalization_nonzero_delta")
        result: dict[str, Any] = {
            "ok": True,
            "status": "ready",
            "reason": "",
            "phase": "preflight_finalization",
            "command": "git diff --name-only --no-renames -z <base_oid>",
            "provisioning_timings_ms": dict(workspace.provisioning_timings_ms or {}),
        }
    except (OSError, RuntimeError, ValueError, WorkspaceError) as exc:
        result = {
            "ok": False,
            "status": "blocked",
            "reason": str(exc)[:500],
            "phase": "preflight_finalization",
            "command": "git diff --name-only --no-renames -z <base_oid>",
            "provisioning_timings_ms": (
                dict(workspace.provisioning_timings_ms or {}) if workspace else {}
            ),
        }
    finally:
        if workspace is not None:
            cleanup_started = time.monotonic()
            try:
                cleanup_workspace(
                    workspace.repo,
                    workspace.path,
                    workspace.home,
                    git_timeout=finalization_git_timeout_seconds(),
                )
            except (OSError, RuntimeError, ValueError, WorkspaceError) as exc:
                if isinstance(exc, GitCommandTimeout):
                    failed_phase = exc.phase
                    failed_command = " ".join(exc.argv)
                else:
                    failed_phase = "workspace_cleanup"
                    failed_command = "workspace cleanup"
                result = {
                    **result,
                    "ok": False,
                    "status": "blocked",
                    "reason": f"preflight_finalization_cleanup_failed:{exc}"[:500],
                    "phase": failed_phase,
                    "command": failed_command,
                }
            cleanup_ms = (time.monotonic() - cleanup_started) * 1000.0
    result = {
        **result,
        "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
        "cleanup_ms": round(cleanup_ms, 3),
        "cache_hit": False,
    }
    if result.get("ok") is True and head_oid != "unresolved":
        with _FINALIZATION_PROBE_LOCK:
            _FINALIZATION_PROBE_CACHE[key] = (time.monotonic(), dict(result))
    return result


def finalization_preflight_probe_nonblocking(
    repo: Path,
    adapter_id: str,
    *,
    cache_seconds: float = _FINALIZATION_PROBE_CACHE_SECONDS,
    failure_cache_seconds: float = 30.0,
) -> dict[str, Any]:
    """Return immediately while one coalesced real probe runs in background."""
    repo = repo.resolve()
    key = _finalization_probe_key(repo, adapter_id)
    now = time.monotonic()
    with _FINALIZATION_PROBE_LOCK:
        cached = _FINALIZATION_PROBE_CACHE.get(key)
        if cached and now - cached[0] <= cache_seconds:
            return {**cached[1], "cache_hit": True, "background": True}
        failed = _FINALIZATION_PROBE_FAILURES.get(key)
        if failed and now - failed[0] <= failure_cache_seconds:
            return {**failed[1], "cache_hit": True, "background": True}
        active = _FINALIZATION_PROBE_ACTIVE.get(key)
        if active and active[1].is_alive():
            return {
                "ok": False,
                "status": "probing",
                "reason": "worker_finalization_probe_running",
                "phase": "preflight_finalization",
                "cache_hit": False,
                "background": True,
                "elapsed_ms": round((now - active[0]) * 1000.0, 3),
            }

        def run_probe() -> None:
            try:
                result = finalization_preflight_probe(
                    repo, adapter_id, cache_seconds=cache_seconds
                )
            except Exception as exc:
                result = {
                    "ok": False,
                    "status": "blocked",
                    "reason": f"preflight_finalization_probe_failed:{exc}"[:500],
                    "phase": "preflight_finalization",
                    "cache_hit": False,
                }
            with _FINALIZATION_PROBE_LOCK:
                _FINALIZATION_PROBE_ACTIVE.pop(key, None)
                if result.get("ok") is True and key[2] != "unresolved":
                    _FINALIZATION_PROBE_CACHE[key] = (
                        time.monotonic(),
                        dict(result),
                    )
                    _FINALIZATION_PROBE_FAILURES.pop(key, None)
                else:
                    _FINALIZATION_PROBE_FAILURES[key] = (
                        time.monotonic(),
                        dict(result),
                    )

        thread = threading.Thread(
            target=run_probe,
            name=f"aiworkhub-finalization-preflight-{key[2][:12]}",
            daemon=True,
        )
        _FINALIZATION_PROBE_ACTIVE[key] = (now, thread)
        thread.start()
    return {
        "ok": False,
        "status": "probing",
        "reason": "worker_finalization_probe_started",
        "phase": "preflight_finalization",
        "cache_hit": False,
        "background": True,
        "elapsed_ms": 0.0,
    }


def _canonical_worktree_delta_paths(repo: Path) -> list[str]:
    """Return the bounded tracked/untracked delta of the live canonical tree."""

    tracked = _run(["git", "diff", "--name-only", "-z", "HEAD"], cwd=repo)
    if tracked.returncode != 0:
        raise WorkspaceError(f"combined_tree_git_diff_failed:{tracked.stderr[:300]}")
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=repo,
    )
    if untracked.returncode != 0:
        raise WorkspaceError(
            f"combined_tree_git_untracked_failed:{untracked.stderr[:300]}"
        )
    rows = sorted(
        {
            _relative_repo_path(value)
            for value in (tracked.stdout + untracked.stdout).split("\x00")
            if value
        }
    )
    if len(rows) > MAX_SEED_FILES:
        raise WorkspaceError(f"combined_tree_path_limit_exceeded:{len(rows)}")
    return rows


def _overlay_regular_path(source_root: Path, target_root: Path, relative: str) -> None:
    source = source_root / relative
    target = target_root / relative
    _require_beneath(source_root, source)
    _require_beneath(target_root, target)
    if source.is_symlink():
        raise WorkspaceError(f"combined_tree_symlink_forbidden:{relative}")
    if not source.exists():
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            raise WorkspaceError(f"combined_tree_delete_non_file:{relative}")
        return
    if not source.is_file():
        raise WorkspaceError(f"combined_tree_source_not_file:{relative}")
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise WorkspaceError(f"combined_tree_target_not_regular:{relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Content-only copy: copystat/copytimes (os.utime) is denied inside the
    # Landlock validation boundary, so metadata-preserving copy2 would fail.
    shutil.copyfile(source, target)


def create_combined_validation_workspace(
    source_workspace: WorkerWorkspace,
    card: Mapping[str, Any],
    candidate_changed_paths: Iterable[str],
) -> tuple[WorkerWorkspace, dict[str, Any]]:
    """Materialize current canonical state plus one retained candidate delta.

    The returned detached worktree starts from ``HEAD``, overlays every
    tracked/untracked change currently present in the canonical worktree, and
    then overlays the exact candidate paths last.  Its baseline is reset after
    the canonical overlay, so later scope checks observe only the candidate
    delta while validations execute against the complete union.  The caller
    owns cleanup through :func:`cleanup_workspace`.
    """

    repo = source_workspace.repo.resolve()
    candidate = sorted({_relative_repo_path(value) for value in candidate_changed_paths})
    if not candidate:
        raise WorkspaceError("combined_tree_candidate_empty")
    canonical_delta = _canonical_worktree_delta_paths(repo)
    union_allowed = sorted(set(source_workspace.allowed_writes) | set(canonical_delta))
    if len(union_allowed) > MAX_SEED_FILES:
        raise WorkspaceError(f"combined_tree_path_limit_exceeded:{len(union_allowed)}")
    union_card = dict(card)
    union_card["allowed_writes"] = union_allowed
    request_id = f"union_{source_workspace.request_id[:70]}_{uuid.uuid4().hex[:16]}"
    combined = create_workspace(repo, request_id, union_card, "validation")
    try:
        for relative in canonical_delta:
            _overlay_regular_path(repo, combined.path, relative)
        baseline_paths = sorted(set(combined.workspace_baseline) | set(canonical_delta))
        combined = replace(
            combined,
            workspace_baseline={
                relative: _hash_path(combined.path / relative)
                for relative in baseline_paths
            },
        )
        for relative in candidate:
            _overlay_regular_path(source_workspace.path, combined.path, relative)
        observed = changed_paths(combined)
        unexpected = sorted(set(observed) - set(candidate))
        missing = sorted(set(candidate) - set(observed))
        if unexpected:
            raise WorkspaceError(
                "combined_tree_unexpected_delta:" + ",".join(unexpected[:20])
            )
        if missing:
            raise WorkspaceError(
                "combined_tree_candidate_not_materialized:" + ",".join(missing[:20])
            )
        return combined, {
            "schema_id": "aiworkhub.combined_tree.v1",
            "candidate_paths": candidate,
            "canonical_delta_paths": canonical_delta,
            "observed_candidate_paths": observed,
        }
    except Exception:
        cleanup_workspace(repo, combined.path, combined.home)
        raise


def _quality_review_read_only_input_paths(
    repo: Path,
    declarations: Iterable[str],
) -> list[str]:
    inputs = _expand_declared(repo, declarations)
    for relative in inputs:
        source = repo / relative
        _require_beneath(repo, source)
        if source.is_symlink():
            raise WorkspaceError(f"quality_review_read_only_input_symlink:{relative}")
        if not source.exists():
            raise WorkspaceError(f"quality_review_read_only_input_missing:{relative}")
        if not source.is_file():
            raise WorkspaceError(f"quality_review_read_only_input_not_file:{relative}")
    return inputs


def quality_review_read_only_input_paths(
    repo: Path,
    *,
    read_first: Iterable[str],
    immutable_inputs: Iterable[str],
) -> list[str]:
    return _quality_review_read_only_input_paths(
        repo,
        [*read_first, *immutable_inputs],
    )


def _overlay_quality_review_read_only_input(
    source_root: Path,
    target_root: Path,
    relative: str,
) -> None:
    source = source_root / relative
    target = target_root / relative
    _require_beneath(source_root, source)
    _require_beneath(target_root, target)
    if source.is_symlink():
        raise WorkspaceError(f"quality_review_read_only_input_symlink:{relative}")
    if not source.exists():
        raise WorkspaceError(f"quality_review_read_only_input_missing:{relative}")
    if not source.is_file():
        raise WorkspaceError(f"quality_review_read_only_input_not_file:{relative}")
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise WorkspaceError(f"quality_review_read_only_target_not_regular:{relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def create_quality_review_workspace(
    source_workspace: WorkerWorkspace,
    request_id: str,
    candidate_changed_paths: Iterable[str],
    adapter_id: str,
    read_only_input_paths: Iterable[str] = (),
) -> tuple[WorkerWorkspace, dict[str, Any]]:
    """Materialize one candidate for a strictly read-only reviewer.

    The filesystem content matches the combined canonical+candidate tree,
    but the returned workspace has no writable paths. The candidate delta is
    retained in its baseline metadata solely so the coordinator can prove
    that the reviewer saw the exact packet-bound bytes and made no edits.
    """

    repo = source_workspace.repo.resolve()
    candidate = sorted({_relative_repo_path(value) for value in candidate_changed_paths})
    if not candidate:
        raise WorkspaceError("quality_review_candidate_empty")
    read_only_inputs = _quality_review_read_only_input_paths(repo, read_only_input_paths)
    canonical_delta = _canonical_worktree_delta_paths(repo)
    seed_paths = sorted(set(candidate) | set(canonical_delta) | set(read_only_inputs))
    if len(seed_paths) > MAX_SEED_FILES:
        raise WorkspaceError(f"quality_review_path_limit_exceeded:{len(seed_paths)}")
    seed_card = {
        "allowed_writes": seed_paths,
        "required_outputs": [],
        "read_first": [],
        "immutable_inputs": [],
    }
    review_workspace = create_workspace(repo, request_id, seed_card, adapter_id)
    try:
        for relative in read_only_inputs:
            _overlay_quality_review_read_only_input(
                repo, review_workspace.path, relative
            )
        for relative in canonical_delta:
            _overlay_regular_path(repo, review_workspace.path, relative)
        baseline_paths = sorted(
            set(review_workspace.workspace_baseline)
            | set(canonical_delta)
            | set(read_only_inputs)
        )
        canonical_baseline = {
            relative: _hash_path(review_workspace.path / relative)
            for relative in baseline_paths
        }
        for relative in candidate:
            _overlay_regular_path(source_workspace.path, review_workspace.path, relative)
        observed = changed_paths(
            replace(review_workspace, workspace_baseline=canonical_baseline)
        )
        if observed != candidate:
            raise WorkspaceError(
                "quality_review_candidate_mismatch:"
                + ",".join(sorted(set(observed) ^ set(candidate))[:20])
            )
        workspace_baseline_paths = sorted(set(baseline_paths) | set(candidate))
        readonly = replace(
            review_workspace,
            allowed_writes=(),
            parent_baseline={},
            workspace_baseline={
                relative: _hash_path(review_workspace.path / relative)
                for relative in workspace_baseline_paths
            },
        )
        read_only_input_hashes = {
            relative: _hash_path(readonly.path / relative)
            for relative in read_only_inputs
        }
        return readonly, {
            "schema_id": "aiworkhub.quality_review_workspace.v1",
            "candidate_paths": candidate,
            "canonical_delta_paths": canonical_delta,
            "read_only_input_paths": read_only_inputs,
            "read_only_input_hashes": read_only_input_hashes,
            "readonly": True,
        }
    except Exception:
        cleanup_workspace(repo, review_workspace.path, review_workspace.home)
        raise


def assert_gc_safe_workspace_shape(
    request_id: str,
    path: Path,
    home: Path,
    *,
    repo: Path | None = None,
) -> Path:
    """Fail closed unless this is the request's exact configured workspace."""
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise WorkspaceError(f"gc_invalid_request_id:{request_id}")
    candidates = (configured_worktree_root(repo), _legacy_worktree_root())
    for root in dict.fromkeys(candidates):
        expected_path = (root / request_id / "worktree").resolve(strict=False)
        expected_home = (root / request_id / "home").resolve(strict=False)
        if (
            path.resolve(strict=False) == expected_path
            and home.resolve(strict=False) == expected_home
        ):
            return root
    raise WorkspaceError(
        f"gc_workspace_shape_mismatch:{request_id}:path={path}:home={home}"
    )


def changed_paths(
    workspace: WorkerWorkspace,
    *,
    git_phase: str = "workspace_scope",
    git_timeout: float = 120,
) -> list[str]:
    # Diff against the OID the worktree was pinned to at creation, never the
    # live symbolic ``HEAD``.  A worker that commits inside its own detached
    # worktree moves ``HEAD``; diffing symbolic ``HEAD`` would then report the
    # committed work as unchanged and it would never be scope-checked or
    # promoted.  If ``HEAD`` moved to something the pinned base cannot explain
    # (the base is not an ancestor of the current commit), fail closed rather
    # than diff against an unrelated tree.
    diff_ref = workspace.base_oid or "HEAD"
    if workspace.base_oid:
        # The detached worktree's administrative HEAD is the authority already
        # verified at creation. Re-spawning ``git rev-parse HEAD`` here caused
        # the exact Windows accept-review 120-second hang reported in 0.9.99.
        current_head = _isolated_worktree_base_oid(
            workspace.repo, workspace.path
        )
        if current_head != workspace.base_oid:
            ancestry = _run(
                ["git", "merge-base", "--is-ancestor", workspace.base_oid, "HEAD"],
                cwd=workspace.path,
                timeout=git_timeout,
                phase=git_phase,
            )
            if ancestry.returncode != 0:
                raise WorkspaceError(
                    "worktree_head_moved_unexplained:"
                    f"{workspace.base_oid}:{current_head}"
                )
    # ``--no-renames`` splits a staged rename into an explicit delete of the
    # source and an add of the destination, so ``git mv src/a.py src/b.py``
    # records both sides.  With Git's default rename detection, ``--name-only``
    # would report only the destination and hide the deleted source from the
    # scope check.
    try:
        tracked = _run(
            ["git", "diff", "--name-only", "--no-renames", "-z", diff_ref],
            cwd=workspace.path,
            timeout=git_timeout,
            phase=git_phase,
        )
        if tracked.returncode != 0:
            raise WorkspaceError(f"git_diff_failed:{tracked.stderr[:300]}")
        untracked = _run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=workspace.path,
            timeout=git_timeout,
            phase=git_phase,
        )
        if untracked.returncode != 0:
            raise WorkspaceError(f"git_untracked_failed:{untracked.stderr[:300]}")
    except GitCommandTimeout as primary_error:
        # The Windows extension-host process context can block a Git child even
        # when the same command is fast in an interactive shell.  _run already
        # terminates the exact child tree; compare the complete creation-time
        # manifest instead of trusting provider output or accepting zero-diff.
        try:
            return _manifest_changed_paths(workspace, git_phase=git_phase)
        except (OSError, RuntimeError, ValueError, WorkspaceError) as fallback_error:
            raise WorkspaceError(
                f"{git_phase}_git_fallback_failed:"
                f"primary={primary_error}:fallback={fallback_error}"
            ) from fallback_error
    rows: set[str] = {
        _relative_repo_path(item)
        for item in (tracked.stdout + untracked.stdout).split("\x00")
        if item
    }
    for relative, initial_hash in workspace.workspace_baseline.items():
        current_hash = _hash_path(workspace.path / relative)
        inherited_change = (
            relative in workspace.inherited_rework_paths
            and current_hash == initial_hash
            and current_hash != workspace.parent_baseline.get(relative)
        )
        if current_hash != initial_hash or inherited_change:
            rows.add(_relative_repo_path(relative))
        else:
            rows.discard(_relative_repo_path(relative))
    return sorted(rows)


def enforce_scope(
    workspace: WorkerWorkspace,
    *,
    git_phase: str = "workspace_scope",
    git_timeout: float = 120,
) -> list[str]:
    changed = changed_paths(
        workspace, git_phase=git_phase, git_timeout=git_timeout
    )
    outside = [path for path in changed if not _matches(path, workspace.allowed_writes)]
    if outside:
        raise WorkspaceError("scope_violation:" + ",".join(outside[:20]))
    for relative in changed:
        target = workspace.path / relative
        if target.is_symlink():
            raise WorkspaceError(f"symlink_output_forbidden:{relative}")
    return changed


def _required_output_glob_matches(workspace_path: Path, pattern: str) -> list[str]:
    if pattern.endswith("/**"):
        base = pattern[:-3]
        root = workspace_path / base
        _require_beneath(workspace_path, root)
        if not root.is_dir() or root.is_symlink():
            return []
        matches = (
            match.relative_to(workspace_path).as_posix()
            for match in root.rglob("*")
            if match.is_file() or match.is_symlink()
        )
    else:
        matches = (
            match.relative_to(workspace_path).as_posix()
            for match in workspace_path.glob(pattern)
            if match.is_file() or match.is_symlink()
        )
    return sorted(set(matches))


def _validation_only_replay_evidence(
    authorization: dict[str, Any] | None,
    relative: str,
    raw_sha256: str | None,
    *,
    task_id: str,
    actor: str,
    predecessor_request_id: str,
    claim_epoch: int | None,
) -> dict[str, Any] | None:
    """Return evidence only for an exact, single-episode replay authorization."""
    if not isinstance(authorization, dict):
        return None
    if authorization.get("one_episode_binding") is not True:
        return None
    if not task_id or str(authorization.get("task_id") or "") != task_id:
        return None
    if not actor or str(authorization.get("actor") or "") != actor:
        return None
    if not predecessor_request_id or str(
        authorization.get("predecessor_request_id") or ""
    ) != predecessor_request_id:
        return None
    if claim_epoch is None:
        return None
    try:
        authorized_epoch = int(authorization.get("next_claim_epoch"))
    except (TypeError, ValueError):
        return None
    if authorized_epoch != int(claim_epoch):
        return None
    pinned_hashes = authorization.get("changed_path_hashes")
    if not isinstance(pinned_hashes, dict):
        return None
    pinned_hash = pinned_hashes.get(relative)
    if not pinned_hash or not raw_sha256 or pinned_hash != raw_sha256:
        return None
    return {
        "schema_id": "aiworkhub.validation_only_replay_evidence.v1",
        "path": relative,
        "sha256": raw_sha256,
        "task_id": task_id,
        "actor": actor,
        "predecessor_request_id": predecessor_request_id,
        "claim_epoch": int(claim_epoch),
        "authorized_at": authorization.get("authorized_at"),
    }


def validate_required_outputs(
    workspace: WorkerWorkspace,
    required_outputs: Iterable[str],
    allow_empty: tuple[str, ...] | None = None,
    allow_unchanged: tuple[str, ...] | None = None,
    *,
    replay_authorization: dict[str, Any] | None = None,
    replay_task_id: str = "",
    replay_actor: str = "",
    replay_predecessor_request_id: str = "",
    replay_claim_epoch: int | None = None,
) -> list[dict[str, Any]]:
    """Validate every declared required output exists, is non-empty, and changed.

    Every declared pattern is inspected before failure. Mismatches are returned
    in one deterministic ``required_output_mismatch`` diagnostic with separate
    missing, unchanged, scope-violation, and passing-record buckets so rework
    receives the complete repair target instead of entering a one-error loop.

    ``allow_empty`` is an exact, repo-relative path allowlist for deliberately
    zero-byte outputs (e.g. a contradiction lane that honestly produced no rows).
    Paths not in this set are still rejected for zero bytes.  The caller must pass
    only snapshotted metadata, never the mutable card.

    ``allow_unchanged`` is an exact path allowlist for required outputs that may
    remain byte-equal to both launch baselines. These records are reported but
    must not be promoted by callers.

    ``replay_authorization`` never widens that allowlist. It permits only an
    unchanged inherited predecessor path whose raw SHA-256 and exact task,
    actor, predecessor request, and claim epoch match a one-episode Phase A
    authorization. Every mismatch preserves the ordinary fail-closed result.
    """
    required_patterns = [_relative_repo_path(raw) for raw in required_outputs]
    unchanged_allowed: set[str] = set()
    for raw in allow_unchanged or ():
        path = _relative_repo_path(raw)
        if any(ch in path for ch in "*?["):
            raise WorkspaceError(f"allow_unchanged_required_output_glob:{path}")
        if path not in required_patterns:
            raise WorkspaceError(f"allow_unchanged_not_in_required_outputs:{path}")
        if path not in workspace.allowed_writes:
            raise WorkspaceError(f"allow_unchanged_not_in_allowed_writes:{path}")
        unchanged_allowed.add(path)
    records: list[dict[str, Any]] = []
    missing_required_artifacts: list[str] = []
    unchanged_mandatory_outputs: list[str] = []
    scope_violations: list[dict[str, str]] = []
    legacy_error_codes: list[str] = []
    for pattern in required_patterns:
        if not _matches(pattern, workspace.allowed_writes):
            scope_violations.append(
                {"path": pattern, "reason": "required_output_not_allowed"}
            )
            legacy_error_codes.append(f"required_output_not_allowed:{pattern}")
            continue
        matches: list[str]
        if any(ch in pattern for ch in "*?["):
            matches = _required_output_glob_matches(workspace.path, pattern)
            if not matches:
                missing_required_artifacts.append(pattern)
                legacy_error_codes.append(f"required_output_no_matches:{pattern}")
                continue
        else:
            matches = [pattern]
        for relative in matches:
            target = workspace.path / relative
            _require_beneath(workspace.path, target)
            if target.is_symlink():
                scope_violations.append(
                    {"path": relative, "reason": "required_output_symlink"}
                )
                legacy_error_codes.append(f"required_output_symlink:{relative}")
                continue
            if not target.is_file():
                if not target.exists():
                    missing_required_artifacts.append(relative)
                    legacy_error_codes.append(f"required_output_missing:{relative}")
                else:
                    scope_violations.append(
                        {"path": relative, "reason": "required_output_non_file"}
                    )
                    legacy_error_codes.append(f"required_output_missing:{relative}")
                continue
            size = target.stat().st_size
            if size <= 0 and (allow_empty is None or relative not in allow_empty):
                scope_violations.append(
                    {"path": relative, "reason": "required_output_zero_bytes"}
                )
                legacy_error_codes.append(f"required_output_zero_bytes:{relative}")
                continue
            current_hash = _hash_path(target)
            inherited_change = (
                relative in workspace.inherited_rework_paths
                and current_hash == workspace.workspace_baseline.get(relative)
                and current_hash != workspace.parent_baseline.get(relative)
            )
            is_unchanged = (
                current_hash == workspace.workspace_baseline.get(relative)
                and not inherited_change
            )
            replay_evidence: dict[str, Any] | None = None
            if is_unchanged:
                if relative not in unchanged_allowed:
                    if relative in workspace.inherited_rework_paths:
                        raw_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
                        replay_evidence = _validation_only_replay_evidence(
                            replay_authorization,
                            relative,
                            raw_sha256,
                            task_id=replay_task_id,
                            actor=replay_actor,
                            predecessor_request_id=replay_predecessor_request_id,
                            claim_epoch=replay_claim_epoch,
                        )
                    if replay_evidence is None:
                        unchanged_mandatory_outputs.append(relative)
                        legacy_error_codes.append(f"required_output_unchanged:{relative}")
                        continue
                parent_hash = workspace.parent_baseline.get(relative)
                if current_hash != parent_hash:
                    scope_violations.append(
                        {
                            "path": relative,
                            "reason": "required_output_unchanged_parent_mismatch",
                        }
                    )
                    legacy_error_codes.append(
                        f"required_output_unchanged_parent_mismatch:{relative}"
                    )
                    continue
                if current_hash is None or target.is_symlink() or not target.is_file() or size <= 0:
                    scope_violations.append(
                        {"path": relative, "reason": "required_output_unchanged_invalid"}
                    )
                    legacy_error_codes.append(
                        f"required_output_unchanged_invalid:{relative}"
                    )
                    continue
            record = {
                "pattern": pattern,
                "path": relative,
                "bytes": size,
                "sha256": current_hash,
                "unchanged_allowed": is_unchanged,
            }
            if replay_evidence is not None:
                record["replay_evidence"] = replay_evidence
            records.append(record)
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


def _promotion_inflight_dir(repo: Path) -> Path:
    """Directory of per-request markers for promotions writing into the parent
    working tree right now (NF-2026-00285)."""
    return configured_worktree_root(repo) / ".promotion_inflight"


def _promotion_begin(workspace: "WorkerWorkspace") -> Path | None:
    """Mark this request's promotion in flight; return the marker path.

    ``promote`` writes into the parent working tree BEFORE the coordinator
    commits. A worktree seeded from git ``HEAD`` in that window sees a tree that
    mixes pre-promotion committed content (everything outside the promoted
    scope) with the just-promoted files -- an inconsistent snapshot of no single
    point in time. ``create_workspace`` refuses to seed while any marker exists,
    so a concurrent create is turned away with a named reason rather than handed
    the inconsistent tree.

    Best-effort: if the marker cannot be created (a read-only root, say) the
    promotion itself must never be blocked, so this returns ``None`` and
    promotion proceeds exactly as before -- the guard only ever ADDS protection.
    """
    try:
        directory = _promotion_inflight_dir(workspace.repo)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = directory / workspace.request_id
        # O_CREAT (no O_EXCL): a same-request re-promotion reuses its marker
        # instead of failing, and the finally-block removal keeps it bounded.
        fd = os.open(marker, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        return marker
    except OSError:
        return None


def _promotion_end(marker: Path | None) -> None:
    if marker is not None:
        unlink_if_regular(marker)


def _refuse_if_promotion_in_flight(repo: Path) -> None:
    """Refuse to seed a new worktree while a promotion is writing into the
    parent working tree (NF-2026-00285), naming the in-flight requests.

    Choosing refusal over a best-effort "consistent" seed is deliberate: the
    declared read_first/allowed inputs are copied from the LIVE parent tree
    (which may hold untracked/gitignored artifacts and prior uncommitted
    promotions), so there is no single committed base that reproduces them --
    the only consistent options are "seed outside the write window" or "refuse".
    """
    directory = _promotion_inflight_dir(repo)
    try:
        entries = sorted(
            name for name in os.listdir(directory) if not name.startswith(".")
        )
    except OSError:
        return
    if entries:
        raise WorkspaceError(
            f"worktree_seed_refused_promotion_in_flight:{repo}:{entries[:8]}"
        )


def promote(workspace: WorkerWorkspace, changed: Iterable[str]) -> list[str]:
    """Copy each declared-changed path from the isolated worktree into the
    parent repo, hash-guarded against concurrent parent edits.

    Rollback/partial-promotion contract (B314_F011, deliberate design, not a
    bug): every path is preflighted (first loop, below) against a hash
    snapshot taken BEFORE any file is written, so a scope violation or a
    parent-changed-since-launch race is detected before any promotion write
    happens -- an all-or-nothing preflight. The narrow window this does not
    cover is a filesystem failure *during* the second loop's writes (disk
    full, permission change mid-run): if files 1..k already succeeded and
    file k+1 raises, this function does not delete files 1..k to "roll
    back" the parent repo. That is intentional: those k files are now
    genuine, hash-verified, in-scope worker output already living in the
    parent tree, and deleting them again on the same code path than wrote
    them risks losing forward progress and touching the parent repo AGAIN
    on an already-failing path. The caller
    (ProcessManager._finalize_isolated_request) is the actual rollback
    boundary: when ``promoted`` is non-empty and promotion still raises, it
    sets ``cleanup=False`` and reports ``review_pending`` instead of a
    terminal failure state, and leaves the worker's isolated worktree
    in place -- so a human/Codex reviewer can inspect exactly what was
    partially promoted before the workspace is ever deleted, rather than
    silently discarding evidence or half-applying a "fix" that could touch
    parent files a second time.
    """
    paths = sorted(set(changed))
    desired: dict[str, str | None] = {}
    for relative in paths:
        if not _matches(relative, workspace.allowed_writes):
            raise WorkspaceError(f"promotion_scope_violation:{relative}")
        parent = workspace.repo / relative
        _require_beneath(workspace.repo, parent)
        expected = workspace.parent_baseline.get(relative)
        current = _hash_path(parent)
        source_hash = _hash_path(workspace.path / relative)
        desired[relative] = source_hash
        if current not in {expected, source_hash}:
            raise WorkspaceError(f"parent_changed_since_launch:{relative}")

    # Mark the parent-tree write window so a concurrent ``create_workspace``
    # refuses to seed rather than capturing a half-promoted, inconsistent tree.
    marker = _promotion_begin(workspace)
    promoted: list[str] = []
    try:
        for relative in paths:
            parent = workspace.repo / relative
            source = workspace.path / relative
            expected = workspace.parent_baseline.get(relative)
            current = _hash_path(parent)
            if current == desired[relative]:
                promoted.append(relative)
                continue
            if current != expected:
                raise WorkspaceError(f"parent_changed_during_promotion:{relative}")
            if not source.exists() and not source.is_symlink():
                if parent.exists() or parent.is_symlink():
                    parent.unlink()
                promoted.append(relative)
                continue
            if source.is_symlink() or not source.is_file():
                raise WorkspaceError(f"invalid_promotion_source:{relative}")
            parent.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{parent.name}.", dir=parent.parent)
            os.close(fd)
            temp = Path(temp_name)
            try:
                shutil.copyfile(source, temp)
                mode = stat.S_IMODE(source.stat().st_mode) & 0o777
                chmod_path(temp, mode or 0o644)
                os.replace(temp, parent)
            finally:
                temp.unlink(missing_ok=True)
            promoted.append(relative)
    finally:
        _promotion_end(marker)
    return promoted


def _verify_owner_private_directory(path: Path, label: str) -> Path:
    if path.is_absolute():
        cursor = Path(path.anchor)
        parts = path.parts[1:]
    else:
        cursor = Path()
        parts = path.parts
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise WorkspaceError(f"{label}_symlink_forbidden:{path}")
    try:
        info = path.stat()
    except OSError as exc:
        raise WorkspaceError(f"{label}_missing:{path}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError(f"{label}_not_directory:{path}")
    if info.st_uid != os.getuid():
        raise WorkspaceError(f"{label}_untrusted_owner:{path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise WorkspaceError(f"{label}_not_private:{path}")
    return path.resolve(strict=True)


def sanitized_env(
    adapter_id: str,
    *,
    home: Path | None = None,
    isolated_task_queue_db: bool = False,
    provider_env: Mapping[str, str] | None = None,
    verify_preprovisioned_home: bool = False,
) -> dict[str, str]:
    if home is not None:
        if os.name == "nt" or not verify_preprovisioned_home:
            selected_home = Path(home).resolve()
            selected_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            chmod_path(selected_home, 0o700)
            temp_home = selected_home / "tmp"
            temp_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            chmod_path(temp_home, 0o700)
        else:
            selected_home = _verify_owner_private_directory(
                Path(home), "sanitized_home"
            )
            temp_home = _verify_owner_private_directory(
                Path(home) / "tmp", "sanitized_tmp"
            )
    else:
        selected_home = Path(bubblewrap_home_env_value()).resolve()
        # Bubblewrap provides a private /tmp. Do not chmod or create anything
        # in the caller's real HOME while constructing the child environment.
        temp_home = Path(tempfile.gettempdir()) if os.name == "nt" else Path("/tmp")
    common: dict[str, str] = {
        "HOME": str(selected_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", os.environ.get("LANG", "C.UTF-8")),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "TMPDIR": str(temp_home),
        "TMP": str(temp_home),
        "TEMP": str(temp_home),
    }
    if os.name == "nt":
        drive, tail = os.path.splitdrive(str(selected_home))
        username = os.environ.get("USERNAME", os.environ.get("USER", "user"))
        safe = {
            **common,
            "USERPROFILE": str(selected_home),
            "HOMEDRIVE": drive or os.environ.get("HOMEDRIVE", ""),
            "HOMEPATH": tail or os.environ.get("HOMEPATH", "\\"),
            "USERNAME": username,
            "USER": username,
            "LOGNAME": username,
            # Windows process creation and CLI shim discovery require the
            # native PATH/PATHEXT plus SystemRoot/ComSpec.  None contain
            # credentials, and dropping them makes even Python and *.cmd
            # launchers fail before the sandbox can start.
            "PATH": os.environ.get("PATH", str(Path(sys.executable).parent)),
        }
        for key in ("PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC"):
            if key in os.environ:
                safe[key] = os.environ[key]
    else:
        safe = {
            **common,
            "USER": os.environ.get("USER", "shrek"),
            "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", "shrek")),
            "SHELL": "/bin/bash",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    if isolated_task_queue_db:
        # B328: point any AITools/taskdb.py usage inside the sandbox at the
        # disposable copy provision_isolated_task_queue_db() pre-seeded under
        # this same HOME -- never the parent's live/authoritative DB, and
        # never the worktree-local path taskdb.py's own DEFAULT_DB fallback
        # would otherwise resolve to (see TASK_QUEUE_ISOLATED_RELATIVE doc).
        safe["BITNN_TASK_QUEUE_DB"] = str(selected_home / TASK_QUEUE_ISOLATED_RELATIVE)
    passthrough = {
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    }
    if adapter_id == "claude_cli":
        passthrough.update({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"})
    elif adapter_id == "codex_cli":
        passthrough.add("OPENAI_API_KEY")
    for key in passthrough:
        if key in os.environ:
            safe[key] = os.environ[key]
    # The DeepSeek/Copilot BYOK provider env (including the sole secret,
    # COPILOT_PROVIDER_API_KEY) is passed explicitly by the launcher, never
    # read out of os.environ -- so the key never has to live in the
    # coordinator's own environment. This is the ONLY place the child receives
    # the key, and it is applied last so an explicit provider value is never
    # shadowed by an allowlisted passthrough of the same name.
    if provider_env:
        for key, value in provider_env.items():
            safe[str(key)] = str(value)
    return safe


def _exec_scratch_candidate_roots() -> tuple[Path, ...]:
    override = os.environ.get(VALIDATION_EXEC_SCRATCH_ROOT_ENV, "").strip()
    if override:
        # An explicit admin override is authoritative: no silent fallback to
        # the defaults if the operator-pinned root turns out to be unusable.
        return (Path(override),)
    return _DEFAULT_EXEC_SCRATCH_ROOTS


def _probe_exec_capable_dir(directory: Path) -> bool:
    """Best-effort, self-cleaning probe: can a regular file placed directly in
    *directory* actually be executed? Returns False (never raises) for any
    reason a candidate scratch root is unusable -- missing, not a directory,
    full, or genuinely mounted noexec. A noexec mount surfaces here as
    ``PermissionError`` (EACCES) from the kernel's own exec(2) check, which is
    exactly the B751 rc=126 failure mode, reproduced deliberately and safely
    against a disposable probe file instead of the worker's real payload.
    """
    if directory.is_symlink() or not directory.is_dir():
        return False
    windows = sys.platform == "win32"
    suffix = ".exe" if windows else ""
    probe_path = directory / f".exec_probe_{uuid.uuid4().hex}{suffix}"
    if windows:
        comspec = os.environ.get("COMSPEC", "").strip()
        if not comspec:
            return False
        try:
            probe_payload = Path(comspec).read_bytes()
        except OSError:
            return False
    else:
        probe_payload = _EXEC_PROBE_SCRIPT
    try:
        # The executable bit is requested directly on the atomic O_CREAT --
        # never via a separate chmod(2)/fchmod(2) follow-up call. This keeps
        # the probe itself usable even where chmod-family syscalls are denied
        # (e.g. this exact code running nested under its own
        # ``_apply_metadata_seccomp`` deny-list) and closes the TOCTOU window
        # a create-then-chmod sequence would otherwise leave open.
        # ``O_BINARY`` is mandatory on Windows.  Without it, writing a native
        # executable through ``os.write`` performs CRLF translation and
        # silently corrupts the private COMSPEC copy used by the probe.
        fd = os.open(
            probe_path,
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_BINARY", 0),
            0o700,
        )
    except OSError:
        return False
    try:
        try:
            os.write(fd, probe_payload)
        finally:
            os.close(fd)
        if windows:
            # Execute a private copy from the candidate itself. Invoking the
            # original COMSPEC on a .cmd file would only prove that cmd.exe
            # can *read* the directory, not that Windows policy permits a
            # native validation artifact located there to execute.
            argv = [str(probe_path), "/d", "/c", "exit 0"]
            probe_env = {
                "COMSPEC": comspec,
                "PATH": os.environ.get("PATH", ""),
                "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            }
        else:
            argv = [str(probe_path)]
            probe_env = {"PATH": "/usr/bin:/bin"}
        result = subprocess.run(
            argv,
            cwd=str(directory),
            env=probe_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            shell=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        probe_path.unlink(missing_ok=True)


def _probe_metadata_capable_dir(directory: Path) -> bool:
    """Best-effort, self-cleaning probe for platform metadata semantics."""
    if directory.is_symlink() or not directory.is_dir():
        return False
    probe_path = directory / f".metadata_probe_{uuid.uuid4().hex}"
    try:
        fd = os.open(probe_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return False
    replacement_path: Path | None = None
    try:
        os.close(fd)
        if sys.platform == "win32":
            replacement_path = directory / f".metadata_replace_{uuid.uuid4().hex}"
            replacement_path.write_bytes(b"aiworkhub")
            os.replace(replacement_path, probe_path)
            return probe_path.read_bytes() == b"aiworkhub"
        os.chmod(probe_path, 0o600)
        if stat.S_IMODE(os.stat(probe_path).st_mode) != 0o600:
            return False
        return stat.S_ISREG(os.stat(probe_path).st_mode) and probe_path.read_bytes() == b""
    except OSError:
        return False
    finally:
        probe_path.unlink(missing_ok=True)
        if replacement_path is not None:
            replacement_path.unlink(missing_ok=True)


def provision_validation_exec_scratch(workspace: WorkerWorkspace) -> Path:
    """Create and return a private, request-unique, exec-capable scratch
    directory for one ``run_validations`` call.

    Fail-closed: raises ``WorkspaceError`` if every candidate root is
    unusable (missing, unwritable, or genuinely noexec) rather than silently
    handing back a directory that will make every compile+execute validation
    fail with rc=126. Never chmods, mounts, or remounts a shared filesystem --
    only ever creates one new 0700 subdirectory it fully owns.
    """
    name = f"{_EXEC_SCRATCH_NAME_PREFIX}{workspace.request_id}"
    tried: list[str] = []
    candidate_roots = list(_exec_scratch_candidate_roots())
    workspace_repo = getattr(workspace, "repo", None)
    repo_temp_validation: Path | None = None
    if (
        workspace_repo is not None
        and not os.environ.get(VALIDATION_EXEC_SCRATCH_ROOT_ENV, "").strip()
    ):
        # The repository-owned temp authority is the preferred validation
        # scratch root.  Every validation run gets its own request-named
        # 0700 directory under <repo>/.aiworkhub/temp/validation, so two
        # repositories and concurrent instances share no mutable temp state.
        try:
            repo_temp_root = configured_temp_root(Path(workspace_repo))
            repo_temp_validation = repo_temp_root / "validation"
            repo_temp_validation.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                chmod_path(repo_temp_validation, 0o700)
            except OSError:
                pass
            candidate_roots.insert(0, repo_temp_validation)
        except (OSError, RuntimeError):
            repo_temp_validation = None
    if (
        sys.platform == "win32"
        and not os.environ.get(VALIDATION_EXEC_SCRATCH_ROOT_ENV, "").strip()
    ):
        # The request-private HOME is already inside AIWorkHub's retained
        # workspace boundary. Prefer it on Windows: unlike POSIX, Windows has
        # no mount-level executable bit to gain from /dev/shm, and global
        # TEMP may be denied by enterprise policy even when the repo-owned
        # runtime is usable.
        candidate_roots.insert(0, workspace.home)
    for raw_root in candidate_roots:
        root = raw_root.expanduser()
        try:
            resolved_root = root.resolve(strict=True)
        except OSError:
            tried.append(f"{root}:unavailable")
            continue
        if resolved_root.is_symlink() or not resolved_root.is_dir():
            tried.append(f"{root}:not_a_directory")
            continue
        scratch_dir = resolved_root / name
        if scratch_dir.is_symlink():
            raise WorkspaceError(f"validation_exec_scratch_symlink_forbidden:{scratch_dir}")
        if scratch_dir.exists():
            raise WorkspaceError(f"validation_exec_scratch_already_exists:{scratch_dir}")
        try:
            os.mkdir(scratch_dir, 0o700)
        except OSError as exc:
            tried.append(f"{root}:mkdir_failed:{exc}")
            continue
        # ``mkdir``'s own mode argument already requests 0700 atomically; this
        # follow-up chmod is defense-in-depth against an unusual umask and is
        # deliberately best-effort -- its failure (e.g. chmod denied by a
        # seccomp filter) must not disqualify a directory mkdir already
        # created with the right bits.
        try:
            os.chmod(scratch_dir, 0o700)
        except OSError:
            pass
        if not _probe_exec_capable_dir(scratch_dir):
            tried.append(f"{root}:noexec")
            shutil.rmtree(scratch_dir, ignore_errors=True)
            continue
        if not _probe_metadata_capable_dir(scratch_dir):
            tried.append(f"{root}:no_metadata")
            shutil.rmtree(scratch_dir, ignore_errors=True)
            continue
        if (
            repo_temp_validation is not None
            and scratch_dir.parent == repo_temp_validation.resolve()
        ):
            # Stamp the exact PID/start-time owner identity so the terminal
            # retention GC can later identify a dead-owner orphan left by a
            # crashed worker -- and never a live or unknown owner.
            try:
                runtime_temp.write_owner_manifest(
                    scratch_dir, workspace.request_id, Path(workspace_repo)
                )
            except (OSError, RuntimeError):
                shutil.rmtree(scratch_dir, ignore_errors=True)
                tried.append(f"{root}:owner_manifest_failed")
                continue
        return scratch_dir
    raise WorkspaceError(
        "validation_exec_scratch_unavailable:" + ";".join(tried[:16])
    )


def cleanup_validation_exec_scratch(path: Path | None) -> None:
    """Fail-closed, best-effort removal of a provisioned exec scratch dir.

    Never follows a symlink and never raises -- called from the
    ``run_validations`` ``finally`` block on every outcome (success, a raised
    ``WorkspaceError``, a timeout, or any other exception), so a scratch dir
    is never left behind regardless of how the validation run ended.
    """
    if path is None:
        return
    try:
        if path.is_symlink():
            return
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        return


def landlock_abi_version() -> int:
    if sys.platform != "linux":
        return 0
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(),
        ctypes.c_size_t(0),
        ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    if result < 0:
        return 0
    return int(result)


def _seccomp_library() -> Any | None:
    try:
        library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError:
        return None
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_release.restype = None
    return library


def _seccomp_available() -> bool:
    return _seccomp_library() is not None


def _bubblewrap_usable(bwrap: Path) -> bool:
    if not bwrap.is_file() or not os.access(bwrap, os.X_OK):
        return False
    try:
        probe = subprocess.run(
            [
                str(bwrap),
                "--unshare-pid",
                "--unshare-ipc",
                "--unshare-uts",
                "--ro-bind", "/", "/",
                "--", "/bin/true",
            ],
            cwd="/",
            env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _is_windows_host() -> bool:
    return os.name == "nt"


def select_sandbox_backend() -> str:
    if _is_windows_host():
        raise WorkspaceError("windows_appcontainer_sandbox_unavailable")
    requested = os.environ.get(SANDBOX_BACKEND_ENV, "auto").strip().lower()
    if requested not in {"auto", "bubblewrap", "landlock"}:
        raise WorkspaceError(f"invalid_sandbox_backend:{requested}")
    bwrap = Path(os.environ.get(BWRAP_ENV, "/usr/bin/bwrap"))
    if requested in {"auto", "bubblewrap"} and _bubblewrap_usable(bwrap):
        return "bubblewrap"
    if requested == "bubblewrap":
        raise WorkspaceError(f"bubblewrap_unusable:{bwrap}")
    abi = landlock_abi_version()
    if abi >= 1 and _seccomp_available():
        return "landlock"
    detail = "landlock_unsupported" if abi < 1 else "seccomp_unavailable"
    raise WorkspaceError(f"secure_sandbox_unavailable:bubblewrap_unusable:{detail}")


_TRUSTED_VALIDATION_BARE_EXECUTABLES = frozenset({"ruff", "mypy"})
SANDBOX_VALIDATION_EXECUTABLE_ROOT = "/validation-executable-root"


def _validation_executable_relative_path(name: str) -> PurePosixPath:
    if os.name == "nt":
        return PurePosixPath("Scripts") / f"{name}.exe"
    return PurePosixPath("bin") / name


def _trusted_validation_runtime_roots(repo: Path | None = None) -> tuple[Path, ...]:
    roots: list[Path] = []
    if repo is None:
        # Prefer the canonical repository .venv even when the caller does
        # not supply an explicit repo, by deriving the repository root from
        # the module's own location. The factory worker always places this
        # module inside src/aiworkhub/.
        repo = Path(__file__).resolve().parents[2]
    roots.append(repo / ".venv")
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        roots.append(Path(virtual_env))
    if Path(sys.prefix) != Path(getattr(sys, "base_prefix", sys.prefix)):
        roots.append(Path(sys.prefix))

    resolved: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve(strict=False))
        if key in seen:
            continue
        seen.add(key)
        resolved.append(root)
    return tuple(resolved)


def _resolve_trusted_validation_executable(
    name: str, repo: Path | None = None
) -> TrustedValidationExecutable:
    """Resolve one approved bare validation executable without trusting PATH."""
    if name not in _TRUSTED_VALIDATION_BARE_EXECUTABLES:
        raise WorkspaceError(f"validation_executable_not_approved:{name}")

    relative = _validation_executable_relative_path(name)
    for raw_root in _trusted_validation_runtime_roots(repo):
        root = raw_root.resolve(strict=False)
        candidate = raw_root / relative
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise WorkspaceError(
                f"validation_executable_untrusted_runtime_root:{resolved}"
            ) from exc
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise WorkspaceError(f"validation_executable_not_executable:{resolved}")
        try:
            root_info = root.stat()
        except OSError as exc:
            raise WorkspaceError(f"validation_executable_unavailable:{name}") from exc
        if os.name != "nt" and root_info.st_uid != os.getuid():
            raise WorkspaceError(f"validation_executable_runtime_root_untrusted_owner:{root}")
        if posix_path_modes_supported(os.name) and stat.S_IMODE(root_info.st_mode) & 0o002:
            raise WorkspaceError(f"validation_executable_runtime_root_world_writable:{root}")
        return _trusted_validation_executable_from_resolved(name, root, resolved)
    raise WorkspaceError(f"validation_executable_unavailable:{name}")


def _trusted_validation_executable_from_resolved(
    name: str, root: Path, resolved: Path
) -> TrustedValidationExecutable:
    try:
        info = resolved.stat()
    except OSError as exc:
        raise WorkspaceError(f"validation_executable_unavailable:{name}") from exc
    if os.name != "nt" and info.st_uid != os.getuid():
        raise WorkspaceError(f"validation_executable_untrusted_owner:{resolved}")
    if posix_path_modes_supported(os.name) and stat.S_IMODE(info.st_mode) & 0o002:
        raise WorkspaceError(f"validation_executable_world_writable:{resolved}")
    return TrustedValidationExecutable(path=resolved, root=root)


def resolve_trusted_validation_executable(name: str, repo: Path | None = None) -> Path:
    return _resolve_trusted_validation_executable(name, repo).path


def _resolve_repo_relative_trusted_validation_executable(
    head: str, repo: Path | None = None
) -> TrustedValidationExecutable | None:
    """Resolve an approved repository-relative virtualenv executable."""
    if repo is None:
        repo = Path(__file__).resolve().parents[2]
    normalized = head.replace("\\", "/")
    if not normalized or normalized.startswith("/") or "\x00" in normalized:
        return None
    parts = PurePosixPath(normalized).parts
    if any(part in {"", ".", ".."} for part in parts):
        return None
    name = parts[-1]
    stem = name[:-4] if os.name == "nt" and name.lower().endswith(".exe") else name
    if stem not in _TRUSTED_VALIDATION_BARE_EXECUTABLES:
        return None
    tail_parts = _validation_executable_relative_path(stem).parts
    if len(parts) <= len(tail_parts) or parts[-len(tail_parts) :] != tail_parts:
        return None

    repo_root = repo.resolve(strict=False)
    root = (
        repo_root / PurePosixPath(*parts[: -len(tail_parts)])
    ).resolve(strict=False)
    try:
        root.relative_to(repo_root)
    except ValueError:
        return None
    candidate = repo_root / PurePosixPath(*parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    try:
        root_info = root.stat()
    except OSError:
        return None
    if os.name != "nt" and root_info.st_uid != os.getuid():
        raise WorkspaceError(
            f"validation_executable_runtime_root_untrusted_owner:{root}"
        )
    if stat.S_IMODE(root_info.st_mode) & 0o002:
        raise WorkspaceError(
            f"validation_executable_runtime_root_world_writable:{root}"
        )
    return _trusted_validation_executable_from_resolved(stem, root, resolved)


_RECOGNIZED_VENV_PYTHON_SPELLINGS = frozenset(
    {".venv/bin/python", ".venv/Scripts/python.exe"}
)
_VALIDATION_INTERPRETER_AUTHORITY_SCHEMA = (
    "aiworkhub.validation_interpreter_authority.v1"
)


def _recognized_venv_python_spelling(head: str) -> str | None:
    if not head or Path(head).is_absolute():
        return None
    normalized = head.replace("\\", "/")
    if normalized in _RECOGNIZED_VENV_PYTHON_SPELLINGS:
        return normalized
    return None


def _verify_validation_interpreter(candidate: Path, bound_root: Path) -> Path:
    bound = bound_root.resolve(strict=False)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("validation_environment:interpreter_missing") from exc
    try:
        resolved.relative_to(bound)
    except ValueError as exc:
        raise WorkspaceError(
            "validation_environment:interpreter_symlink_escape"
        ) from exc
    if not resolved.is_file():
        raise WorkspaceError("validation_environment:interpreter_missing")
    if not os.access(resolved, os.X_OK):
        raise WorkspaceError("validation_environment:interpreter_not_executable")
    try:
        info = resolved.stat()
    except OSError as exc:
        raise WorkspaceError("validation_environment:interpreter_missing") from exc
    if os.name != "nt" and info.st_uid != os.getuid():
        raise WorkspaceError("validation_environment:interpreter_untrusted_owner")
    if posix_path_modes_supported(os.name) and stat.S_IMODE(info.st_mode) & 0o002:
        raise WorkspaceError("validation_environment:interpreter_world_writable")
    return resolved


def _normalize_validation_interpreter_argv(
    workspace: WorkerWorkspace, argv: list[str]
) -> tuple[list[str], dict[str, Any] | None]:
    if not argv:
        return [], None
    spelling = _recognized_venv_python_spelling(argv[0])
    if spelling is None:
        return list(argv), None
    relative = Path(*PurePosixPath(spelling).parts)
    local = workspace.path / relative
    if local.exists() or local.is_symlink():
        resolved = _verify_validation_interpreter(local, workspace.path)
        return [str(resolved), *argv[1:]], {
            "schema_id": _VALIDATION_INTERPRETER_AUTHORITY_SCHEMA,
            "declared": argv[0],
            "source": "workspace_local",
            "resolved": str(resolved),
        }
    canonical = workspace.repo / relative
    if canonical.exists() or canonical.is_symlink():
        resolved = _verify_validation_interpreter(canonical, workspace.repo)
        return [str(resolved), *argv[1:]], {
            "schema_id": _VALIDATION_INTERPRETER_AUTHORITY_SCHEMA,
            "declared": argv[0],
            "source": "canonical_repository",
            "resolved": str(resolved),
        }
    raise WorkspaceError("validation_environment:interpreter_missing")


def _normalize_trusted_validation_executable_argv(
    argv: list[str], repo: Path | None = None
) -> list[str]:
    normalized, _roots = _normalize_trusted_validation_executable_argv_with_roots(
        argv, repo
    )
    return normalized


_BARE_PYTHON_INTERPRETER_RE = re.compile(r"^python(3(\.[0-9]+)?)?$")


def _normalize_trusted_validation_executable_argv_with_roots(
    argv: list[str], repo: Path | None = None
) -> tuple[list[str], tuple[Path, ...]]:
    if not argv:
        return [], ()
    head = argv[0]
    if (
        len(argv) >= 3
        and Path(head).name.startswith("python")
        and argv[1:3] == ["-m", "ruff"]
    ):
        executable = _resolve_trusted_validation_executable("ruff", repo)
        return [str(executable.path), *argv[3:]], (executable.root,)
    if Path(head).is_absolute():
        return list(argv), ()
    if "/" in head or "\\" in head:
        repo_relative = _resolve_repo_relative_trusted_validation_executable(head, repo)
        if repo_relative is None:
            return list(argv), ()
        return [str(repo_relative.path), *argv[1:]], (repo_relative.root,)
    # NF-2026-00448: a bare ``python``/``python3``/``python3.NN`` head is
    # resolved directly to the trusted coordinator interpreter instead of
    # trusting execvpe's PATH search -- the credential-free validation PATH
    # does not reliably expose a working ``python3`` on every host, while
    # ``sys.executable`` is always the exact interpreter already running this
    # code. Explicit relative (handled above, or already resolved to an
    # absolute path by ``_normalize_validation_interpreter_argv`` before this
    # function runs) and absolute interpreter declarations are never touched
    # here, preserving their existing fail-closed rules.
    if _BARE_PYTHON_INTERPRETER_RE.match(head):
        return [sys.executable, *argv[1:]], ()
    if head not in _TRUSTED_VALIDATION_BARE_EXECUTABLES:
        return list(argv), ()
    executable = _resolve_trusted_validation_executable(head, repo)
    return [str(executable.path), *argv[1:]], (executable.root,)


def _node_install_root(executable: str) -> Path | None:
    path = Path(executable).resolve()
    for parent in path.parents:
        if parent.parent.name == "node" and parent.parent.parent.name == "versions":
            return parent
    return None


def _resolve_existing_worker_temp(workspace: WorkerWorkspace) -> Path | None:
    """Return the request-owned worker temp root iff it exists as a real dir.

    Self-derived from the workspace identity (never a caller-supplied path) and
    fail-closed: an invalid request identity, a symlink, or a not-yet-
    provisioned root all yield ``None`` so a sandbox is never widened by a temp
    root the launcher did not actually create (NF430).
    """
    try:
        candidate = worker_temp_root(workspace.repo, workspace.request_id)
    except runtime_temp.RuntimeTempError:
        return None
    if candidate.is_symlink() or not candidate.is_dir():
        return None
    return candidate


def _landlock_worker_temp_flags(workspace: WorkerWorkspace) -> list[str]:
    """Return the ``--worker-temp`` landlock-exec flag pair, or ``[]``."""
    worker_temp = _resolve_existing_worker_temp(workspace)
    return ["--worker-temp", str(worker_temp)] if worker_temp is not None else []


def sandbox_argv(
    workspace: WorkerWorkspace,
    adapter_id: str,
    adapter_argv: list[str],
    *,
    backend: str | None = None,
    validation_readonly_dirs: tuple[Path, ...] = (),
    validation_exec_scratch: Path | None = None,
    package_import_root: Path | None = None,
    validation_cwd: str | None = None,
    validation_executable_roots: tuple[Path, ...] = (),
    outer_validation_authority: bool = False,
) -> list[str]:
    if not adapter_argv:
        raise WorkspaceError("adapter_argv_empty")
    selected = backend or select_sandbox_backend()
    if selected == VSCODE_LM_IN_PROCESS_BACKEND:
        # The model runs inside VS Code's LM host, never in this subprocess.
        # This narrowly-scoped worker only consumes the owner-only response
        # spool and applies a complete response after path/scope validation.
        # AppContainer is therefore a native-CLI requirement, not a gate for
        # these three editor-hosted adapters.
        if adapter_id not in _VSCODE_LM_IN_PROCESS_ADAPTERS:
            raise WorkspaceError(
                f"vscode_lm_in_process_adapter_forbidden:{adapter_id}"
            )
        return list(adapter_argv)
    # B892: resolve the validated ``cd`` prefix target once, against the real
    # workspace filesystem, before either backend's argv is built -- so
    # Landlock and bubblewrap bind/chdir into the exact same
    # repository-relative directory rather than each re-deriving it.
    resolved_cwd_relative = (
        _resolve_validation_cwd(workspace, validation_cwd)
        if validation_cwd is not None
        else None
    )
    if selected == "landlock":
        for pattern in workspace.allowed_writes:
            if any(ch in pattern for ch in "*?["):
                prefix = _static_prefix(pattern)
                base = PurePosixPath(prefix) if prefix.endswith("/") else PurePosixPath(prefix).parent
                if str(base) in {"", "."}:
                    raise WorkspaceError(f"landlock_root_glob_forbidden:{pattern}")
        exec_scratch_flags = (
            ["--exec-scratch", str(validation_exec_scratch)]
            if validation_exec_scratch is not None
            else []
        )
        cwd_flags = (
            ["--cwd", resolved_cwd_relative] if resolved_cwd_relative is not None else []
        )
        authority_flags = (
            ["--outer-validation-authority"] if outer_validation_authority else []
        )
        # NF430: authorize exactly the request-owned temp root the launcher
        # already provisioned -- never the whole repository -- so a worker's
        # TMPDIR under ``.aiworkhub/temp/worker/<request_id>`` is writable while
        # the candidate worktree stays confined to allowed_writes.  Gated on the
        # directory actually existing so a request without a provisioned temp
        # root (or an invalid identity) never widens the ruleset.
        worker_temp_flags = _landlock_worker_temp_flags(workspace)
        return [
            sys.executable,
            str(Path(__file__).resolve()),
            "--landlock-exec",
            "--workspace", str(workspace.path),
            "--home", str(workspace.home),
            *(value for pattern in workspace.allowed_writes for value in ("--allow", pattern)),
            *exec_scratch_flags,
            *worker_temp_flags,
            *cwd_flags,
            *authority_flags,
            "--",
            *adapter_argv,
        ]
    if selected != "bubblewrap":
        raise WorkspaceError(f"unsupported_sandbox_backend:{selected}")

    bwrap = Path(os.environ.get(BWRAP_ENV, "/usr/bin/bwrap"))

    sandbox_home = bubblewrap_home_env_value()
    host_home = Path(sandbox_home)
    rewritten = [
        SANDBOX_WORKSPACE if value == str(workspace.path) else value
        for value in adapter_argv
    ]
    validation_binds: list[str] = []
    for index, root in enumerate(validation_executable_roots):
        resolved_root = root.resolve(strict=False)
        alias = f"{SANDBOX_VALIDATION_EXECUTABLE_ROOT}/{index}"
        if index == 0:
            validation_binds.extend(("--dir", SANDBOX_VALIDATION_EXECUTABLE_ROOT))
        validation_binds.extend(("--ro-bind", str(resolved_root), alias))
        rewritten = [
            (
                f"{alias}/{value_path.relative_to(resolved_root).as_posix()}"
                if _path_is_relative_to(value_path, resolved_root)
                else value
            )
            for value in rewritten
            for value_path in (Path(value).resolve(strict=False),)
        ]
    if validation_readonly_dirs:
        validation_binds.extend(("--dir", "/validation-pythonpath"))
        for index, path in enumerate(validation_readonly_dirs):
            validation_binds.extend(
                ("--ro-bind", str(path), f"/validation-pythonpath/{index}")
            )
    if validation_exec_scratch is not None:
        # A writable, request-private bind -- distinct from every read-only
        # bind above -- so a validation command can compile+execute a native
        # binary at a path this exact sandbox invocation was already probed
        # to allow exec() on (see provision_validation_exec_scratch).
        validation_binds.extend(
            ("--bind", str(validation_exec_scratch), SANDBOX_VALIDATION_EXEC_SCRATCH)
        )
    worker_temp_bind = _resolve_existing_worker_temp(workspace)
    if worker_temp_bind is not None:
        # NF430: a writable bind at the exact request-owned temp root, at its
        # real absolute path, so a TMPDIR pointing there resolves inside the
        # bubblewrap mount namespace without widening any other bind.
        validation_binds.extend(
            ("--bind", str(worker_temp_bind), str(worker_temp_bind))
        )
    argv = [
        str(bwrap),
        "--new-session",
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/sbin", "/sbin",
        "--ro-bind", "/etc", "/etc",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--dir", "/home",
        "--bind", str(workspace.home), sandbox_home,
        *validation_binds,
        "--bind", str(workspace.path), SANDBOX_WORKSPACE,
        "--ro-bind", str(workspace.path / ".git"), f"{SANDBOX_WORKSPACE}/.git",
        "--ro-bind", str(workspace.repo), SANDBOX_AUTHORITY_REPO,
    ]
    if package_import_root is not None:
        # A dedicated bind, independent of the SANDBOX_AUTHORITY_REPO bind
        # above: the package import root may live entirely outside
        # workspace.repo (a bundled/installed package), so it cannot be
        # expressed as a path beneath that alias.
        argv.extend(("--ro-bind", str(package_import_root), SANDBOX_PACKAGE_IMPORT_ROOT))
    sandbox_chdir = (
        f"{SANDBOX_WORKSPACE}/{resolved_cwd_relative}"
        if resolved_cwd_relative is not None
        else SANDBOX_WORKSPACE
    )
    argv.extend(("--chdir", sandbox_chdir))
    node_root = _node_install_root(adapter_argv[0])
    if node_root is not None:
        relative = node_root.relative_to(host_home)
        destination = workspace.home / relative
        destination.mkdir(parents=True, exist_ok=True)
        argv.extend(("--ro-bind", str(node_root), str(host_home / relative)))
    argv.extend(("--", *rewritten))
    return argv


def _landlock_supported_mutations(abi: int) -> int:
    rights = _LL_MUTATE_ABI1
    if abi >= 2:
        rights |= _LL_REFER
    if abi >= 3:
        rights |= _LL_TRUNCATE
    return rights


def _landlock_add_path_rule(libc: Any, ruleset_fd: int, path: Path, rights: int) -> None:
    path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        rule = _LandlockPathBeneathAttr(rights, path_fd)
        result = libc.syscall(
            _LANDLOCK_ADD_RULE,
            ruleset_fd,
            _LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(rule),
            0,
        )
        if result < 0:
            error = ctypes.get_errno()
            raise WorkspaceError(f"landlock_add_rule_failed:{path}:{os.strerror(error)}")
    finally:
        os.close(path_fd)


def _apply_landlock(
    workspace: Path,
    home: Path,
    allowed_writes: Iterable[str],
    exec_scratch: Path | None = None,
    worker_temp: Path | None = None,
) -> None:
    workspace = workspace.resolve()
    home = home.resolve()
    abi = landlock_abi_version()
    if abi < 1:
        raise WorkspaceError("landlock_unsupported")
    handled = _landlock_supported_mutations(abi)
    libc = ctypes.CDLL(None, use_errno=True)
    attr = _LandlockRulesetAttr(handled)
    ruleset_fd = libc.syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr),
        ctypes.sizeof(attr),
        0,
    )
    if ruleset_fd < 0:
        error = ctypes.get_errno()
        raise WorkspaceError(f"landlock_create_ruleset_failed:{os.strerror(error)}")
    try:
        _landlock_add_path_rule(libc, ruleset_fd, home, handled)
        if exec_scratch is not None:
            exec_scratch = exec_scratch.resolve()
            if exec_scratch.is_symlink() or not exec_scratch.is_dir():
                raise WorkspaceError(f"landlock_exec_scratch_not_directory:{exec_scratch}")
            _landlock_add_path_rule(libc, ruleset_fd, exec_scratch, handled)
        if worker_temp is not None:
            # NF430: grant full mutation beneath the exact request-owned worker
            # temp root so a worker/pytest TMPDIR there is writable, while the
            # worktree stays restricted to the allowed_writes rules below.
            worker_temp = worker_temp.resolve()
            if worker_temp.is_symlink() or not worker_temp.is_dir():
                raise WorkspaceError(f"landlock_worker_temp_not_directory:{worker_temp}")
            _landlock_add_path_rule(libc, ruleset_fd, worker_temp, handled)
        for device in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom"):
            path = Path(device)
            if path.exists():
                _landlock_add_path_rule(libc, ruleset_fd, path, _LL_WRITE_FILE)
        for raw in allowed_writes:
            pattern = _relative_repo_path(raw)
            if any(ch in pattern for ch in "*?["):
                prefix = _static_prefix(pattern)
                relative_base = (
                    PurePosixPath(prefix)
                    if prefix.endswith("/")
                    else PurePosixPath(prefix).parent
                )
                if str(relative_base) in {"", "."}:
                    raise WorkspaceError(f"landlock_root_glob_forbidden:{pattern}")
                base = workspace / str(relative_base)
                _require_beneath(workspace, base)
                base.mkdir(parents=True, exist_ok=True)
                _landlock_add_path_rule(libc, ruleset_fd, base, handled)
                continue
            target = workspace / pattern
            _require_beneath(workspace, target)
            if not target.is_file() or target.is_symlink():
                raise WorkspaceError(f"landlock_target_not_regular:{pattern}")
            file_rights = _LL_WRITE_FILE | (_LL_TRUNCATE if abi >= 3 else 0)
            _landlock_add_path_rule(libc, ruleset_fd, target, file_rights)
            # Nested output directories may use atomic temp-file replacement.
            # The repository root is deliberately never granted directory
            # mutation rights, preserving the detached worktree's .git file.
            if target.parent != workspace:
                _landlock_add_path_rule(libc, ruleset_fd, target.parent, handled)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise WorkspaceError(f"landlock_no_new_privs_failed:{os.strerror(error)}")
        if libc.syscall(_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) != 0:
            error = ctypes.get_errno()
            raise WorkspaceError(f"landlock_restrict_self_failed:{os.strerror(error)}")
    finally:
        os.close(ruleset_fd)


_SCMP_ACT_NOTIFY = 0x7FC00000
_METADATA_BROKER_SYSCALLS = ("chmod", "fchmod", "fchmodat", "fchmodat2")
_METADATA_BROKER_POLL_MS = 200
_METADATA_BROKER_HANDSHAKE_SECONDS = 30.0
_METADATA_BROKER_PATH_LIMIT = 4096
_AT_FDCWD = -100
# Defensive upper bound on the whole broker loop so a wedged poll/waitpid can
# never hang the trusted parent; the outer subprocess timeout is the primary
# bound, this is the belt-and-braces backstop that kills the validator group.
_METADATA_BROKER_DEADLINE_SECONDS = MAX_VALIDATION_SECONDS
# ``openat2(2)`` (Linux 5.6+) with RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS is the
# sole target-acquisition authority: the kernel re-resolves every component
# against a stable scratch directory fd and refuses any symlink or any escape
# beneath it, so authority is never a string-prefix comparison the caller
# performs by hand. The syscall number is identical across the Linux
# architectures this deployment runs on.
_OPENAT2_SYSCALL = 437
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08
_PR_SET_PDEATHSIG = 1
# libseccomp API level 5 is the first that exposes SCMP_ACT_NOTIFY and the
# notification APIs; ``seccomp_api_get``
# probes the running kernel, so this is a real capability check rather than a
# libseccomp symbol-presence guess.
_SECCOMP_NOTIFY_MIN_API = 5
_SECCOMP_NOTIFY_RUNTIME_SUPPORTED: bool | None = None


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


def _openat2_beneath(dir_fd: int, relative: str, flags: int) -> int:
    """Open ``relative`` strictly beneath ``dir_fd`` with no symlink traversal.

    Returns a new file descriptor (the caller owns it) or raises
    ``WorkspaceError``. RESOLVE_BENEATH rejects any ``..``/escape and
    RESOLVE_NO_SYMLINKS rejects a symlinked component or final target, so a
    swapped path or symlink root/target fails closed inside the kernel rather
    than being validated by a fragile userspace string check.
    """
    if relative.startswith("/") or ".." in PurePosixPath(relative).parts:
        raise WorkspaceError(f"metadata_broker_unsafe_relative:{relative}")
    libc = ctypes.CDLL(None, use_errno=True)
    how = _OpenHow(
        flags=ctypes.c_uint64(flags),
        mode=ctypes.c_uint64(0),
        resolve=ctypes.c_uint64(_RESOLVE_BENEATH | _RESOLVE_NO_SYMLINKS),
    )
    ctypes.set_errno(0)
    fd = libc.syscall(
        _OPENAT2_SYSCALL,
        ctypes.c_int(dir_fd),
        ctypes.c_char_p(relative.encode("utf-8")),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if fd < 0:
        error = ctypes.get_errno()
        raise WorkspaceError(
            f"metadata_broker_openat2_failed:{relative}:{os.strerror(error)}"
        )
    return int(fd)


def _openat2_available() -> bool:
    """Real kernel probe: can ``openat2(2)`` open the filesystem root?"""
    if sys.platform != "linux":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    how = _OpenHow(
        flags=ctypes.c_uint64(os.O_RDONLY | os.O_DIRECTORY),
        mode=ctypes.c_uint64(0),
        resolve=ctypes.c_uint64(0),
    )
    try:
        fd = libc.syscall(
            _OPENAT2_SYSCALL,
            ctypes.c_int(_AT_FDCWD),
            ctypes.c_char_p(b"/"),
            ctypes.byref(how),
            ctypes.c_size_t(ctypes.sizeof(how)),
        )
    except OSError:
        return False
    if fd < 0:
        return False
    os.close(int(fd))
    return True


class _SeccompData(ctypes.Structure):
    _fields_ = [
        ("nr", ctypes.c_int32),
        ("arch", ctypes.c_uint32),
        ("instruction_pointer", ctypes.c_uint64),
        ("args", ctypes.c_uint64 * 6),
    ]


class _SeccompNotif(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint64),
        ("pid", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("data", _SeccompData),
    ]


class _SeccompNotifResp(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint64),
        ("val", ctypes.c_int64),
        ("error", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
    ]


def _seccomp_notify_library() -> Any | None:
    """Return libseccomp bound for user-notification use, or ``None``.

    ``None`` means this host genuinely lacks seccomp user notification (old
    libseccomp or old kernel); it never means "skip the confinement" -- the
    caller falls back to the strict deny-only metadata filter.
    """
    library = _seccomp_library()
    if library is None:
        return None
    try:
        library.seccomp_notify_alloc.argtypes = [
            ctypes.POINTER(ctypes.POINTER(_SeccompNotif)),
            ctypes.POINTER(ctypes.POINTER(_SeccompNotifResp)),
        ]
        library.seccomp_notify_alloc.restype = ctypes.c_int
        library.seccomp_notify_free.argtypes = [
            ctypes.POINTER(_SeccompNotif),
            ctypes.POINTER(_SeccompNotifResp),
        ]
        library.seccomp_notify_free.restype = None
        library.seccomp_notify_receive.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(_SeccompNotif),
        ]
        library.seccomp_notify_receive.restype = ctypes.c_int
        library.seccomp_notify_respond.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(_SeccompNotifResp),
        ]
        library.seccomp_notify_respond.restype = ctypes.c_int
        library.seccomp_notify_id_valid.argtypes = [ctypes.c_int, ctypes.c_uint64]
        library.seccomp_notify_id_valid.restype = ctypes.c_int
        library.seccomp_notify_fd.argtypes = [ctypes.c_void_p]
        library.seccomp_notify_fd.restype = ctypes.c_int
    except AttributeError:
        return None
    return library


def _seccomp_kernel_notify_api() -> bool:
    """Query the running kernel's seccomp API level via libseccomp.

    ``seccomp_api_get`` probes the live kernel (not just the library build),
    so a level below the user-notification listener threshold means this host
    genuinely cannot broker metadata syscalls -- a legitimate capability skip,
    never a silent confinement bypass.
    """
    library = _seccomp_library()
    if library is None:
        return False
    try:
        library.seccomp_api_get.argtypes = []
        library.seccomp_api_get.restype = ctypes.c_uint
    except AttributeError:
        return False
    try:
        level = int(library.seccomp_api_get())
    except OSError:
        return False
    return level >= _SECCOMP_NOTIFY_MIN_API


def _seccomp_notify_supported() -> bool:
    """True only when the kernel really supports the broker's whole mechanism.

    Requires the libseccomp notification symbols, a kernel API level that
    negotiates the user-notification listener, and ``openat2`` for
    stable-fd target resolution. Any missing piece returns ``False`` so the
    caller falls back to the strict deny-only filter and the integration test
    skips as a genuine host-capability gap.
    """
    if _seccomp_notify_library() is None:
        return False
    if not _seccomp_kernel_notify_api():
        return False
    if not _openat2_available():
        return False
    return _seccomp_notify_runtime_supported()


def _metadata_broker_verify_mode(mode: int) -> int:
    """Return safe permission bits from either chmod bits or full st_mode.

    ``Path.chmod(path.stat().st_mode)`` includes the file-type bits that the
    native syscall ignores.  Strip only those recognised type bits while
    continuing to reject setuid, setgid, sticky, negative, and unknown bits.
    """
    if mode < 0:
        raise WorkspaceError(f"metadata_broker_unsafe_mode:{mode:o}")
    permission_bits = mode & ~stat.S_IFMT(mode)
    if permission_bits & ~0o777:
        raise WorkspaceError(f"metadata_broker_unsafe_mode:{mode:o}")
    return permission_bits


def _metadata_broker_verify_flags(flags: int) -> int:
    if flags != 0:
        raise WorkspaceError(f"metadata_broker_unsupported_flags:{flags}")
    return flags


def _metadata_broker_verify_fd(
    fd: int, candidate: str, requested_mode: int | None = None
) -> bool:
    """Fail closed unless ``fd`` is a scratch-owned target beneath the request scratch.

    Owned directories are permitted so validators can manage scratch-directory
    metadata (e.g. ``os.chmod(parent, 0o700)`` after ``path.parent.mkdir``).
    Regular files still require ``st_nlink == 1`` (no hardlinks) UNLESS
    ``requested_mode`` is given and already equals the file's current
    permission bits exactly -- that one metadata no-op is accepted so a
    validator that redundantly re-chmods a hardlinked file to its own mode
    (e.g. a nested Git/pytest ``config.lock``) is not spuriously denied, while
    any actual requested mode change against a hardlink stays denied. Special
    files (devices, sockets, FIFOs) remain denied.

    Returns ``True`` when the caller should perform the mutation, ``False``
    when the fd is verified but the mutation is an already-satisfied hardlink
    no-op that must be skipped entirely (no ``fchmod`` call at all, so no
    ctime bump is ever visible through the file's other hardlinked names).
    """
    info = os.fstat(fd)
    if stat.S_ISDIR(info.st_mode):
        if info.st_uid != os.getuid():
            raise WorkspaceError(f"metadata_broker_foreign_owner:{candidate}")
        return True
    if not stat.S_ISREG(info.st_mode):
        raise WorkspaceError(f"metadata_broker_not_regular_file:{candidate}")
    if info.st_nlink != 1:
        if info.st_uid != os.getuid():
            raise WorkspaceError(f"metadata_broker_foreign_owner:{candidate}")
        if requested_mode is None or stat.S_IMODE(info.st_mode) != requested_mode:
            raise WorkspaceError(f"metadata_broker_hardlink_forbidden:{candidate}")
        return False
    if info.st_uid != os.getuid():
        raise WorkspaceError(f"metadata_broker_foreign_owner:{candidate}")
    return True


def _metadata_broker_verify_target(
    candidate: str,
    scratch_fd: int,
    scratch_root: PurePosixPath,
    requested_mode: int | None = None,
) -> "tuple[int, bool]":
    """Open and return a verified, mutable fd strictly beneath the scratch.

    ``scratch_fd`` is a stable directory descriptor for the exact request-owned
    validation exec scratch and ``scratch_root`` its resolved absolute path
    (used only to derive the beneath-scratch relative component). The kernel is
    the resolution authority: ``openat2`` with RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS
    fails closed on traversal, absolute/outside paths, symlinked roots and
    symlink targets -- never a userspace string-prefix comparison. The returned
    descriptor passes ``_metadata_broker_verify_fd`` (owned directories are
    permitted; regular files require ``st_nlink == 1`` unless ``requested_mode``
    is an exact permission-bit no-op); the caller mutates that exact fd -- only
    when the paired ``bool`` is ``True`` -- and closes it.

    For directories the first ``openat2`` with ``O_RDONLY|O_NOCTTY`` yields an
    O_PATH descriptor that cannot be ``fchmod``'d.  A second ``openat2`` with
    ``O_DIRECTORY`` opens the same already-validated relative path; inode
    comparison defeats any TOCTOU swap between the two kernel resolutions.
    """
    if not candidate or not candidate.startswith("/"):
        raise WorkspaceError(f"metadata_broker_path_not_absolute:{candidate}")
    target = PurePosixPath(candidate)
    if ".." in target.parts:
        raise WorkspaceError(f"metadata_broker_traversal_forbidden:{candidate}")
    try:
        relative = target.relative_to(scratch_root)
    except ValueError as exc:
        raise WorkspaceError(f"metadata_broker_outside_scratch:{candidate}") from exc
    rel_str = relative.as_posix()
    if rel_str in ("", "."):
        raise WorkspaceError(f"metadata_broker_scratch_root_target:{candidate}")
    fd = _openat2_beneath(scratch_fd, rel_str, os.O_RDONLY | os.O_NOCTTY)
    try:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            # fd is O_PATH for directories -- reopen the validated relative
            # path with O_DIRECTORY so the returned descriptor is mutable.
            # Drop the closed descriptor first so no failure branch ever
            # closes the same fd twice (a double close raises EBADF and
            # would mask the real denial error).
            os.close(fd)
            fd = -1
            fd = _openat2_beneath(
                scratch_fd, rel_str, os.O_RDONLY | os.O_NOCTTY | os.O_DIRECTORY
            )
            reopen_info = os.fstat(fd)
            if (reopen_info.st_dev, reopen_info.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise WorkspaceError(
                    f"metadata_broker_directory_inode_drift:{candidate}"
                )
            mutate = _metadata_broker_verify_fd(fd, candidate, requested_mode)
            return fd, mutate
        mutate = _metadata_broker_verify_fd(fd, candidate, requested_mode)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise
    return fd, mutate


def _metadata_broker_verify_target_any(
    candidate: str,
    scratch_specs: "list[tuple[int, PurePosixPath]]",
    requested_mode: int | None = None,
) -> "tuple[int, bool]":
    """Open and return a verified fd beneath the first authorized scratch root
    that actually contains ``candidate``.

    NF430: a validation run owns more than one request-scoped writable root --
    the exec scratch and the ``.aiworkhub/temp/worker/<request_id>`` temp
    authority the launcher provisioned -- and a worker's TMPDIR points at the
    latter, so a nested Git/pytest ``config.lock`` chmod lands there.  Each
    ``(scratch_fd, scratch_root)`` is tried with the identical kernel-authoritative
    ``_metadata_broker_verify_target`` (``openat2`` RESOLVE_BENEATH|
    RESOLVE_NO_SYMLINKS, owner, ``st_nlink``, inode-stability): a target merely
    *outside* one root falls through to the next, while any *beneath-but-invalid*
    result (traversal, symlink, hardlink, foreign owner, root-itself) still
    denies fail-closed.  A target beneath none of the authorized roots is
    rejected -- authority is never widened to the repository or an arbitrary
    path.

    Returns ``(fd, mutate)``: ``mutate`` is ``False`` only for the accepted
    hardlink permission-bit no-op (see ``_metadata_broker_verify_fd``); the
    caller must skip the actual ``fchmod`` in that case.
    """
    outside: WorkspaceError | None = None
    for scratch_fd, scratch_root in scratch_specs:
        try:
            return _metadata_broker_verify_target(
                candidate, scratch_fd, scratch_root, requested_mode
            )
        except WorkspaceError as exc:
            if str(exc).startswith("metadata_broker_outside_scratch"):
                outside = exc
                continue
            raise
    raise outside or WorkspaceError(f"metadata_broker_outside_scratch:{candidate}")


def _metadata_broker_process_stat_fields(pid: int) -> list[bytes]:
    """Return the ``/proc/<pid>/stat`` fields after the ``comm`` field.

    The ``comm`` field can contain spaces and parentheses, so the fields after
    the final ``)`` are used: ``state ppid pgrp ...``.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise WorkspaceError(f"metadata_broker_pid_unavailable:{pid}:{exc}") from exc
    close_paren = data.rfind(b")")
    if close_paren < 0:
        raise WorkspaceError(f"metadata_broker_stat_malformed:{pid}")
    fields = data[close_paren + 1:].split()
    if len(fields) < 3:
        raise WorkspaceError(f"metadata_broker_stat_malformed:{pid}")
    return fields


def _metadata_broker_process_pgid(pid: int) -> int:
    """Return ``pid``'s process-group id (``pgrp``, the third stat field)."""
    fields = _metadata_broker_process_stat_fields(pid)
    try:
        return int(fields[2])
    except ValueError as exc:
        raise WorkspaceError(f"metadata_broker_stat_malformed:{pid}") from exc


def _metadata_broker_process_ppid(pid: int) -> int:
    """Return ``pid``'s live parent pid (``ppid``, the second stat field)."""
    fields = _metadata_broker_process_stat_fields(pid)
    try:
        return int(fields[1])
    except ValueError as exc:
        raise WorkspaceError(f"metadata_broker_stat_malformed:{pid}") from exc


_METADATA_BROKER_MAX_ANCESTRY_DEPTH = 32


def _metadata_broker_ppid_ancestry_reaches(pid: int, child_pid: int) -> bool:
    """Walk ``pid``'s live PPID chain, bounded, for the exact ``child_pid``.

    A descendant that itself calls ``setsid`` gets a fresh ``pgid`` (its own
    pid), so the pgid fast path in ``_metadata_broker_authenticate_pid`` no
    longer matches it even though it is a legitimate nested validator process
    (e.g. a nested ``git``/``pytest`` subprocess). Its live PPID chain still
    leads back to the broker child as long as it was never reparented.
    Reparenting (a dead intermediate ancestor reaped by init/a subreaper), a
    cycle, a malformed ``/proc`` entry or exceeding the bounded depth all fail
    closed (``False``) rather than trusting anything but the kernel's current,
    live ancestry.
    """
    seen: set[int] = set()
    current = pid
    for _ in range(_METADATA_BROKER_MAX_ANCESTRY_DEPTH):
        if current in seen:
            return False
        seen.add(current)
        try:
            ppid = _metadata_broker_process_ppid(current)
        except WorkspaceError:
            return False
        if ppid == child_pid:
            return True
        if ppid <= 1:
            return False
        current = ppid
    return False


def _metadata_broker_authenticate_pid(pid: int, child_pid: int) -> None:
    """Accept the broker child, its process group, or a bounded PPID descendant.

    The disposable child calls ``setsid`` before ``exec``, so most legitimate
    validator descendants share ``pgid == child_pid`` and no unrelated process
    can join that freshly created session (the fast path). A nested descendant
    that itself calls ``setsid`` breaks that fast path, so it is additionally
    accepted when its bounded, live PPID ancestry chain reaches the exact
    ``child_pid`` -- never merely because it is alive or owned by the same
    uid. Re-read on every request so a dead, reused, reparented or foreign pid
    is rejected fail-closed.
    """
    if pid <= 0:
        raise WorkspaceError(f"metadata_broker_bad_pid:{pid}")
    if pid == child_pid:
        return
    if _metadata_broker_process_pgid(pid) == child_pid:
        return
    if _metadata_broker_ppid_ancestry_reaches(pid, child_pid):
        return
    raise WorkspaceError(f"metadata_broker_foreign_pid:{pid}")


def _kill_validator_group(child_pid: int) -> None:
    """Best-effort SIGKILL of the whole validator process group, then the leader."""
    import signal

    try:
        os.killpg(child_pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass


def _reap_validator(child_pid: int) -> None:
    """Best-effort reap of the broker child so it can never become a zombie."""
    try:
        os.waitpid(child_pid, 0)
    except OSError:
        pass


def _verify_broker_parent_identity(expected_parent_pid: int) -> None:
    # If the parent exited after fork but before prctl, the kernel could not
    # deliver the configured signal for that already-completed transition.
    # Fail before exec when the identity no longer matches; after this check,
    # the armed PDEATHSIG covers every subsequent parent exit.
    if os.getppid() != expected_parent_pid:
        raise WorkspaceError("metadata_broker_parent_identity_changed")


def _set_pdeathsig_sigkill(expected_parent_pid: int) -> None:
    """Arm parent-death SIGKILL and close the fork/prctl identity race."""
    libc = ctypes.CDLL(None, use_errno=True)
    import signal

    ctypes.set_errno(0)
    result = libc.prctl(_PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)
    if result != 0:
        error = ctypes.get_errno()
        raise WorkspaceError(f"metadata_broker_pdeathsig_failed:{os.strerror(error)}")
    _verify_broker_parent_identity(expected_parent_pid)


def _read_child_cstring(pid: int, address: int) -> str:
    if address <= 0:
        raise WorkspaceError("metadata_broker_null_path_pointer")
    try:
        handle = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
    except OSError as exc:
        raise WorkspaceError(f"metadata_broker_child_memory_unavailable:{exc}") from exc
    try:
        chunk = os.pread(handle, _METADATA_BROKER_PATH_LIMIT, address)
    except OSError as exc:
        raise WorkspaceError(f"metadata_broker_child_memory_read_failed:{exc}") from exc
    finally:
        os.close(handle)
    end = chunk.find(b"\x00")
    if end < 0:
        raise WorkspaceError("metadata_broker_path_unterminated")
    try:
        return chunk[:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceError("metadata_broker_path_not_utf8") from exc


def _metadata_broker_child_link(pid: int, name: str) -> str:
    try:
        return os.readlink(f"/proc/{pid}/{name}")
    except OSError as exc:
        raise WorkspaceError(f"metadata_broker_child_link_unavailable:{exc}") from exc


def _metadata_broker_abs_path(pid: int, dirfd: int, raw: str) -> str:
    if raw.startswith("/"):
        return raw
    if not raw:
        raise WorkspaceError("metadata_broker_empty_path")
    if dirfd == _AT_FDCWD:
        base = _metadata_broker_child_link(pid, "cwd")
    elif dirfd >= 0:
        base = _metadata_broker_child_link(pid, f"fd/{dirfd}")
    else:
        raise WorkspaceError(f"metadata_broker_bad_dirfd:{dirfd}")
    if base.endswith(" (deleted)"):
        raise WorkspaceError("metadata_broker_deleted_base")
    return os.path.join(base, raw)


def _metadata_broker_syscall_names(library: Any) -> dict[int, str]:
    table: dict[int, str] = {}
    for name in _METADATA_BROKER_SYSCALLS:
        number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
        if number >= 0:
            table[int(number)] = name
    return table


def _install_metadata_notify_filter() -> int:
    """Install the disposable child's user-notification metadata filter.

    Metadata syscalls are never globally allowed and no continue flag is
    used: the brokered family traps to the trusted parent, every other
    denied metadata syscall still returns EPERM in-kernel.
    """
    library = _seccomp_notify_library()
    if library is None:
        raise WorkspaceError("seccomp_user_notification_unavailable")
    context = library.seccomp_init(_SCMP_ACT_ALLOW)
    if not context:
        raise WorkspaceError("seccomp_init_failed")
    listener = -1
    try:
        notified = 0
        for name in _METADATA_BROKER_SYSCALLS:
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                continue
            if library.seccomp_rule_add(context, _SCMP_ACT_NOTIFY, number, 0) == 0:
                notified += 1
        if notified == 0:
            raise WorkspaceError("seccomp_notify_rules_unavailable")
        action = _SCMP_ACT_ERRNO | errno.EPERM
        for name in _SECCOMP_DENIED_SYSCALLS:
            if name in _METADATA_BROKER_SYSCALLS:
                continue
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                continue
            result = library.seccomp_rule_add(context, action, number, 0)
            if result != 0:
                raise WorkspaceError(f"seccomp_rule_failed:{name}:{-result}")
        result = library.seccomp_load(context)
        if result != 0:
            raise WorkspaceError(f"seccomp_load_failed:{-result}")
        raw_fd = library.seccomp_notify_fd(context)
        if raw_fd < 0:
            raise WorkspaceError("seccomp_notify_fd_failed")
        listener = os.dup(raw_fd)
        return listener
    finally:
        library.seccomp_release(context)


def _metadata_broker_check_notification(
    library: Any, listener_fd: int, notif_id: int
) -> None:
    if library.seccomp_notify_id_valid(listener_fd, notif_id) != 0:
        raise WorkspaceError("metadata_broker_notification_stale")


def _metadata_broker_apply(
    library: Any,
    listener_fd: int,
    request: _SeccompNotif,
    child_pid: int,
    scratch_specs: "list[tuple[int, PurePosixPath]] | int",
    legacy_scratch_root: PurePosixPath | None = None,
) -> None:
    """Emulate exactly one verified brokered metadata syscall in the parent.

    Authenticates the notifying pid as a live descendant in the broker child's
    process group (validators legitimately fork -- e.g. ``git`` -- so the caller
    is often not ``child_pid`` itself), re-checks the notification id both
    before and after acquiring the target, resolves the target with the kernel
    as authority (``openat2`` beneath one of the exact authorized scratch fds --
    the exec scratch and the request-owned worker temp authority) and mutates
    only a stable, inode-verified file or directory.
    """
    if legacy_scratch_root is not None:
        # Preserve the established private test/caller ABI while normalizing it
        # into the same multi-root authority used by the live broker path.
        scratch_specs = [(int(scratch_specs), legacy_scratch_root)]
    if not isinstance(scratch_specs, list):
        raise WorkspaceError("metadata_broker_scratch_authority_invalid")

    pid = int(request.pid)
    _metadata_broker_authenticate_pid(pid, child_pid)
    names = _metadata_broker_syscall_names(library)
    name = names.get(int(request.data.nr))
    if name is None:
        raise WorkspaceError(f"metadata_broker_unsupported_syscall:{request.data.nr}")
    args = request.data.args

    if name == "fchmod":
        mode = _metadata_broker_verify_mode(int(args[1]))
        raw_fd = ctypes.c_int32(int(args[0]) & 0xFFFFFFFF).value
        if raw_fd < 0:
            raise WorkspaceError(f"metadata_broker_bad_fd:{raw_fd}")
        _metadata_broker_check_notification(library, listener_fd, request.id)
        link = _metadata_broker_child_link(pid, f"fd/{raw_fd}")
        if link.endswith(" (deleted)"):
            raise WorkspaceError("metadata_broker_deleted_fd")
        verified_fd, _verified_mutate = _metadata_broker_verify_target_any(
            link, scratch_specs, mode
        )
        try:
            verified_info = os.fstat(verified_fd)
            # verified_fd is kernel-resolved via openat2; for directories it
            # may be an O_PATH descriptor that cannot be fchmod'd directly.
            # Stat it first so the proc reopen can include O_DIRECTORY for
            # directory targets -- open() on a directory symlink target
            # without O_DIRECTORY returns EISDIR.
            open_flags = os.O_RDONLY | os.O_NOCTTY
            if stat.S_ISDIR(verified_info.st_mode):
                open_flags |= os.O_DIRECTORY
            try:
                fd_target = os.open(
                    f"/proc/{pid}/fd/{raw_fd}", open_flags
                )
            except OSError as exc:
                raise WorkspaceError(
                    f"metadata_broker_fd_reopen_failed:{exc}"
                ) from exc
            try:
                target_info = os.fstat(fd_target)
                if (target_info.st_dev, target_info.st_ino) != (
                    verified_info.st_dev,
                    verified_info.st_ino,
                ):
                    raise WorkspaceError("metadata_broker_fd_inode_drift")
                mutate = _metadata_broker_verify_fd(
                    fd_target, f"/proc/{pid}/fd/{raw_fd}", mode
                )
                _metadata_broker_check_notification(library, listener_fd, request.id)
                if mutate:
                    # Mutate the exact descriptor the child blocked on, proven
                    # by inode identity to be the same beneath-scratch file --
                    # never a readlink+reopen-by-name that a swap could race.
                    os.fchmod(fd_target, mode)
            finally:
                os.close(fd_target)
        finally:
            os.close(verified_fd)
        return

    if name == "chmod":
        mode = _metadata_broker_verify_mode(int(args[1]))
        raw_target = _metadata_broker_abs_path(
            pid, _AT_FDCWD, _read_child_cstring(pid, int(args[0]))
        )
    elif name == "fchmodat":
        # The classic ``fchmodat`` syscall is (dirfd, path, mode) -- it has NO
        # flags argument, so args[3] is undefined register content and must not
        # be read or validated here.
        mode = _metadata_broker_verify_mode(int(args[2]))
        raw_target = _metadata_broker_abs_path(
            pid,
            ctypes.c_int32(int(args[0]) & 0xFFFFFFFF).value,
            _read_child_cstring(pid, int(args[1])),
        )
    elif name == "fchmodat2":
        # Only ``fchmodat2`` carries a flags argument (args[3]); require 0 so an
        # AT_SYMLINK_NOFOLLOW / other variant is denied fail-closed.
        mode = _metadata_broker_verify_mode(int(args[2]))
        _metadata_broker_verify_flags(int(args[3]))
        raw_target = _metadata_broker_abs_path(
            pid,
            ctypes.c_int32(int(args[0]) & 0xFFFFFFFF).value,
            _read_child_cstring(pid, int(args[1])),
        )
    else:
        raise WorkspaceError(f"metadata_broker_unsupported_syscall:{name}")

    _metadata_broker_check_notification(library, listener_fd, request.id)
    verified_fd, mutate = _metadata_broker_verify_target_any(
        raw_target, scratch_specs, mode
    )
    try:
        _metadata_broker_check_notification(library, listener_fd, request.id)
        if mutate:
            os.fchmod(verified_fd, mode)
    finally:
        os.close(verified_fd)


def _metadata_broker_exit_code(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def _metadata_broker_denial_reason(exc: BaseException) -> str:
    """Return a bounded, path-free reason code for a broker denial (NF430).

    Every ``WorkspaceError`` the broker raises is ``reason:detail`` where the
    ``detail`` half can embed the target path; only the stable ``reason``
    prefix is kept.  An ``OSError`` collapses to its errno name.  The result is
    an ASCII token -- never a path, fd content, mode, pid, or any credential --
    so denial telemetry can be retained without leaking secrets.
    """
    if isinstance(exc, WorkspaceError):
        return str(exc).split(":", 1)[0] or "metadata_broker_workspace_error"
    if isinstance(exc, OSError) and exc.errno is not None:
        return f"oserror_{errno.errorcode.get(exc.errno, exc.errno)}"
    return type(exc).__name__


def _record_metadata_broker_denial(exc: BaseException, request: _SeccompNotif) -> None:
    """Retain one bounded, structured, path-free denial record (NF430).

    Replaces the previous silent collapse of every ``WorkspaceError``/
    ``OSError``/``ValueError`` into a generic ``EPERM``: the exact rejection
    reason and the decoded syscall number are written as a single capped line
    to the trusted parent's stderr, which flows into the validator's captured
    ``stderr_tail`` so a real broker rejection is diagnosable instead of
    invisible.  Best-effort and never raises -- telemetry must not perturb the
    fail-closed ``EPERM`` response already set by the caller.
    """
    try:
        reason = _metadata_broker_denial_reason(exc)
        line = (
            f"metadata_broker_denied reason={reason} "
            f"syscall_nr={int(request.data.nr)}\n"
        )
        os.write(2, line.encode("ascii", "replace")[:256])
    except BaseException:  # noqa: BLE001 - telemetry is strictly best-effort
        pass


def _open_broker_scratch_root(scratch: Path) -> "tuple[int, PurePosixPath]":
    """Open one stable, owner-verified directory fd for an authorized root.

    Opens ``scratch`` without following its final component (a symlinked root
    would otherwise become authority for its target), confirms it is a
    directory owned by the current uid, and cross-checks its resolved identity
    by ``(st_dev, st_ino)`` so a swapped root fails closed.  The returned fd is
    the single kernel-authoritative resolution anchor handed to every
    ``openat2`` acquisition beneath this root; the caller owns and closes it.
    """
    try:
        scratch_dir_fd = os.open(
            scratch,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as exc:
        raise WorkspaceError(f"metadata_broker_scratch_unavailable:{exc}") from exc
    try:
        dir_info = os.fstat(scratch_dir_fd)
        if not stat.S_ISDIR(dir_info.st_mode) or dir_info.st_uid != os.getuid():
            raise WorkspaceError("metadata_broker_scratch_untrusted")
        try:
            scratch_root = Path(f"/proc/self/fd/{scratch_dir_fd}").resolve(strict=True)
            resolved_info = os.stat(scratch_root, follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceError(
                f"metadata_broker_scratch_identity_unavailable:{exc}"
            ) from exc
        if (resolved_info.st_dev, resolved_info.st_ino) != (
            dir_info.st_dev,
            dir_info.st_ino,
        ):
            raise WorkspaceError("metadata_broker_scratch_identity_changed")
    except BaseException:
        os.close(scratch_dir_fd)
        raise
    return scratch_dir_fd, PurePosixPath(str(scratch_root))


def _run_metadata_broker(
    listener_fd: int,
    child_pid: int,
    scratch: Path,
    extra_roots: "Iterable[Path]" = (),
) -> int:
    """Trusted parent loop: emulate only verified brokered operations.

    Opens one stable directory fd for the exact exec scratch and for each
    additional authorized request-owned root (NF430: the
    ``.aiworkhub/temp/worker/<request_id>`` temp authority a worker's TMPDIR
    points at), the single resolution roots handed to every ``openat2``
    acquisition; polls with a bounded timeout, reaps the child non-blockingly,
    and enforces an overall deadline. On any deadline or error the entire
    validator process group is killed and the child reaped, so a wedged
    validator can never deadlock the broker, orphan descendants, or leak
    descriptors; every allocated notification pair is freed on every path.
    """
    import select

    library = _seccomp_notify_library()
    if library is None:
        raise WorkspaceError("seccomp_user_notification_unavailable")
    scratch_specs: list[tuple[int, PurePosixPath]] = []
    try:
        # The exec scratch is the mandatory anchor -- a failure to open it is a
        # real fault. Every additional root goes through the identical stable-fd
        # + owner + identity verification; a missing or foreign extra root is
        # skipped fail-closed rather than widening the broker's authority.
        scratch_specs.append(_open_broker_scratch_root(scratch))
        for extra in extra_roots:
            try:
                scratch_specs.append(_open_broker_scratch_root(Path(extra)))
            except WorkspaceError:
                continue
        deadline = time.monotonic() + _METADATA_BROKER_DEADLINE_SECONDS
        poller = select.poll()
        poller.register(listener_fd, select.POLLIN)
        while True:
            reaped, status = os.waitpid(child_pid, os.WNOHANG)
            if reaped == child_pid:
                return _metadata_broker_exit_code(status)
            if time.monotonic() > deadline:
                raise WorkspaceError("metadata_broker_deadline_exceeded")
            try:
                events = poller.poll(_METADATA_BROKER_POLL_MS)
            except InterruptedError:
                continue
            if not events:
                continue
            event_mask = 0
            for _event_fd, mask in events:
                event_mask |= mask
            if event_mask & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                # Listener teardown and child exit are separate kernel events;
                # allow the leader a short bounded grace to become waitable so
                # its real exit status is not replaced by a broker error.
                for _attempt in range(50):
                    reaped, status = os.waitpid(child_pid, os.WNOHANG)
                    if reaped == child_pid:
                        return _metadata_broker_exit_code(status)
                    time.sleep(0.01)
                raise WorkspaceError("metadata_broker_listener_closed")
            if not event_mask & select.POLLIN:
                continue
            request_ptr = ctypes.POINTER(_SeccompNotif)()
            response_ptr = ctypes.POINTER(_SeccompNotifResp)()
            if (
                library.seccomp_notify_alloc(
                    ctypes.byref(request_ptr), ctypes.byref(response_ptr)
                )
                != 0
            ):
                raise WorkspaceError("seccomp_notify_alloc_failed")
            try:
                if library.seccomp_notify_receive(listener_fd, request_ptr) != 0:
                    continue
                request = request_ptr.contents
                response = response_ptr.contents
                response.id = request.id
                response.flags = 0
                try:
                    _metadata_broker_apply(
                        library,
                        listener_fd,
                        request,
                        child_pid,
                        scratch_specs,
                    )
                    response.val = 0
                    response.error = 0
                except (WorkspaceError, OSError, ValueError) as exc:
                    # Fail closed with EPERM, but never swallow the cause: retain
                    # a bounded, path-free denial record so a real rejection is
                    # diagnosable instead of an opaque generic EPERM (NF430).
                    response.val = 0
                    response.error = -errno.EPERM
                    _record_metadata_broker_denial(exc, request)
                if library.seccomp_notify_respond(listener_fd, response_ptr) != 0:
                    # A respond failure for a no-longer-valid notification (the
                    # caller died or was killed mid-request) is benign; a
                    # failure while the id is still live is a real fault.
                    if (
                        library.seccomp_notify_id_valid(listener_fd, request.id)
                        == 0
                    ):
                        raise WorkspaceError("metadata_broker_respond_failed")
            finally:
                library.seccomp_notify_free(request_ptr, response_ptr)
    except BaseException:
        _kill_validator_group(child_pid)
        _reap_validator(child_pid)
        raise
    finally:
        for spec_fd, _spec_root in scratch_specs:
            os.close(spec_fd)


def _apply_metadata_seccomp() -> None:
    library = _seccomp_library()
    if library is None:
        raise WorkspaceError("seccomp_unavailable")
    context = library.seccomp_init(_SCMP_ACT_ALLOW)
    if not context:
        raise WorkspaceError("seccomp_init_failed")
    try:
        action = _SCMP_ACT_ERRNO | errno.EPERM
        for name in _SECCOMP_DENIED_SYSCALLS:
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                continue
            result = library.seccomp_rule_add(context, action, number, 0)
            if result != 0:
                raise WorkspaceError(f"seccomp_rule_failed:{name}:{-result}")
        result = library.seccomp_load(context)
        if result != 0:
            raise WorkspaceError(f"seccomp_load_failed:{-result}")
    finally:
        library.seccomp_release(context)


def _metadata_broker_handshake_send(sock: Any, listener_fd: int) -> None:
    """Deliver the notification listener from the disposable child to the parent.

    One atomic ``SCM_RIGHTS`` transfer (``b"1"`` plus the listener descriptor).
    Every blocking operation is bounded by a socket timeout so a wedged or
    slow-to-drain parent can never hold the child before ``exec``.
    """
    import socket

    sock.settimeout(_METADATA_BROKER_HANDSHAKE_SECONDS)
    socket.send_fds(sock, [b"1"], [listener_fd])


def _metadata_broker_handshake_error(sock: Any, diagnostic: bytes) -> None:
    """Best-effort, bounded child error report (data-only, never carries an fd)."""
    try:
        sock.settimeout(_METADATA_BROKER_HANDSHAKE_SECONDS)
        sock.sendall(b"E" + diagnostic)
    except OSError:
        pass


def _metadata_broker_handshake_receive(sock: Any, deadline: float) -> tuple[int, str]:
    """Bounded, deterministic parent-side listener receipt.

    Returns ``(listener_fd, diagnostic)`` where ``listener_fd`` is ``-1`` on any
    failure and ``diagnostic`` names the exact terminal state (timeout, EOF,
    I/O error, a protocol violation, or the child's bounded ``E``-prefixed
    report). The caller fails closed with an observable cause instead of a
    silent downgrade; no code path can block past ``deadline``.
    """
    import select
    import socket

    sock.setblocking(False)
    listener_fd = -1
    error = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error = "handshake_timeout"
            break
        try:
            ready, _writable, _exceptional = select.select(
                [sock], [], [], remaining
            )
        except InterruptedError:
            continue
        except OSError as exc:
            error = f"handshake_select_error:{type(exc).__name__}:{exc}"
            break
        if not ready:
            error = "handshake_timeout"
            break
        try:
            payload, fds, _flags, _addr = socket.recv_fds(
                sock, _METADATA_BROKER_PATH_LIMIT, 1
            )
        except BlockingIOError:
            # Spurious readiness from a partial drain: stay within the deadline.
            continue
        except OSError as exc:
            error = f"handshake_recv_error:{type(exc).__name__}:{exc}"
            break
        if fds:
            listener_fd = fds[0]
            break
        if payload.startswith(b"E"):
            error = payload[1:].decode("utf-8", "replace")
            break
        if not payload:
            # EOF: the child closed the socket without delivering a listener.
            error = "handshake_eof"
            break
        # A non-empty data-only message with neither an fd nor an error marker
        # violates the handshake protocol; never loop on a wedged peer.
        error = "handshake_protocol_violation"
        break
    return listener_fd, error


def _metadata_broker_child_exec(argv: list[str]) -> int:
    """Install the notify filter in a freshly exec'd interpreter.

    ``_landlock_exec`` can run inside a multi-threaded pytest or manager
    process.  Running Python, ctypes and libseccomp between ``fork`` and
    ``exec`` is not safe in that situation: an inherited runtime/library lock
    can make the child disappear before it transfers the listener, which the
    parent observes only as ``handshake_eof``.  The parent therefore starts
    this small helper with ``Popen`` (whose fork/exec path stays in CPython's C
    implementation) and all non-trivial setup happens after a fresh exec.
    """
    import socket

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--metadata-broker-child", action="store_true", required=True)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--socket-fd", required=True, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise WorkspaceError("metadata_broker_command_empty")

    child_sock = socket.socket(fileno=args.socket_fd)
    try:
        _set_pdeathsig_sigkill(args.parent_pid)
        listener = _install_metadata_notify_filter()
        try:
            _metadata_broker_handshake_send(child_sock, listener)
        finally:
            os.close(listener)
    except BaseException as exc:
        diagnostic = f"{type(exc).__name__}:{exc}".encode("utf-8", "replace")[:1024]
        _metadata_broker_handshake_error(child_sock, diagnostic)
        return 126
    finally:
        child_sock.close()

    try:
        os.execvpe(command[0], command, os.environ.copy())
    except BaseException as exc:
        try:
            os.write(
                2,
                (
                    f"metadata_broker_exec_failed:{type(exc).__name__}:{exc}\n"
                ).encode("utf-8", "replace")[:2048],
            )
        except BaseException:
            pass
    return 126


def _seccomp_notify_runtime_supported() -> bool:
    """Probe listener creation and ``SCM_RIGHTS`` transfer exactly once.

    Kernel/libseccomp API levels alone are insufficient inside containers:
    the outer runtime policy can terminate ``SECCOMP_FILTER_FLAG_NEW_LISTENER``
    even though the API probe succeeds.  Use the same freshly exec'd helper
    and descriptor handoff as the real broker, but run only ``/bin/true`` so
    the probe cannot mutate the repository or weaken confinement.
    """
    global _SECCOMP_NOTIFY_RUNTIME_SUPPORTED

    if _SECCOMP_NOTIFY_RUNTIME_SUPPORTED is not None:
        return _SECCOMP_NOTIFY_RUNTIME_SUPPORTED
    if os.name == "nt" or not sys.platform.startswith("linux"):
        _SECCOMP_NOTIFY_RUNTIME_SUPPORTED = False
        return False

    import socket

    parent_sock, child_sock = socket.socketpair()
    process: subprocess.Popen[bytes] | None = None
    listener_fd = -1
    try:
        parent_pid = os.getpid()
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--metadata-broker-child",
                "--parent-pid",
                str(parent_pid),
                "--socket-fd",
                str(child_sock.fileno()),
                "--",
                "/bin/true",
            ],
            close_fds=True,
            pass_fds=(child_sock.fileno(),),
            start_new_session=True,
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child_sock.close()
        listener_fd, _error = _metadata_broker_handshake_receive(
            parent_sock,
            time.monotonic() + _METADATA_BROKER_HANDSHAKE_SECONDS,
        )
        if listener_fd < 0:
            _kill_validator_group(process.pid)
            process.wait(timeout=5)
            _SECCOMP_NOTIFY_RUNTIME_SUPPORTED = False
            return False
        _SECCOMP_NOTIFY_RUNTIME_SUPPORTED = process.wait(timeout=5) == 0
        return _SECCOMP_NOTIFY_RUNTIME_SUPPORTED
    except (OSError, subprocess.SubprocessError):
        if process is not None and process.poll() is None:
            _kill_validator_group(process.pid)
            try:
                process.wait(timeout=5)
            except subprocess.SubprocessError:
                pass
        _SECCOMP_NOTIFY_RUNTIME_SUPPORTED = False
        return False
    finally:
        parent_sock.close()
        try:
            child_sock.close()
        except OSError:
            pass
        if listener_fd >= 0:
            os.close(listener_fd)


def _landlock_exec(argv: list[str]) -> int:
    import socket

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--landlock-exec", action="store_true", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument("--exec-scratch", default=None)
    parser.add_argument("--worker-temp", default=None)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--outer-validation-authority", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise WorkspaceError("landlock_command_empty")
    workspace = Path(args.workspace).resolve()
    home = Path(args.home).resolve()
    exec_scratch = Path(args.exec_scratch).resolve() if args.exec_scratch else None
    worker_temp = Path(args.worker_temp).resolve() if args.worker_temp else None
    if args.outer_validation_authority:
        plant_outer_validation_authority(workspace, exec_scratch=exec_scratch)
    _apply_landlock(workspace, home, args.allow, exec_scratch, worker_temp=worker_temp)
    if args.cwd is not None:
        # Re-validate against this exec'd child's own view of the workspace
        # (fail closed rather than trusting the parent-provided flag) --
        # traversal/absolute paths are already rejected before this argv is
        # built, so only symlink-escape/existence can differ here.
        chdir_target = _require_beneath(workspace, workspace / args.cwd)
        if chdir_target.is_symlink() or not chdir_target.is_dir():
            raise WorkspaceError(f"landlock_cwd_not_directory:{args.cwd}")
    else:
        chdir_target = workspace
    os.chdir(chdir_target)
    if exec_scratch is not None and _seccomp_notify_supported():
        # NF27: the validator child previously received EPERM for the whole
        # chmod family, breaking unmodified tools (git init writing
        # .git/config.lock). Install a user-notification filter in the
        # disposable child and emulate ONLY verified chmod/fchmod/fchmodat
        # operations on stable, request-owned regular files and owned
        # directories beneath this exact validation exec scratch from
        # this trusted parent.
        parent_sock, child_sock = socket.socketpair()
        broker_parent_pid = os.getpid()
        helper_argv = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--metadata-broker-child",
            "--parent-pid",
            str(broker_parent_pid),
            "--socket-fd",
            str(child_sock.fileno()),
            "--",
            *command,
        ]
        try:
            child_process = subprocess.Popen(
                helper_argv,
                close_fds=True,
                pass_fds=(child_sock.fileno(),),
                start_new_session=True,
                env=os.environ.copy(),
            )
        except OSError as exc:
            parent_sock.close()
            child_sock.close()
            raise WorkspaceError(
                f"metadata_broker_child_start_failed:{type(exc).__name__}:{exc}"
            ) from exc
        child_pid = child_process.pid
        child_sock.close()
        try:
            listener_fd, listener_error = _metadata_broker_handshake_receive(
                parent_sock,
                time.monotonic() + _METADATA_BROKER_HANDSHAKE_SECONDS,
            )
        finally:
            parent_sock.close()
        if listener_fd < 0:
            _kill_validator_group(child_pid)
            _reap_validator(child_pid)
            suffix = f":{listener_error}" if listener_error else ""
            raise WorkspaceError(f"metadata_broker_listener_transfer_failed{suffix}")
        try:
            # NF430: authorize the request-owned worker temp authority as an
            # additional broker resolution root (identically owner/inode/symlink
            # verified) so a worker's TMPDIR-rooted Git/pytest ``config.lock``
            # chmod beneath ``.aiworkhub/temp/worker/<request_id>`` is emulated,
            # exactly like the exec scratch, without widening any other path.
            return _run_metadata_broker(
                listener_fd,
                child_pid,
                exec_scratch,
                extra_roots=(worker_temp,) if worker_temp is not None else (),
            )
        finally:
            os.close(listener_fd)
    _apply_metadata_seccomp()
    os.execvpe(command[0], command, os.environ.copy())
    return 126


def _tokenize_validation_command(command: str) -> list[str]:
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise WorkspaceError("invalid_validation_command")
    if any(ch in command for ch in ("\n", "\r", "|", ";", "`", ">", "<")):
        raise WorkspaceError(f"validation_shell_syntax_forbidden:{command[:120]}")
    try:
        if _is_windows_host():
            # Keep POSIX-style quote removal because task-card validation
            # commands are cross-platform, but do not treat every backslash
            # in a native Windows path as an escape character.
            lexer = shlex.shlex(command, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            lexer.escape = ""
            argv = list(lexer)
        else:
            argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise WorkspaceError(f"validation_parse_failed:{exc}") from exc
    if not argv:
        raise WorkspaceError("validation_argv_empty")
    return argv


_ENV_ASSIGNMENT_TOKEN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_SUPPORTED_VALIDATION_ENV_ASSIGNMENTS = frozenset({"PYTHONPATH", "TMPDIR"})
_PYTHONPATH_FORBIDDEN_VALUE_CHARS = frozenset("$`*?[]~{}()|;&<>\n\r\t ")
_SUPPORTED_VALIDATION_TMPDIR_VALUE = "/dev/shm"


def _split_validation_env_prefix(argv: list[str]) -> tuple[dict[str, str], list[str]]:
    """Extract at most one supported leading validation-only assignment."""
    if not argv:
        return {}, argv
    match = _ENV_ASSIGNMENT_TOKEN_RE.match(argv[0])
    if match is None:
        return {}, argv
    name, raw_value = match.group(1), match.group(2)
    if name not in _SUPPORTED_VALIDATION_ENV_ASSIGNMENTS:
        raise WorkspaceError(f"validation_env_assignment_not_supported:{name}")
    rest = argv[1:]
    if not rest:
        raise WorkspaceError("validation_env_assignment_without_executable")
    if _ENV_ASSIGNMENT_TOKEN_RE.match(rest[0]) is not None:
        raise WorkspaceError("validation_env_assignment_multiple_not_supported")
    return {name: raw_value}, rest


def _validate_pythonpath_value(raw_value: str) -> tuple[str, ...]:
    if not raw_value:
        raise WorkspaceError("validation_pythonpath_empty")
    if "\x00" in raw_value:
        raise WorkspaceError("validation_pythonpath_nul_forbidden")
    if any(ch in _PYTHONPATH_FORBIDDEN_VALUE_CHARS for ch in raw_value):
        raise WorkspaceError("validation_pythonpath_forbidden_char")
    components = raw_value.split(":")
    if any(not component for component in components):
        raise WorkspaceError("validation_pythonpath_empty_component")
    for component in components:
        if component == "." or component.startswith("/"):
            continue
        if any(segment in ("", ".", "..") for segment in component.split("/")):
            raise WorkspaceError(f"validation_pythonpath_traversal_forbidden:{component}")
    return tuple(components)


_VALIDATION_CD_TOKEN = "cd"
_VALIDATION_CHAIN_TOKENS = frozenset({"&&", "&"})
_CD_PATH_FORBIDDEN_CHARS = frozenset("&$()`;|<>\n\r\t")


def _validate_cd_path_chars(raw: str) -> None:
    if any(ch in _CD_PATH_FORBIDDEN_CHARS for ch in raw):
        raise WorkspaceError(f"validation_cd_path_forbidden_char:{raw}")


def _split_validation_cwd_prefix(argv: list[str]) -> tuple[str | None, list[str]]:
    """Extract an optional leading ``cd RELATIVE_DIR &&`` prefix.

    Only the exact three-token form ``cd``, a single relative directory
    argument, then a literal ``&&`` is accepted immediately before the
    remaining command argv (which may itself still carry a leading env
    assignment -- merged by the caller). Anything else that looks like a
    shell chain (a bare ``&&``/``&`` anywhere, a ``cd`` missing its ``&&``,
    a second ``cd`` prefix, or a ``cd`` with nothing left to run) is
    rejected fail-closed rather than silently reinterpreted.
    """
    if not argv:
        return None, argv
    if argv[0] != _VALIDATION_CD_TOKEN:
        if any(token in _VALIDATION_CHAIN_TOKENS for token in argv):
            raise WorkspaceError(
                f"validation_shell_syntax_forbidden:chain:validation_shell_chain_forbidden:{argv}"
            )
        return None, argv
    if len(argv) < 3 or argv[2] != "&&":
        raise WorkspaceError("validation_cd_prefix_malformed")
    raw_path = argv[1]
    _validate_cd_path_chars(raw_path)
    if raw_path == ".":
        raise WorkspaceError(f"unsafe_repo_path:{raw_path}")
    relative = _relative_repo_path(raw_path)
    rest = argv[3:]
    if not rest:
        raise WorkspaceError(
            "validation_cd_command_empty:validation_cd_prefix_without_executable"
        )
    if rest[0] == _VALIDATION_CD_TOKEN:
        raise WorkspaceError("validation_multiple_cd_prefix_forbidden")
    if any(token in _VALIDATION_CHAIN_TOKENS for token in rest):
        raise WorkspaceError(
            f"validation_shell_syntax_forbidden:chain:validation_shell_chain_forbidden:{rest}"
        )
    return relative, rest


def _parse_validation_command_detailed(
    command: str,
) -> tuple[list[str], tuple[str, ...], str | None, str | None]:
    """Parse the private validation-only environment and cwd-prefix channel.

    The public ``parse_validation_command`` API intentionally remains a
    two-tuple.  TMPDIR is accepted only for the one canonical value used by
    task cards; arbitrary assignments and alternate paths stay fail-closed.
    An optional environment-assignment prefix (``PYTHONPATH=``/``TMPDIR=``)
    is accepted in EITHER of two positions -- never both -- so a command
    shaped either ``PYTHONPATH=. cd sub && python3 -m pytest`` (env before
    ``cd``) or ``cd sub && PYTHONPATH=. python3 -m pytest`` (env immediately
    after the ``cd RELATIVE_DIR &&`` prefix) strips exactly one supported
    assignment and merges it into the same ``components``/``tmpdir_override``
    result.
    """
    argv = _tokenize_validation_command(command)
    env, argv = _split_validation_env_prefix(argv)
    cd_relative, rest = _split_validation_cwd_prefix(argv)
    if not env and cd_relative is not None:
        env, rest = _split_validation_env_prefix(rest)
    components: tuple[str, ...] = ()
    tmpdir_override: str | None = None
    if "PYTHONPATH" in env:
        components = _validate_pythonpath_value(env["PYTHONPATH"])
    if "TMPDIR" in env:
        raw_value = env["TMPDIR"]
        if raw_value != _SUPPORTED_VALIDATION_TMPDIR_VALUE:
            raise WorkspaceError(f"validation_tmpdir_value_not_supported:{raw_value}")
        tmpdir_override = raw_value
    return rest, components, tmpdir_override, cd_relative


def parse_validation_command(command: str) -> tuple[list[str], tuple[str, ...]]:
    """Return executable argv plus an optional bounded PYTHONPATH component list."""
    argv, components, _tmpdir_override, _cd_relative = _parse_validation_command_detailed(
        command
    )
    return argv, components


def validation_argv(command: str) -> list[str]:
    return parse_validation_command(command)[0]


def _approved_pythonpath_site(component: str) -> Path:
    candidate = Path(component)
    if not candidate.is_absolute():
        raise WorkspaceError(
            f"validation_pythonpath_absolute_component_forbidden:{component}"
        )
    approved_raw = Path(site.getusersitepackages())
    approved_site = approved_raw.resolve()
    pytest_runtime = _current_pytest_runtime_root()
    approved_paths = {approved_raw, approved_site}
    if pytest_runtime is not None:
        approved_paths.add(pytest_runtime)
    # Reject untrusted spellings before containment can dereference a UNC path.
    if candidate not in approved_paths:
        raise WorkspaceError(
            f"validation_pythonpath_absolute_component_forbidden:{component}"
        )
    try:
        target = _require_beneath(Path(candidate.anchor), candidate)
    except WorkspaceError as exc:
        # Normalize the shared _require_beneath invariant's lexical-symlink /
        # escape rejections at this public validation boundary so callers see
        # the absolute-PYTHONPATH identity rather than the internal helper's,
        # without accepting the path or broadening any trust root.
        if str(exc).startswith(
            ("symlink_path_component_forbidden", "path_escapes_workspace")
        ):
            raise WorkspaceError(
                f"validation_pythonpath_absolute_component_forbidden:{component}"
            ) from exc
        raise
    if target not in approved_paths or not target.is_dir():
        raise WorkspaceError(
            f"validation_pythonpath_absolute_component_forbidden:{component}"
        )
    return target


def resolve_validation_pythonpath(
    workspace: WorkerWorkspace, backend: str, components: tuple[str, ...]
) -> str:
    """Resolve validated relative directories in the child-visible workspace."""
    base = SANDBOX_WORKSPACE if backend == "bubblewrap" else str(workspace.path)
    resolved: list[str] = []
    absolute_index = 0
    for component in components:
        if component == ".":
            resolved.append(base)
            continue
        component_path = Path(component)
        if component_path.is_absolute():
            target = _approved_pythonpath_site(component)
            resolved.append(
                f"/validation-pythonpath/{absolute_index}"
                if backend == "bubblewrap"
                else str(target)
            )
            absolute_index += 1
            continue
        if component_path.anchor:
            raise WorkspaceError(
                f"validation_pythonpath_absolute_component_forbidden:{component}"
            )
        target = _require_beneath(workspace.path, workspace.path / component)
        if target.is_symlink() or not target.is_dir():
            raise WorkspaceError(f"validation_pythonpath_not_directory:{component}")
        if backend == "bubblewrap":
            resolved.append(f"{base}/{PurePosixPath(component).as_posix()}")
        else:
            resolved.append(str(workspace.path / Path(*PurePosixPath(component).parts)))
    return os.pathsep.join(resolved)


def _candidate_pythonpath_components(
    workspace: WorkerWorkspace, components: tuple[str, ...]
) -> tuple[str, ...]:
    """Prepend the verified sparse candidate package root to Python imports."""
    candidate_src = workspace.path / "src"
    if not candidate_src.exists():
        return components
    if "src" in components:
        return components
    if candidate_src.is_symlink() or not candidate_src.is_dir():
        raise WorkspaceError("python_candidate_import_root_invalid:src")
    if _require_beneath(workspace.path, candidate_src) != candidate_src:
        raise WorkspaceError("python_candidate_import_root_mismatch:src")
    package_init = candidate_src / "aiworkhub" / "__init__.py"
    if not (candidate_src / "aiworkhub").exists():
        return components
    if package_init.is_symlink() or not package_init.is_file():
        raise WorkspaceError("python_candidate_package_anchor_missing:src/aiworkhub")
    if (candidate_src / "pytest.py").exists() or (candidate_src / "pytest").exists():
        raise WorkspaceError("python_candidate_pytest_shadow_forbidden")
    return ("src", *components)


def _resolve_validation_cwd(workspace: WorkerWorkspace, relative: str) -> str:
    """Fail-closed resolution of a ``cd`` prefix's target against the real
    workspace filesystem: rejects a symlinked path component or escape
    (``_require_beneath``, shared with every other workspace-relative
    resolution) and requires the resolved target to actually be a directory,
    not a symlink itself. Returns the workspace-relative POSIX path so
    callers can bind/chdir into the identical location under either sandbox
    backend.
    """
    target = _require_beneath(workspace.path, workspace.path / relative)
    if target.is_symlink() or not target.is_dir():
        raise WorkspaceError(f"validation_cwd_not_directory:{relative}")
    return target.relative_to(workspace.path).as_posix()


def _is_pytest_validation_command(argv: list[str]) -> bool:
    """True when *argv* (post-PYTHONPATH-prefix-strip) invokes pytest."""
    if not argv:
        return False
    head = Path(argv[0]).name
    if head == "pytest":
        return True
    return len(argv) >= 3 and head.startswith("python") and argv[1] == "-m" and argv[2] == "pytest"


def _is_python_validation_command(argv: list[str]) -> bool:
    return bool(argv) and Path(argv[0]).name.startswith("python")


def _normalize_pytest_validation_argv(argv: list[str]) -> list[str]:
    """Make the console-script spelling independent of the sanitized PATH.

    The validation sandbox intentionally excludes the user's ``~/.local/bin``.
    A declared ``pytest ...`` command can therefore import the trusted pytest
    package we bind into PYTHONPATH but still fail before import while PATH
    searches for a non-existent ``/bin/pytest``.  Execute the same package
    through the already-running trusted coordinator interpreter instead.
    Explicit ``python* -m pytest`` commands remain byte-for-byte unchanged.
    """

    if argv and Path(argv[0]).name == "pytest":
        return [sys.executable, "-m", "pytest", *argv[1:]]
    return list(argv)


def _is_candidate_pytest_wrapper_command(argv: list[str]) -> bool:
    """True when *argv* is exactly ``python3 tools/candidate_pytest.py ...``.

    Only the exact repo-relative literal ``tools/candidate_pytest.py`` with
    the exact interpreter name ``python3`` is recognized.  Near-matches
    (``python``, ``python3.11``, absolute or relative variations with extra
    path components) all fail closed and are never reclassified.
    """
    if len(argv) < 2:
        return False
    return argv[0] == "python3" and argv[1] == "tools/candidate_pytest.py"


def _resolve_candidate_pytest_wrapper(repo_root: Path) -> tuple[Path, dict[str, Any]]:
    """Resolve and validate the exact candidate pytest wrapper file.

    Returns ``(resolved_wrapper_path, sys_modules_entry)`` where
    *resolved_wrapper_path* is the absolute, validated, regular,
    non-symlink file, and *sys_modules_entry* is a dict suitable for
    temporary ``sys.modules`` registration so the wrapper can be safely
    imported without polluting the module namespace.

    Rejects:
    * Missing file or symlink
    * Wrong owner (POSIX only)
    * World-writable mode bits (POSIX only)
    * Any path that is not a regular file
    """
    wrapper_relative = "tools/candidate_pytest.py"
    wrapper_path = repo_root / wrapper_relative
    # Race-safe symlink gate before resolve: .resolve() dereferences, so
    # a symlink at wrapper_path would never appear as is_symlink() on the
    # resolved target.  Reject the original path first (fail-closed).
    if wrapper_path.is_symlink():
        raise WorkspaceError(
            f"candidate_pytest_wrapper_symlink_forbidden:{wrapper_relative}"
        )
    try:
        resolved = wrapper_path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(
            f"candidate_pytest_wrapper_unavailable:{wrapper_relative}"
        ) from exc
    if resolved.is_symlink():
        raise WorkspaceError(
            f"candidate_pytest_wrapper_symlink_forbidden:{wrapper_relative}"
        )
    if not resolved.is_file():
        raise WorkspaceError(
            f"candidate_pytest_wrapper_not_regular:{wrapper_relative}"
        )
    try:
        info = resolved.stat()
    except OSError as exc:
        raise WorkspaceError(
            f"candidate_pytest_wrapper_unavailable:{wrapper_relative}"
        ) from exc
    if os.name != "nt" and info.st_uid != os.getuid():
        raise WorkspaceError(
            f"candidate_pytest_wrapper_untrusted_owner:{wrapper_relative}"
        )
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o002:
        raise WorkspaceError(
            f"candidate_pytest_wrapper_world_writable:{wrapper_relative}"
        )
    # Safe import: register the wrapper as a fake package under a stable,
    # request-scoped name so ``import candidate_pytest`` resolves in the
    # child process. The real source lives at *resolved*, not under any
    # site-packages or PYTHONPATH directory; the spec-based loader is the
    # only route.
    module_name = "aiworkhub_candidate_pytest_wrapper"
    safe_spec = importlib.util.spec_from_file_location(
        module_name, str(resolved)
    )
    if safe_spec is None or safe_spec.loader is None:
        raise WorkspaceError(
            f"candidate_pytest_wrapper_import_failed:{wrapper_relative}"
        )
    sys_modules_entry: dict[str, Any] = {
        "name": module_name,
        "spec": safe_spec,
    }
    return resolved, sys_modules_entry


def _install_candidate_pytest_wrapper_module(
    sys_modules_entry: dict[str, Any],
) -> None:
    """Temporarily register the candidate wrapper in ``sys.modules``.

    The entry is recorded so the caller can restore the original state after
    the wrapped subprocess or import completes.  The module object is created
    but NOT executed -- the child process's own Python interpreter will
    execute the wrapper from the filesystem path baked into its PYTHONPATH.
    """
    module_name: str = sys_modules_entry["name"]
    if module_name in sys.modules:
        raise WorkspaceError(
            f"candidate_pytest_wrapper_module_conflict:{module_name}"
        )
    spec: importlib.machinery.ModuleSpec = sys_modules_entry["spec"]
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module


def _uninstall_candidate_pytest_wrapper_module(
    sys_modules_entry: dict[str, Any] | None,
) -> None:
    """Remove the temporary candidate wrapper registration from ``sys.modules``.

    Safe to call when *sys_modules_entry* is None (no-op).
    """
    if sys_modules_entry is None:
        return
    module_name: str = sys_modules_entry["name"]
    sys.modules.pop(module_name, None)


def _current_pytest_runtime_root() -> Path | None:
    """Return the exact site-packages root supplying this interpreter's pytest."""
    spec = importlib.util.find_spec("pytest")
    if spec is None or not isinstance(spec.origin, str) or not spec.origin:
        return None
    package_init = Path(spec.origin)
    if package_init.name != "__init__.py" or package_init.parent.name != "pytest":
        return None
    return package_init.parent.parent.resolve(strict=False)


def _validate_pytest_runtime_root(
    raw: Path,
    *,
    allow_active_runtime_world_writable: bool = False,
) -> Path:
    """Validate one exact pytest package root without broadening PYTHONPATH."""
    if raw.is_symlink():
        raise WorkspaceError(f"validation_pytest_runtime_symlink_forbidden:{raw}")
    candidate = raw.resolve(strict=False)
    if candidate.is_symlink():
        raise WorkspaceError(f"validation_pytest_runtime_symlink_forbidden:{candidate}")
    if not candidate.is_dir():
        raise WorkspaceError(f"validation_pytest_runtime_unavailable:{candidate}")
    try:
        info = candidate.stat()
    except OSError as exc:
        raise WorkspaceError(f"validation_pytest_runtime_unavailable:{candidate}") from exc
    # The host pytest runtime is only checked for same-owner where POSIX
    # ownership is meaningful; Windows ACLs protect the package tree.
    if os.name != "nt" and info.st_uid != os.getuid():
        raise WorkspaceError(f"validation_pytest_runtime_untrusted_owner:{candidate}")
    if (
        os.name != "nt"
        and stat.S_IMODE(info.st_mode) & 0o002
        and not allow_active_runtime_world_writable
    ):
        raise WorkspaceError(f"validation_pytest_runtime_world_writable:{candidate}")
    package_init = candidate / "pytest" / "__init__.py"
    if package_init.is_symlink() or not package_init.is_file():
        raise WorkspaceError(f"validation_pytest_runtime_missing_pytest:{candidate}")
    return candidate


def resolve_trusted_pytest_runtime_root() -> Path:
    """Resolve and validate the one canonical, read-only pytest package root
    used to repair ``pytest``/``python3 -m pytest`` validation commands under
    the sanitized, credential-free validation HOME (B755).

    B753/B674 false negative: an isolated validation run's sanitized HOME has
    no ``~/.local/lib/pythonX/site-packages`` of its own, so a pytest
    validation command fails with ``ModuleNotFoundError: No module named
    'pytest'`` even though the parent host has pytest installed. This
    resolves the exact same trusted root ``resolve_validation_pythonpath``
    accepts as an approved absolute PYTHONPATH component. The configured user
    site remains preferred; when that real configured directory is absent (as
    in an activated CI virtualenv), the exact site-packages root supplying the
    coordinator interpreter's own pytest package is used. Both paths reject
    outright if it is a symlink, not owned by this process's user,
    world-writable, or does not actually contain an importable ``pytest``
    package -- never a copy, never any other real-HOME content, never
    writable. The sole exception is the exact package root already supplying
    pytest to the active interpreter: some ephemeral CI toolcache installs
    expose that root with permissive mode bits, but its code has necessarily
    already been imported by this process and validation binds it read-only.
    An arbitrary writable path is never admitted. Fails closed with a
    ``WorkspaceError`` if no such root exists,
    instead of silently handing back a path that will fail to import at test
    time.
    """
    raw = Path(site.getusersitepackages())
    try:
        return _validate_pytest_runtime_root(raw)
    except WorkspaceError as exc:
        configured = getattr(site, "USER_SITE", None)
        configured_raw = Path(configured) if isinstance(configured, str) else None
        recoverable = str(exc).startswith(
            ("validation_pytest_runtime_unavailable:", "validation_pytest_runtime_missing_pytest:")
        )
        if configured_raw is None or raw != configured_raw or not recoverable:
            raise
        runtime_root = _current_pytest_runtime_root()
        if runtime_root is None or runtime_root == raw.resolve(strict=False):
            raise
        return _validate_pytest_runtime_root(
            runtime_root,
            allow_active_runtime_world_writable=True,
        )


def _validation_pythonpath_readonly_dirs(components: tuple[str, ...]) -> tuple[Path, ...]:
    rows: list[Path] = []
    for component in components:
        if not component.startswith("/"):
            continue
        rows.append(_approved_pythonpath_site(component))
    return tuple(rows)


_VALIDATION_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_./+=-]{32,}")


def _validation_failure_class(record: Mapping[str, Any]) -> str:
    if record.get("timed_out") is True:
        return "timeout"
    returncode = record.get("returncode")
    if returncode == 0:
        return "passed"
    command = " ".join(
        str(value) for value in (
            record.get("executed_argv")
            or record.get("argv")
            or [record.get("command") or ""]
        )
    ).lower()
    diagnostic = (
        str(record.get("stderr_tail") or "")
        + "\n"
        + str(record.get("stdout_tail") or "")
    ).lower()
    launch_error = record.get("launch_error")
    if launch_error == "PermissionError":
        return "permission_denied"
    if launch_error == "FileNotFoundError":
        return "executable_unavailable"
    if launch_error:
        return "launch_failed"
    if "permission denied" in diagnostic or "access is denied" in diagnostic:
        return "permission_denied"
    if returncode in {126, 127} or "not found" in diagnostic:
        return "executable_unavailable"
    if "mypy" in command and (
        "internal error" in diagnostic or "traceback (most recent call last)" in diagnostic
    ):
        return "type_check_internal_error"
    if "syntaxerror" in diagnostic or "syntax error" in diagnostic:
        return "syntax_error"
    if "mypy" in command or "type error" in diagnostic:
        return "type_check_failure"
    if "ruff" in command:
        return "lint_failure"
    if "pytest" in command or "unittest" in command or "assertionerror" in diagnostic:
        return "test_failure"
    return "nonzero_exit"


def _validation_failure_receipt(record: Mapping[str, Any]) -> dict[str, Any] | None:
    failure_class = _validation_failure_class(record)
    if failure_class == "passed":
        return None
    argv = record.get("executed_argv") or record.get("argv") or []
    if not isinstance(argv, (list, tuple)):
        argv = []
    exact_argv = [str(value) for value in argv]
    raw_diagnostic = (
        str(record.get("stderr_tail") or "")
        or str(record.get("stdout_tail") or "")
    )[-4096:]
    diagnostic = "".join(
        character for character in raw_diagnostic
        if character in "\n\r\t" or ord(character) >= 0x20
    )
    diagnostic = _VALIDATION_LONG_TOKEN_RE.sub("<redacted>", diagnostic)[-2048:]
    identity = {
        "failure_class": failure_class,
        "argv": exact_argv,
        "returncode": record.get("returncode"),
        "timed_out": record.get("timed_out") is True,
        "diagnostic_sha256": hashlib.sha256(
            raw_diagnostic.encode("utf-8", errors="replace")
        ).hexdigest(),
    }
    return {
        "schema_id": "aiworkhub.validation_failure_receipt.v1",
        **identity,
        "command_sha256": hashlib.sha256(
            json.dumps(exact_argv, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        ).hexdigest(),
        "diagnostic_tail": diagnostic,
        "receipt_sha256": hashlib.sha256(
            json.dumps(identity, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest(),
    }


_PROVENANCE_ENV_KEYS = ("MYPY_CACHE_DIR", "TMPDIR", "RUFF_CACHE_DIR")


def _bounded_mypy_traceback(stderr: str) -> str:
    """Bounded tail of a mypy INTERNAL ERROR traceback.

    mypy prints the final ``Traceback (most recent call last):`` block ending
    with the exception and the ``INTERNAL ERROR`` line.  Prefer that final
    block (the most diagnostic part) but never retain more than 4096
    characters.
    """
    text = (stderr or "").replace("\x00", "")
    marker = text.rfind("Traceback (most recent call last):")
    if marker != -1:
        text = text[marker:]
    return text[-4_096:]


def _validation_environment_provenance(env: Mapping[str, str]) -> dict[str, str]:
    """Bounded, non-secret validation environment provenance.

    Copies only the fixed, non-secret keys needed to diagnose an internal
    error (where the cache/temp dirs live), never the whole child env, so
    credential-bearing passthrough values (proxies, adapter keys) are never
    written into failure receipts.
    """
    provenance = {
        key: env[key] for key in _PROVENANCE_ENV_KEYS if key in env and env[key]
    }
    provenance["python_version"] = ".".join(
        str(part) for part in sys.version_info[:3]
    )
    return provenance


def validation_failure_delta_packet(
    results: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return only normalized failed-command evidence for one rework turn."""

    receipts = []
    for row in results:
        receipt = row.get("failure_receipt")
        if not isinstance(receipt, dict):
            receipt = _validation_failure_receipt(row)
        if receipt is not None:
            receipts.append(receipt)
    observed_count = len(receipts)
    while True:
        packet = {
            "schema_id": "aiworkhub.validation_failure_delta.v1",
            "observed_failure_count": observed_count,
            "failure_count": len(receipts),
            "receipts": receipts,
            "truncated": len(receipts) < observed_count,
            "automatic_repair_authorized": False,
        }
        encoded = json.dumps(
            packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if len(encoded) <= 6 * 1024:
            break
        if not receipts:
            raise WorkspaceError("validation_failure_delta_too_large")
        receipts.pop()
    packet["packet_sha256"] = hashlib.sha256(encoded).hexdigest()
    packet["packet_bytes"] = len(encoded)
    return packet


# A minimal child that reports which of the named modules are NOT importable, so
# the terminal state is DECIDED by a real import in the invoked interpreter
# rather than by matching a message in the candidate's own output.
_VALIDATOR_MODULE_PROBE = (
    "import importlib.util, sys\n"
    "sys.stdout.write(chr(10).join("
    "n for n in sys.argv[1:] if importlib.util.find_spec(n) is None))\n"
)


def _validator_probe_interpreter(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    head = str(tokens[0])
    if Path(head).name.startswith("python"):
        if os.path.isabs(head) and Path(head).exists():
            return head
        return shutil.which(head) or sys.executable
    # ``pytest``-console-script commands are normalized to ``sys.executable -m
    # pytest`` before this point, so the invoked interpreter is the trusted
    # coordinator interpreter.
    return sys.executable


def _host_probe_pythonpath(
    workspace: WorkerWorkspace, components: tuple[str, ...]
) -> str:
    """Host-side PYTHONPATH for the validator-import probe, with every
    candidate-writable component removed (NF-WAVE-SANDBOX-TRUTH security fix).

    The probe runs on the HOST -- in the coordinator process, NOT inside the
    sandbox -- to decide whether a ``-m``-invoked validator is importable. It
    must therefore never place a candidate-writable directory on the probe
    interpreter's ``sys.path``: CPython's site initialization imports
    ``sitecustomize`` (and, absent ``PYTHONNOUSERSITE``, ``usercustomize``) from
    ANY ``sys.path`` entry, so a candidate that dropped a ``sitecustomize.py``
    into its worktree root would gain arbitrary code execution in the
    coordinator as soon as the probe interpreter started. ``PYTHONNOUSERSITE``
    only disables the user site and ``usercustomize`` -- it does nothing about a
    ``sitecustomize`` reached through a PYTHONPATH entry.

    Only the trusted, host-absolute validator roots in ``components`` (the
    approved user-site / pytest runtime root, already identity-checked by
    ``_approved_pythonpath_site`` / ``resolve_trusted_pytest_runtime_root``) are
    kept. Every workspace-relative component -- ``.`` (the worktree root) and any
    ``sub/dir`` beneath it, all candidate-writable -- is dropped. This never
    causes a false "absent": a genuine validator (pytest/ruff/mypy/coverage) is
    never shipped by the candidate through a relative component -- it resolves via
    a trusted absolute root or the interpreter's own site-packages, both still on
    the probe path -- so the probe still cannot report an importable validator as
    absent, while the code-execution vector is closed.
    """
    parts: list[str] = []
    for component in components:
        if component == "." or not os.path.isabs(component):
            # A workspace-relative (candidate-writable) component -- never placed
            # on the host probe's sys.path.
            continue
        parts.append(component)
    return os.pathsep.join(parts)


def _probe_absent_validator_modules(
    workspace: WorkerWorkspace,
    tokens: list[str],
    components: tuple[str, ...],
) -> tuple[str, ...]:
    """Return the ``-m``-invoked validator modules genuinely absent from the
    interpreter that ran the command (NF-WAVE-SANDBOX-TRUTH).

    ``validation_runner.row_restriction`` DECIDES ``missing_package`` from this
    result alone, so it must be authored here by a real import probe -- never
    from candidate output text. Fail-closed toward the strict state: any probe
    that cannot run (no interpreter, OSError, timeout, or a nonzero probe exit)
    returns ``()`` so a genuine gate failure is never mis-downgraded; only a
    validator the runner positively proved absent is reported.

    Which ``-m <module>`` is python's module selector (and which is pytest's own
    ``-m <marker>``) is decided by the SAME shared pytest argument model the
    create-time preflight uses (``validation_runner.dash_m_validator_modules``),
    so ``pytest -m coverage`` never probes a module named ``coverage``. Imported
    lazily: this function only runs under the package-imported ``run_validations``
    entrypoint, never the bare-script Landlock wrapper.
    """
    from . import validation_runner

    modules = validation_runner.dash_m_validator_modules(tokens)
    if not modules:
        return ()
    interpreter = _validator_probe_interpreter(tokens)
    if interpreter is None:
        return ()
    probe_env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    # Mirror the real validation run's import surface: ``sanitized_env`` gives the
    # command an isolated HOME with no ``~/.local`` user site, binding validators
    # only through the trusted roots carried in ``components``. Suppress user site
    # here too so the probe cannot "find" a validator via the coordinator user's
    # ~/.local that the sandboxed command could never import -- which would
    # falsely mark it present.
    probe_env["PYTHONNOUSERSITE"] = "1"
    # Security (NF-WAVE-SANDBOX-TRUTH): the probe runs on the HOST, so it must
    # never load a candidate-authored ``sitecustomize``. Two independent
    # ``sys.path`` entries could reach one: a PYTHONPATH component (closed by
    # ``_host_probe_pythonpath``, which drops every candidate-writable component)
    # and the implicit ``-c`` cwd/empty entry (closed here by PYTHONSAFEPATH,
    # honoured on 3.11+ and inert earlier -- equivalent to ``-P``, and unlike
    # ``-I``/``-E`` it does NOT ignore PYTHONPATH, so the trusted validator roots
    # the probe genuinely needs stay visible).
    probe_env["PYTHONSAFEPATH"] = "1"
    pythonpath = _host_probe_pythonpath(workspace, components)
    if pythonpath:
        probe_env["PYTHONPATH"] = pythonpath
    try:
        probe = subprocess.run(
            [interpreter, "-c", _VALIDATOR_MODULE_PROBE, *modules],
            env=probe_env,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if probe.returncode != 0:
        return ()
    reported = {line.strip() for line in (probe.stdout or "").splitlines() if line.strip()}
    return tuple(name for name in modules if name in reported)


def _exec_scratch_environment_restriction(detail: str) -> str | None:
    """Classify a scratch-provisioning failure as a named sandbox restriction.

    ``provision_validation_exec_scratch`` fails closed with one aggregate
    ``tried`` detail describing every candidate root's rejection reason
    (NF-2026-00458). Delegates the classification itself to
    ``validation_runner.exec_scratch_denied_restriction`` -- the single source
    of truth for which rejection details reflect the OUTER sandbox denying the
    exact metadata syscalls git's own ``config.lock`` chmod/utime needs, before
    any candidate command ran (infrastructure evidence, never a candidate gate
    failure) -- so the two modules cannot drift on what counts as a refused
    chmod. Returns ``None`` for any other rejection reason (a genuinely
    noexec-only root, a missing directory, ...) so the caller re-raises the
    original ``WorkspaceError`` unchanged rather than mis-classifying an
    unrelated provisioning failure.
    """
    from . import validation_runner

    return validation_runner.exec_scratch_denied_restriction(detail)


def _validation_unsupported_in_sandbox_error(restriction: str) -> WorkspaceError:
    """Wrap a sandbox-provisioning restriction with its truthful terminal name.

    ``select_sandbox_backend`` (and the explicit ``backend=`` allowlist check)
    fail BEFORE any command is parsed or spawned: no bubblewrap, no
    landlock+seccomp, an invalid or unsupported backend request. The candidate
    never executed, so this is not a candidate validation failure. Prefixing the
    original restriction token (``secure_sandbox_unavailable``,
    ``unsupported_sandbox_backend``, ...) with
    ``VALIDATION_UNSUPPORTED_IN_SANDBOX`` keeps the exact restriction visible
    for the finalizer's routing (``_terminal_state_for_workspace_error`` maps it
    to the recoverable ``finalize_failed`` bucket, preserving the retained
    workspace/hashes for provider-free ``retry_finalization``) while existing
    substring callers/tests keep matching the unchanged token.
    """
    return WorkspaceError(f"{VALIDATION_UNSUPPORTED_IN_SANDBOX}:{restriction}")


def run_validations(
    workspace: WorkerWorkspace,
    commands: Iterable[str],
    *,
    timeout_seconds: int = MAX_VALIDATION_SECONDS,
    backend: str | None = None,
    adapter_id: str = "",
    outer_validation_authority: bool = False,
) -> list[dict[str, Any]]:
    rows = list(commands)
    if len(rows) > MAX_VALIDATION_COMMANDS:
        raise WorkspaceError(f"validation_command_limit_exceeded:{len(rows)}")
    # A task with no validation commands has nothing to execute. Resolve no
    # host sandbox in this case: on Windows, VS Code LM workers already ran in
    # the editor-host boundary, and selecting a native CLI/AppContainer
    # backend here falsely converted their successful no-validation result to
    # ``finalize_failed:windows_appcontainer_sandbox_unavailable``.
    if not rows:
        return []
    results: list[dict[str, Any]] = []
    if backend:
        selected_backend = backend
    else:
        try:
            selected_backend = select_sandbox_backend()
        except WorkspaceError as exc:
            raise _validation_unsupported_in_sandbox_error(str(exc)) from exc
    if selected_backend not in {
        "landlock",
        "bubblewrap",
        VSCODE_LM_IN_PROCESS_BACKEND,
    }:
        raise _validation_unsupported_in_sandbox_error(
            f"unsupported_sandbox_backend:{selected_backend}"
        )
    if (
        selected_backend == VSCODE_LM_IN_PROCESS_BACKEND
        and adapter_id not in _VSCODE_LM_IN_PROCESS_ADAPTERS
    ):
        raise WorkspaceError(
            f"vscode_lm_in_process_validation_adapter_forbidden:{adapter_id}"
        )
    validation_home = (
        workspace.home
        if selected_backend in {"landlock", VSCODE_LM_IN_PROCESS_BACKEND}
        else None
    )
    bounded_timeout = max(1, min(timeout_seconds, MAX_VALIDATION_SECONDS))
    # B753: one private, exec-probed scratch directory for this whole
    # validation run -- provisioned before any command executes, and always
    # torn down in the ``finally`` below regardless of how the run ends
    # (every command passing, a failing command's WorkspaceError, a timeout,
    # or any other exception propagating out of this function).
    try:
        scratch_dir = provision_validation_exec_scratch(workspace)
    except WorkspaceError as exc:
        # NF-2026-00458: no candidate command has run yet, so a scratch root
        # rejected specifically because the outer sandbox denies the exact
        # chmod/utime-family metadata syscalls git's own config.lock needs is
        # infrastructure evidence, not a candidate gate failure -- terminalize
        # it as the recoverable ``validation_environment_blocked`` state
        # instead of letting an untyped ``WorkspaceError`` surface as
        # ``validation_failed``.
        restriction = _exec_scratch_environment_restriction(str(exc))
        if restriction is None:
            raise
        raise ValidationEnvironmentBlocked(
            f"{VALIDATION_ENVIRONMENT_BLOCKED}:{restriction}:{exc}",
            [],
            restriction=restriction,
        ) from exc
    scratch_env_value = (
        str(scratch_dir)
        if selected_backend in {"landlock", VSCODE_LM_IN_PROCESS_BACKEND}
        else SANDBOX_VALIDATION_EXEC_SCRATCH
    )
    # NF430 (rework): every validation invocation routes TMPDIR/TMP/TEMP at its
    # own private, per-invocation exec scratch -- the same request-scoped root
    # MYPY_CACHE_DIR and RUFF_CACHE_DIR use -- rather than a single per-request
    # worker temp authority shared across invocations.  A shared request TMPDIR
    # made two concurrent validations of the same request collide on one temp
    # root and defeated parallel-request isolation; the exec scratch is provisioned
    # fresh (and exec/metadata probed) per ``run_validations`` call, so two
    # concurrent requests -- each with a distinct exec scratch -- never share a
    # temp root, and a worker-side pytest/tempfile creates ``pytest-of-*`` beneath
    # this request-owned temp authority rather than the shared system temp.  The
    # seccomp metadata broker therefore authorizes exactly this one mechanically
    # verified per-invocation root (openat2/uid/nlink/inode checks unchanged), and
    # a nested Git ``config.lock`` chmod beneath it is authorized without widening
    # canonical/worktree writes.  There is deliberately no shared per-request
    # TMPDIR fallback.  The request-owned worker temp authority + multi-root broker
    # design remain wired for the real ProcessManager launch paths
    # (``process_launcher.worker_launch_env``) and its direct sandbox/broker tests;
    # ``cleanup_workspace`` still disposes any such root with the workspace.
    tmp_env_value = scratch_env_value
    try:
        for command in rows:
            (
                tokens,
                pythonpath_components,
                tmpdir_override,
                cd_relative,
            ) = _parse_validation_command_detailed(command)
            declared_argv = list(tokens)
            tokens, interpreter_authority = _normalize_validation_interpreter_argv(
                workspace, tokens
            )
            candidate_authority = python_candidate_authority(workspace)
            effective_components = pythonpath_components
            if _is_candidate_pytest_wrapper_command(tokens):
                # NF128: the exact candidate wrapper gets declared
                # candidate PYTHONPATH components first and the trusted
                # pytest runtime root last so the wrapper's own
                # dependencies are visible without shadowing the real
                # pytest package.
                _resolve_candidate_pytest_wrapper(workspace.repo)
                pytest_root = resolve_trusted_pytest_runtime_root()
                effective_components = pythonpath_components + (str(pytest_root),)
                tokens = _normalize_pytest_validation_argv(tokens)
            elif _is_pytest_validation_command(tokens):
                # B755: bind and prepend the one trusted pytest package root
                # ahead of whatever relative project PYTHONPATH the card
                # already declared. Fails closed before this command ever
                # runs if no approved pytest runtime exists. Non-pytest
                # commands never reach this branch, so their env/argv stay
                # byte-equivalent to before.
                pytest_root = resolve_trusted_pytest_runtime_root()
                effective_components = (str(pytest_root),) + pythonpath_components
                tokens = _normalize_pytest_validation_argv(tokens)
            if _is_python_validation_command(tokens):
                effective_components = _candidate_pythonpath_components(
                    workspace, effective_components
                )
            tokens, validation_executable_roots = (
                _normalize_trusted_validation_executable_argv_with_roots(
                    tokens, workspace.repo
                )
            )
            if selected_backend == VSCODE_LM_IN_PROCESS_BACKEND:
                # The editor-hosted provider has already stopped.  This is a
                # trusted-manager finalization step, not a second model or a
                # native CLI worker launch: execute only the card's exact,
                # shell-free argv inside the retained isolated worktree.  The
                # adapter allowlist above prevents native routes from using
                # this boundary to bypass their AppContainer requirement.
                wrapped = list(tokens)
                resolved_cwd = (
                    _resolve_validation_cwd(workspace, cd_relative)
                    if cd_relative is not None
                    else ""
                )
                subprocess_cwd: str | Path = (
                    workspace.path / Path(*PurePosixPath(resolved_cwd).parts)
                    if resolved_cwd
                    else workspace.path
                )
                execution_boundary = "trusted_manager_shell_free_validation"
            else:
                wrapped = sandbox_argv(
                    workspace,
                    "validation",
                    tokens,
                    backend=selected_backend,
                    validation_readonly_dirs=_validation_pythonpath_readonly_dirs(
                        effective_components
                    ),
                    validation_exec_scratch=scratch_dir,
                    validation_cwd=cd_relative,
                    validation_executable_roots=validation_executable_roots,
                    outer_validation_authority=outer_validation_authority,
                )
                subprocess_cwd = "/"
                execution_boundary = "os_sandbox"
            env = sanitized_env(
                "validation",
                home=validation_home,
                isolated_task_queue_db=True,
                verify_preprovisioned_home=selected_backend == "landlock",
            )
            env["TMPDIR"] = tmp_env_value
            env["TMP"] = tmp_env_value
            env["TEMP"] = tmp_env_value
            # Nested validation helpers must provision beneath this same exact
            # request-owned scratch root.  Without the explicit authority,
            # candidate tests that create temporary Git repositories fall back
            # to a sibling repo temp path that the outer Landlock/seccomp broker
            # cannot authorize, producing false config.lock/chmod failures.  The
            # exec scratch is the exec-capable authority nested runs anchor on;
            # it is now also the invocation's TMPDIR/TMP/TEMP above, so a nested
            # helper's own temporaries and this explicit scratch authority point
            # at one and the same mechanically verified per-invocation root.
            env[VALIDATION_EXEC_SCRATCH_ROOT_ENV] = scratch_env_value
            # Ruff writes its cache below the current working tree by default.
            # Validation worktrees are intentionally read-only, so that
            # default turns a clean lint result into a false permission
            # failure.  Keep Ruff's disposable cache inside the same private,
            # request-scoped writable scratch used for other validation
            # temporaries.  Setting this for every validation command is
            # harmless; only Ruff consumes it.
            env["RUFF_CACHE_DIR"] = scratch_env_value
            # NF180: mypy writes its incremental cache into ``.mypy_cache``
            # relative to the current directory by default. Validation
            # worktrees are intentionally read-only, so that default either
            # fails with a permission error or -- when two requests share a
            # writable mount -- races another request's cache and surfaces as
            # a spurious mypy INTERNAL ERROR. Point the cache at the same
            # private, request-scoped scratch used for the other validation
            # temporaries so identical candidate bytes and command yield
            # identical results inside and outside the validator. Setting
            # this for every validation command is harmless; only mypy
            # consumes it.
            env["MYPY_CACHE_DIR"] = scratch_env_value
            if candidate_authority["digest"]:
                env[PYTHON_CANDIDATE_AUTHORITY_ENV] = candidate_authority["digest"]
            if _is_pytest_validation_command(tokens):
                # Validation workspaces are intentionally read-only outside
                # the declared task outputs.  Pytest's cache provider writes
                # ``.pytest_cache`` beside the tests even when the tests
                # themselves are read-only, turning a green suite into a
                # false ``validation_failed`` under Landlock/bubblewrap.
                # The cache is not validation evidence, so disable only that
                # plugin while preserving the exact test selection/arguments.
                env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
            env_override_evidence: dict[str, Any] | None = None
            if effective_components:
                env["PYTHONPATH"] = resolve_validation_pythonpath(
                    workspace, selected_backend, effective_components
                )
                env_override_evidence = {
                    "variable": "PYTHONPATH",
                    "components": list(effective_components),
                }
            elif tmpdir_override is not None:
                env_override_evidence = {
                    "variable": "TMPDIR",
                    "value": tmpdir_override,
                }
            started = time.monotonic()
            try:
                result = subprocess.run(
                    wrapped,
                    cwd=subprocess_cwd,
                    env=env,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=bounded_timeout,
                    check=False,
                    shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = (
                    exc.stdout.decode("utf-8", errors="replace")
                    if isinstance(exc.stdout, bytes)
                    else str(exc.stdout or "")
                )
                stderr = (
                    exc.stderr.decode("utf-8", errors="replace")
                    if isinstance(exc.stderr, bytes)
                    else str(exc.stderr or "")
                )
                timeout_record = {
                        "command": command,
                        "argv": tokens,
                        "declared_command": command,
                        "declared_argv": declared_argv,
                        "executed_argv": tokens,
                        "argv_rewritten": declared_argv != tokens,
                        "cwd": cd_relative,
                        "env_override": env_override_evidence,
                        "sandbox_backend": selected_backend,
                        "execution_boundary": execution_boundary,
                        "python_candidate_authority": candidate_authority,
                        "interpreter_authority": interpreter_authority,
                        "returncode": None,
                        "timed_out": True,
                        "duration_seconds": round(time.monotonic() - started, 6),
                        "stdout_head": stdout[:4_096],
                        "stdout_tail": stdout[-4_096:],
                        "stdout_truncated": len(stdout) > 8_192,
                        "stderr_head": stderr[:4_096],
                        "stderr_tail": stderr[-4_096:],
                        "stderr_truncated": len(stderr) > 8_192,
                }
                timeout_record["failure_receipt"] = _validation_failure_receipt(
                    timeout_record
                )
                results.append(timeout_record)
                continue
            except OSError as exc:
                # A launch/setup failure (missing executable, EACCES, stdio
                # fds unavailable, ...) must be reported truthfully as an
                # environment failure and must never masquerade as a candidate
                # test/type failure, nor abort the remaining commands.
                launch_message = str(exc)
                launch_record = {
                    "command": command,
                    "argv": tokens,
                    "declared_command": command,
                    "declared_argv": declared_argv,
                    "executed_argv": tokens,
                    "argv_rewritten": declared_argv != tokens,
                    "cwd": cd_relative,
                    "env_override": env_override_evidence,
                    "sandbox_backend": selected_backend,
                    "execution_boundary": execution_boundary,
                    "python_candidate_authority": candidate_authority,
                    "interpreter_authority": interpreter_authority,
                    "returncode": None,
                    "timed_out": False,
                    "launch_error": type(exc).__name__,
                    "launch_error_message": launch_message,
                    "duration_seconds": round(time.monotonic() - started, 6),
                    "stdout_head": "",
                    "stdout_tail": "",
                    "stdout_truncated": False,
                    "stderr_head": launch_message[:4_096],
                    "stderr_tail": launch_message[-4_096:],
                    "stderr_truncated": len(launch_message) > 8_192,
                }
                launch_record["failure_receipt"] = _validation_failure_receipt(
                    launch_record
                )
                results.append(launch_record)
                continue
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            record = {
                "command": command,
                "argv": tokens,
                "declared_command": command,
                "declared_argv": declared_argv,
                "executed_argv": tokens,
                "argv_rewritten": declared_argv != tokens,
                "cwd": cd_relative,
                "env_override": env_override_evidence,
                "sandbox_backend": selected_backend,
                "execution_boundary": execution_boundary,
                "python_candidate_authority": candidate_authority,
                "interpreter_authority": interpreter_authority,
                "returncode": result.returncode,
                "duration_seconds": round(time.monotonic() - started, 6),
                "stdout_head": stdout[:4_096],
                "stdout_tail": stdout[-4_096:],
                "stdout_truncated": len(stdout) > 8_192,
                "stderr_head": stderr[:4_096],
                "stderr_tail": stderr[-4_096:],
                "stderr_truncated": len(stderr) > 8_192,
            }
            if result.returncode != 0:
                # NF-WAVE-SANDBOX-TRUTH: prove any missing *validator* module by
                # importing it in the SAME interpreter that ran the command, so
                # the terminal state is decided by a structural probe rather than
                # by "No module named ..." text the candidate can author.
                absent_modules = _probe_absent_validator_modules(
                    workspace, tokens, effective_components
                )
                if absent_modules:
                    record["absent_validator_modules"] = list(absent_modules)
                record["failure_receipt"] = _validation_failure_receipt(record)
                if record["failure_receipt"].get("failure_class") == "type_check_internal_error":
                    record["internal_error"] = {
                        "traceback_tail": _bounded_mypy_traceback(stderr),
                        "environment": _validation_environment_provenance(env),
                    }
            results.append(record)
        failed = [
            row
            for row in results
            if row.get("timed_out") or row.get("returncode") != 0
        ]
        if failed:
            first = failed[0]
            # NF-2026-00271/298: separate "the candidate failed its gate" from
            # "this command could not run here". ``classify_validation_results``
            # returns environment-blocked ONLY when every failing command is an
            # environment restriction -- if any is a genuine gate failure the
            # batch stays ``validation_failed``, so a broken candidate is never
            # let through as merely blocked.
            from . import validation_runner

            terminal = validation_runner.classify_validation_results(results)
            if terminal.state == validation_runner.VALIDATION_ENVIRONMENT_BLOCKED:
                stderr_detail = str(first.get("stderr_tail") or "")[-1_000:].replace("\n", "\\n")
                reason = (
                    f"{terminal.state}:{terminal.restriction}:"
                    f"{first.get('command')}:restrictions="
                    f"{','.join(terminal.restrictions)}:stderr={stderr_detail}"
                )
                raise ValidationEnvironmentBlocked(
                    reason,
                    results,
                    restriction=terminal.restriction or "",
                    restrictions=terminal.restrictions,
                )
            if first.get("timed_out"):
                reason = (
                    f"validation_timeout:{first.get('command')}:"
                    f"timeout_seconds={bounded_timeout}"
                )
            else:
                stdout_detail = str(first.get("stdout_tail") or "")[-1_000:].replace("\n", "\\n")
                stderr_detail = str(first.get("stderr_tail") or "")[-1_000:].replace("\n", "\\n")
                reason = (
                    f"validation_failed:{first.get('command')}:"
                    f"rc={first.get('returncode')}:"
                    f"stdout={stdout_detail}:stderr={stderr_detail}"
                )
            raise ValidationRunError(reason, results)
        return results
    finally:
        cleanup_validation_exec_scratch(scratch_dir)


def unlink_if_regular(path: Path) -> None:
    """Remove ``path`` only if it exists and is not a symlink.

    ``unlink(2)`` itself never dereferences a symlink for removal (it always
    removes the last path component's directory entry, not the symlink's
    target), so this is defense-in-depth rather than a fix for arbitrary-file
    deletion. It still closes the O_NOFOLLOW-safety gap an auditor would flag
    for any cleanup path that removes a short-lived, attacker-writable-looking
    filename (worker request spec files, cancel markers): if the expected
    regular file has been replaced by a symlink, skip silently instead of
    removing anything, and never follow the link to inspect its target.
    """
    try:
        if path.is_symlink():
            return
        path.unlink(missing_ok=True)
    except OSError:
        return


def write_json_0600(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    chmod_path(path.parent, 0o700)
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        chmod_fd(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.close(fd)
        fd = -1
        os.replace(temp, path)
        chmod_path(path, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


__all__ = [
    "SANDBOX_AUTHORITY_REPO",
    "SANDBOX_PACKAGE_IMPORT_ROOT",
    "SANDBOX_WORKSPACE",
    "TASK_QUEUE_ISOLATED_RELATIVE",
    "ValidationEnvironmentBlocked",
    "ValidationRunError",
    "WorkerWorkspace",
    "WorkspaceError",
    "assert_gc_safe_workspace_shape",
    "changed_paths",
    "cleanup_validation_exec_scratch",
    "cleanup_workspace",
    "configured_worktree_root",
    "create_workspace",
    "enforce_scope",
    "parse_validation_command",
    "promote",
    "provision_isolated_task_queue_db",
    "provision_validation_exec_scratch",
    "resolve_trusted_pytest_runtime_root",
    "resolve_validation_pythonpath",
    "run_validations",
    "sandbox_argv",
    "select_sandbox_backend",
    "sanitized_env",
    "unlink_if_regular",
    "validate_required_outputs",
    "validation_argv",
    "write_json_0600",
]


if __name__ == "__main__":
    try:
        if "--metadata-broker-child" in sys.argv[1:]:
            raise SystemExit(_metadata_broker_child_exec(sys.argv[1:]))
        raise SystemExit(_landlock_exec(sys.argv[1:]))
    except (OSError, WorkspaceError) as exc:
        print(f"secure sandbox setup failed: {exc}", file=sys.stderr)
        raise SystemExit(126)


@dataclass
class ReworkOverlayPacket:
    """Signed, digest-bound overlay for retained rework files."""
    successor_request_id: str
    successor_task_id: str
    predecessor_request_id: str
    predecessor_task_id: str
    authority_repo: str
    files: list[dict[str, Any]]
    canonical_digest: str


def materialize_rework_overlay(
    successor_request_id: str,
    successor_task_id: str,
    predecessor_request_id: str,
    predecessor_task_id: str,
    authority_repo: Path,
    file_entries: list[tuple[str, str | None, bytes | None]],
) -> bytes:
    """Emit a bounded, canonical-digest-bound retained-rework overlay.

    Each file entry is (repo_relative_path, sha256_or_None, content_bytes_or_None).
    sha256=None means delete; content=None with sha256 indicates a hash-only reference.
    Returns JSON bytes with a deterministic canonical_digest over the sorted files payload.
    """
    if not _REQUEST_ID_RE.fullmatch(successor_request_id):
        raise ValueError(f"invalid successor_request_id: {successor_request_id!r}")
    if not _REQUEST_ID_RE.fullmatch(predecessor_request_id):
        raise ValueError(f"invalid predecessor_request_id: {predecessor_request_id!r}")
    if successor_request_id == predecessor_request_id:
        raise ValueError("successor and predecessor request_ids must be distinct")
    # Rework intentionally reuses the canonical task ID.  The immutable claim
    # attempt is identified by a new request ID, so requiring a distinct task
    # ID made the packet impossible to wire into the real recovery path.
    if not successor_task_id or not predecessor_task_id:
        raise ValueError("successor and predecessor task_ids are required")
    authority_repo = authority_repo.resolve()
    if not authority_repo.is_dir():
        raise FileNotFoundError(f"authority_repo not found: {authority_repo}")
    if len(file_entries) > MAX_REWORK_OVERLAY_FILES:
        raise ValueError("rework overlay file count exceeds limit")
    normalized_files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    total_content_bytes = 0
    for rel_path, file_sha, content in file_entries:
        normalized_path = PurePosixPath(str(rel_path)).as_posix()
        if (
            not rel_path
            or "\\" in str(rel_path)
            or PurePosixPath(str(rel_path)).is_absolute()
            or any(part in {"", ".", ".."} for part in PurePosixPath(str(rel_path)).parts)
        ):
            raise ValueError(f"invalid repo-relative path: {rel_path!r}")
        if normalized_path in seen_paths:
            raise ValueError(f"duplicate rework overlay path: {normalized_path}")
        seen_paths.add(normalized_path)
        entry: dict[str, Any] = {"path": normalized_path}
        if file_sha is None:
            if content is not None:
                raise ValueError(f"deleted overlay path carries content: {normalized_path}")
            entry["deleted"] = True
        else:
            if not re.fullmatch(r"[0-9a-f]{64}", str(file_sha)):
                raise ValueError(f"invalid rework overlay hash: {normalized_path}")
            entry["sha256"] = file_sha
        if content is not None:
            if hashlib.sha256(content).hexdigest() != file_sha:
                raise ValueError(f"rework overlay content hash mismatch: {normalized_path}")
            total_content_bytes += len(content)
            if total_content_bytes > MAX_REWORK_OVERLAY_CONTENT_BYTES:
                raise ValueError("rework overlay content exceeds limit")
            entry["content_base64"] = base64.b64encode(content).decode("ascii")
        normalized_files.append(entry)
    normalized_files.sort(key=lambda e: e["path"])
    payload = {
        "successor_request_id": successor_request_id,
        "successor_task_id": successor_task_id,
        "predecessor_request_id": predecessor_request_id,
        "predecessor_task_id": predecessor_task_id,
        "authority_repo": str(authority_repo),
        "files": normalized_files,
    }
    payload_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    packet = {
        **payload,
        "canonical_digest": payload_digest,
    }
    return json.dumps(packet, indent=2, ensure_ascii=True).encode("utf-8")
