"""Isolated worktree and fail-closed sandbox helpers for Task MCP workers.

The parent repository is never the model process working directory. A worker
receives a detached Git worktree and a minimal HOME containing only the selected
adapter credential. Bubblewrap is preferred when usable; Landlock confines
writes when unprivileged user namespaces are blocked. Changes are promoted only
after scope, validation, and parent-content checks.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
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
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


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


WORKTREE_ROOT_ENV = "GEOAI_TASK_MCP_WORKTREE_ROOT"
BWRAP_ENV = "GEOAI_TASK_MCP_BWRAP"
SANDBOX_BACKEND_ENV = "GEOAI_TASK_MCP_SANDBOX_BACKEND"
SANDBOX_WORKSPACE = "/workspace"
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
# GEOAI_TASK_MCP_VALIDATION_EXEC_SCRATCH_ROOT; otherwise a small fixed
# candidate list is probed in order and the first exec-capable one wins.
# Fails closed (raises WorkspaceError) if nothing usable is found -- never
# chmods or remounts a shared filesystem to force it to work.
VALIDATION_EXEC_SCRATCH_ROOT_ENV = "GEOAI_TASK_MCP_VALIDATION_EXEC_SCRATCH_ROOT"
_DEFAULT_EXEC_SCRATCH_ROOTS: tuple[Path, ...] = (Path("/dev/shm"), Path(tempfile.gettempdir()))
SANDBOX_VALIDATION_EXEC_SCRATCH = "/validation-exec-scratch"
_EXEC_SCRATCH_NAME_PREFIX = "geoai_task_mcp_validation_exec_"
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
_ISOLATED_QUEUE_PLACEHOLDER_TASK_ID = "GEOAI_TASK_MCP_ISOLATED_QUEUE_PLACEHOLDER_B328"

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


@dataclass(frozen=True, slots=True)
class WorkerWorkspace:
    request_id: str
    repo: Path
    path: Path
    home: Path
    allowed_writes: tuple[str, ...]
    parent_baseline: dict[str, str | None]
    workspace_baseline: dict[str, str | None]

    def as_metadata(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "repo": str(self.repo),
            "path": str(self.path),
            "home": str(self.home),
            "allowed_writes": list(self.allowed_writes),
            "parent_baseline": dict(self.parent_baseline),
            "workspace_baseline": dict(self.workspace_baseline),
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
        )


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
    if source.is_symlink():
        raise WorkspaceError(f"symlink_seed_forbidden:{source}")
    if source.is_dir():
        raise WorkspaceError(f"directory_seed_forbidden:{source}")
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _touch_placeholder(worktree: Path, relative: str) -> None:
    target = worktree / relative
    _require_beneath(worktree, target)
    if target.exists() or target.is_symlink():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.fchmod(fd, 0o644)
    os.close(fd)


def _credential_home(home: Path, adapter_id: str, project_root: Path | None = None) -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    source_home = Path.home()
    if adapter_id == "claude_cli":
        source = source_home / ".claude" / ".credentials.json"
        if source.is_file():
            destination = home / ".claude" / ".credentials.json"
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)
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
            os.chmod(trust_config, 0o600)
    elif adapter_id == "codex_cli":
        source = source_home / ".codex" / "auth.json"
        if source.is_file():
            destination = home / ".codex" / "auth.json"
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)


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
        f"_geoai_task_mcp_taskdb_ro_{uuid.uuid4().hex}", module_path
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
        "allowed_writes": ["tools/geoai-task-mcp/eval/_b328_isolated_queue_placeholder_never_written.json"],
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
    os.chmod(destination.parent, 0o700)
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


def configured_worktree_root() -> Path:
    """Return the single configured root for isolated worker workspaces."""
    return Path(
        os.environ.get(
            WORKTREE_ROOT_ENV,
            str(Path(tempfile.gettempdir()) / "geoai-task-mcp-worktrees"),
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
    allowed = tuple(_relative_repo_path(v) for v in card.get("allowed_writes") or [])
    if not allowed:
        raise WorkspaceError("allowed_writes_empty")
    if any(PurePosixPath(pattern).parts[0] == ".git" for pattern in allowed):
        raise WorkspaceError("git_metadata_write_forbidden")
    root = configured_worktree_root()
    if root == repo or repo in root.parents:
        raise WorkspaceError(f"worktree_root_inside_parent_repo:{root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
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
    )
    if result.returncode != 0:
        shutil.rmtree(path.parent, ignore_errors=True)
        raise WorkspaceError(f"git_worktree_add_failed:{result.stderr[:300]}")
    declared = list(card.get("read_first") or []) + list(allowed)
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
        if _hash_path(workspace.path / relative) != initial_hash:
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


def validate_required_outputs(
    workspace: WorkerWorkspace,
    required_outputs: Iterable[str],
    allow_empty: tuple[str, ...] | None = None,
    allow_unchanged: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Validate every declared required output exists, is non-empty, and changed.

    ``allow_empty`` is an exact, repo-relative path allowlist for deliberately
    zero-byte outputs (e.g. a contradiction lane that honestly produced no rows).
    Paths not in this set are still rejected for zero bytes.  The caller must pass
    only snapshotted metadata, never the mutable card.

    ``allow_unchanged`` is an exact path allowlist for required outputs that may
    remain byte-equal to both launch baselines. These records are reported but
    must not be promoted by callers.
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
            is_unchanged = current_hash == workspace.workspace_baseline.get(relative)
            if is_unchanged:
                if relative not in unchanged_allowed:
                    raise WorkspaceError(f"required_output_unchanged:{relative}")
                parent_hash = workspace.parent_baseline.get(relative)
                if current_hash != parent_hash:
                    raise WorkspaceError(f"required_output_unchanged_parent_mismatch:{relative}")
                if current_hash is None or target.is_symlink() or not target.is_file() or size <= 0:
                    raise WorkspaceError(f"required_output_unchanged_invalid:{relative}")
            if is_unchanged is False and relative in unchanged_allowed:
                raise WorkspaceError(f"allow_unchanged_required_output_changed:{relative}")
            records.append({
                "pattern": pattern,
                "path": relative,
                "bytes": size,
                "sha256": current_hash,
                "unchanged_allowed": is_unchanged,
            })
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
            os.chmod(temp, mode or 0o644)
            os.replace(temp, parent)
        finally:
            temp.unlink(missing_ok=True)
        promoted.append(relative)
    return promoted


def sanitized_env(
    adapter_id: str,
    *,
    home: Path | None = None,
    isolated_task_queue_db: bool = False,
    provider_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    selected_home = (home or Path(bubblewrap_home_env_value())).resolve()
    if home is not None:
        selected_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(selected_home, 0o700)
        temp_home = selected_home / "tmp"
        temp_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(temp_home, 0o700)
    else:
        # Bubblewrap provides a private /tmp. Do not chmod or create anything
        # in the caller's real HOME while constructing the child environment.
        temp_home = Path("/tmp")
    safe: dict[str, str] = {
        "HOME": str(selected_home),
        "USER": os.environ.get("USER", "shrek"),
        "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", "shrek")),
        "SHELL": "/bin/bash",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", os.environ.get("LANG", "C.UTF-8")),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "TMPDIR": str(temp_home),
        "TMP": str(temp_home),
        "TEMP": str(temp_home),
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
        if _probe_exec_capable_dir(scratch_dir):
            return scratch_dir
        tried.append(f"{root}:noexec")
        shutil.rmtree(scratch_dir, ignore_errors=True)
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


def select_sandbox_backend() -> str:
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
) -> list[str]:
    if not adapter_argv:
        raise WorkspaceError("adapter_argv_empty")
    selected = backend or select_sandbox_backend()
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
        return [
            sys.executable,
            str(Path(__file__).resolve()),
            "--landlock-exec",
            "--workspace", str(workspace.path),
            "--home", str(workspace.home),
            *(value for pattern in workspace.allowed_writes for value in ("--allow", pattern)),
            *exec_scratch_flags,
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
        "--chdir", SANDBOX_WORKSPACE,
    ]
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
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--landlock-exec", action="store_true", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument("--exec-scratch", default=None)
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
    _apply_metadata_seccomp()
    os.chdir(workspace)
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


def _parse_validation_command_detailed(
    command: str,
) -> tuple[list[str], tuple[str, ...], str | None]:
    """Parse the private validation-only environment channel.

    The public ``parse_validation_command`` API intentionally remains a
    two-tuple.  TMPDIR is accepted only for the one canonical value used by
    task cards; arbitrary assignments and alternate paths stay fail-closed.
    """
    argv = _tokenize_validation_command(command)
    env, rest = _split_validation_env_prefix(argv)
    components: tuple[str, ...] = ()
    tmpdir_override: str | None = None
    if "PYTHONPATH" in env:
        components = _validate_pythonpath_value(env["PYTHONPATH"])
    if "TMPDIR" in env:
        raw_value = env["TMPDIR"]
        if raw_value != _SUPPORTED_VALIDATION_TMPDIR_VALUE:
            raise WorkspaceError(f"validation_tmpdir_value_not_supported:{raw_value}")
        tmpdir_override = raw_value
    return rest, components, tmpdir_override


def parse_validation_command(command: str) -> tuple[list[str], tuple[str, ...]]:
    """Return executable argv plus an optional bounded PYTHONPATH component list."""
    argv, components, _tmpdir_override = _parse_validation_command_detailed(command)
    return argv, components


def validation_argv(command: str) -> list[str]:
    return parse_validation_command(command)[0]


def resolve_validation_pythonpath(
    workspace: WorkerWorkspace, backend: str, components: tuple[str, ...]
) -> str:
    """Resolve validated relative directories in the child-visible workspace."""
    base = SANDBOX_WORKSPACE if backend == "bubblewrap" else str(workspace.path)
    resolved: list[str] = []
    approved_site = Path(site.getusersitepackages()).resolve()
    absolute_index = 0
    for component in components:
        if component == ".":
            resolved.append(base)
            continue
        if component.startswith("/"):
            target = Path(component).resolve(strict=False)
            if target != approved_site or target.is_symlink() or not target.is_dir():
                raise WorkspaceError(
                    f"validation_pythonpath_absolute_component_forbidden:{component}"
                )
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
        resolved.append(f"{base}/{PurePosixPath(component).as_posix()}")
    return os.pathsep.join(resolved)


def _is_pytest_validation_command(argv: list[str]) -> bool:
    """True when *argv* (post-PYTHONPATH-prefix-strip) invokes pytest."""
    if not argv:
        return False
    head = Path(argv[0]).name
    if head == "pytest":
        return True
    return len(argv) >= 3 and head.startswith("python") and argv[1] == "-m" and argv[2] == "pytest"


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
    if info.st_uid != os.getuid():
        raise WorkspaceError(f"validation_pytest_runtime_untrusted_owner:{candidate}")
    if stat.S_IMODE(info.st_mode) & 0o002:
        raise WorkspaceError(f"validation_pytest_runtime_world_writable:{candidate}")
    package_init = candidate / "pytest" / "__init__.py"
    if package_init.is_symlink() or not package_init.is_file():
        raise WorkspaceError(f"validation_pytest_runtime_missing_pytest:{candidate}")
    return candidate


def _validation_pythonpath_readonly_dirs(components: tuple[str, ...]) -> tuple[Path, ...]:
    approved_site = Path(site.getusersitepackages()).resolve()
    rows: list[Path] = []
    for component in components:
        if not component.startswith("/"):
            continue
        target = Path(component).resolve(strict=False)
        if target != approved_site or target.is_symlink() or not target.is_dir():
            raise WorkspaceError(
                f"validation_pythonpath_absolute_component_forbidden:{component}"
            )
        rows.append(target)
    return tuple(rows)


def run_validations(
    workspace: WorkerWorkspace,
    commands: Iterable[str],
    *,
    timeout_seconds: int = MAX_VALIDATION_SECONDS,
) -> list[dict[str, Any]]:
    rows = list(commands)
    if len(rows) > MAX_VALIDATION_COMMANDS:
        raise WorkspaceError(f"validation_command_limit_exceeded:{len(rows)}")
    results: list[dict[str, Any]] = []
    backend = select_sandbox_backend()
    validation_home = workspace.home if backend == "landlock" else None
    bounded_timeout = max(1, min(timeout_seconds, MAX_VALIDATION_SECONDS))
    # B753: one private, exec-probed scratch directory for this whole
    # validation run -- provisioned before any command executes, and always
    # torn down in the ``finally`` below regardless of how the run ends
    # (every command passing, a failing command's WorkspaceError, a timeout,
    # or any other exception propagating out of this function).
    scratch_dir = provision_validation_exec_scratch(workspace)
    scratch_env_value = (
        str(scratch_dir) if backend == "landlock" else SANDBOX_VALIDATION_EXEC_SCRATCH
    )
    try:
        for command in rows:
            tokens, pythonpath_components, tmpdir_override = _parse_validation_command_detailed(
                command
            )
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
            wrapped = sandbox_argv(
                workspace,
                "validation",
                tokens,
                backend=backend,
                validation_readonly_dirs=_validation_pythonpath_readonly_dirs(
                    effective_components
                ),
                validation_exec_scratch=scratch_dir,
            )
            env = sanitized_env(
                "validation", home=validation_home, isolated_task_queue_db=True
            )
            env["TMPDIR"] = scratch_env_value
            env["TMP"] = scratch_env_value
            env["TEMP"] = scratch_env_value
            env_override_evidence: dict[str, Any] | None = None
            if effective_components:
                env["PYTHONPATH"] = resolve_validation_pythonpath(
                    workspace, backend, effective_components
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
            try:
                result = subprocess.run(
                    wrapped,
                    cwd="/",
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=bounded_timeout,
                    check=False,
                    shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise WorkspaceError(
                    f"validation_timeout:{command}:timeout_seconds={bounded_timeout}"
                ) from exc
            record = {
                "command": command,
                "argv": tokens,
                "env_override": env_override_evidence,
                "returncode": result.returncode,
                "stdout_tail": result.stdout[-8_192:],
                "stderr_tail": result.stderr[-8_192:],
            }
            results.append(record)
            if result.returncode != 0:
                stdout_detail = result.stdout[-1_000:].replace("\n", "\\n")
                stderr_detail = result.stderr[-1_000:].replace("\n", "\\n")
                raise WorkspaceError(
                    f"validation_failed:{command}:rc={result.returncode}:"
                    f"stdout={stdout_detail}:stderr={stderr_detail}"
                )
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
    os.chmod(path.parent, 0o700)
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        os.close(fd)
        temp.unlink(missing_ok=True)


__all__ = [
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
        raise SystemExit(126)
