"""Isolated worktree and fail-closed sandbox helpers for Task MCP workers.

The parent repository is never the model process working directory. A worker
receives a detached Git worktree and a minimal HOME containing only the selected
adapter credential. Bubblewrap is preferred when usable; Landlock confines
writes when unprivileged user namespaces are blocked. Changes are promoted only
after scope, validation, and parent-content checks.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import errno
import base64
import fnmatch
import hashlib
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
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

try:
    from .platform_io import atomic_replace, chmod_fd, chmod_path
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


class ValidationRunError(WorkspaceError):
    """A bounded validation batch failed with structured rows retained."""

    def __init__(self, message: str, results: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.results = [dict(row) for row in results]


@dataclass(frozen=True, slots=True)
class WorkerWorkspace:
    request_id: str
    repo: Path
    path: Path
    home: Path
    allowed_writes: tuple[str, ...]
    parent_baseline: dict[str, str | None]
    workspace_baseline: dict[str, str | None]
    inherited_rework_paths: tuple[str, ...] = ()

    def as_metadata(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "repo": str(self.repo),
            "path": str(self.path),
            "home": str(self.home),
            "allowed_writes": list(self.allowed_writes),
            "parent_baseline": dict(self.parent_baseline),
            "workspace_baseline": dict(self.workspace_baseline),
            "inherited_rework_paths": list(self.inherited_rework_paths),
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
            inherited_rework_paths=tuple(
                _relative_repo_path(v)
                for v in payload.get("inherited_rework_paths") or ()
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


def _run(argv: list[str], *, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        shell=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )


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


def _matches(path: str, patterns: Iterable[str]) -> bool:
    normalized = _relative_repo_path(path)
    for raw in patterns:
        pattern = _relative_repo_path(raw)
        if normalized == pattern or fnmatch.fnmatchcase(normalized, pattern):
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


# ---- local quoted-include dependency preflight (B664) -----------------------
_HEADER_FILE_SUFFIXES = frozenset(
    {".h", ".hpp", ".hxx", ".hh", ".inl", ".cuh", ".c", ".cpp", ".cu", ".cc", ".cxx"}
)
_QUOTED_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+"([^"]+)"')
_DEFAULT_INCLUDE_ROOTS: tuple[str, ...] = (".",)


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

    _validate_include_roots(repo, include_roots)

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
                repo, including_dir, include_target, include_roots
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


def _validate_include_roots(repo: Path, roots: tuple[str, ...]) -> None:
    for raw in roots:
        norm = _relative_repo_path(raw) if raw != "." else "."
        if norm == ".":
            continue
        candidate = repo / norm
        if candidate.is_symlink() or not candidate.is_dir():
            raise WorkspaceError(f"include_root_not_directory:{raw}")


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
    direct = (including_dir / target).resolve()
    try:
        direct.relative_to(repo)
    except ValueError:
        pass  # escapes repo root
    else:
        if direct.exists():
            return direct

    # Rule 2: configured include roots.
    for root_raw in include_roots:
        base = repo if root_raw == "." else (repo / root_raw).resolve()
        candidate = (base / target).resolve()
        try:
            candidate.relative_to(repo)
        except ValueError:
            continue
        if candidate.exists():
            return candidate

    return None
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

    Only paths that the predecessor's terminal evidence recorded as changed
    and that the successor may write are materialized.  The retained source
    workspace, request identity, repository identity and every content hash
    are revalidated before copying.  This makes rework incremental without
    promoting rejected bytes into the canonical checkout.
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
        request_id, source_workspace.path, source_workspace.home
    )
    if source_workspace.path.is_symlink() or not source_workspace.path.is_dir():
        raise WorkspaceError("rework_predecessor_workspace_missing")

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
        source = source_home / ".claude" / ".credentials.json"
        if source.is_file():
            destination = home / ".claude" / ".credentials.json"
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, destination)
            chmod_path(destination, 0o600)
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
        )
    except worker_ai_tools_mcp.WorkerToolError as exc:
        # Provisioning/config-injection failure must reject the launch, not
        # silently degrade to a worker without a working tool surface --
        # WorkspaceError is already in process_launcher's caught-and-rejected
        # exception tuple, WorkerToolError is not.
        raise WorkspaceError(f"worker_mcp_runtime_provisioning_failed:{exc}") from exc


def configured_worktree_root() -> Path:
    """Return the single configured root for isolated worker workspaces."""
    return Path(
        os.environ.get(
            WORKTREE_ROOT_ENV,
            str(Path(tempfile.gettempdir()) / "aiworkhub-worktrees"),
        )
    ).expanduser().resolve()


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
    root = configured_worktree_root()
    if root == repo or repo in root.parents:
        raise WorkspaceError(f"worktree_root_inside_parent_repo:{root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    chmod_path(root, 0o700)
    path = root / request_id / "worktree"
    home = root / request_id / "home"
    if path == repo or repo in path.parents:
        raise WorkspaceError("worker_path_is_parent_worktree")
    if path.exists() or home.exists():
        raise WorkspaceError(f"workspace_exists:{request_id}")
    path.parent.mkdir(parents=True, exist_ok=False, mode=0o700)

    result = _run(
        ["git", "worktree", "add", "--detach", str(path), "HEAD"],
        cwd=repo,
        timeout=WORKTREE_CREATE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        shutil.rmtree(path.parent, ignore_errors=True)
        # Git reports the actionable cause (for example ENOSPC) at the end,
        # after a long checkout progress stream. Preserve that tail.
        raise WorkspaceError(f"git_worktree_add_failed:{result.stderr[-1000:]}")
    declared = list(card.get("read_first") or []) + list(card.get("immutable_inputs") or []) + list(allowed)
    try:
        seeded = _expand_declared(repo, declared)
        seeded = _resolve_local_quoted_includes(repo, seeded)
        for relative in seeded:
            destination = path / relative
            _require_beneath(path, destination)
            _require_beneath(repo, repo / relative)
            _copy_one(repo / relative, destination)
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
        baseline: dict[str, str | None] = {}
        for relative in _expand_declared(repo, allowed):
            baseline[relative] = _hash_path(repo / relative)
        workspace_baseline = {
            relative: _hash_path(path / relative)
            for relative in sorted(set(seeded) | set(_expand_declared(path, allowed)))
        }
        _credential_home(home, adapter_id, repo)
        provision_isolated_task_queue_db(repo, home)
    except Exception:
        cleanup_workspace(repo, path, home)
        raise
    detached = _run(["git", "symbolic-ref", "-q", "HEAD"], cwd=path)
    top = _run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if detached.returncode == 0 or top.returncode != 0 or Path(top.stdout.strip()).resolve() != path:
        cleanup_workspace(repo, path, home)
        raise WorkspaceError("worktree_is_not_detached_and_isolated")
    return WorkerWorkspace(
        request_id=request_id,
        repo=repo,
        path=path,
        home=home,
        allowed_writes=allowed,
        parent_baseline=baseline,
        workspace_baseline=workspace_baseline,
        inherited_rework_paths=tuple(sorted(set(rework_seeded))),
    )


def cleanup_workspace(repo: Path, path: Path, home: Path) -> None:
    repo = repo.resolve()
    path = path.resolve()
    home = home.resolve()
    if path == repo or repo in path.parents:
        raise WorkspaceError("refusing_to_cleanup_parent_worktree")
    if path.name != "worktree" or home.name != "home" or path.parent != home.parent:
        raise WorkspaceError("refusing_unsafe_workspace_cleanup")
    if path.exists():
        _run(["git", "worktree", "remove", "--force", str(path)], cwd=repo)
    shutil.rmtree(path.parent, ignore_errors=True)
    if home.exists():
        shutil.rmtree(home, ignore_errors=True)


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
    shutil.copy2(source, target)


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


def create_quality_review_workspace(
    source_workspace: WorkerWorkspace,
    request_id: str,
    candidate_changed_paths: Iterable[str],
    adapter_id: str,
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
    canonical_delta = _canonical_worktree_delta_paths(repo)
    seed_paths = sorted(set(candidate) | set(canonical_delta))
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
        for relative in canonical_delta:
            _overlay_regular_path(repo, review_workspace.path, relative)
        baseline_paths = sorted(
            set(review_workspace.workspace_baseline) | set(canonical_delta)
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
        readonly = replace(
            review_workspace,
            allowed_writes=(),
            parent_baseline={},
            workspace_baseline={
                relative: _hash_path(review_workspace.path / relative)
                for relative in sorted(set(baseline_paths) | set(candidate))
            },
        )
        return readonly, {
            "schema_id": "aiworkhub.quality_review_workspace.v1",
            "candidate_paths": candidate,
            "canonical_delta_paths": canonical_delta,
            "readonly": True,
        }
    except Exception:
        cleanup_workspace(repo, review_workspace.path, review_workspace.home)
        raise


def assert_gc_safe_workspace_shape(request_id: str, path: Path, home: Path) -> Path:
    """Fail closed unless this is the request's exact configured workspace."""
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise WorkspaceError(f"gc_invalid_request_id:{request_id}")
    root = configured_worktree_root()
    expected_path = (root / request_id / "worktree").resolve(strict=False)
    expected_home = (root / request_id / "home").resolve(strict=False)
    if path.resolve(strict=False) != expected_path or home.resolve(strict=False) != expected_home:
        raise WorkspaceError(
            f"gc_workspace_shape_mismatch:{request_id}:path={path}:home={home}"
        )
    return root


def changed_paths(workspace: WorkerWorkspace) -> list[str]:
    tracked = _run(
        ["git", "diff", "--name-only", "-z", "HEAD"], cwd=workspace.path
    )
    if tracked.returncode != 0:
        raise WorkspaceError(f"git_diff_failed:{tracked.stderr[:300]}")
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=workspace.path,
    )
    if untracked.returncode != 0:
        raise WorkspaceError(f"git_untracked_failed:{untracked.stderr[:300]}")
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


def enforce_scope(workspace: WorkerWorkspace) -> list[str]:
    changed = changed_paths(workspace)
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
    for pattern in required_patterns:
        if not _matches(pattern, workspace.allowed_writes):
            raise WorkspaceError(f"required_output_not_allowed:{pattern}")
        matches: list[str]
        if any(ch in pattern for ch in "*?["):
            matches = _required_output_glob_matches(workspace.path, pattern)
            if not matches:
                raise WorkspaceError(f"required_output_no_matches:{pattern}")
        else:
            matches = [pattern]
        for relative in matches:
            target = workspace.path / relative
            _require_beneath(workspace.path, target)
            if target.is_symlink():
                raise WorkspaceError(f"required_output_symlink:{relative}")
            if not target.is_file():
                raise WorkspaceError(f"required_output_missing:{relative}")
            size = target.stat().st_size
            if size <= 0 and (allow_empty is None or relative not in allow_empty):
                raise WorkspaceError(f"required_output_zero_bytes:{relative}")
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
                        raise WorkspaceError(f"required_output_unchanged:{relative}")
                parent_hash = workspace.parent_baseline.get(relative)
                if current_hash != parent_hash:
                    raise WorkspaceError(f"required_output_unchanged_parent_mismatch:{relative}")
                if current_hash is None or target.is_symlink() or not target.is_file() or size <= 0:
                    raise WorkspaceError(f"required_output_unchanged_invalid:{relative}")
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
    return records


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

    promoted: list[str] = []
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
    probe_path = directory / f".exec_probe_{uuid.uuid4().hex}"
    try:
        # The executable bit is requested directly on the atomic O_CREAT --
        # never via a separate chmod(2)/fchmod(2) follow-up call. This keeps
        # the probe itself usable even where chmod-family syscalls are denied
        # (e.g. this exact code running nested under its own
        # ``_apply_metadata_seccomp`` deny-list) and closes the TOCTOU window
        # a create-then-chmod sequence would otherwise leave open.
        fd = os.open(
            probe_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o700
        )
    except OSError:
        return False
    try:
        try:
            os.write(fd, _EXEC_PROBE_SCRIPT)
        finally:
            os.close(fd)
        result = subprocess.run(
            [str(probe_path)],
            cwd=str(directory),
            env={"PATH": "/usr/bin:/bin"},
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
    """Best-effort, self-cleaning probe for git-required chmod semantics."""
    if directory.is_symlink() or not directory.is_dir():
        return False
    probe_path = directory / f".metadata_probe_{uuid.uuid4().hex}"
    try:
        fd = os.open(probe_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return False
    try:
        os.close(fd)
        os.chmod(probe_path, 0o600)
        return stat.S_IMODE(os.stat(probe_path).st_mode) == 0o600
    except OSError:
        return False
    finally:
        probe_path.unlink(missing_ok=True)


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
    for raw_root in _exec_scratch_candidate_roots():
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
        if stat.S_IMODE(root_info.st_mode) & 0o002:
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
    if stat.S_IMODE(info.st_mode) & 0o002:
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


def _normalize_trusted_validation_executable_argv(
    argv: list[str], repo: Path | None = None
) -> list[str]:
    normalized, _roots = _normalize_trusted_validation_executable_argv_with_roots(
        argv, repo
    )
    return normalized


def _normalize_trusted_validation_executable_argv_with_roots(
    argv: list[str], repo: Path | None = None
) -> tuple[list[str], tuple[Path, ...]]:
    if not argv:
        return [], ()
    head = argv[0]
    # AIWorkHub itself requires Python 3, while many POSIX hosts deliberately
    # expose only ``python3`` in the credential-free validation PATH.  Treat
    # the common ``python`` spelling as the portable Python-3 alias instead
    # of letting execvpe fail against a non-existent /bin/python.  Explicit
    # python3/path spellings remain untouched.
    if os.name != "nt" and head == "python":
        argv = ["python3", *argv[1:]]
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
        return [
            sys.executable,
            str(Path(__file__).resolve()),
            "--landlock-exec",
            "--workspace", str(workspace.path),
            "--home", str(workspace.home),
            *(value for pattern in workspace.allowed_writes for value in ("--allow", pattern)),
            *exec_scratch_flags,
            *cwd_flags,
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
    return _openat2_available()


def _metadata_broker_verify_mode(mode: int) -> int:
    """Permit only ordinary permission bits; never setuid/setgid/sticky."""
    if mode < 0 or mode & ~0o777:
        raise WorkspaceError(f"metadata_broker_unsafe_mode:{mode:o}")
    return mode


def _metadata_broker_verify_flags(flags: int) -> int:
    if flags != 0:
        raise WorkspaceError(f"metadata_broker_unsupported_flags:{flags}")
    return flags


def _metadata_broker_verify_fd(fd: int, candidate: str) -> None:
    """Fail closed unless ``fd`` is a scratch-owned target beneath the request scratch.

    Owned directories are permitted so validators can manage scratch-directory
    metadata (e.g. ``os.chmod(parent, 0o700)`` after ``path.parent.mkdir``).
    Regular files still require ``st_nlink == 1`` (no hardlinks). Special files
    (devices, sockets, FIFOs) remain denied.
    """
    info = os.fstat(fd)
    if stat.S_ISDIR(info.st_mode):
        if info.st_uid != os.getuid():
            raise WorkspaceError(f"metadata_broker_foreign_owner:{candidate}")
        return
    if not stat.S_ISREG(info.st_mode):
        raise WorkspaceError(f"metadata_broker_not_regular_file:{candidate}")
    if info.st_nlink != 1:
        raise WorkspaceError(f"metadata_broker_hardlink_forbidden:{candidate}")
    if info.st_uid != os.getuid():
        raise WorkspaceError(f"metadata_broker_foreign_owner:{candidate}")


def _metadata_broker_verify_target(
    candidate: str, scratch_fd: int, scratch_root: PurePosixPath
) -> int:
    """Open and return a verified, mutable fd strictly beneath the scratch.

    ``scratch_fd`` is a stable directory descriptor for the exact request-owned
    validation exec scratch and ``scratch_root`` its resolved absolute path
    (used only to derive the beneath-scratch relative component). The kernel is
    the resolution authority: ``openat2`` with RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS
    fails closed on traversal, absolute/outside paths, symlinked roots and
    symlink targets -- never a userspace string-prefix comparison. The returned
    descriptor passes ``_metadata_broker_verify_fd`` (owned directories are
    permitted; regular files require ``st_nlink == 1``); the caller mutates
    that exact fd and closes it.

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
            _metadata_broker_verify_fd(fd, candidate)
            return fd
        _metadata_broker_verify_fd(fd, candidate)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise
    return fd


def _metadata_broker_process_pgid(pid: int) -> int:
    """Return ``pid``'s process-group id parsed from ``/proc/<pid>/stat``.

    The ``comm`` field can contain spaces and parentheses, so the fields after
    the final ``)`` are used: ``state ppid pgrp ...`` -- ``pgrp`` is the third.
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
    try:
        return int(fields[2])
    except ValueError as exc:
        raise WorkspaceError(f"metadata_broker_stat_malformed:{pid}") from exc


def _metadata_broker_authenticate_pid(pid: int, child_pid: int) -> None:
    """Accept only the broker child or a live descendant in its process group.

    The disposable child calls ``setsid`` before ``exec``, so every legitimate
    validator descendant shares ``pgid == child_pid`` and no unrelated process
    can join that freshly created session. Re-read on every request so a dead,
    reused or foreign pid is rejected fail-closed.
    """
    if pid <= 0:
        raise WorkspaceError(f"metadata_broker_bad_pid:{pid}")
    if pid == child_pid:
        return
    if _metadata_broker_process_pgid(pid) != child_pid:
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
    scratch_fd: int,
    scratch_root: PurePosixPath,
) -> None:
    """Emulate exactly one verified brokered metadata syscall in the parent.

    Authenticates the notifying pid as a live descendant in the broker child's
    process group (validators legitimately fork -- e.g. ``git`` -- so the caller
    is often not ``child_pid`` itself), re-checks the notification id both
    before and after acquiring the target, resolves the target with the kernel
    as authority (``openat2`` beneath the exact scratch fd) and mutates only a
    stable, inode-verified file or directory.
    """
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
        verified_fd = _metadata_broker_verify_target(link, scratch_fd, scratch_root)
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
                _metadata_broker_verify_fd(fd_target, f"/proc/{pid}/fd/{raw_fd}")
                _metadata_broker_check_notification(library, listener_fd, request.id)
                # Mutate the exact descriptor the child blocked on, proven by
                # inode identity to be the same beneath-scratch file --
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
    verified_fd = _metadata_broker_verify_target(raw_target, scratch_fd, scratch_root)
    try:
        _metadata_broker_check_notification(library, listener_fd, request.id)
        os.fchmod(verified_fd, mode)
    finally:
        os.close(verified_fd)


def _metadata_broker_exit_code(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def _run_metadata_broker(listener_fd: int, child_pid: int, scratch: Path) -> int:
    """Trusted parent loop: emulate only verified brokered operations.

    Opens one stable directory fd for the exact request scratch (the single
    resolution root handed to every ``openat2`` acquisition), polls with a
    bounded timeout, reaps the child non-blockingly, and enforces an overall
    deadline. On any deadline or error the entire validator process group is
    killed and the child reaped, so a wedged validator can never deadlock the
    broker, orphan descendants, or leak descriptors; every allocated
    notification pair is freed on every path.
    """
    import select

    library = _seccomp_notify_library()
    if library is None:
        raise WorkspaceError("seccomp_user_notification_unavailable")
    scratch_dir_fd = -1
    try:
        # Open the exact supplied root without following its final component.
        # Resolving first would silently turn a symlink root into authority for
        # its target. All later target resolution is anchored to this stable fd.
        try:
            scratch_dir_fd = os.open(
                scratch,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
        except OSError as exc:
            raise WorkspaceError(
                f"metadata_broker_scratch_unavailable:{exc}"
            ) from exc
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
        scratch_root_posix = PurePosixPath(str(scratch_root))
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
                        scratch_dir_fd,
                        scratch_root_posix,
                    )
                    response.val = 0
                    response.error = 0
                except (WorkspaceError, OSError, ValueError):
                    response.val = 0
                    response.error = -errno.EPERM
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
        if scratch_dir_fd >= 0:
            os.close(scratch_dir_fd)


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


def _landlock_exec(argv: list[str]) -> int:
    import select
    import socket

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--landlock-exec", action="store_true", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument("--exec-scratch", default=None)
    parser.add_argument("--cwd", default=None)
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
    _apply_landlock(workspace, home, args.allow, exec_scratch)
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
        child_pid = os.fork()
        if child_pid == 0:
            try:
                parent_sock.close()
                # New session so the whole validator subtree shares
                # ``pgid == child_pid``: this lets the trusted parent
                # authenticate brokered requests as descendants and kill the
                # entire group on error/timeout, and the parent-death signal
                # makes the child die if the broker parent is killed first.
                os.setsid()
                _set_pdeathsig_sigkill(broker_parent_pid)
                listener = _install_metadata_notify_filter()
                socket.send_fds(child_sock, [b"1"], [listener])
                os.close(listener)
                child_sock.close()
            except BaseException as exc:
                try:
                    diagnostic = (
                        f"{type(exc).__name__}:{exc}".encode("utf-8", "replace")[:1024]
                    )
                    child_sock.sendall(b"E" + diagnostic)
                except BaseException:
                    pass
                os._exit(126)
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
            os._exit(126)
        child_sock.close()
        listener_fd = -1
        listener_error = ""
        try:
            parent_sock.setblocking(False)
            ready, _writable, _exceptional = select.select(
                [parent_sock], [], [], _METADATA_BROKER_HANDSHAKE_SECONDS
            )
            if ready:
                payload, fds, _flags, _addr = socket.recv_fds(parent_sock, 1025, 1)
                if fds:
                    listener_fd = fds[0]
                elif payload.startswith(b"E"):
                    listener_error = payload[1:].decode("utf-8", "replace")
        except OSError:
            listener_fd = -1
        finally:
            parent_sock.close()
        if listener_fd < 0:
            _kill_validator_group(child_pid)
            _reap_validator(child_pid)
            suffix = f":{listener_error}" if listener_error else ""
            raise WorkspaceError(f"metadata_broker_listener_transfer_failed{suffix}")
        try:
            return _run_metadata_broker(listener_fd, child_pid, exec_scratch)
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
    try:
        target = _require_beneath(Path("/"), Path(component))
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
    approved_site = Path(site.getusersitepackages()).resolve()
    if target != approved_site or not target.is_dir():
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
        if component.startswith("/"):
            target = _approved_pythonpath_site(component)
            resolved.append(
                f"/validation-pythonpath/{absolute_index}"
                if backend == "bubblewrap"
                else str(target)
            )
            absolute_index += 1
            continue
        target = _require_beneath(workspace.path, workspace.path / component)
        if target.is_symlink() or not target.is_dir():
            raise WorkspaceError(f"validation_pythonpath_not_directory:{component}")
        if backend == "bubblewrap":
            resolved.append(f"{base}/{PurePosixPath(component).as_posix()}")
        else:
            resolved.append(str(workspace.path / Path(*PurePosixPath(component).parts)))
    return os.pathsep.join(resolved)


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


def resolve_trusted_pytest_runtime_root() -> Path:
    """Resolve and validate the one canonical, read-only pytest package root
    used to repair ``pytest``/``python3 -m pytest`` validation commands under
    the sanitized, credential-free validation HOME (B755).

    B753/B674 false negative: an isolated validation run's sanitized HOME has
    no ``~/.local/lib/pythonX/site-packages`` of its own, so a pytest
    validation command fails with ``ModuleNotFoundError: No module named
    'pytest'`` even though the parent host has pytest installed. This
    resolves the exact same trusted root ``resolve_validation_pythonpath``
    already accepts as the sole approved absolute PYTHONPATH component
    (``site.getusersitepackages()`` under the current, unsandboxed
    coordinator interpreter's real HOME), and additionally rejects it
    outright if it is a symlink, not owned by this process's user,
    world-writable, or does not actually contain an importable ``pytest``
    package -- never a copy, never any other real-HOME content, never
    writable. Fails closed with a ``WorkspaceError`` if no such root exists,
    instead of silently handing back a path that will fail to import at test
    time.
    """
    raw = Path(site.getusersitepackages())
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
    # ownership is meaningful; Windows ACLs protect the user site-packages
    # tree, and os.getuid() is unavailable there.
    if os.name != "nt" and info.st_uid != os.getuid():
        raise WorkspaceError(f"validation_pytest_runtime_untrusted_owner:{candidate}")
    if stat.S_IMODE(info.st_mode) & 0o002:
        raise WorkspaceError(f"validation_pytest_runtime_world_writable:{candidate}")
    package_init = candidate / "pytest" / "__init__.py"
    if package_init.is_symlink() or not package_init.is_file():
        raise WorkspaceError(f"validation_pytest_runtime_missing_pytest:{candidate}")
    return candidate


def _validation_pythonpath_readonly_dirs(components: tuple[str, ...]) -> tuple[Path, ...]:
    rows: list[Path] = []
    for component in components:
        if not component.startswith("/"):
            continue
        rows.append(_approved_pythonpath_site(component))
    return tuple(rows)


def run_validations(
    workspace: WorkerWorkspace,
    commands: Iterable[str],
    *,
    timeout_seconds: int = MAX_VALIDATION_SECONDS,
    backend: str | None = None,
    adapter_id: str = "",
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
    selected_backend = backend or select_sandbox_backend()
    if selected_backend not in {
        "landlock",
        "bubblewrap",
        VSCODE_LM_IN_PROCESS_BACKEND,
    }:
        raise WorkspaceError(f"unsupported_sandbox_backend:{selected_backend}")
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
    scratch_dir = provision_validation_exec_scratch(workspace)
    scratch_env_value = (
        str(scratch_dir)
        if selected_backend in {"landlock", VSCODE_LM_IN_PROCESS_BACKEND}
        else SANDBOX_VALIDATION_EXEC_SCRATCH
    )
    try:
        for command in rows:
            (
                tokens,
                pythonpath_components,
                tmpdir_override,
                cd_relative,
            ) = _parse_validation_command_detailed(command)
            declared_argv = list(tokens)
            effective_components = pythonpath_components
            if _is_pytest_validation_command(tokens):
                # B755: bind and prepend the one trusted pytest package root
                # ahead of whatever relative project PYTHONPATH the card
                # already declared. Fails closed before this command ever
                # runs if no approved pytest runtime exists. Non-pytest
                # commands never reach this branch, so their env/argv stay
                # byte-equivalent to before.
                pytest_root = resolve_trusted_pytest_runtime_root()
                effective_components = (str(pytest_root),) + pythonpath_components
                tokens = _normalize_pytest_validation_argv(tokens)
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
                )
                subprocess_cwd = "/"
                execution_boundary = "os_sandbox"
            env = sanitized_env(
                "validation",
                home=validation_home,
                isolated_task_queue_db=True,
                verify_preprovisioned_home=selected_backend == "landlock",
            )
            env["TMPDIR"] = scratch_env_value
            env["TMP"] = scratch_env_value
            env["TEMP"] = scratch_env_value
            # Ruff writes its cache below the current working tree by default.
            # Validation worktrees are intentionally read-only, so that
            # default turns a clean lint result into a false permission
            # failure.  Keep Ruff's disposable cache inside the same private,
            # request-scoped writable scratch used for other validation
            # temporaries.  Setting this for every validation command is
            # harmless; only Ruff consumes it.
            env["RUFF_CACHE_DIR"] = scratch_env_value
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
                results.append(
                    {
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
                )
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
                "returncode": result.returncode,
                "duration_seconds": round(time.monotonic() - started, 6),
                "stdout_head": stdout[:4_096],
                "stdout_tail": stdout[-4_096:],
                "stdout_truncated": len(stdout) > 8_192,
                "stderr_head": stderr[:4_096],
                "stderr_tail": stderr[-4_096:],
                "stderr_truncated": len(stderr) > 8_192,
            }
            results.append(record)
        failed = [
            row
            for row in results
            if row.get("timed_out") or row.get("returncode") != 0
        ]
        if failed:
            first = failed[0]
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
        raise SystemExit(_landlock_exec(sys.argv[1:]))
    except (OSError, WorkspaceError) as exc:
        print(f"secure sandbox setup failed: {exc}", file=sys.stderr)


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
    """Emit canonical-digest-bound overlay packet with distinct successor/predecessor identities.

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
    if successor_task_id == predecessor_task_id:
        raise ValueError("successor and predecessor task_ids must be distinct")
    authority_repo = authority_repo.resolve()
    if not authority_repo.is_dir():
        raise FileNotFoundError(f"authority_repo not found: {authority_repo}")
    normalized_files: list[dict[str, Any]] = []
    for rel_path, file_sha, content in file_entries:
        if not rel_path or ".." in rel_path or rel_path.startswith("/"):
            raise ValueError(f"invalid repo-relative path: {rel_path!r}")
        entry: dict[str, Any] = {"path": rel_path}
        if file_sha is None:
            entry["deleted"] = True
        else:
            entry["sha256"] = file_sha
        if content is not None:
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
