from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


from . import (
    COORDINATOR_TOKEN_ENV,
    COORDINATOR_TOKEN_FILE_ENV,
    coordinator_config,
    refresh_coordinator_config,
)
from . import repository_state
from . import shared_router
from . import sqlite_readonly
from . import task_store
from . import callback_store
from . import task_plan
from . import dependency_autolaunch


DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("AIWORKHUB_TIMEOUT", "60"))
# Repository-local runtime tree (never durable, never shared across repos):
# .aiworkhub/runtime/process_logs/audit.jsonl -- see repository_state.py's
# RepositoryManifest runtime layout ("process_logs" is one of its declared
# contents). Never a fixed path relative to this package's own install
# location or to a historical monorepo layout.
AUDIT_LOG_DEFAULT_REL = (
    Path(repository_state.HUB_DIRNAME)
    / repository_state.RUNTIME_DIRNAME
    / "process_logs"
    / "audit.jsonl"
)

_SECRET_ENV_PATTERNS = (
    "SECRET", "TOKEN", "PASSWORD", "PASSWD", "KEY", "API_KEY",
    "CREDENTIAL", "AUTH", "PRIVATE", "CERT",
)


WRITE_COMMANDS = {
    "add-card",
    "archive",
    "auto-pickup",
    "claim-start",
    "done",
    "export-jsonl",
    "import-jsonl",
    "init-db",
    "owner-review-recover",
    "pickup",
    "recover-stale",
    "restore",
    "review",
    "reject-review",
    "release-launch",
    "stage",
    "start",
    "unstick-pending",
    "usage",
}

COORDINATOR_COMMANDS = frozenset({"done", "reject-review", "release-launch", "archive", "restore"})
# COORDINATOR_TOKEN_ENV / COORDINATOR_TOKEN_FILE_ENV are re-exported from the
# package __init__ (imported above) so existing callers keep working against
# core.COORDINATOR_TOKEN_ENV unchanged.
DEFAULT_COORDINATOR_TOKEN_FILE = Path.home() / ".config/aiworkhub/taskctl_coordinator.token"
_WORKSPACE_GC_JOBS_LOCK = threading.Lock()
_WORKSPACE_GC_JOBS: set[str] = set()
# RFC 9562 defines UUID versions 6, 7 and 8 in addition to the historical
# 1-5 set.  Current Codex thread ids are UUIDv7, so rejecting the version
# nibble above 5 discards a genuine mux-owned origin thread.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_TASK_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REPO_ID_RE = re.compile(r"^repo_[a-f0-9]{32}$")
_PROCESS_REPO_ROOT_OVERRIDE: Path | None = None
_MAX_REWORK_FEEDBACK_BYTES = 4 * 1024


def _bounded_utf8_prefix(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True
_REPOSITORY_SWITCH_LOCK = threading.RLock()

# ---------------------------------------------------------------------------
# B852: canonical, repo-local task-store write layer.
#
# Every function below this block talks directly to the per-repo canonical
# ``.aiworkhub/tasking/task_queue.sqlite`` via ``task_store.py``. None of them
# ever imports or shells out to ``AITools/taskctl.py`` / ``AITools/taskdb.py``:
# a repository with no ``AITools/`` directory at all gets a fully working
# AIWorkHub task lifecycle. Two repos with the same task_id stay isolated
# because ``repo_root()`` (and therefore the canonical DB path each of these
# helpers resolves) is per-process/per-config, never a shared fixed path.
# ---------------------------------------------------------------------------


def _canonical_db_path() -> Path:
    readiness = task_store.storage_readiness(repo_root())
    if not readiness.ready:
        raise task_store.StorageNotReadyError(readiness.reason)
    return Path(readiness.canonical_db)


def _canonical_connect(*, readonly: bool = False) -> sqlite3.Connection:
    path = _canonical_db_path()
    if readonly:
        # A raw ``file:{path}?mode=ro`` f-string is not a read-only open: a path
        # containing ``#`` starts a URI fragment that swallows ``?mode=ro`` and
        # silently opens a DIFFERENT file read-write.  Route every read-only
        # open through the fail-closed helper, which percent-encodes the path
        # and also issues ``PRAGMA query_only=ON``.
        conn = sqlite_readonly.connect_readonly(path)
    else:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _canonical_result(
    *,
    ok: bool,
    stdout: str = "",
    stderr: str = "",
    returncode: int | None = None,
    command: list[str] | None = None,
) -> dict[str, Any]:
    """Build a ``TaskCtlResult.as_dict()``-shaped envelope without ever
    launching a subprocess. Keeps every existing caller (server.py tool
    wrappers, ``_live_card``, ``_parse_show_task_card``,
    ``_extract_collision_report``, ``_parse_jsonl``) working unchanged."""
    return {
        "ok": ok,
        "returncode": returncode if returncode is not None else (0 if ok else 1),
        "command": command or [],
        "stdout": stdout,
        "stderr": stderr,
    }


def _claude_manager_identity() -> dict[str, str] | None:
    """Verify that this MCP server is the direct child of an interactive
    Claude Code VS Code session bound to the same repository.

    The parent process and Claude's per-PID session descriptor are both
    same-uid local runtime state. No credential is read or exposed.
    """
    if os.name == "nt":
        # Windows has no /proc: confirm the exact direct parent through a
        # native Toolhelp snapshot plus process-token SIDs before trusting
        # Claude's per-PID session descriptor.
        return _claude_windows_manager_identity()
    parent_pid = os.getppid()
    try:
        cmdline = Path(f"/proc/{parent_pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8")
        descriptor_path = Path.home() / ".claude" / "sessions" / f"{parent_pid}.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(descriptor, dict) or "/claude " not in cmdline:
        return None
    return _claude_descriptor_identity(parent_pid, descriptor)


def _claude_descriptor_identity(
    parent_pid: int, descriptor: object
) -> dict[str, str] | None:
    """Validate a Claude per-PID session descriptor and bind it to this repo.

    Shared by the POSIX ``/proc`` path and the Windows-native path so both
    fail closed on the same malformed, foreign or stale descriptor evidence.
    """
    if not isinstance(descriptor, dict):
        return None
    if int(descriptor.get("pid") or -1) != parent_pid:
        return None
    if descriptor.get("kind") != "interactive" or descriptor.get("entrypoint") != "claude-vscode":
        return None
    session_id = str(descriptor.get("sessionId") or "").strip()
    if not _UUID_RE.fullmatch(session_id):
        return None
    try:
        descriptor_cwd = Path(str(descriptor.get("cwd") or "")).resolve()
    except (OSError, RuntimeError):
        return None
    if descriptor_cwd != repo_root():
        return None
    return {
        "provider": "claude",
        "session_id": session_id,
        "window_id": f"claude_vscode_{parent_pid}",
    }


def _claude_windows_manager_identity() -> dict[str, str] | None:
    """Windows-native Claude Code VS Code manager verification.

    There is no ``/proc`` on Windows. Confirm the exact direct parent process
    through a native Toolhelp snapshot (same-user SID and a live ``claude``
    image name), then validate ``~/.claude/sessions/<pid>.json`` exactly like
    the POSIX path. Missing, malformed, foreign, stale or unverifiable
    evidence fails closed.
    """
    parent_pid = os.getppid()
    if parent_pid <= 1:
        return None
    current_sid = _windows_process_owner_sid(os.getpid())
    parent_sid = _windows_process_owner_sid(parent_pid)
    if current_sid is None or parent_sid is None or parent_sid != current_sid:
        return None
    image_names = _windows_process_image_names()
    if image_names is None:
        return None
    parent_image = str(image_names.get(parent_pid) or "")
    if not parent_image or "claude" not in parent_image.lower():
        return None
    try:
        descriptor_path = Path.home() / ".claude" / "sessions" / f"{parent_pid}.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _claude_descriptor_identity(parent_pid, descriptor)


def _codex_manager_identity() -> dict[str, str] | None:
    """Verify the bounded local process chain for this Codex chat MCP.

    Codex spawns MCP servers below its App Server process, so the server's
    *direct* parent is not named ``codex``.  Requiring a direct parent made a
    genuine manager look like a headless worker and forced users to shuttle a
    mutable host-global token.  Accept only a same-uid, short ancestor chain
    containing BOTH the Codex App Server and the installed AIWorkHub mux.
    """
    if os.name == "nt":
        # Windows has no /proc.  The extension-owned route verifier below uses
        # a native Toolhelp process snapshot plus process-token SIDs, retaining
        # the same bounded-ancestry and same-user authority boundary.
        return (
            _codex_vscode_env_manager_identity()
            or _codex_extension_route_manager_identity()
            or _codex_shared_repo_route_manager_identity()
            or _codex_route_manager_identity_from_parent_chain()
        )

    pid = os.getppid()
    saw_codex_app_server = False
    mux_pid = 0
    for _ in range(6):
        if pid <= 1:
            break
        proc = Path(f"/proc/{pid}")
        try:
            if proc.stat().st_uid != os.geteuid():
                return None
            cmdline = proc.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8")
            status = proc.joinpath("status").read_text(encoding="utf-8")
            parent_pid = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
        except (OSError, UnicodeDecodeError, StopIteration, ValueError):
            return None
        lowered = cmdline.lower()
        if "codex" in lowered and "app-server" in lowered:
            saw_codex_app_server = True
        if "aiworkhub-app-server-mux" in lowered:
            mux_pid = pid
            break
        pid = parent_pid
    if not saw_codex_app_server or not mux_pid:
        return (
            _codex_vscode_env_manager_identity()
            or _codex_extension_route_manager_identity()
            or _codex_shared_repo_route_manager_identity()
            or _codex_route_manager_identity_from_parent_chain()
        )
    # VS Code extension hosts normally start Codex from a neutral cwd, so cwd
    # cannot identify the owning repository.  Bind the mux to the exact
    # extension-host PID persisted by this repository's AIWorkHub extension.
    # This prevents a globally visible MCP from accepting repo-B writes from
    # repo-A's chat and then returning repo-B callbacks to repo A.
    resolved = _repo_bound_codex_mux(mux_pid)
    if resolved is None:
        return None
    mux_instance, target = resolved
    identity = {
        "provider": "codex",
        "session_id": f"codex_mux_{mux_pid}",
        "window_id": str(target.get("window_id") or f"codex_vscode_{mux_pid}"),
    }
    if mux_instance.owned_thread_ids:
        thread_id = mux_instance.owned_thread_ids[-1]
        if _UUID_RE.fullmatch(thread_id):
            identity["thread_id"] = thread_id
            identity["session_id"] = thread_id
    return identity


def _codex_shared_repo_route_manager_identity() -> dict[str, str] | None:
    """Verify a repo-bound manager MCP through the shared VS Code registry.

    A Codex MCP tool process can be bound to this repository without inheriting
    ``AIWORKHUB_WINDOW_ID`` from the VS Code extension host.  If the extension
    has exactly one live, non-stale shared route record for the same
    repo_id/root, that process is a repo-local manager.  This is polling-only
    unless the route also carries a real Codex thread UUID.
    """

    try:
        root = repo_root()
        readiness = task_store.storage_readiness(root)
        if not readiness.ready or not readiness.repo_id:
            return None
        registry = shared_router.list_known_repositories(current_root=root, limit=32)
    except (OSError, RuntimeError, task_store.TaskStoreError, KeyError, TypeError, ValueError):
        return None
    if not isinstance(registry, dict) or not registry.get("ok"):
        return None
    matches = [
        record for record in registry.get("repositories", [])
        if isinstance(record, dict)
        and record.get("current_repo") is True
        and str(record.get("repo_id") or "") == str(readiness.repo_id)
        and bool(record.get("extension_host_alive"))
        and not bool(record.get("stale"))
        and str(record.get("selected_provider") or "").strip().lower() == "codex"
    ]
    if len(matches) != 1:
        return None
    record = matches[0]
    targets = record.get("targets", {})
    codex_target = targets.get("codex", {}) if isinstance(targets, dict) else {}
    route = codex_target.get("route", {}) if isinstance(codex_target, dict) else {}
    if not isinstance(route, dict):
        route = {}
    thread_id = str(route.get("thread_id") or "").strip()
    session_id = str(route.get("session_id") or record.get("window_id") or "").strip()
    return {
        "provider": "codex",
        "session_id": thread_id if _UUID_RE.fullmatch(thread_id) else session_id,
        "thread_id": thread_id if _UUID_RE.fullmatch(thread_id) else "",
        "window_id": str(record.get("window_id") or ""),
        "callback_supported": "true" if _UUID_RE.fullmatch(thread_id) else "false",
        "route_state": str(codex_target.get("capability_state") or "route_pending"),
    }


def _codex_extension_route_manager_identity() -> dict[str, str] | None:
    """Verify the VS Code extension-owned MCP child as the Codex manager.

    The MCP server exposed by the AIWorkHub VS Code extension is intentionally
    spawned by the extension host, not by Codex. That child will never inherit
    ``CODEX_THREAD_ID``. It is still a verified repo-local manager endpoint
    when the selected provider is Codex and the route record is bound to this
    same repo/window/extension-host PID. A real Codex thread UUID is a
    callback capability, not the only manager authority. Keep those states
    separate: route_pending may manage repo-local tasks, but cannot create
    callback-required tasks until a real thread UUID is persisted.
    """

    window_id = os.environ.get("AIWORKHUB_WINDOW_ID", "").strip()
    if not window_id:
        return None
    try:
        root = repo_root()
        readiness = task_store.storage_readiness(root)
        if not readiness.ready or not readiness.repo_id:
            return None
        target = read_selected_coordinator_target(root)
    except (OSError, RuntimeError, task_store.TaskStoreError, KeyError, TypeError, ValueError):
        return None
    if str(target.get("selected_provider") or "").strip().lower() != "codex":
        return None
    if str(target.get("repo_id") or "").strip() != str(readiness.repo_id):
        return None
    if str(target.get("window_id") or "").strip() != window_id:
        return None
    extension_host_pid = int(target.get("extension_host_pid") or 0)
    if extension_host_pid <= 1 or not _pid_in_same_uid_ancestor_chain(extension_host_pid, max_depth=4):
        return None
    codex_target = target.get("targets", {}).get("codex", {})
    if not isinstance(codex_target, dict):
        return None
    capability_state = str(codex_target.get("capability_state") or "").strip().lower()
    if capability_state not in ("available", "ready", "route_pending"):
        return None
    route = codex_target.get("route", {})
    if not isinstance(route, dict):
        return None
    thread_id = str(route.get("thread_id") or "").strip()
    session_id = str(route.get("session_id") or target.get("claim_episode") or window_id).strip()
    if str(route.get("repo_id") or readiness.repo_id).strip() != str(readiness.repo_id):
        return None
    if str(route.get("window_id") or window_id).strip() != window_id:
        return None
    return {
        "provider": "codex",
        "session_id": thread_id if _UUID_RE.fullmatch(thread_id) else session_id,
        "thread_id": thread_id if _UUID_RE.fullmatch(thread_id) else "",
        "window_id": window_id,
        "callback_supported": "true" if _UUID_RE.fullmatch(thread_id) else "false",
        "route_state": capability_state or "route_pending",
    }


def _codex_vscode_env_manager_identity() -> dict[str, str] | None:
    """Verify a Codex VS Code manager route from injected local environment.

    Some Codex MCP launches do not expose the expected App Server -> AIWorkHub
    mux process chain to the child process, even though the chat is still a
    VS Code-originated Codex manager and carries the exact thread UUID in the
    environment.  Accept this route only when all cheap local signals agree:

    * Codex explicitly marks the origin as VS Code.
    * ``CODEX_THREAD_ID`` is a valid UUID.
    * The current repository is initialized and currently selects Codex.

    The route file may still be ``route_pending`` while the extension is
    learning/persisting the app-server mux binding.  A real
    ``CODEX_THREAD_ID`` plus VS Code provenance is already stronger callback
    identity than a synthetic ``codex:window_*`` route alias, so do not reject
    it merely because the route file has not caught up yet.
    """

    if os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "").strip() != "codex_vscode":
        return None
    thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not _UUID_RE.fullmatch(thread_id):
        return None
    if not (
        os.environ.get("VSCODE_IPC_HOOK_CLI")
        or os.environ.get("VSCODE_ESM_ENTRYPOINT")
        or os.environ.get("VSCODE_AGENT_FOLDER")
    ):
        return None
    try:
        root = repo_root()
        readiness = task_store.storage_readiness(root)
        if not readiness.ready:
            return None
        target = read_selected_coordinator_target(root)
    except (OSError, RuntimeError, task_store.TaskStoreError, KeyError, TypeError, ValueError):
        return None
    if str(target.get("selected_provider") or "").strip().lower() != "codex":
        return None
    codex_target = target.get("targets", {}).get("codex", {})
    route = codex_target.get("route", {}) if isinstance(codex_target, dict) else {}
    extension_host_pid = int(target.get("extension_host_pid") or 0)
    if extension_host_pid <= 1 or not _pid_in_same_uid_ancestor_chain(extension_host_pid, max_depth=12):
        return None
    window_id = str(route.get("window_id") or target.get("window_id") or f"codex_vscode_{thread_id[:8]}")
    return {
        "provider": "codex",
        "session_id": thread_id,
        "thread_id": thread_id,
        "window_id": window_id,
    }


def _codex_route_manager_identity_from_parent_chain() -> dict[str, str] | None:
    """Verify a Codex VS Code manager from the local parent chain + route file.

    The Codex MCP host does not always pass ``CODEX_THREAD_ID`` or other VS
    Code environment markers into stdio MCP servers.  In that launch shape the
    process tree still carries the authority boundary:

    ``aiworkhub.server`` -> ``codex`` -> VS Code extension host.

    Accept the manager only when a same-uid ancestor is named Codex and this
    repository's current route record names the same extension-host PID as an
    ancestor.  The route supplies the stable window/session identifiers.  This
    is repo-local and fails closed for cross-repository or headless launches.
    """

    saw_codex = False
    try:
        root = repo_root()
        readiness = task_store.storage_readiness(root)
        if not readiness.ready:
            return None
        target = read_selected_coordinator_target(root)
    except (OSError, RuntimeError, task_store.TaskStoreError, KeyError, TypeError, ValueError):
        return None
    if str(target.get("selected_provider") or "").strip().lower() != "codex":
        return None
    codex_target = target.get("targets", {}).get("codex", {})
    if not isinstance(codex_target, dict):
        return None
    if str(codex_target.get("capability_state") or "").strip().lower() not in ("available", "ready"):
        return None
    extension_host_pid = int(target.get("extension_host_pid") or 0)
    if extension_host_pid <= 1:
        return None
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            return None
        proc = Path(f"/proc/{pid}")
        try:
            if proc.stat().st_uid != os.geteuid():
                return None
            cmdline = proc.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8")
            if "codex" in cmdline.lower():
                saw_codex = True
            if pid == extension_host_pid:
                if not saw_codex:
                    return None
                route = codex_target.get("route", {})
                if not isinstance(route, dict):
                    route = {}
                session_id = str(route.get("session_id") or target.get("claim_episode") or "").strip()
                thread_id = str(route.get("thread_id") or "").strip()
                window_id = str(route.get("window_id") or target.get("window_id") or f"codex_vscode_{extension_host_pid}")
                if not _UUID_RE.fullmatch(thread_id):
                    return None
                identity = {
                    "provider": "codex",
                    "session_id": thread_id,
                    "window_id": window_id,
                    "thread_id": thread_id,
                }
                return identity
            status = proc.joinpath("status").read_text(encoding="utf-8")
            pid = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
        except (OSError, UnicodeDecodeError, StopIteration, ValueError):
            return None
    return None


def _pid_in_same_uid_ancestor_chain(target_pid: int, *, max_depth: int) -> bool:
    if os.name == "nt":
        return _pid_in_same_windows_user_ancestor_chain(target_pid, max_depth=max_depth)

    pid = os.getppid()
    for _ in range(max_depth):
        if pid <= 1:
            return False
        proc = Path(f"/proc/{pid}")
        try:
            if proc.stat().st_uid != os.geteuid():
                return False
            if pid == target_pid:
                return True
            status = proc.joinpath("status").read_text(encoding="utf-8")
            pid = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
        except (OSError, StopIteration, ValueError):
            return False
    return False


def _windows_process_parent_map() -> dict[int, int] | None:
    """Return a native Windows PID -> parent PID snapshot."""

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_snapshot = kernel32.CreateToolhelp32Snapshot
        create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        create_snapshot.restype = wintypes.HANDLE
        process_first = kernel32.Process32FirstW
        process_first.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        process_first.restype = wintypes.BOOL
        process_next = kernel32.Process32NextW
        process_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        process_next.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        snapshot = create_snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if snapshot == wintypes.HANDLE(-1).value:
            return None
        parents: dict[int, int] = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not process_first(snapshot, ctypes.byref(entry)):
                return None
            while True:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not process_next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            close_handle(snapshot)
        return parents
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _windows_process_image_names() -> dict[int, str] | None:
    """Return a native Windows PID -> executable image name snapshot."""

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_snapshot = kernel32.CreateToolhelp32Snapshot
        create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        create_snapshot.restype = wintypes.HANDLE
        process_first = kernel32.Process32FirstW
        process_first.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        process_first.restype = wintypes.BOOL
        process_next = kernel32.Process32NextW
        process_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        process_next.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        snapshot = create_snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if snapshot == wintypes.HANDLE(-1).value:
            return None
        names: dict[int, str] = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not process_first(snapshot, ctypes.byref(entry)):
                return None
            while True:
                names[int(entry.th32ProcessID)] = str(entry.szExeFile)
                if not process_next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            close_handle(snapshot)
        return names
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _windows_process_owner_sid(pid: int) -> str | None:
    """Return a process token's user SID, failing closed on access errors."""

    if os.name != "nt" or pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

        class TOKEN_USER(ctypes.Structure):
            _fields_ = [("User", SID_AND_ATTRIBUTES)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        local_free = kernel32.LocalFree
        local_free.argtypes = (wintypes.HLOCAL,)
        local_free.restype = wintypes.HLOCAL
        open_token = advapi32.OpenProcessToken
        open_token.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
        open_token.restype = wintypes.BOOL
        get_token_info = advapi32.GetTokenInformation
        get_token_info.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        get_token_info.restype = wintypes.BOOL
        sid_to_string = advapi32.ConvertSidToStringSidW
        sid_to_string.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
        sid_to_string.restype = wintypes.BOOL

        process = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return None
        token = wintypes.HANDLE()
        try:
            if not open_token(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
                return None
            required = wintypes.DWORD()
            get_token_info(token, 1, None, 0, ctypes.byref(required))  # TokenUser
            if not required.value:
                return None
            buffer = ctypes.create_string_buffer(required.value)
            if not get_token_info(token, 1, buffer, required.value, ctypes.byref(required)):
                return None
            token_user = ctypes.cast(buffer, ctypes.POINTER(TOKEN_USER)).contents
            sid_text = wintypes.LPWSTR()
            if not sid_to_string(token_user.User.Sid, ctypes.byref(sid_text)):
                return None
            try:
                return str(sid_text.value or "") or None
            finally:
                local_free(sid_text)
        finally:
            if token:
                close_handle(token)
            close_handle(process)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _pid_in_same_windows_user_ancestor_chain(
    target_pid: int,
    *,
    max_depth: int,
    start_pid: int | None = None,
) -> bool:
    """Windows equivalent of the same-uid bounded ``/proc`` ancestry check."""

    if target_pid <= 1 or max_depth <= 0:
        return False
    current_sid = _windows_process_owner_sid(os.getpid())
    parents = _windows_process_parent_map()
    if current_sid is None or parents is None:
        return False
    pid = os.getppid() if start_pid is None else start_pid
    for _ in range(max_depth):
        if pid <= 1 or _windows_process_owner_sid(pid) != current_sid:
            return False
        if pid == target_pid:
            return True
        pid = parents.get(pid, 0)
    return False


def _valid_origin_thread_id(value: str) -> bool:
    """Accept only callback-capable canonical chat/session UUIDs.

    Window aliases (``codex:window_*`` / ``claude:window_*``) are UI routing
    labels, not App Server or Claude session ids.  Treating them as callback
    origins creates durable outbox rows that no live transport can own, which
    leaves the manager asleep while review tasks accumulate.
    """

    return bool(_UUID_RE.fullmatch(value))


WINDOW_ROUTE_DIR_REL = Path("config") / "routing" / "windows"


def _window_route_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / ".aiworkhub" / WINDOW_ROUTE_DIR_REL


def _read_live_window_route_records(root: Path | None = None) -> list[dict[str, Any]]:
    """Enumerate non-expired per-window routing records for this repo.

    Each VS Code window persists its own routing authority at
    ``.aiworkhub/config/routing/windows/<window_id>.json`` instead of the
    single shared last-writer-wins ``coordinator-targets.json``, so opening a
    second window for the same repo can never silently steal a first
    window's active route. Expired leases and malformed records are skipped
    rather than treated as candidates.
    """
    directory = _window_route_dir(root)
    records: list[dict[str, Any]] = []
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return records
    now = datetime.now(timezone.utc)
    for entry in entries:
        try:
            payload = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        lease_expires_at = str(payload.get("lease_expires_at") or "").strip()
        try:
            expires = datetime.fromisoformat(lease_expires_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            continue
        records.append(payload)
    return records


def _repo_bound_codex_mux(mux_pid: int) -> tuple[Any, dict[str, Any]] | None:
    """Resolve one mux only through a live, non-expired per-window route
    record bound to both this repo_id and the exact mux parent PID.

    Fails closed (returns ``None``) on zero matching window records and on
    ambiguity (more than one live window record claiming the same mux
    parent PID) -- it never falls back to guessing among candidates.
    """
    try:
        from . import app_server_mux

        root = repo_root()
        repo_id = task_store.storage_readiness(root).repo_id
        instances = app_server_mux.list_live_sideband_instances(app_server_mux.default_sideband_dir())
        matches: list[tuple[Any, dict[str, Any]]] = []
        for record in _read_live_window_route_records(root):
            if str(record.get("repo_id") or "") != repo_id:
                continue
            extension_host_pid = int(record.get("extension_host_pid") or 0)
            if extension_host_pid <= 1:
                continue
            mux_hits = [
                instance
                for instance in instances
                if instance.pid == mux_pid
                and instance.parent_pid == extension_host_pid
                and instance.is_owner_fresh
                and instance.ready
            ]
            if len(mux_hits) == 1:
                matches.append((mux_hits[0], record))
    except (OSError, RuntimeError, TypeError, ValueError, task_store.TaskStoreError):
        return None
    if len(matches) != 1:
        return None
    return matches[0]


def _current_chat_provider(card: dict[str, Any] | None = None) -> str:
    """Resolve callback ownership from the task/session, never a dashboard
    global toggle when a concrete chat identity is available."""
    explicit = str((card or {}).get("coordinator_provider") or "").strip().lower()
    if explicit in ("codex", "claude", "copilot"):
        return explicit
    if _claude_manager_identity() is not None:
        return "claude"
    try:
        parent_cmd = Path(f"/proc/{os.getppid()}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8").lower()
    except (OSError, UnicodeDecodeError):
        parent_cmd = ""
    if "codex" in parent_cmd:
        return "codex"
    try:
        selected = read_selected_coordinator_target(repo_root()).get("selected_provider")
    except Exception:
        selected = ""
    return selected if selected in ("codex", "claude", "copilot") else "codex"


_COORDINATOR_TOKEN_REL = Path(".aiworkhub") / "runtime" / "coordinator.token"


def _resolve_coordinator_token_path(configured_path: str) -> tuple[Path, bool]:
    """Resolve the coordinator token file (Issue 1).

    An explicitly-configured env path (``BITNN_TASKCTL_COORDINATOR_TOKEN_FILE``)
    is an intentional override and wins -- backward compatible with any
    deployment that already exports it. Otherwise the DEFAULT is the ACTIVE
    repository's own ``.aiworkhub/runtime/coordinator.token`` (a git-ignored,
    owner-only secret created at Init Repo), then the legacy global
    ``~/.config/aiworkhub/taskctl_coordinator.token`` fallback. Returns
    ``(path, is_repo_local)``. Portable: only ``Path`` joins, no host-specific
    assumptions, so it resolves identically on Linux, WSL, and Windows."""
    if configured_path:
        return Path(configured_path).expanduser().resolve(), False
    try:
        repo_local = repo_root() / _COORDINATOR_TOKEN_REL
        if repo_local.is_file():
            return repo_local.resolve(), True
    except OSError:
        pass
    return DEFAULT_COORDINATOR_TOKEN_FILE.resolve(), False


def _verify_coordinator_capability(runner: str | None) -> tuple[bool, str]:
    """In-process equivalent of ``AITools/taskctl.py::_require_coordinator``.

    Reuses the exact same trusted pieces core.py already had for this
    (``coordinator_config()``, ``DEFAULT_COORDINATOR_TOKEN_FILE``,
    ``scrub_coordinator_capability_from_environment``) instead of shelling
    out to taskctl.py and letting ITS ``_require_coordinator`` do the check
    inside a child process.
    """
    claude_identity = _claude_manager_identity()
    if claude_identity is not None:
        return True, "trusted_claude_manager_route"
    if runner == CODEX_RUNNER and _codex_manager_identity() is not None:
        return True, "trusted_codex_manager_route"
    if runner != CODEX_RUNNER:
        return False, f"coordinator_runner_mismatch:expected={CODEX_RUNNER}:got={runner}"
    scrub_coordinator_capability_from_environment()
    supplied, configured_path = coordinator_config()
    token_path, is_repo_local = _resolve_coordinator_token_path(configured_path)
    try:
        fd = os.open(str(token_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False, "coordinator_capability_denied:token_file_unreadable"
    try:
        file_stat = os.fstat(fd)
        mode = stat.S_IMODE(file_stat.st_mode)
        # os.geteuid is POSIX-only; on Windows the git-ignored repo-local file's
        # ACL is the boundary, so skip the same-uid assertion there.
        owner_ok = (not hasattr(os, "geteuid")) or file_stat.st_uid == os.geteuid()
        if not stat.S_ISREG(file_stat.st_mode) or not owner_ok:
            return False, "coordinator_capability_denied:token_file_not_owner_regular"
        with os.fdopen(fd, encoding="utf-8") as fh:
            fd = -1
            expected = fh.read().strip()
    finally:
        if fd >= 0:
            os.close(fd)
    # POSIX 0600 permission gate (mode bits are not meaningful on Windows).
    if hasattr(os, "geteuid") and mode != 0o600:
        return False, "coordinator_capability_denied:token_file_permissions"
    # A correctly-owned repo-local coordinator token file IS the capability for
    # the repository owner -- no separately-exported env token is required (that
    # was the reported failure: `coordinator capability denied` unless the env
    # file was manually exported). An env-supplied token, when present, must
    # still match.
    if not supplied and is_repo_local and expected:
        supplied = expected
    if not supplied or not expected or not hmac.compare_digest(supplied, expected):
        return False, "coordinator_capability_denied:token_mismatch"
    return True, "ok"


def _canonical_write_gate(
    action: str,
    *,
    runner: str | None = None,
    topic: str | None = None,
    coordinator_capability: bool = False,
    task_id: str | None = None,
) -> dict[str, Any] | None:
    """Faithful in-process replica of ``run_taskctl``'s write-protection
    sequence (write gate -> runner/topic allowlist -> coordinator token),
    minus the subprocess launch. Returns a blocked ``TaskCtlResult``-shaped
    dict if the write is refused, else ``None``."""
    scrub_coordinator_capability_from_environment()

    if not writes_allowed():
        blocked_reason = (
            "write command blocked; set AIWORKHUB_ALLOW_WRITES=1 "
            "and pass allow_write=True"
        )
        write_audit_entry(tool_name=action, action="blocked_write", blocked_reason=blocked_reason)
        return _canonical_result(ok=False, returncode=126, stderr=blocked_reason, command=[action])

    if runner is not None or topic is not None:
        decision = check_runner_topic_allowlist(runner, topic, action)
        if not decision["allowed"]:
            card_decision: dict[str, Any] = {"allowed": False, "reason": "card_scoped_task_id_required"}
            if task_id is not None:
                card_decision = _check_card_scoped_write_authority([action, task_id], runner, topic)
            if card_decision.get("allowed"):
                decision = card_decision
            else:
                reason = card_decision.get("reason") or decision["reason"]
                audit_action = (
                    "blocked_malformed_runner_or_topic"
                    if str(reason).startswith("malformed_")
                    else "blocked_runner_topic_not_in_allowlist"
                )
                blocked_reason = f"runner/topic allowlist denied: {reason}"
                write_audit_entry(tool_name=action, action=audit_action, blocked_reason=blocked_reason)
                return _canonical_result(ok=False, returncode=126, stderr=blocked_reason, command=[action])

    if coordinator_capability:
        cap_ok, cap_reason = _verify_coordinator_capability(runner)
        if not cap_ok:
            write_audit_entry(
                tool_name=action, action="blocked_coordinator_capability", blocked_reason=cap_reason
            )
            return _canonical_result(ok=False, returncode=126, stderr=cap_reason, command=[action])

    return None

# The actual env-var pop happens at package-init time (see
# aiworkhub/__init__.py::refresh_coordinator_config) -- before this
# module or any sibling submodule executes any of its own top-level code.
# That closes the import-order race a module-local pop here could not
# guarantee: no matter which submodule a caller imports first, the package's
# __init__.py always runs first. This module never keeps its own separate
# copy of the secret; it always reads the late-bound cache below.


from .runner_topic_policy import (
    CODEX_ALLOWED_ACTIONS,
    CODEX_RUNNER,
    PER_WAVE_RUNNER_TOPIC_ALLOWLIST,
    RUNNER_TOPIC_ALLOWLIST,
    _is_malformed_identity_token,
    check_runner_topic_allowlist,
)


@dataclass(frozen=True)
class TaskCtlResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.returncode == 0,
            "returncode": self.returncode,
            "command": self.command,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def _resolve_repo_root_env(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _implicit_windows_codex_repository_root() -> Path | None:
    """Resolve a repo-neutral Windows MCP from its owning VS Code window.

    Codex does not consistently propagate ``CODEX_THREAD_ID`` to stdio MCP
    children on Windows.  The shared route registry still carries a stronger
    local binding: the live extension-host PID that owns the repository
    window.  Accept that route only when the host is in this process's
    same-user ancestor chain and exactly one coherent Codex window matches.
    """

    try:
        registry = shared_router.list_known_repositories(limit=256)
    except (OSError, RuntimeError, KeyError, TypeError, ValueError):
        return None
    if not isinstance(registry, dict) or not registry.get("ok"):
        return None

    matches: dict[tuple[str, str, str], Path] = {}
    for record in registry.get("repositories", []):
        if not isinstance(record, dict):
            continue
        if not bool(record.get("extension_host_alive")) or bool(record.get("stale")):
            continue
        if str(record.get("selected_provider") or "").strip().lower() != "codex":
            continue
        extension_host_pid = int(record.get("extension_host_pid") or 0)
        if extension_host_pid <= 1 or not _pid_in_same_windows_user_ancestor_chain(
            extension_host_pid,
            max_depth=16,
        ):
            continue
        repo_id = str(record.get("repo_id") or "").strip()
        window_id = str(record.get("window_id") or "").strip()
        root_raw = str(record.get("repo_root") or "").strip()
        targets = record.get("targets")
        codex_target = targets.get("codex") if isinstance(targets, dict) else None
        route = codex_target.get("route") if isinstance(codex_target, dict) else None
        if not repo_id or not window_id or not root_raw or not isinstance(route, dict):
            continue
        capability_state = str(codex_target.get("capability_state") or "").strip().lower()
        if capability_state not in {"available", "ready", "route_pending"}:
            continue
        if str(route.get("repo_id") or "").strip() != repo_id:
            continue
        if str(route.get("window_id") or "").strip() != window_id:
            continue
        try:
            root = Path(root_raw).resolve()
        except (OSError, RuntimeError):
            continue
        matches[(repo_id, window_id, str(root))] = root

    if len(matches) != 1:
        return None
    return next(iter(matches.values()))


def _implicit_codex_repository_root() -> Path | None:
    """Resolve a repo-neutral Codex MCP process from its live chat route.

    Application-global Codex MCP registration intentionally contains no
    repository path.  A long-lived chat can therefore outlive the cwd from
    which its MCP child was first spawned.  Prefer the exact live
    ``provider/thread/window`` route over that stale cwd, while keeping all
    explicitly repo-bound extension/worker children unchanged.
    """

    if os.name == "nt":
        routed = _implicit_windows_codex_repository_root()
        if routed is not None:
            return routed

    thread_id = ""
    extension_host_pid = 0
    if (
        os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "").strip() == "codex_vscode"
        and (
            os.environ.get("VSCODE_IPC_HOOK_CLI")
            or os.environ.get("VSCODE_ESM_ENTRYPOINT")
            or os.environ.get("VSCODE_AGENT_FOLDER")
        )
    ):
        candidate = os.environ.get("CODEX_THREAD_ID", "").strip()
        if _UUID_RE.fullmatch(candidate):
            thread_id = candidate
    if not thread_id:
        pid = os.getppid()
        mux_pid = 0
        for _ in range(8):
            if pid <= 1:
                break
            proc = Path(f"/proc/{pid}")
            try:
                if proc.stat().st_uid != os.geteuid():
                    return None
                cmdline = proc.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8")
                status = proc.joinpath("status").read_text(encoding="utf-8")
                parent_pid = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
            except (OSError, UnicodeDecodeError, StopIteration, ValueError):
                break
            if "aiworkhub-app-server-mux" in cmdline.lower():
                mux_pid = pid
                break
            pid = parent_pid
        if mux_pid:
            try:
                from . import app_server_mux

                instances = [
                    item for item in app_server_mux.list_live_sideband_instances(
                        app_server_mux.default_sideband_dir()
                    )
                    if item.pid == mux_pid and item.ready and item.is_owner_fresh
                ]
            except (OSError, RuntimeError, TypeError, ValueError):
                instances = []
            if len(instances) == 1:
                instance = instances[0]
                candidate = instance.active_thread_id or (
                    instance.owned_thread_ids[-1] if instance.owned_thread_ids else ""
                )
                if _UUID_RE.fullmatch(candidate):
                    thread_id = candidate
                    extension_host_pid = int(instance.parent_pid or 0)
    if not thread_id:
        return None
    resolved = shared_router.resolve_repository_route(
        provider="codex",
        thread_id=thread_id,
        extension_host_pid=extension_host_pid,
    )
    if not resolved.get("ok"):
        return None
    try:
        return Path(str(resolved["repo_root"])).resolve()
    except (KeyError, OSError, RuntimeError):
        return None


def repo_root() -> Path:
    """Return the canonical repository binding for this MCP process.

    ``AIWORKHUB_REPO_ROOT`` is the installable extension's canonical binding.
    ``AIWORKHUB_REPO`` remains a legacy explicit binding for old children and
    tests, but it must never silently override the canonical root.  A manager
    switch override is process-local and takes precedence over that legacy
    starting point.  Live Codex route discovery is used only for a genuinely
    repo-neutral process; it must never steal an explicitly bound child.
    """
    canonical_raw = os.environ.get("AIWORKHUB_REPO_ROOT", "").strip()
    legacy_raw = os.environ.get("AIWORKHUB_REPO", "").strip()
    if canonical_raw:
        canonical = _resolve_repo_root_env(canonical_raw)
        if legacy_raw:
            legacy = _resolve_repo_root_env(legacy_raw)
            if legacy != canonical:
                raise RuntimeError(
                    "repo_root_env_mismatch:"
                    f"AIWORKHUB_REPO_ROOT={canonical}:"
                    f"AIWORKHUB_REPO={legacy}"
                )
        return canonical
    if _PROCESS_REPO_ROOT_OVERRIDE is not None:
        return _PROCESS_REPO_ROOT_OVERRIDE
    if legacy_raw:
        return _resolve_repo_root_env(legacy_raw)
    routed = _implicit_codex_repository_root()
    if routed is not None:
        return routed
    return repository_state.resolve_repository_root(require_manifest=False)


def taskctl_path(repo: Path | None = None) -> Path:
    """Compatibility-only legacy path helper.

    The installable AIWorkHub runtime does not require or execute this path.
    Kept only for old tests/docs that compare historical taskctl locations.
    """
    root = repo or repo_root()
    return root / "AITools/taskctl.py"


def writes_allowed() -> bool:
    return os.environ.get("AIWORKHUB_ALLOW_WRITES", "0") == "1"


def scrub_coordinator_capability_from_environment() -> None:
    """Move late-bound coordinator configuration out of the inherited env.

    Delegates to the package-level scrub (``aiworkhub.refresh_coordinator_config``)
    so there is exactly one place that ever pops these env vars.
    """
    refresh_coordinator_config()


def _coordinator_taskctl_env() -> dict[str, str]:
    """Build an env that grants capability to one trusted taskctl process."""
    scrub_coordinator_capability_from_environment()
    configured_token, configured_path = coordinator_config()

    token_path, _is_repo_local = _resolve_coordinator_token_path(configured_path)
    try:
        file_token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        file_token = ""

    child_env = os.environ.copy()
    child_env.pop(COORDINATOR_TOKEN_ENV, None)
    child_env.pop(COORDINATOR_TOKEN_FILE_ENV, None)
    child_env[COORDINATOR_TOKEN_FILE_ENV] = str(token_path)
    token = file_token or configured_token
    if token:
        child_env[COORDINATOR_TOKEN_ENV] = token
    return child_env


def require_repo() -> Path:
    """Return the bound repository root.

    Standalone VSIX/runtime installs must work in arbitrary repositories that
    have never contained ``AITools/``. Storage readiness is checked by the
    concrete operation via ``task_store``; this helper must not require a
    legacy parent-repo script.
    """
    return repo_root()


def _is_write_command(args: list[str]) -> bool:
    return bool(args) and args[0] in WRITE_COMMANDS


def _audit_log_path(repo: Path | None = None) -> Path:
    """Resolve audit log path from env or default, relative to repo root."""
    env_path = os.environ.get("AIWORKHUB_AUDIT_LOG_PATH", "")
    if env_path:
        return Path(env_path).expanduser().resolve()
    root = repo or repo_root()
    return root / AUDIT_LOG_DEFAULT_REL


def _sanitize_env_for_audit() -> dict[str, str]:
    """Return env var NAMES (not values) relevant to write-gate decisions."""
    relevant = ["AIWORKHUB_ALLOW_WRITES", "AIWORKHUB_AUDIT_LOG_PATH"]
    return {k: "<set>" if k in os.environ else "<unset>" for k in relevant}


def write_audit_entry(
    tool_name: str,
    action: str,
    blocked_reason: str,
    *,
    repo: Path | None = None,
) -> dict[str, Any]:
    """Append a single audit entry to the JSONL audit log.

    Never logs secret/env values — only env var NAMES and set/unset status.
    Returns the entry dict that was written (or would have been written).
    Does not raise on IO errors; writes a warning to stderr instead.
    """
    entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_name": tool_name,
        "action": action,
        "blocked_reason": blocked_reason,
        "caller_info": {
            "pid": os.getpid(),
            "env_vars_checked": _sanitize_env_for_audit(),
        },
    }

    log_path = _audit_log_path(repo)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(
            f"[aiworkhub] WARNING: audit log write failed: {exc}",
            file=sys.stderr,
        )

    return entry


def run_taskctl(
    args: list[str],
    *,
    allow_write: bool = False,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    runner: str | None = None,
    topic: str | None = None,
    coordinator_capability: bool = False,
) -> TaskCtlResult:
    """Native taskctl-compat dispatcher with explicit write protection.

    Older in-package callers were written against ``core.run_taskctl([...])``.
    Keep that API, but execute against this package's canonical
    ``.aiworkhub/tasking/task_queue.sqlite`` task engine directly. This path
    never shells out and never requires a repository-local ``AITools/`` tree.
    """

    scrub_coordinator_capability_from_environment()
    if not args:
        raise ValueError("taskctl args must not be empty")
    if coordinator_capability and args[0] not in COORDINATOR_COMMANDS:
        raise ValueError(
            "coordinator capability may only be passed to done, reject-review, release-launch, archive, or restore"
        )

    command = list(args)
    cmd = args[0]

    def _as_result(result: dict[str, Any]) -> TaskCtlResult:
        return TaskCtlResult(
            command=list(result.get("command") or command),
            returncode=int(result.get("returncode") if result.get("returncode") is not None else (0 if result.get("ok") else 1)),
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or ""),
        )

    def _value(flag: str, default: str | None = None) -> str | None:
        try:
            idx = args.index(flag)
        except ValueError:
            return default
        if idx + 1 >= len(args):
            return default
        return args[idx + 1]

    def _int_value(flag: str, default: int) -> int:
        raw = _value(flag)
        if raw is None:
            return default
        try:
            return max(0, min(int(raw), 5000_000_000))
        except ValueError:
            return default

    try:
        if cmd == "verify":
            readiness = task_store.storage_readiness(require_repo())
            return TaskCtlResult(
                command=command,
                returncode=0 if readiness.ready else 1,
                stdout=json.dumps(readiness.as_dict(), ensure_ascii=False),
                stderr="" if readiness.ready else readiness.reason,
            )
        if cmd in {"init-db", "init-repo"}:
            blocked = _canonical_write_gate("init-db", runner=runner, topic=topic)
            if blocked is not None:
                return _as_result({**blocked, "command": command})
            return _as_result(_canonical_result(
                ok=True,
                stdout=json.dumps(task_store.initialize_repository(require_repo()), ensure_ascii=False),
                command=command,
            ))
        if cmd == "list":
            return _as_result(list_tasks(
                status=_value("--status", "pending") or "pending",
                topic=_value("--topic"),
                limit=_int_value("--limit", 80),
            ))
        if cmd == "review-queue":
            return _as_result(review_queue())
        if cmd == "show":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "show requires task_id")
            return _as_result(show_task(args[1]))
        if cmd == "export":
            selected_runner = _value("--runner", runner)
            if not selected_runner:
                return TaskCtlResult(command, 2, "", "export requires --runner")
            return _as_result(pending_for_runner(selected_runner, topic=_value("--topic", topic)))
        if cmd == "auto-pickup":
            selected_runner = _value("--runner", runner)
            if not selected_runner:
                return TaskCtlResult(command, 2, "", "auto-pickup requires --runner")
            return _as_result(auto_pickup(selected_runner, _value("--topic", topic)))
        if cmd == "claim-start":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "claim-start requires task_id")
            selected_runner = _value("--runner", runner)
            selected_topic = _value("--topic", topic)
            if not selected_runner or not selected_topic:
                return TaskCtlResult(command, 2, "", "claim-start requires --runner and --topic")
            return _as_result(claim_start_exact(args[1], selected_runner, selected_topic, _value("--request-id", "") or ""))
        if cmd == "review":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "review requires task_id")
            return _as_result(mark_review(args[1], runner=_value("--runner", runner), topic=_value("--topic", topic)))
        if cmd == "done":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "done requires task_id")
            return _as_result(mark_done(args[1], runner=_value("--runner", runner), topic=_value("--topic", topic)))
        if cmd == "reject-review":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "reject-review requires task_id")
            return _as_result(reject_review(args[1], reason=_value("--reason", "") or "", topic=_value("--topic", topic)))
        if cmd == "release-launch":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "release-launch requires task_id")
            return _as_result(release_launch(
                args[1],
                claimed_by=_value("--claimed-by", "") or "",
                reason=_value("--reason", "") or "",
                topic=_value("--topic", topic),
            ))
        if cmd == "archive":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "archive requires task_id")
            blocked = _canonical_write_gate("archive", runner=_value("--runner", runner), topic=_value("--topic", topic), coordinator_capability=coordinator_capability)
            if blocked is not None:
                return _as_result({**blocked, "command": command})
            ok, reason = task_store.archive_task(require_repo(), args[1], actor=_value("--runner", runner) or "dashboard", reason=_value("--reason", "") or "")
            return TaskCtlResult(command, 0 if ok else 1, reason, "" if ok else reason)
        if cmd == "restore":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "restore requires task_id")
            blocked = _canonical_write_gate("restore", runner=_value("--runner", runner), topic=_value("--topic", topic), coordinator_capability=coordinator_capability)
            if blocked is not None:
                return _as_result({**blocked, "command": command})
            ok, reason = task_store.restore_task(require_repo(), args[1], actor=_value("--runner", runner) or "dashboard", reason=_value("--reason", "") or "")
            return TaskCtlResult(command, 0 if ok else 1, reason, "" if ok else reason)
        if cmd == "collision-guard":
            return _as_result(collision_guard(print_json="--print" in args or "--json" in args))
        if cmd == "callback-outbox-status":
            return _as_result(callback_outbox_status())
        if cmd == "usage-report":
            return _as_result(usage_report(runner=_value("--runner", runner), topic=_value("--topic", topic), status=_value("--status")))
        if cmd == "usage":
            if len(args) < 2:
                return TaskCtlResult(command, 2, "", "usage requires task_id")
            return _as_result(record_usage(
                args[1],
                runner=_value("--runner", runner) or "",
                topic=_value("--topic", topic),
                model=_value("--model", ""),
                requested_model=_value("--requested-model", ""),
                observed_model=_value("--observed-model", ""),
                model_observed="--model-observed" in args,
                role=_value("--role", "worker"),
                provider=_value("--provider", ""),
                source=_value("--source", ""),
                note=_value("--note", ""),
                input_tokens=_int_value("--input-tokens", 0),
                output_tokens=_int_value("--output-tokens", 0),
                visible_output_tokens=_int_value("--visible-output-tokens", 0),
                reasoning_output_tokens=_int_value("--reasoning-output-tokens", 0),
                total_tokens=_int_value("--total-tokens", 0),
                cached_input_tokens=_int_value("--cached-input-tokens", 0),
                cache_creation_input_tokens=_int_value("--cache-creation-input-tokens", 0),
                cache_write_input_tokens=_int_value("--cache-write-input-tokens", 0),
                usage_observed=True if "--usage-observed" in args else None,
                telemetry_reason=_value("--telemetry-reason", ""),
                cache_metrics_observed="--cache-metrics-observed" in args,
                cost_usd=float(_value("--cost-usd", "0") or 0),
                cost_observed="--cost-observed" in args,
            ))
        if cmd == "export-jsonl":
            return _as_result(export_jsonl(runner=_value("--runner", runner), topic=_value("--topic", topic)))
    except Exception as exc:
        return TaskCtlResult(command, 1, "", f"native_task_command_failed:{type(exc).__name__}:{str(exc)[:500]}")

    return TaskCtlResult(command, 127, "", f"unsupported_native_task_command:{cmd}")


# Card-scoped write actions an exact card owner may perform on its own card.
# ``launch-failed`` is the release half of the reserve->claim boundary: the
# owner that legally created a claim must be able to release it, otherwise a
# reconciled reservation strands its card in processing/claimed forever.
_CARD_SCOPED_ACTIONS = frozenset(
    {"claim-start", "launch-blocked", "launch-failed", "review", "usage"}
)


def _task_id_from_write_args(args: list[str]) -> str | None:
    if not args or args[0] not in _CARD_SCOPED_ACTIONS:
        return None
    if len(args) < 2:
        return None
    task_id = str(args[1])
    return task_id if task_id else None


def _check_card_scoped_write_authority(
    args: list[str],
    runner: str | None,
    topic: str | None,
) -> dict[str, Any]:
    """Allow exact one-off card owners without widening the static matrix."""
    action = args[0] if args else ""
    if action not in _CARD_SCOPED_ACTIONS:
        return {"allowed": False, "reason": f"card_scoped_action_not_allowed:{action}"}
    if runner == CODEX_RUNNER and action != "launch-blocked":
        return {"allowed": False, "reason": "card_scoped_codex_forbidden"}
    if runner is None or topic is None:
        return {"allowed": False, "reason": "runner_and_topic_required_for_card_scoped_authority"}
    runner_reason = _is_malformed_identity_token(runner)
    if runner_reason:
        return {"allowed": False, "reason": f"malformed_runner:{runner_reason}"}
    topic_reason = _is_malformed_identity_token(topic)
    if topic_reason:
        return {"allowed": False, "reason": f"malformed_topic:{topic_reason}"}
    task_id = _task_id_from_write_args(args)
    if task_id is None:
        return {"allowed": False, "reason": "card_scoped_task_id_required"}
    task_reason = _is_malformed_identity_token(task_id)
    if task_reason:
        return {"allowed": False, "reason": f"malformed_task_id:{task_reason}"}
    card, error = _live_card(task_id)
    if error:
        return {"allowed": False, "reason": "card_scoped_task_unresolved"}
    assert card is not None
    if card.get("task_id") != task_id or card.get("runner") != runner or card.get("topic") != topic:
        return {"allowed": False, "reason": "card_scoped_identity_mismatch"}
    claimed_by = str(card.get("claimed_by") or "")
    lifecycle = _lifecycle_state(card)
    if action == "launch-blocked":
        worker_status = str(card.get("worker_status") or "unclaimed")
        if lifecycle == "pending" and worker_status == "unclaimed" and not claimed_by:
            return {"allowed": True, "reason": "card_scoped_launch_blocker_allowed"}
        return {
            "allowed": False,
            "reason": f"card_scoped_launch_blocker_ineligible:{lifecycle}",
        }
    if action == "claim-start":
        worker_status = str(card.get("worker_status") or "unclaimed")
        if lifecycle == "pending" and worker_status == "unclaimed" and not claimed_by:
            return {"allowed": True, "reason": "card_scoped_claim_start_allowed"}
        if (
            lifecycle == "processing"
            and worker_status == "claimed"
            and claimed_by == runner
            and not str(card.get("launch_request_id") or "")
        ):
            # ``auto-pickup`` owns the claim but has no process request yet.
            # Permit only the exact card owner to enter the narrower
            # claim-start path; task_engine then compare-and-swaps the concrete
            # launch_request_id and rejects concurrent/different requests.
            return {"allowed": True, "reason": "card_scoped_launch_attach_allowed"}
        return {"allowed": False, "reason": f"card_scoped_claim_start_ineligible:{lifecycle}"}
    if claimed_by != runner:
        return {"allowed": False, "reason": f"card_scoped_claimed_by_mismatch:{claimed_by}"}
    if action == "review":
        if lifecycle == "processing":
            return {"allowed": True, "reason": "card_scoped_review_allowed"}
        return {"allowed": False, "reason": f"card_scoped_review_ineligible:{lifecycle}"}
    if lifecycle in {"processing", "review"}:
        return {"allowed": True, "reason": "card_scoped_usage_allowed"}
    return {"allowed": False, "reason": f"card_scoped_usage_ineligible:{lifecycle}"}


def health() -> dict[str, Any]:
    root = repo_root()
    readiness = task_store.storage_readiness(root)
    verify = _canonical_result(
        ok=readiness.ready,
        returncode=0 if readiness.ready else 1,
        stdout=json.dumps(readiness.as_dict(), ensure_ascii=False),
        stderr="" if readiness.ready else readiness.reason,
        command=["verify"],
    )
    return {
        "ok": readiness.ready,
        "repo": str(root),
        "task_engine": "native_aiworkhub",
        "runtime_storage": str(Path(repository_state.HUB_DIRNAME)),
        "writes_allowed": writes_allowed(),
        "verify": verify,
        "storage": readiness.as_dict(),
    }


def review_queue() -> dict[str, Any]:
    command = ["review-queue"]
    def _review_substatus(row: Mapping[str, Any]) -> str:
        terminal = row.get("terminal_review")
        return str(
            row.get("terminal_substatus")
            or (terminal.get("substatus") if isinstance(terminal, Mapping) else "")
            or ""
        )

    try:
        rows = [
            row
            for row in task_store.list_task_cards(repo_root(), limit=5000)
            if row.get("status") == "review"
            and _review_substatus(row) != "finalize_failed"
        ][:500]
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    lines = [f"=== Codex Review Queue ({len(rows)}) ==="]
    lines.extend(
        f"  [{r.get('topic') or '?'}] [{r.get('runner') or '?'}] {r.get('task_id') or ''}"
        for r in rows
    )
    return _canonical_result(ok=True, returncode=0, stdout="\n".join(lines), command=command)


def list_tasks(status: str = "pending", topic: str | None = None, limit: int = 80) -> dict[str, Any]:
    """Stdout stays the exact ``taskctl.py cmd_list`` compact-line format
    (``[bucket] [topic] [runner] task_id``) -- ``dashboard.py::parse_task_list``
    and ``completion_inbox.py::_parse_list_task_ids`` both regex-parse this
    stdout and must keep working unchanged."""
    command = ["list", "--status", status]
    if topic:
        command.extend(["--topic", topic])
    command.extend(["--limit", str(limit)])
    try:
        rows = task_store.list_tasks(repo_root(), status=status, limit=5000)
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    if topic:
        rows = [r for r in rows if r.get("topic") == topic]
    rows = rows[: max(1, int(limit))]
    lines = [
        f"[{r.get('status') or 'pending'}] [{r.get('topic') or '?'}] [{r.get('runner') or '?'}] "
        f"{r.get('task_id') or ''}"
        for r in rows
    ]
    stdout = "\n".join(lines)
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def show_task(task_id: str) -> dict[str, Any]:
    command = ["show", task_id]
    try:
        card = task_store.get_task(repo_root(), task_id)
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    if card is None:
        return _canonical_result(ok=True, returncode=0, stdout=f"Task not found: {task_id}", command=command)
    stdout = json.dumps(card, indent=2, ensure_ascii=False, default=str)
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def pending_for_runner(runner: str, topic: str | None = None) -> dict[str, Any]:
    """Matches ``AITools/taskctl.py::_cmd_export``: pending-only, JSONL rows."""
    command = ["export", "--runner", runner]
    if topic:
        command.extend(["--topic", topic])
    try:
        rows = task_store.list_tasks(repo_root(), status="pending", limit=5000)
    except task_store.TaskStoreError as exc:
        result = _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
        result["jsonl_rows"] = []
        return result
    filtered = [r for r in rows if r.get("runner") == runner and (topic is None or r.get("topic") == topic)]
    stdout = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in filtered)
    result = _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
    result["jsonl_rows"] = filtered
    return result


def auto_pickup(runner: str, topic: str | None = None) -> dict[str, Any]:
    command = ["auto-pickup", "--runner", runner]
    if topic:
        command.extend(["--topic", topic])
    blocked = _canonical_write_gate("auto-pickup", runner=runner, topic=topic)
    if blocked is not None:
        return blocked
    try:
        cards = _full_cards_for_plan()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    ready_ids = set(task_plan.build_snapshot(cards)["ready"])
    eligible = eligible_dryrun_candidates(cards, runner, topic, ready_ids=ready_ids)
    if not eligible:
        return _canonical_result(
            ok=False, returncode=1, stderr=f"no_eligible_task:runner={runner}:topic={topic}", command=command
        )
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    # One colliding candidate at the head of the queue must not starve the ready
    # cards behind it. Scan every eligible candidate in order, skipping any whose
    # atomic claim loses the unclaimed+pending guard (already claimed, blocked,
    # or retired since the snapshot was taken), and report what was shadowed so
    # the operator can see the queue was not empty -- it was shadowed.
    skipped: list[dict[str, Any]] = []
    claimed_task_id: str | None = None
    try:
        for candidate in eligible:
            task_id = str(candidate["task_id"])
            row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            try:
                stored_card = json.loads(row["card_json"] or "{}") if row is not None else {}
            except (TypeError, json.JSONDecodeError):
                stored_card = {}
            if not isinstance(stored_card, dict):
                stored_card = {}
            try:
                claim_epoch = int(stored_card.get("claim_epoch") or 0) + 1
            except (TypeError, ValueError):
                claim_epoch = 1
            prior_episode = task_store.begin_claim_episode(stored_card)
            stored_card.update(
                claim_epoch=claim_epoch,
                status="processing",
                worker_status="claimed",
                claimed_by=runner,
            )
            cur = conn.execute(
                "UPDATE tasks SET card_json=?, worker_status='claimed', status='processing', claimed_by=?, "
                "claimed_at=?, started_at=?, completed_at=NULL, updated_at=? "
                "WHERE task_id=? AND worker_status='unclaimed' AND status='pending'",
                (json.dumps(stored_card, ensure_ascii=False), runner, now, now, now, task_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                skipped.append({"task_id": task_id, "reason": "claim_conflict"})
                continue
            conn.execute(
                "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
                (
                    task_id,
                    "auto_pickup",
                    runner,
                    json.dumps(
                        {
                            "runner": runner,
                            "topic": topic,
                            "claim_epoch": claim_epoch,
                            "prior_episode": prior_episode,
                            "skipped_candidates": skipped,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            conn.commit()
            claimed_task_id = task_id
            break
    finally:
        conn.close()
    if claimed_task_id is None:
        result = _canonical_result(
            ok=False,
            returncode=1,
            stderr=f"no_claimable_task:runner={runner}:topic={topic}",
            command=command,
        )
        result["skipped_candidates"] = skipped
        return result
    card = task_store.get_task(repo_root(), claimed_task_id)
    stdout = json.dumps(card, ensure_ascii=False, default=str) if card else ""
    result = _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
    result["skipped_candidates"] = skipped
    return result


def claim_start_exact(
    task_id: str, runner: str, topic: str, request_id: str = ""
) -> dict[str, Any]:
    """Atomic exact-task claim/start against the canonical task store."""
    command = ["claim-start", task_id, "--runner", runner, "--topic", topic]
    if request_id:
        command.extend(["--request-id", request_id])
    blocked = _canonical_write_gate("claim-start", runner=runner, topic=topic, task_id=task_id)
    if blocked is not None:
        return blocked
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        row = conn.execute(
            "SELECT runner, topic, card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return _canonical_result(ok=False, returncode=1, stderr=f"task_not_found:{task_id}", command=command)
        try:
            stored_card = json.loads(row["card_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            stored_card = {}
        if not isinstance(stored_card, dict):
            stored_card = {}
        stored_runner = str(row["runner"] or stored_card.get("runner") or "")
        stored_topic = str(row["topic"] or stored_card.get("topic") or "")
        if stored_runner != runner or stored_topic != topic:
            conn.rollback()
            return _canonical_result(
                ok=False, returncode=1, stderr=f"identity_mismatch:task_id={task_id}", command=command
            )
        try:
            claim_epoch = int(stored_card.get("claim_epoch") or 0) + 1
        except (TypeError, ValueError):
            claim_epoch = 1
        prior_episode = task_store.begin_claim_episode(stored_card)
        stored_card.update(
            claim_epoch=claim_epoch,
            status="processing",
            worker_status="claimed",
            claimed_by=runner,
        )
        cur = conn.execute(
            "UPDATE tasks SET card_json=?, runner=?, topic=?, worker_status='claimed', status='processing', claimed_by=?, "
            "claimed_at=?, started_at=?, completed_at=NULL, updated_at=? "
            "WHERE task_id=? AND worker_status='unclaimed' AND status='pending'",
            (json.dumps(stored_card, ensure_ascii=False), runner, topic, runner, now, now, now, task_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return _canonical_result(
                ok=False, returncode=1, stderr=f"claim_conflict:task_id={task_id}", command=command
            )
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id,
                "claim_start",
                runner,
                json.dumps(
                    {
                        "runner": runner,
                        "topic": topic,
                        "request_id": request_id,
                        "claim_epoch": claim_epoch,
                        "prior_episode": prior_episode,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    card = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card, ensure_ascii=False, default=str) if card else ""
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


# ---------------------------------------------------------------------------
# B118: read-only auto-pickup DRY-RUN preview.
#
# Reports which task ``auto_pickup`` WOULD claim for a (runner, topic) WITHOUT
# mutating the parent queue and WITHOUT touching the write gate. It never
# invokes the write-gated ``auto-pickup`` taskctl command; it reuses only the
# read-only ``export`` command (see ``pending_for_runner``) and then replicates
# the exact candidate-selection predicate of ``taskctl.cmd_auto_pickup``
# (AITools/taskctl.py:953-1018): the first card that is unclaimed + pending +
# runner-matched + topic-matched. The real claim path (``auto_pickup``) stays
# behind the existing ``AIWORKHUB_ALLOW_WRITES`` gate; this preview offers
# no alternate write path and cannot bypass that gate.
# ---------------------------------------------------------------------------

def _lifecycle_state(card: dict[str, Any]) -> str:
    """Compact lifecycle state — faithful local replica of
    ``taskdb.canonical_status`` (AITools/taskdb.py:115-134).

    Kept as a local copy so this MCP module stays import-light (no parent
    ``AITools`` dependency). It MUST stay in sync with the helper it mirrors;
    it is a deterministic classifier, not a runtime cue router.
    """
    status = str(card.get("status") or "").strip().lower()
    worker_status = str(card.get("worker_status") or "").strip().lower()
    if status == "archived":
        return "archived"
    if status in {"finished", "completed", "stale_already_done"} or worker_status == "done":
        return "finished"
    if status.startswith("blocked") or worker_status.startswith(("blocked", "deferred")):
        return "blocked"
    if status in {"review", "ready_for_review", "codex_review", "awaiting_review"} or worker_status in {
        "review",
        "ready_for_review",
        "codex_review",
        "awaiting_review",
    }:
        return "review"
    if status in {"processing", "in_progress"} or worker_status in {"claimed", "in_progress"}:
        return "processing"
    # Mirror ``task_store.canonical_status`` (task_store.py:611): a reviewer-
    # declared ``superseded`` card is closed -- its successor carries the work --
    # not fresh pending work. Folding it into ``pending`` (the drift this fixes)
    # made a replaced card reappear on planning surfaces as work still waiting.
    if status == "superseded" or worker_status == "superseded":
        return "superseded"
    return "pending"


def eligible_dryrun_candidates(
    rows: list[dict[str, Any]],
    runner: str,
    topic: str | None = None,
    *,
    ready_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Ordered list of cards ``auto_pickup`` would consider claimable, mirroring
    the ``taskctl.cmd_auto_pickup`` predicate. Pure: no IO, no mutation.

    A card is eligible iff, in order:
      * worker_status (default 'unclaimed') == 'unclaimed'
      * lifecycle state == 'pending'
      * runner matches exactly
      * topic matches (``topic is None`` -> any topic)
      * ``ready_ids is None`` (no Plan-DAG filtering requested) OR the card's
        task_id is in ``ready_ids`` -- i.e. every ``depends_on`` entry has
        reached the ``finished`` lifecycle state and its ``allowed_writes``
        does not overlap a processing/review card's, per
        ``task_plan.build_snapshot``. Cards with no ``depends_on`` and no
        overlapping writes behave identically to before this filter existed.

    Order is preserved; ``auto_pickup`` claims element ``[0]``.
    """
    out: list[dict[str, Any]] = []
    for c in rows:
        worker_status = str(c.get("worker_status", "unclaimed") or "unclaimed").strip().lower()
        if worker_status != "unclaimed":
            continue
        if _lifecycle_state(c) != "pending":
            continue
        if c.get("runner") != runner:
            continue
        if topic is not None and c.get("topic") != topic:
            continue
        if ready_ids is not None and str(c.get("task_id")) not in ready_ids:
            continue
        out.append(c)
    return out


def _full_cards_for_plan() -> list[dict[str, Any]]:
    """Every canonical card in this repo, merged with its ``card_json`` (so
    ``allowed_writes``/``depends_on`` are present) -- the input ``task_plan``
    needs to build a DAG snapshot."""
    rows = task_store.list_tasks(repo_root(), status=None, limit=5000)
    cards: list[dict[str, Any]] = []
    for row in rows:
        full = task_store.get_task(repo_root(), str(row["task_id"]))
        if full is not None:
            cards.append(full)
    return cards


def task_plan_snapshot() -> dict[str, Any]:
    """Read-only Plan-DAG snapshot: dependencies, blockers, write-scope
    overlaps, and the deterministic set of task_ids ready to be claimed."""
    cards = _full_cards_for_plan()
    snapshot = task_plan.build_snapshot(cards)
    return {
        "ok": True,
        "schema_id": "aiworkhub.task_plan_snapshot.v1",
        **snapshot,
    }


def _compact_card(
    card: dict[str, Any],
    *,
    depends_on: list[str] | None = None,
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    """Token-bounded summary of a card for the dry-run report."""
    out = {
        "task_id": card.get("task_id"),
        "runner": card.get("runner"),
        "topic": card.get("topic"),
        "mode": card.get("mode"),
        "worker_status": card.get("worker_status", "unclaimed"),
        "priority": card.get("priority"),
        "objective": str(card.get("objective", ""))[:160],
        "depends_on": depends_on if depends_on is not None else (card.get("depends_on") or []),
    }
    if blockers:
        out["blockers"] = blockers
    return out


def auto_pickup_dryrun(runner: str, topic: str | None = None) -> dict[str, Any]:
    """READ-ONLY preview of ``auto_pickup``: report the task that WOULD be
    claimed for ``(runner, topic)`` without mutating the parent queue and
    without touching the write gate.

    Contract (all invariants asserted by the B118 smoke test):
      * Never invokes the write-gated ``auto-pickup`` command — the only
        subprocess is the read-only ``export`` command via ``pending_for_runner``.
      * Leaves the parent queue byte-identical (no card status change).
      * Respects the ``--runner``/``--topic`` filters and reports the filtering.
      * Does NOT bypass ``AIWORKHUB_ALLOW_WRITES``; the real claim path
        (``auto_pickup`` / ``aiworkhub_task_auto_pickup``) stays write-gated.
    """
    export_result = pending_for_runner(runner=runner, topic=topic)
    rows = export_result.get("jsonl_rows", []) or []

    dag_error: str | None = None
    snapshot: dict[str, Any] | None = None
    try:
        cards = _full_cards_for_plan()
        snapshot = task_plan.build_snapshot(cards)
        ready_ids = set(snapshot["ready"])
    except task_store.TaskStoreError as exc:
        # Fail closed: if the DAG snapshot cannot be built, report zero
        # ready tasks rather than falling back to an unfiltered (DAG-blind)
        # eligibility check.
        dag_error = str(exc)
        ready_ids: set[str] = set()

    eligible = eligible_dryrun_candidates(rows, runner, topic, ready_ids=ready_ids)
    candidate = eligible[0] if eligible else None
    candidate_blockers = None
    candidate_depends_on = None
    if candidate is not None and snapshot is not None:
        candidate_tid = str(candidate.get("task_id"))
        candidate_blockers = snapshot.get("blockers", {}).get(candidate_tid)
        candidate_depends_on = snapshot.get("dependencies", {}).get(candidate_tid)
    excluded_claimed = sum(
        1
        for c in rows
        if str(c.get("worker_status", "unclaimed") or "unclaimed").strip().lower() != "unclaimed"
    )

    return {
        "ok": bool(export_result.get("ok", False)),
        "dry_run": True,
        "tool": "aiworkhub_task_auto_pickup_dryrun",
        "contract": "B118_v1_autopickup_dryrun",
        "runner": runner,
        "topic": topic,
        "would_claim_task_id": candidate.get("task_id") if candidate else None,
        "candidate": (
            _compact_card(candidate, depends_on=candidate_depends_on, blockers=candidate_blockers)
            if candidate
            else None
        ),
        "filtering": {
            "runner_filter": runner,
            "topic_filter": topic,
            "pending_for_runner_topic": len(rows),
            "eligible_unclaimed": len(eligible),
            "excluded_already_claimed": excluded_claimed,
            "dag_snapshot_error": dag_error,
        },
        "mutation": {
            "queue_mutated": False,
            "write_gate_bypassed": False,
            "write_command_invoked": False,
            "real_auto_pickup_write_gated": True,
            "writes_allowed_env": writes_allowed(),
        },
        "source_command": export_result.get("command"),
        "export_returncode": export_result.get("returncode"),
    }


def _queue_request_authority_flags() -> dict[str, bool]:
    return {
        "runtime_authority": False,
        "support_authority": False,
        "apply_authority": False,
        "training_authority": False,
        "source_registry_live_mutation": False,
        "process_launch": False,
        "process_launch_authority": False,
        "agent_launch": False,
        "shell_invocation": False,
        "queue_write": writes_allowed(),
        "audit_write": writes_allowed(),
        "write_gate_enabled": writes_allowed(),
    }


def queue_request(
    *,
    task_id: str,
    runner: str,
    topic: str,
    adapter_id: str,
    model: str | None = None,
    owner_prompt: str = "",
    argv_template: list[str] | None = None,
    priority: str = "normal",
    stale_timeout_seconds: int = 7200,
    requested_at: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Write-gated Task MCP launch-queue request.

    This does not launch a process. With writes disabled, it returns a blocked
    response and appends nothing. With writes enabled, it appends one entry to
    the existing launch-queue audit JSONL unless the request is idempotent or
    a different runner already has an open entry for the same task_id.
    """
    from aiworkhub import completion_inbox, launch_queue_contract, launch_queue_persist

    now = datetime.now(timezone.utc)
    requested_at = requested_at or now.isoformat()
    request_id = request_id or launch_queue_contract.deterministic_request_id(task_id, runner, requested_at)
    authority_flags = _queue_request_authority_flags()
    base = {
        "tool": "aiworkhub_task_queue_request",
        "contract": "B286_v1_mvp_queue_request_extends_launch_queue_contract",
        "readonly": False,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "adapter_id": adapter_id,
        "request_id": request_id,
        "authority_flags": authority_flags,
        "server_tool": "aiworkhub_task_queue_request",
    }

    for label, value in (("runner", runner), ("topic", topic)):
        reason = _is_malformed_identity_token(value)
        if reason:
            return {
                **base,
                "ok": False,
                "blocked_reason": f"malformed_{label}:{reason}",
                "idempotent_noop": False,
                "duplicate_runner_blocked": False,
                "stale_recovery_requeue": False,
                "request": None,
                "decision": None,
                "persisted_entry": None,
            }

    if adapter_id not in launch_queue_contract.KNOWN_ADAPTERS:
        return {
            **base,
            "ok": False,
            "blocked_reason": f"unknown_adapter:{adapter_id}",
            "idempotent_noop": False,
            "duplicate_runner_blocked": False,
            "stale_recovery_requeue": False,
            "request": None,
            "decision": None,
            "persisted_entry": None,
        }

    request = launch_queue_contract.enqueue_intent(
        task_id=task_id,
        runner=runner,
        topic=topic,
        adapter_id=adapter_id,
        argv_template=argv_template,
        request_id=request_id,
        created_ts=now.timestamp(),
        priority=priority,
        model=model,
        owner_prompt=owner_prompt,
        requested_at=requested_at,
        stale_timeout_seconds=stale_timeout_seconds,
    )
    decision = launch_queue_contract.evaluate_launch(request)

    if not writes_allowed():
        return {
            **base,
            "ok": False,
            "blocked_reason": "write_gate_closed:AIWORKHUB_ALLOW_WRITES!=1",
            "idempotent_noop": False,
            "duplicate_runner_blocked": False,
            "stale_recovery_requeue": False,
            "request": request.as_dict(),
            "decision": decision.as_dict(),
            "persisted_entry": None,
        }

    open_requests = launch_queue_persist.find_open_requests(task_id)
    for entry in open_requests:
        if entry.get("request_id") == request_id:
            return {
                **base,
                "ok": True,
                "blocked_reason": "",
                "idempotent_noop": True,
                "duplicate_runner_blocked": False,
                "stale_recovery_requeue": False,
                "request": request.as_dict(),
                "decision": decision.as_dict(),
                "persisted_entry": None,
                "existing_entry": entry,
            }
    duplicate = next((e for e in open_requests if e.get("runner") != runner), None)
    if duplicate is not None:
        return {
            **base,
            "ok": False,
            "blocked_reason": f"duplicate_runner_open_request:{duplicate.get('runner')}",
            "idempotent_noop": False,
            "duplicate_runner_blocked": True,
            "stale_recovery_requeue": False,
            "request": request.as_dict(),
            "decision": decision.as_dict(),
            "persisted_entry": None,
            "existing_entry": duplicate,
        }

    stale_recovery_requeue = False
    try:
        inbox = completion_inbox.build_completion_inbox(topic=topic, limit=500)
        stale_recovery_requeue = any(
            row.get("task_id") == task_id for row in inbox.get("stale_processing", [])
        )
    except Exception:
        stale_recovery_requeue = False

    persisted_entry = launch_queue_persist.persist_transition(
        request,
        decision,
        ts=now.timestamp(),
    )
    return {
        **base,
        "ok": True,
        "blocked_reason": "",
        "idempotent_noop": False,
        "duplicate_runner_blocked": False,
        "stale_recovery_requeue": stale_recovery_requeue,
        "request": request.as_dict(),
        "decision": decision.as_dict(),
        "persisted_entry": persisted_entry,
    }


def _lifecycle_error(message: str, returncode: int = 126) -> dict[str, Any]:
    return {
        "ok": False,
        "returncode": returncode,
        "command": [],
        "stdout": "",
        "stderr": message,
    }


def _live_card(task_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    shown = show_task(task_id)
    try:
        card = json.loads(shown.get("stdout", ""))
    except (TypeError, json.JSONDecodeError):
        return None, _lifecycle_error(f"cannot resolve live task identity: {task_id}", 1)
    if not isinstance(card, dict) or card.get("task_id") != task_id:
        return None, _lifecycle_error(f"cannot resolve live task identity: {task_id}", 1)
    return card, None


MCP_MANAGER_CONTRACT_BANNER = (
    "AIWORKHUB MANAGER CONTRACT (MANDATORY): call "
    "aiworkhub_manager_bootstrap first and trust only repository-bound MCP "
    "receipts. For every non-trivial code request, use "
    "aiworkhub_manager_source_graph_query before built-in filesystem discovery; "
    "start with focus/slice and re-query when the active boundary changes. If "
    "required Source Graph is unavailable, report it instead of silently "
    "bypassing AIWorkHub. Creating a task leaves it pending; only an exact claim plus "
    "launch may make it processing. Launch independent tasks in parallel only "
    "after dependency, write-scope, preflight and collision checks. Workers "
    "stop at review_ready; every review/terminal transition emits a callback, "
    "but only the current verified manager may inspect evidence and accept or "
    "reject it. Never invent task/status/callback IDs, infer state from prose, "
    "or recreate a task after a lost acknowledgement—reconcile the same ID. "
    "Tasks are uncapped by default; never infer or auto-assign a token cap "
    "unless the owner supplies an exact budget or repository policy "
    "pre-registers one."
)


def manager_bootstrap() -> dict[str, Any]:
    """Compact, model-readable manager contract for a newly attached chat."""
    identity = _claude_manager_identity() or _codex_manager_identity()
    provider = str((identity or {}).get("provider") or _current_chat_provider())
    return {
        "ok": True,
        "schema_id": "aiworkhub.manager_bootstrap.v1",
        "role": "manager" if identity else "worker_or_unverified_client",
        "provider": provider,
        "repo": str(repo_root()),
        "manager_route": identity or {},
        "operating_contract": {
            "schema_id": "aiworkhub.manager_operating_contract.v1",
            "mandatory": True,
            "banner": MCP_MANAGER_CONTRACT_BANNER,
            "authority": [
                "Use only receipts whose repo and repo_id match the active repository.",
                "Proceed as manager only when role=manager and manager_route is verified.",
                "The verified bootstrap/repository_current repo and repo_id override host cwd, workspace_roots, environment_context and chat prose.",
                "On any repository mismatch, stop before filesystem access and switch/reload the route; never inspect the hinted repository as fallback.",
                "Repository tools and canonical stores are the state authority; chat prose and dashboard impressions are not.",
            ],
            "start_sequence": [
                "aiworkhub_manager_bootstrap",
                "aiworkhub_repository_current and aiworkhub_task_health",
                "aiworkhub_manager_source_graph_query with focus/slice and workflow_stage=orientation before built-in code discovery",
                "aiworkhub_manager_session_current_state, one task-specific AI Memory query, and KB only when relevant",
                "aiworkhub_task_plan_snapshot before creating or launching a dependency wave",
            ],
            "task_state_machine": {
                "create": "aiworkhub_task_create creates/reconciles one canonical pending card; it does not run the model",
                "token_budget": (
                    "Tasks are uncapped by default. Never infer, estimate, or "
                    "auto-assign max_live_tokens from task complexity, model, "
                    "historical usage, or cost. Set it only when the owner "
                    "explicitly supplies an exact cap or a repository policy "
                    "pre-registers one; optimize reads, context, edits, retries, "
                    "and validation instead of truncating useful work. When an "
                    "authorized cap exists, AIWorkHub enforces it only from "
                    "structured live provider usage and labels terminal-only "
                    "usage posthoc_only."
                ),
                "claim": "aiworkhub_task_auto_pickup is an optional explicit claim step for one dependency-ready non-colliding card",
                "launch": "aiworkhub_agent_launch_task is always required; it atomically claims a pending card or attaches the exact prior claim, then starts the worker; only then may runtime truth become processing",
                "worker_finish": "the worker submits evidence and stops at review_ready or another truthful terminal substatus",
                "manager_finish": "the current verified manager independently verifies evidence, then accepts or rejects review",
            },
            "parallelism": [
                "Parallel launch is encouraged for independent ready cards.",
                "Never parallelize unmet dependencies or overlapping allowed_writes.",
                "Use preflight/workforce evidence and the plan/collision receipts; do not assume a model or route is available.",
            ],
            "callbacks_and_review": [
                "Every transition into review or another terminal state is callback-eligible; task category does not suppress delivery.",
                "A callback is a wake-up notification, not acceptance or proof of quality.",
                "Inspect the completion inbox, diff, tests, logs, artifacts and tool-use receipts before finalization.",
                "The repository's current verified manager receives the callback; originating thread identity remains audit provenance.",
            ],
            "recovery": [
                "After Transport closed, query the same task_id before retrying.",
                "Retry an identical create payload with the same task_id; never create a replacement ID to guess around a lost response.",
                "On repository switch or runtime reload, call bootstrap/current-state again before acting.",
            ],
        },
        "workflow": [
            "aiworkhub_manager_source_graph_query / session_current_state / ai_memory_search / kb_*",
            "aiworkhub_task_create",
            "aiworkhub_task_auto_pickup (optional exact claim)",
            "aiworkhub_agent_launch_task (required worker start; may atomically claim pending)",
            "aiworkhub_task_mark_review",
            "provider callback receipt",
            "aiworkhub_task_mark_done or aiworkhub_task_reject_review",
        ],
        "callback": {
            "codex": (
                "delivers to the repository's current verified Codex manager; "
                "the originating thread remains immutable audit provenance"
            ),
            "claude": "call aiworkhub_dispatcher_ensure_started, then aiworkhub_claude_callback_wait and immediately ack",
        },
        "rules": [
            "Managers use AIWorkHub manager AI tools before repository discovery, review, rebase, and planning.",
            "For non-trivial code work, Source Graph is the first discovery surface and must be re-queried as the working boundary changes.",
            "Create tasks through aiworkhub_task_create; never require repository-local AITools/taskctl.py.",
            "Use the returned task_id exactly and never fabricate callback batch/lease ids.",
            "Workers stop at review; a verified manager finalizes or rejects review.",
            "Each repository owns its own task database, callback lanes, and runtime capability.",
        ],
    }


def repository_current() -> dict[str, Any]:
    """Return the exact process/repository authority currently in effect."""

    root = repo_root()
    readiness = task_store.storage_readiness(root)
    identity = _claude_manager_identity() or _codex_manager_identity()
    if os.environ.get("AIWORKHUB_REPO_ROOT", "").strip():
        binding_source = "explicit_repo_child"
    elif _PROCESS_REPO_ROOT_OVERRIDE is not None:
        binding_source = "manager_switch"
    elif os.environ.get("AIWORKHUB_REPO", "").strip():
        binding_source = "legacy_explicit"
    elif _implicit_codex_repository_root() is not None:
        binding_source = "live_codex_route"
    else:
        binding_source = "process_cwd"
    return {
        "ok": bool(readiness.ready),
        "schema_id": "aiworkhub.repository_current.v1",
        "repo_id": str(readiness.repo_id or ""),
        "repo_root": str(root),
        "storage_ready": bool(readiness.ready),
        "storage_reason": str(readiness.reason or ""),
        "binding_source": binding_source,
        "manager_verified": bool(identity),
        "manager_route": identity or {},
    }


def repository_switch(repo_id: str) -> dict[str, Any]:
    """Serialize one complete repository lifecycle handoff.

    The lock covers route validation, old-service shutdown, binding swap,
    target-service convergence and rollback.  Other switch requests therefore
    observe either the old repository or the fully-started target, never an
    interleaved half-switch.
    """

    with _REPOSITORY_SWITCH_LOCK:
        return _repository_switch_locked(repo_id)


def _repository_switch_locked(repo_id: str) -> dict[str, Any]:
    """Atomically bind a repo-neutral Codex manager MCP to one live repo.

    The caller supplies only a manifest ``repo_id``.  The path is resolved
    from a live shared-router record and must carry this exact verified
    manager thread/window.  Explicit extension/worker children remain
    immutable and require their owner to replace the child instead.
    """

    global _PROCESS_REPO_ROOT_OVERRIDE

    requested = str(repo_id or "").strip()
    if not _REPO_ID_RE.fullmatch(requested):
        return {"ok": False, "error": "repo_id_invalid"}
    if os.environ.get("AIWORKHUB_REPO_ROOT", "").strip():
        return {"ok": False, "error": "explicit_repo_child_binding_immutable"}
    identity = _claude_manager_identity() or _codex_manager_identity()
    if not isinstance(identity, dict):
        return {"ok": False, "error": "verified_manager_identity_required"}
    provider = str(identity.get("provider") or "").strip().lower()
    if provider != "codex":
        return {"ok": False, "error": "repository_switch_requires_codex_route"}
    thread_id = str(identity.get("thread_id") or identity.get("session_id") or "").strip()
    window_id = str(identity.get("window_id") or "").strip()
    if not _valid_origin_thread_id(thread_id) or not window_id:
        return {"ok": False, "error": "callback_capable_manager_route_required"}
    registry = shared_router.list_known_repositories(limit=256)
    matches = [
        record for record in registry.get("repositories", [])
        if isinstance(record, dict)
        and str(record.get("repo_id") or "") == requested
        and bool(record.get("extension_host_alive"))
        and not bool(record.get("stale"))
    ] if registry.get("ok") else []
    if len(matches) != 1:
        return {"ok": False, "error": "repository_route_not_live" if not matches else "repository_route_ambiguous"}
    record = matches[0]
    targets = record.get("targets")
    target = targets.get(provider) if isinstance(targets, dict) else None
    route = target.get("route") if isinstance(target, dict) else None
    if not isinstance(route, dict):
        return {"ok": False, "error": "target_manager_route_missing"}
    if (
        str(record.get("window_id") or "") != window_id
        or str(route.get("thread_id") or "") != thread_id
        or str(route.get("repo_id") or requested) != requested
    ):
        return {"ok": False, "error": "target_route_not_owned_by_current_manager"}
    try:
        target_root = Path(str(record.get("repo_root") or "")).resolve()
        readiness = task_store.storage_readiness(target_root)
    except (OSError, RuntimeError, task_store.TaskStoreError):
        return {"ok": False, "error": "target_repository_unavailable"}
    if not readiness.ready or str(readiness.repo_id or "") != requested:
        return {"ok": False, "error": "target_repository_identity_mismatch"}
    current_root = repo_root()
    if current_root == target_root:
        return {**repository_current(), "switched": False}
    previous_override = _PROCESS_REPO_ROOT_OVERRIDE
    bridge = _callback_bridge_module()
    daemon = _source_graph_daemon_module()
    target_binding_active = False
    try:
        bridge.stop_dispatcher(current_root)
        daemon.stop_daemon(current_root)
        _PROCESS_REPO_ROOT_OVERRIDE = target_root
        target_binding_active = True
        rebound_identity = _codex_manager_identity()
        if (
            not isinstance(rebound_identity, dict)
            or str(rebound_identity.get("thread_id") or rebound_identity.get("session_id") or "") != thread_id
        ):
            raise RuntimeError("target_manager_identity_not_verified")
        source_graph = daemon.ensure_started(target_root)
        if isinstance(source_graph, dict) and not source_graph.get("ok", True):
            raise RuntimeError(
                str(source_graph.get("error") or source_graph.get("reason") or "source_graph_start_failed")
            )
        callback = dispatcher_ensure_started()
        if not callback.get("ok") and callback.get("status") not in {"manager_inbox", "started"}:
            raise RuntimeError(str(callback.get("reason") or callback.get("status") or "callback_start_failed"))
        return {
            **repository_current(),
            "switched": True,
            "previous_repo_root": str(current_root),
            "source_graph": source_graph,
            "callback": callback,
        }
    except (OSError, RuntimeError, ValueError, task_store.TaskStoreError) as exc:
        # A failed target convergence must not leave target-owned background
        # services alive after authority returns to the old repository.
        if target_binding_active:
            try:
                bridge.stop_dispatcher(target_root)
            except (OSError, RuntimeError, ValueError, task_store.TaskStoreError):
                pass
            try:
                daemon.stop_daemon(target_root)
            except (OSError, RuntimeError, ValueError, task_store.TaskStoreError):
                pass
        _PROCESS_REPO_ROOT_OVERRIDE = previous_override
        try:
            daemon.ensure_started(current_root)
            dispatcher_ensure_started()
        except (OSError, RuntimeError, ValueError, task_store.TaskStoreError):
            pass
        return {"ok": False, "error": f"repository_switch_failed:{type(exc).__name__}:{str(exc)[:160]}"}


def _task_contract_path(raw: Any) -> str:
    """Return a normalized path-like card value, or ``""`` for prose."""
    if not isinstance(raw, str):
        return ""
    value = raw.strip().replace("\\", "/")
    if not value or "\x00" in value or any(ch.isspace() for ch in value):
        return ""
    leaf = value.rsplit("/", 1)[-1]
    if not (
        "/" in value
        or any(ch in value for ch in "*?[")
        or value.startswith(".")
        or "." in leaf
    ):
        return ""
    return value.lstrip("./")


def _task_contract_paths_overlap(left: str, right: str) -> bool:
    if fnmatch.fnmatchcase(left, right) or fnmatch.fnmatchcase(right, left):
        return True
    def static_prefix(value: str) -> str:
        indices = [value.find(ch) for ch in "*?[" if ch in value]
        stop = min(indices) if indices else len(value)
        return value[:stop].rstrip("/")

    left_prefix = static_prefix(left)
    right_prefix = static_prefix(right)
    if not left_prefix or not right_prefix:
        return False
    return (
        left_prefix == right_prefix
        or left_prefix.startswith(right_prefix + "/")
        or right_prefix.startswith(left_prefix + "/")
    )


def task_card_path_conflicts(card: dict[str, Any]) -> list[dict[str, str]]:
    """Return bounded required/forbidden path contradictions in one card."""
    forbidden = [
        path
        for path in (_task_contract_path(v) for v in card.get("forbidden") or [])
        if path
    ]
    if not forbidden:
        return []
    conflicts: list[dict[str, str]] = []
    for field in ("allowed_writes", "required_outputs", "read_first", "immutable_inputs"):
        values = card.get(field) or []
        if not isinstance(values, list):
            continue
        for raw in values:
            declared = _task_contract_path(raw)
            if not declared:
                continue
            for denied in forbidden:
                if _task_contract_paths_overlap(declared, denied):
                    conflicts.append({
                        "field": field,
                        "path": declared,
                        "forbidden": denied,
                    })
                    if len(conflicts) >= 32:
                        return conflicts
    text_fields = [("objective", str(card.get("objective") or ""))]
    text_fields.extend(
        ("validation", str(value)) for value in (card.get("validation") or [])
    )
    for field, text in text_fields:
        normalized_text = text.replace("\\", "/")
        for denied in forbidden:
            if denied and denied in normalized_text:
                row = {"field": field, "path": denied, "forbidden": denied}
                if row not in conflicts:
                    conflicts.append(row)
                    if len(conflicts) >= 32:
                        return conflicts
    return conflicts


_CONTEXT_QUERY_STOPWORDS = frozenset({
    "aiworkhub", "task", "worker", "review", "audit", "repair", "implement",
    "validate", "validation", "code", "source", "graph", "model", "manager",
    "claude", "codex", "deepseek", "glm", "vscode", "the", "and", "for",
    "with", "from", "into", "this", "that", "using",
})


def _task_context_query(
    *,
    title: str,
    topic: str,
    objective: str,
    acceptance: list[str],
    read_first: list[str],
    immutable_inputs: list[str],
    allowed_writes: list[str],
) -> str:
    """Derive a bounded task-entity query instead of a project-name token."""

    ranked: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        token = raw.strip(" ./\\,:;()[]{}<>`'\"")
        if len(token) < 3 or len(token) > 160:
            return
        identity = token.casefold()
        if (
            identity in _CONTEXT_QUERY_STOPWORDS
            or identity.startswith("aiworkhub_")
            or identity in seen
        ):
            return
        seen.add(identity)
        ranked.append(token)

    # Exact declared files/symbol-bearing paths are the strongest authority.
    for raw in (*read_first, *immutable_inputs, *allowed_writes):
        if any(ch in raw for ch in "*?["):
            continue
        normalized = raw.replace("\\", "/").strip()
        if normalized and not normalized.startswith(".git"):
            add(normalized)

    text = " ".join((title, objective, *acceptance, topic))
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_:.+/-]{2,}", text)
    # Prefer identifiers (CamelCase, snake_case, qualified names and paths)
    # over ordinary prose words, then retain a few meaningful fallbacks.
    identifiers = [
        token for token in tokens
        if "_" in token
        or "::" in token
        or "/" in token
        or "." in token
        or any(ch.isupper() for ch in token[1:])
    ]
    for token in (*identifiers, *tokens):
        add(token)
        if len(ranked) >= 8:
            break
    query = " ".join(ranked[:8]).encode("utf-8")[:512].decode(
        "utf-8", errors="ignore"
    ).strip()
    return query or "repository"


def create_task(
    task_id: str,
    title: str,
    runner: str,
    topic: str,
    objective: str,
    acceptance: list[str],
    allowed_writes: list[str],
    forbidden: list[str] | None = None,
    required_outputs: list[str] | None = None,
    allow_empty_required_outputs: list[str] | None = None,
    allow_unchanged_required_outputs: list[str] | None = None,
    validation: list[str] | None = None,
    priority: str = "normal",
    callback_required: bool = True,
    task_type: str = "code",
    depends_on: list[str] | None = None,
    read_first: list[str] | None = None,
    immutable_inputs: list[str] | None = None,
    read_only: bool = False,
    max_live_tokens: int | None = None,
    work_kind: str = "generic",
    validation_roles: list[str] | None = None,
    risk_tier: str | None = None,
) -> dict[str, Any]:
    """Create one new canonical task card for the verified manager chat.

    Identity, callback provider, and origin thread are derived from the live
    manager route.  They are intentionally not caller-controlled parameters.
    Existing task ids are never overwritten.

    ``callback_required`` controls whether creation must fail closed until a
    callback-capable manager route is observed. Polling-only cards may be
    created while that route is pending. ``depends_on`` (optional) names other task_ids in the same repo that must
    reach the ``finished`` lifecycle state before this card is DAG-ready (see
    ``task_plan.py``). Omitting it (the default, ``None``) is identical to the
    pre-DAG behavior: an empty dependency list that never blocks readiness.
    """
    identity = _claude_manager_identity() or _codex_manager_identity()
    if identity is None:
        return _lifecycle_error("manager_identity_required:task_create", 126)
    # Creation has no pre-existing card whose runner/topic could authorize the
    # write, so the normal card-scoped allowlist is inapplicable here.  Keep
    # the write gate, then require the verified manager capability directly.
    blocked = _canonical_write_gate("add-card")
    if blocked is not None:
        return blocked
    capability_ok, capability_reason = _verify_coordinator_capability(CODEX_RUNNER)
    if not capability_ok:
        return _lifecycle_error(capability_reason, 126)

    task_id = str(task_id or "").strip()
    runner = str(runner or "").strip()
    topic = str(topic or "").strip()
    title = str(title or "").strip()
    objective = str(objective or "").strip()
    priority = str(priority or "normal").strip().lower()
    task_type = str(task_type or "code").strip().lower()
    work_kind = str(work_kind or "generic").strip().lower()
    risk_tier = (
        str(risk_tier).strip().lower()
        if risk_tier is not None
        else None
    )
    if not _TASK_ID_RE.fullmatch(task_id):
        return _lifecycle_error("invalid_task_id", 2)
    if not _TASK_IDENTITY_RE.fullmatch(runner) or not _TASK_IDENTITY_RE.fullmatch(topic):
        return _lifecycle_error("invalid_runner_or_topic", 2)
    coordinator_worker_runner = runner == CODEX_RUNNER
    if not title or len(title) > 300 or not objective or len(objective) > 4000:
        return _lifecycle_error("invalid_title_or_objective", 2)
    if priority not in ("low", "normal", "high", "critical"):
        return _lifecycle_error("invalid_priority", 2)
    if risk_tier is not None and risk_tier not in (
        "low", "medium", "high", "critical"
    ):
        return _lifecycle_error("invalid_risk_tier", 2)
    allowed_task_types = ("code", "data_classification", "research")
    if task_type not in allowed_task_types:
        result = _lifecycle_error("invalid_task_type", 2)
        result["allowed_task_types"] = list(allowed_task_types)
        result["received_task_type"] = task_type[:80]
        return result
    if not isinstance(read_only, bool):
        return _lifecycle_error("read_only_invalid", 2)
    if (
        max_live_tokens is not None
        and (
            isinstance(max_live_tokens, bool)
            or not isinstance(max_live_tokens, int)
            or not 1 <= max_live_tokens <= 100_000_000
        )
    ):
        return _lifecycle_error("max_live_tokens_out_of_range", 2)

    def bounded_strings(value: list[str] | None, name: str, *, required: bool = False) -> list[str]:
        if not isinstance(value, list) or (required and not value) or len(value) > 128:
            raise ValueError(f"invalid_{name}")
        result: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if not text or len(text) > 1000:
                raise ValueError(f"invalid_{name}")
            result.append(text)
        return result

    try:
        acceptance2 = bounded_strings(acceptance, "acceptance", required=True)
        writes2 = bounded_strings(allowed_writes, "allowed_writes")
        forbidden2 = bounded_strings(forbidden or [], "forbidden")
        outputs2 = bounded_strings(required_outputs or [], "required_outputs")
        allow_empty_outputs2 = bounded_strings(
            allow_empty_required_outputs or [],
            "allow_empty_required_outputs",
        )
        allow_unchanged_outputs2 = bounded_strings(
            allow_unchanged_required_outputs or [],
            "allow_unchanged_required_outputs",
        )
        validation2 = bounded_strings(validation or [], "validation")
        validation_roles2 = bounded_strings(
            validation_roles or [], "validation_roles"
        )
        read_first2 = bounded_strings(read_first or [], "read_first")
        immutable_inputs2 = bounded_strings(immutable_inputs or [], "immutable_inputs")
    except ValueError as exc:
        return _lifecycle_error(str(exc), 2)
    if read_only:
        if writes2:
            return _lifecycle_error("read_only_allowed_writes_forbidden", 2)
        if outputs2:
            return _lifecycle_error("read_only_required_outputs_forbidden", 2)
        if allow_empty_outputs2 or allow_unchanged_outputs2:
            return _lifecycle_error("read_only_output_exceptions_forbidden", 2)
    elif not writes2 and not outputs2:
        return _lifecycle_error("read_only_declaration_required", 2)
    elif not outputs2:
        # Launch rejects a writable card with no authenticated result and
        # promotion contract. Reject it before durable task creation too, so
        # task_create and launch cannot disagree after provider work is queued.
        return _lifecycle_error("required_outputs_invalid", 2)
    if task_type == "code" and (writes2 or outputs2) and not validation2:
        return _lifecycle_error("code_task_validation_required", 2)
    from . import quality_evidence

    try:
        work_kind, validation_roles2 = quality_evidence.normalize_behavioral_contract(
            work_kind,
            validation2,
            validation_roles2,
        )
    except ValueError as exc:
        result = _lifecycle_error(str(exc), 2)
        result["allowed_work_kinds"] = list(quality_evidence.WORK_KINDS)
        result["allowed_validation_roles"] = list(
            quality_evidence.VALIDATION_ROLES
        )
        return result
    # Parse every validation command before persisting the card.  The worker
    # uses this exact fail-closed parser later, so accepting syntax here that
    # can never reach execution only burns a provider run before ending in
    # validation_failed.  Import lazily to keep core's startup dependency
    # surface unchanged.
    from . import worker_workspace

    for output_index, output_pattern in enumerate(outputs2):
        try:
            normalized_output = worker_workspace._relative_repo_path(output_pattern)
            output_allowed = worker_workspace._matches(normalized_output, writes2)
        except worker_workspace.WorkspaceError as exc:
            result = _lifecycle_error(
                f"invalid_required_output_path:{exc}",
                2,
            )
            result.update({
                "required_output_index": output_index,
                "required_output": output_pattern[:240],
            })
            return result
        if not output_allowed:
            result = _lifecycle_error(
                f"required_output_not_allowed:{normalized_output}",
                2,
            )
            result.update({
                "required_output_index": output_index,
                "required_output": output_pattern[:240],
                "contract_hint": (
                    "required_outputs accepts repo-relative file paths or glob patterns only; "
                    "put prose requirements in acceptance"
                ),
            })
            return result

    def validate_required_output_exceptions(
        values: list[str],
        *,
        field: str,
        allow_required_glob_match: bool,
        allow_write_glob_match: bool,
    ) -> dict[str, Any] | None:
        for index, raw_path in enumerate(values):
            try:
                normalized = worker_workspace._relative_repo_path(raw_path)
            except worker_workspace.WorkspaceError as exc:
                result = _lifecycle_error(f"invalid_{field}:{exc}", 2)
                result.update({f"{field}_index": index, field: raw_path[:240]})
                return result
            if any(ch in normalized for ch in "*?["):
                result = _lifecycle_error(f"invalid_{field}:glob_forbidden", 2)
                result.update({f"{field}_index": index, field: raw_path[:240]})
                return result
            output_match = any(
                normalized == required
                or (
                    allow_required_glob_match
                    and fnmatch.fnmatchcase(normalized, required)
                )
                for required in outputs2
            )
            if not output_match:
                result = _lifecycle_error(
                    f"{field.removesuffix('s')}_not_in_required_outputs:{normalized}",
                    2,
                )
                result.update({f"{field}_index": index, field: raw_path[:240]})
                return result
            write_match = any(
                normalized == allowed
                or (
                    allow_write_glob_match
                    and fnmatch.fnmatchcase(normalized, allowed)
                )
                for allowed in writes2
            )
            if not write_match:
                result = _lifecycle_error(
                    f"{field.removesuffix('s')}_not_in_allowed_writes:{normalized}",
                    2,
                )
                result.update({f"{field}_index": index, field: raw_path[:240]})
                return result
        return None

    exception_error = validate_required_output_exceptions(
        allow_empty_outputs2,
        field="allow_empty_required_outputs",
        allow_required_glob_match=True,
        allow_write_glob_match=True,
    )
    if exception_error is not None:
        return exception_error
    exception_error = validate_required_output_exceptions(
        allow_unchanged_outputs2,
        field="allow_unchanged_required_outputs",
        allow_required_glob_match=False,
        allow_write_glob_match=False,
    )
    if exception_error is not None:
        return exception_error

    for validation_index, validation_command in enumerate(validation2):
        try:
            worker_workspace.validation_argv(validation_command)
        except worker_workspace.WorkspaceError as exc:
            result = _lifecycle_error(
                f"invalid_validation_command:{exc}",
                2,
            )
            result.update({
                "validation_index": validation_index,
                "validation_command": validation_command[:240],
                "supported_validation_examples": [
                    "pytest -q tests/test_target.py",
                    "ruff check src/target.py tests/test_target.py",
                    "python -m pytest -q tests/test_target.py",
                    "python scripts/validate_target.py",
                ],
            })
            return result
    for item in writes2:
        path = Path(item)
        if path.is_absolute() or ".." in path.parts:
            return _lifecycle_error("invalid_allowed_write_path", 2)
    conflicts = task_card_path_conflicts({
        "objective": objective,
        "validation": validation2,
        "allowed_writes": writes2,
        "required_outputs": outputs2,
        **(
            {"allow_empty_required_outputs": allow_empty_outputs2}
            if allow_empty_outputs2
            else {}
        ),
        **(
            {"allow_unchanged_required_outputs": allow_unchanged_outputs2}
            if allow_unchanged_outputs2
            else {}
        ),
        "read_first": read_first2,
        "immutable_inputs": immutable_inputs2,
        "forbidden": forbidden2,
    })
    if conflicts:
        result = _lifecycle_error("contradictory_task_path_contract", 2)
        result["conflicts"] = conflicts
        return result
    try:
        depends_on2 = task_plan.normalize_depends_on(depends_on)
    except task_plan.TaskPlanError as exc:
        return _lifecycle_error(str(exc), 2)

    # Codex routes expose ``thread_id`` while Claude's verified VS Code
    # manager descriptor exposes ``session_id``.  Both are exact originating
    # chat identities and must be persisted in the same canonical card field.
    # Requiring only thread_id made every valid Claude manager-created task
    # fail with the misleading Codex-specific route_pending error.
    candidate_origin_thread_id = str(
        identity.get("thread_id") or identity.get("session_id") or ""
    ).strip()
    origin_thread_id = (
        candidate_origin_thread_id
        if _valid_origin_thread_id(candidate_origin_thread_id)
        else ""
    )
    if callback_required and not origin_thread_id:
        provider_name = str(identity.get("provider") or "manager").strip().lower()
        missing_route = (
            "codex_thread_id_not_observed"
            if provider_name == "codex"
            else f"{provider_name}_origin_id_not_observed"
        )
        return _lifecycle_error(
            f"callback_route_pending:{missing_route}",
            126,
        )
    provider = str(identity["provider"])
    now = datetime.now(timezone.utc).isoformat()
    context_query = _task_context_query(
        title=title,
        topic=topic,
        objective=objective,
        acceptance=acceptance2,
        read_first=read_first2,
        immutable_inputs=immutable_inputs2,
        allowed_writes=writes2,
    )
    session_topic = title.encode("utf-8")[:128].decode("utf-8", errors="ignore").strip() or topic
    semantic_query = f"{title} {topic}".encode("utf-8")[:512].decode(
        "utf-8", errors="ignore"
    ).strip()
    card = {
        "schema_id": "aiworkhub.task_card.v1",
        "task_id": task_id,
        "title": title,
        "runner": runner,
        "topic": topic,
        "mode": "",
        "priority": priority,
        "status": "pending",
        "worker_status": "unclaimed",
        "objective": objective,
        "origin_thread_id": origin_thread_id,
        "coordinator_provider": provider,
        "callback_required": bool(callback_required),
        "callback_supported": bool(origin_thread_id),
        "manager_route_state": str(identity.get("route_state") or ""),
        "acceptance": acceptance2,
        "read_only": read_only,
        "allowed_writes": writes2,
        "forbidden": forbidden2,
        "required_outputs": outputs2,
        **(
            {"allow_empty_required_outputs": allow_empty_outputs2}
            if allow_empty_outputs2
            else {}
        ),
        **(
            {"allow_unchanged_required_outputs": allow_unchanged_outputs2}
            if allow_unchanged_outputs2
            else {}
        ),
        "read_first": read_first2,
        "immutable_inputs": immutable_inputs2,
        "validation": validation2,
        "validation_roles": validation_roles2,
        "work_kind": work_kind,
        **({"risk_tier": risk_tier} if risk_tier is not None else {}),
        "depends_on": depends_on2,
        "token_budget": (
            {
                "schema_id": "aiworkhub.task_token_budget.v1",
                "cap_tokens": max_live_tokens,
                "enforcement": "live_when_provider_reports_usage",
            }
            if max_live_tokens is not None
            else None
        ),
        "project_context": {
            "required": True,
            "task_type": task_type,
            "source_graph": {
                "mode": "focus",
                "query": context_query,
                "budget": 48,
                "bundle_type": "explore",
                "required": task_type == "code",
            },
            "session": {"topic": session_topic, "limit": 8},
            "ai_memory": {"query": semantic_query, "limit": 5},
            "kb": {"query": semantic_query, "limit": 5},
        },
    }

    # Only caller-controlled task semantics participate in idempotency.  The
    # manager route fields are runtime provenance and may legitimately change
    # after a transport restart, while the requested operation is still the
    # same create.  A same-id/different-payload retry remains a hard conflict.
    requested_payload = {
        "title": title,
        "runner": runner,
        "topic": topic,
        "objective": objective,
        "acceptance": acceptance2,
        "read_only": read_only,
        "allowed_writes": writes2,
        "forbidden": forbidden2,
        "required_outputs": outputs2,
        "allow_empty_required_outputs": allow_empty_outputs2,
        "allow_unchanged_required_outputs": allow_unchanged_outputs2,
        "validation": validation2,
        "validation_roles": validation_roles2,
        "work_kind": work_kind,
        "risk_tier": risk_tier,
        "priority": priority,
        "task_type": task_type,
        "depends_on": depends_on2,
        "read_first": read_first2,
        "immutable_inputs": immutable_inputs2,
        "max_live_tokens": max_live_tokens,
    }

    def reconcile_existing(existing_json: Any) -> dict[str, Any]:
        try:
            existing_card = json.loads(existing_json or "{}")
        except (TypeError, json.JSONDecodeError):
            existing_card = {}
        if not isinstance(existing_card, dict):
            existing_card = {}
        # A retry landing on a LIVE row is the lost-response recovery path and
        # must still hand back that card (the contract depends on it). But a
        # finished/archived/superseded row is closed: reconciling it returns a
        # card the caller cannot use and does not know is dead. Return a receipt
        # naming the terminal state instead of pretending a usable card exists.
        existing_lifecycle = _lifecycle_state(existing_card)
        if existing_lifecycle in {"finished", "archived", "superseded"}:
            result = _canonical_result(
                ok=False,
                returncode=1,
                stderr=f"task_terminal:{existing_lifecycle}:{task_id}",
                command=["add-card", task_id],
            )
            result.update({
                "task_id": task_id,
                "created": False,
                "reconciled": False,
                "receipt_state": "existing_terminal",
                "terminal_state": existing_lifecycle,
            })
            return result
        existing_context = existing_card.get("project_context")
        if not isinstance(existing_context, dict):
            existing_context = {}
        existing_payload = {
            "title": str(existing_card.get("title") or ""),
            "runner": str(existing_card.get("runner") or ""),
            "topic": str(existing_card.get("topic") or ""),
            "objective": str(existing_card.get("objective") or ""),
            "acceptance": existing_card.get("acceptance") or [],
            "read_only": existing_card.get("read_only") is True,
            "allowed_writes": existing_card.get("allowed_writes") or [],
            "forbidden": existing_card.get("forbidden") or [],
            "required_outputs": existing_card.get("required_outputs") or [],
            "allow_empty_required_outputs": (
                existing_card.get("allow_empty_required_outputs") or []
            ),
            "allow_unchanged_required_outputs": (
                existing_card.get("allow_unchanged_required_outputs") or []
            ),
            "validation": existing_card.get("validation") or [],
            "validation_roles": existing_card.get("validation_roles") or [
                quality_evidence.VALIDATION_ROLE_GENERIC
                for _ in (existing_card.get("validation") or [])
            ],
            "work_kind": str(
                existing_card.get("work_kind")
                or quality_evidence.WORK_KIND_GENERIC
            ),
            "risk_tier": (
                str(existing_card.get("risk_tier") or "").strip().lower()
                or None
            ),
            "priority": str(existing_card.get("priority") or "normal"),
            "task_type": str(existing_context.get("task_type") or "code"),
            "depends_on": existing_card.get("depends_on") or [],
            "read_first": existing_card.get("read_first") or [],
            "immutable_inputs": existing_card.get("immutable_inputs") or [],
            "max_live_tokens": (
                (existing_card.get("token_budget") or {}).get("cap_tokens")
                if isinstance(existing_card.get("token_budget"), dict)
                else None
            ),
        }
        if existing_payload == requested_payload:
            result = _canonical_result(
                ok=True,
                stdout=json.dumps(existing_card, ensure_ascii=False),
                command=["add-card", task_id],
            )
            result.update({
                "task_id": task_id,
                "created": False,
                "reconciled": True,
                "receipt_state": "existing_identical",
            })
            return result
        differing_fields = sorted(
            key for key, value in requested_payload.items()
            if existing_payload.get(key) != value
        )
        result = _canonical_result(
            ok=False,
            returncode=1,
            stderr=f"task_already_exists:{task_id}",
            command=["add-card", task_id],
        )
        result.update({
            "task_id": task_id,
            "created": False,
            "reconciled": False,
            "conflict_fields": differing_fields,
        })
        return result

    command = ["add-card", task_id]
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        callback_store.init_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        existing_row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if existing_row is not None:
            conn.rollback()
            return reconcile_existing(existing_row["card_json"])
        if coordinator_worker_runner:
            conn.rollback()
            result = _lifecycle_error(
                "worker_runner_required:coordinator_codex_forbidden", 2
            )
            result.update({
                "received_runner": runner,
                "contract_hint": (
                    "Use the selected workforce decision's launch_contract.runner for both "
                    "task_create and agent_launch_task. The exact runner 'codex' is the "
                    "manager identity and cannot own or claim worker cards."
                ),
            })
            return result
        if depends_on2:
            existing_cards: dict[str, dict[str, Any]] = {}
            for row in conn.execute(
                "SELECT task_id, card_json, archived_at FROM tasks"
            ):
                try:
                    existing_card = json.loads(row["card_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    existing_card = {}
                if not isinstance(existing_card, dict):
                    existing_card = {}
                if str(row["archived_at"] or "").strip():
                    existing_card["archived_at"] = str(row["archived_at"])
                    existing_card["status"] = "archived"
                existing_cards[row["task_id"]] = existing_card
            existing_edges, invalid_ids = task_plan.existing_edges_from_cards(existing_cards)
            try:
                task_plan.validate_new_dependency_edge(
                    task_id, depends_on2, existing_edges, invalid_ids=invalid_ids
                )
            except task_plan.TaskPlanError as exc:
                conn.rollback()
                return _canonical_result(ok=False, returncode=2, stderr=str(exc), command=command)
        conn.execute(
            "INSERT INTO tasks(task_id,runner,topic,mode,status,worker_status,priority,objective,card_json,created_at,updated_at,origin_thread_id,archived_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?, '')",
            (
                task_id, runner, topic, "", "pending", "unclaimed", priority,
                objective, json.dumps(card, ensure_ascii=False), now, now,
                origin_thread_id,
            ),
        )
        conn.execute(
            "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) VALUES(?,?,?,?,?)",
            (
                task_id, "created", CODEX_RUNNER,
                json.dumps({"provider": provider, "topic": topic}, ensure_ascii=False), now,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        existing_row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if existing_row is not None:
            return reconcile_existing(existing_row["card_json"])
        return _canonical_result(
            ok=False, returncode=1, stderr="task_create_integrity_error", command=command
        )
    finally:
        conn.close()
    result = _canonical_result(
        ok=True,
        stdout=json.dumps(card, ensure_ascii=False),
        command=command,
    )
    result.update({
        "task_id": task_id,
        "created": True,
        "reconciled": False,
        "receipt_state": "created",
    })
    return result


def mark_review(task_id: str, runner: str | None = None, topic: str | None = None) -> dict[str, Any]:
    """Request review for the exact task owner recorded on the live card.

    Older wiring omitted ``--runner`` and taskctl therefore substituted its
    legacy ``deepseek_flash`` default. Resolve the live identity when needed,
    require any caller-supplied identity to match it, then pass the exact
    runner/topic through to taskctl. The live card establishes the requested
    identity, which must then pass the MCP runner/topic policy allowlist.
    """
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_runner = card.get("claimed_by") or card.get("runner")
    live_topic = card.get("topic")
    if runner is not None and runner != live_runner:
        return _lifecycle_error(f"runner mismatch expected={live_runner} got={runner}")
    if topic is not None and topic != live_topic:
        return _lifecycle_error(f"topic mismatch expected={live_topic} got={topic}")
    if not live_runner or not live_topic:
        return _lifecycle_error("task has no exact runner/topic identity")
    command = ["review", task_id, "--runner", str(live_runner), "--topic", str(live_topic)]
    blocked = _canonical_write_gate(
        "review", runner=str(live_runner), topic=str(live_topic), task_id=task_id
    )
    if blocked is not None:
        return blocked
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        callback_store.init_db(conn)
        cur = conn.execute(
            "UPDATE tasks SET worker_status='review', status='review', updated_at=? "
            "WHERE task_id=? AND worker_status IN ('claimed','in_progress')",
            (now, task_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return _canonical_result(
                ok=False, returncode=1, stderr=f"review_not_startable:task_id={task_id}", command=command
            )
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id,
                "review",
                str(live_runner),
                json.dumps({"runner": live_runner, "topic": live_topic}, ensure_ascii=False),
                now,
            ),
        )
        # The task's persisted origin_thread_id is immutable: it identifies
        # the chat that authored/owns the task, never the chat that happens
        # to be reviewing it. Overwriting it with the reviewing manager's own
        # session let a second window/manager silently steal callback
        # ownership of a task it did not create.
        origin_thread_id = callback_store.read_origin_thread(conn, task_id)
        if not origin_thread_id:
            origin_thread_id = str(card.get("origin_thread_id") or "").strip()
        callback_provider = _current_chat_provider(card)
        callback_enqueued = callback_store.enqueue_callback(
            conn,
            task_id,
            origin_thread_id or "",
            "review_ready",
            provider=callback_provider,
        )
        conn.commit()
    finally:
        conn.close()
    card2 = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
    result = _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
    result["callback_enqueued"] = callback_enqueued
    return result


def mark_done(task_id: str, runner: str | None = None, topic: str | None = None) -> dict[str, Any]:
    """Finalize one exact reviewed card with server-held coordinator authority."""
    if runner not in (None, CODEX_RUNNER):
        return _lifecycle_error(
            f"coordinator runner mismatch expected={CODEX_RUNNER} got={runner}"
        )
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_topic = card.get("topic")
    if not live_topic:
        return _lifecycle_error("task has no exact topic identity")
    if topic is not None and topic != live_topic:
        return _lifecycle_error(f"topic mismatch expected={live_topic} got={topic}")
    if _lifecycle_state(card) == "finished":
        # ``agent_accept_review`` is the authoritative promotion + finish
        # operation for isolated candidates.  Clients commonly reconcile a
        # lost/late acknowledgement by issuing the generic done operation
        # afterwards.  Finished is an idempotent success, not another attempt
        # to bypass promotion, and must be recognized before the retained
        # terminal-review receipt triggers the candidate gate below.
        command = ["done", task_id, "--runner", CODEX_RUNNER, "--topic", str(live_topic)]
        result = _canonical_result(
            ok=True,
            returncode=0,
            stdout=json.dumps(card, ensure_ascii=False, default=str),
            command=command,
        )
        result["already_done"] = True
        result["reconciled"] = True
        return result
    terminal_review = card.get("terminal_review")
    if isinstance(terminal_review, dict) and terminal_review:
        terminal_substatus = str(terminal_review.get("substatus") or "")
        if terminal_substatus != "review_ready":
            return _lifecycle_error(
                "done_terminal_review_not_acceptable:"
                + (terminal_substatus or "missing_substatus")
            )
        evidence = terminal_review.get("evidence")
        request_identity = (
            evidence.get("request_identity")
            if isinstance(evidence, dict)
            else None
        )
        if isinstance(request_identity, dict) and str(
            request_identity.get("request_id") or ""
        ).strip():
            # A review-first isolated candidate is not in the canonical tree
            # yet. Only ProcessManager.accept_review may revalidate hashes,
            # promote it, and atomically finish the exact request. The generic
            # mark-done surface must never bypass that phase.
            return _lifecycle_error("agent_accept_review_required")
        verification = terminal_review.get("deterministic_verification")
        if not isinstance(verification, dict):
            verification = card.get("deterministic_verification")
        if isinstance(verification, dict) and verification.get("applicable"):
            if not verification.get("pass"):
                return _lifecycle_error("done_deterministic_verification_failed")
    command = ["done", task_id, "--runner", CODEX_RUNNER, "--topic", str(live_topic)]
    blocked = _canonical_write_gate(
        "done", runner=CODEX_RUNNER, topic=str(live_topic), coordinator_capability=True
    )
    if blocked is not None:
        return blocked
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        cur = conn.execute(
            "UPDATE tasks SET worker_status='done', status='finished', completed_at=?, updated_at=? "
            "WHERE task_id=? AND worker_status='review'",
            (now, now, task_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return _canonical_result(
                ok=False, returncode=1, stderr=f"done_not_reviewable:task_id={task_id}", command=command
            )
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (task_id, "done", CODEX_RUNNER, json.dumps({"topic": live_topic}, ensure_ascii=False), now),
        )
        conn.commit()
    finally:
        conn.close()
    card2 = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
    result = _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
    # Issue 5 (stale Source Graph false blocker): this task's outputs were
    # promoted into the canonical working tree (uncommitted) before this
    # finalize. Refresh the Source Graph index BEFORE launching any depends_on
    # dependents, so a child does not query a stale index and falsely report
    # this task's just-produced artifact missing (the measured B954->B955 false
    # blocker). Best-effort and non-overlapping: an index refresh must never
    # fail the finalization itself. Only refresh an ALREADY-running daemon (the
    # production case -- Init Repo starts one per repo); never START a daemon or
    # run a first-ever full build as a side effect of mark_done, which would
    # otherwise spawn an indexing thread on every finalize (e.g. across a whole
    # test suite, where no daemon is running).
    try:
        sg = _source_graph_daemon_module()
        if sg.get_daemon(repo_root()) is not None:
            result["source_graph_refresh"] = source_graph_refresh_now()
        else:
            result["source_graph_refresh"] = {"ok": True, "triggered": False, "reason": "no_running_daemon"}
    except Exception as exc:  # noqa: BLE001 -- refresh must never fail mark_done
        result["source_graph_refresh"] = {"ok": False, "error": f"{type(exc).__name__}:{exc}"[:200]}
    result["dependency_autolaunch"] = dependency_autolaunch.reconcile_after_accept(
        repo_root(), task_id, claim_start_exact
    )
    _reconcile_retained_workspaces(result)
    return result


def _reconcile_retained_workspaces(result: dict[str, Any]) -> dict[str, Any]:
    """Queue best-effort GC outside the lifecycle transition critical path.

    The periodic process reconciler remains the durable safety net, but a
    successful done/reject/archive/supersede should not leave a large isolated
    checkout on disk until an extension refresh happens.  Import lazily to
    avoid the module-level ``core <-> process_launcher`` cycle.  Cleanup is
    fail-closed and can never turn a successful task transition into failure.
    """
    if not result.get("ok"):
        return result
    root = repo_root().resolve()
    key = str(root)
    with _WORKSPACE_GC_JOBS_LOCK:
        if key in _WORKSPACE_GC_JOBS:
            result["workspace_retention"] = {
                "ok": True,
                "queued": True,
                "coalesced": True,
                "mode": "async_periodic_sweep",
            }
            return result
        _WORKSPACE_GC_JOBS.add(key)

    def run_gc() -> None:
        try:
            from . import process_launcher  # local import: cycle-safe

            process_launcher.ProcessManager(repo=root)._gc_finalized_workspaces()
        except Exception:
            # The durable periodic reconciler retries. A post-commit cleanup
            # failure must never rewrite the already-returned transition.
            pass
        finally:
            with _WORKSPACE_GC_JOBS_LOCK:
                _WORKSPACE_GC_JOBS.discard(key)

    threading.Thread(
        target=run_gc,
        name=f"aiworkhub-workspace-gc-{abs(hash(key)) & 0xffff:x}",
        daemon=True,
    ).start()
    result["workspace_retention"] = {
        "ok": True,
        "queued": True,
        "coalesced": False,
        "mode": "async_periodic_sweep",
    }
    return result


def reject_review(
    task_id: str,
    reason: str,
    topic: str | None = None,
    to: str = "pending",
    residual_identities: list[dict[str, str]] | None = None,
    predecessor_request_id: str | None = None,
) -> dict[str, Any]:
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_topic = card.get("topic")
    if not live_topic:
        return _lifecycle_error("task has no exact topic identity")
    if topic is not None and topic != live_topic:
        return _lifecycle_error(f"topic mismatch expected={live_topic} got={topic}")
    # Issue 4: reject-review supports an explicit target disposition. Default
    # "pending" (rework, unchanged). "blocked" parks the task as a coordinator-
    # blocked outcome; "archived"/"superseded" retire it through the atomic
    # archive backend instead of silently requeuing a task whose real next step
    # is dependency-gated replacement.
    disposition = str(to or "pending").strip().lower()
    if disposition not in ("pending", "blocked", "archived", "superseded"):
        return _lifecycle_error(f"invalid reject-review disposition: {disposition}")
    normalized_residuals: list[dict[str, str]] = []

    def residual_error(code: str, *, index: int | None = None) -> dict[str, Any]:
        result = _lifecycle_error(code, 2)
        result["residual_identities_schema"] = {
            "type": "array",
            "minItems": 1,
            "maxItems": 256,
            "items": {
                "type": "object",
                "required": ["path", "pointer"],
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "description": "repo-relative file path"},
                    "pointer": {"type": "string", "description": "JSON pointer beginning with /"},
                },
            },
            "example": [{"path": "data/residual.json", "pointer": "/rows/7"}],
        }
        if index is not None:
            result["invalid_index"] = index
        return result

    if residual_identities is not None:
        if disposition != "pending" or not isinstance(residual_identities, list):
            return residual_error("residual_identities_require_pending_rework")
        if not residual_identities or len(residual_identities) > 256:
            return residual_error("invalid_residual_identities")
        seen_residuals: set[tuple[str, str]] = set()
        for index, row in enumerate(residual_identities):
            if not isinstance(row, dict):
                return residual_error("invalid_residual_identities", index=index)
            path = _task_contract_path(row.get("path"))
            pointer = str(row.get("pointer") or "").strip()
            if (
                not path
                or not pointer.startswith("/")
                or len(pointer) > 1000
                or "\x00" in pointer
            ):
                return residual_error("invalid_residual_identities", index=index)
            key = (path, pointer)
            if key in seen_residuals:
                continue
            seen_residuals.add(key)
            normalized_residuals.append({"path": path, "pointer": pointer})

    # V2 predecessor selection: resolve the explicit predecessor_request_id
    # or default to the current review request identity.  None (omitted)
    # defaults to the current terminal_review request.  "" (empty string)
    # fails closed -- an explicit no-predecessor intent must be unambiguous.
    # A non-empty value is validated against durable card evidence
    # (rework_predecessor or terminal_review.evidence) and must pass
    # same-repo, same-task workspace containment plus changed-path hash
    # verification before any card state change or GC scheduling.
    def _resolve_predecessor(
        explicit: str | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if explicit is None:
            return None, None  # default: current review
        stripped = explicit.strip()
        if not stripped:
            return None, "predecessor_request_id must be a non-empty request id or omitted"
        # Only card rework_predecessor (durably pinned from a prior cycle)
        # and terminal_review.evidence (current cycle) are authoritative.
        existing = card.get("rework_predecessor")
        terminal_review = card.get("terminal_review")
        evidence = (
            terminal_review.get("evidence")
            if isinstance(terminal_review, dict)
            else None
        )
        identity = evidence.get("request_identity") if isinstance(evidence, dict) else None
        t_workspace = evidence.get("workspace") if isinstance(evidence, dict) else None
        t_hashes = (
            evidence.get("changed_path_hashes") if isinstance(evidence, dict) else None
        )
        # Check existing rework_predecessor
        if isinstance(existing, dict):
            er = str(existing.get("request_id") or "").strip()
            ew = existing.get("workspace")
            eh = existing.get("changed_path_hashes")
            if (
                er == stripped
                and isinstance(ew, dict)
                and str(ew.get("request_id") or "").strip() == stripped
                and isinstance(eh, dict)
                and eh
            ):
                return {
                    "request_id": stripped,
                    "workspace": ew,
                    "changed_path_hashes": eh,
                }, None
        # Check current terminal_review evidence
        if (
            isinstance(identity, dict)
            and str(identity.get("request_id") or "").strip() == stripped
            and isinstance(t_workspace, dict)
            and str(t_workspace.get("request_id") or "").strip() == stripped
            and isinstance(t_hashes, dict)
            and t_hashes
        ):
            return {
                "request_id": stripped,
                "workspace": t_workspace,
                "changed_path_hashes": t_hashes,
            }, None
        return (
            None,
            f"predecessor_request_id {stripped} not found in retained review evidence",
        )

    resolved_predecessor, pred_error = _resolve_predecessor(predecessor_request_id)
    if pred_error:
        return _lifecycle_error(pred_error)

    raw_reason = str(reason or "")
    reason_bytes = raw_reason.encode("utf-8")
    bounded_reason, reason_truncated = _bounded_utf8_prefix(
        raw_reason.strip(), _MAX_REWORK_FEEDBACK_BYTES
    )
    reason_identity = {
        "bytes": len(reason_bytes),
        "sha256": hashlib.sha256(reason_bytes).hexdigest(),
        "truncated": reason_truncated,
    }
    command = [
        "reject-review", task_id, "--runner", CODEX_RUNNER, "--topic", str(live_topic),
        "--reason", bounded_reason, "--to", disposition,
    ]
    blocked = _canonical_write_gate(
        "reject-review", runner=CODEX_RUNNER, topic=str(live_topic), coordinator_capability=True
    )
    if blocked is not None:
        return blocked

    # archived / superseded retire the card atomically (archived_at + card_json
    # + task_events) via the shared archive backend. Only a card actually in
    # review may be rejected.
    if disposition in ("archived", "superseded"):
        if str(card.get("worker_status") or "").strip().lower() != "review":
            return _canonical_result(
                ok=False, returncode=1, stderr=f"reject_not_reviewable:task_id={task_id}", command=command
            )
        ok, state = task_store.archive_task(
            repo_root(), task_id,
            actor=CODEX_RUNNER, reason=f"reject_review:{reason}"[:200],
            allow_processing=(disposition == "superseded"),
            operation=disposition,
        )
        if not ok:
            return _canonical_result(
                ok=False, returncode=1, stderr=f"reject_{disposition}_failed:{state}", command=command
            )
        card2 = task_store.get_task(repo_root(), task_id)
        stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
        return _reconcile_retained_workspaces(
            _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
        )

    now = datetime.now(timezone.utc).isoformat()
    # A pending disposition means "rework this exact candidate", not "throw
    # the candidate away and start again from Git HEAD".  Preserve a bounded,
    # hash-authenticated pointer to the retained review workspace before
    # begin_claim_episode() clears terminal_review.  ProcessManager's GC keeps
    # this exact request pinned while the task is pending, and the next
    # isolated launch materializes only these reviewed changed paths as its
    # initial baseline.  Legacy/malformed review evidence is left unpinned so
    # old cards remain rejectable; a new launch then follows the historical
    # clean-HEAD behavior rather than trusting incomplete evidence.
    if disposition in ("pending", "blocked"):
        terminal_review = card.get("terminal_review")
        evidence = (
            terminal_review.get("evidence")
            if isinstance(terminal_review, dict)
            else None
        )
        identity = evidence.get("request_identity") if isinstance(evidence, dict) else None
        workspace = evidence.get("workspace") if isinstance(evidence, dict) else None
        changed_hashes = (
            evidence.get("changed_path_hashes") if isinstance(evidence, dict) else None
        )
        if resolved_predecessor is not None:
            pred_request_id: str = resolved_predecessor["request_id"]
            pred_workspace: dict[str, Any] = resolved_predecessor["workspace"]
            pred_changed_hashes: dict[str, str] = resolved_predecessor["changed_path_hashes"]
        else:
            pred_request_id = (
                str(identity.get("request_id") or "").strip()
                if isinstance(identity, dict)
                else ""
            )
            pred_workspace = workspace
            pred_changed_hashes = changed_hashes
        if (
            pred_request_id
            and isinstance(pred_workspace, dict)
            and str(pred_workspace.get("request_id") or "").strip() == pred_request_id
            and isinstance(pred_changed_hashes, dict)
            and pred_changed_hashes
        ):
            card["rework_predecessor"] = {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": pred_request_id,
                "workspace": pred_workspace,
                "changed_path_hashes": pred_changed_hashes,
                "residual_identities": (
                    normalized_residuals if disposition == "pending" else []
                ),
                "pinned_at": now,
            }
        elif disposition == "pending" and normalized_residuals:
            return residual_error("residual_contract_requires_review_predecessor")
    if disposition == "pending":
        # Rework prompts carry one compact delta, never the previous terminal
        # envelope or raw worker transcript.  The retained workspace is the
        # content authority; this object only tells the successor what failed
        # and which residual identities remain in scope.
        card["review_feedback"] = {
            "schema_id": "aiworkhub.rework_feedback_delta.v1",
            "instruction": bounded_reason,
            "reason_identity": reason_identity,
            "predecessor_request_id": pred_request_id,
            "predecessor_changed_paths": sorted(
                str(path) for path in (pred_changed_hashes or {})
            )[:256],
            "residual_identities": normalized_residuals,
        }
    prior_episode = task_store.begin_claim_episode(card)
    if disposition == "blocked":
        card.update(status="blocked", worker_status="blocked")
        set_clause = "worker_status='blocked', status='blocked', card_json=?, updated_at=?"
    else:  # "pending" -- rework, requeue for a fresh claim
        card.update(status="pending", worker_status="unclaimed", claimed_by=None)
        set_clause = (
            "worker_status='unclaimed', status='pending', claimed_by=NULL, "
            "card_json=?, updated_at=?"
        )
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        cur = conn.execute(
            f"UPDATE tasks SET {set_clause} WHERE task_id=? AND worker_status='review'",
            (
                json.dumps(
                    task_store.persistable_card_payload(card),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
                task_id,
            ),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return _canonical_result(
                ok=False, returncode=1, stderr=f"reject_not_reviewable:task_id={task_id}", command=command
            )
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id,
                "reject_review",
                CODEX_RUNNER,
                json.dumps(
                    {
                        "topic": live_topic,
                        "reason": bounded_reason,
                        "reason_identity": reason_identity,
                        "to": disposition,
                        "prior_episode": prior_episode,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    card2 = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
    return _reconcile_retained_workspaces(
        _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
    )


def recover_blocked_rework(
    task_id: str,
    *,
    feedback_reason: str = "",
    topic: str | None = None,
    validation_only_replay: bool = False,
    clean_root_if_predecessor_missing: bool = False,
) -> dict[str, Any]:
    """Recover one exact blocked task through the canonical task-store transaction."""
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_topic = card.get("topic")
    if not live_topic:
        return _lifecycle_error("task has no exact topic identity")
    if topic is not None and topic != live_topic:
        return _lifecycle_error(f"topic mismatch expected={live_topic} got={topic}")

    bounded_feedback, _truncated = _bounded_utf8_prefix(
        str(feedback_reason or "").strip(), _MAX_REWORK_FEEDBACK_BYTES
    )
    command = [
        "recover-blocked-rework",
        task_id,
        "--runner",
        CODEX_RUNNER,
        "--topic",
        str(live_topic),
    ]
    blocked = _canonical_write_gate(
        "recover-blocked-rework",
        runner=CODEX_RUNNER,
        topic=str(live_topic),
        coordinator_capability=True,
        task_id=task_id,
    )
    if blocked is not None:
        return blocked
    try:
        ok, state = task_store.recover_blocked_rework(
            repo_root(),
            task_id,
            actor=CODEX_RUNNER,
            feedback_reason=bounded_feedback,
            validation_only_replay=bool(validation_only_replay),
            clean_root_if_predecessor_missing=bool(
                clean_root_if_predecessor_missing
            ),
        )
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    if not ok:
        return _canonical_result(
            ok=False,
            returncode=1,
            stderr=f"recover_blocked_rework_failed:{state}",
            command=command,
        )
    card2 = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
    return _reconcile_retained_workspaces(
        _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
    )


_RETRYABLE_OPERATIONAL_TERMINAL_SUBSTATUSES: frozenset[str] = frozenset(
    {
        "cancelled",
        "timed_out",
        "output_budget_exceeded",
        "launch_failed",
        "worker_failed",
        "finalize_failed",
        "process_lost",
        "liveness_lost",
    }
)


def retry_terminal_task(
    task_id: str,
    request_id: str,
    terminal_substatus: str,
    reason: str = "",
    topic: str | None = None,
) -> dict[str, Any]:
    """Requeue one exact operational terminal episode under the same task ID.

    This is deliberately narrower than review rejection.  It accepts only a
    blocked operational failure, requires the caller to name the exact launch
    request and terminal substatus, preserves durable review/rework context,
    and clears only current-episode claim/terminal fields.  Semantic failures
    (validation/scope/review outcomes), finished tasks and archived tasks must
    follow their existing coordinator workflows instead.
    """

    request_id = str(request_id or "").strip()
    terminal_substatus = str(terminal_substatus or "").strip()
    bounded_reason = str(reason or "").strip()[:500]
    if not request_id or len(request_id) > 120:
        return _lifecycle_error("terminal_retry_request_id_invalid")
    if terminal_substatus not in _RETRYABLE_OPERATIONAL_TERMINAL_SUBSTATUSES:
        return _lifecycle_error(
            f"terminal_retry_substatus_not_operational:{terminal_substatus}"
        )
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_topic = str(card.get("topic") or "")
    if not live_topic:
        return _lifecycle_error("task has no exact topic identity")
    if topic is not None and topic != live_topic:
        return _lifecycle_error(f"topic mismatch expected={live_topic} got={topic}")

    prior_retry = card.get("terminal_retry")
    if (
        _lifecycle_state(card) == "pending"
        and str(card.get("worker_status") or "") == "unclaimed"
        and isinstance(prior_retry, dict)
        and str(prior_retry.get("request_id") or "") == request_id
        and str(prior_retry.get("terminal_substatus") or "") == terminal_substatus
    ):
        result = _canonical_result(
            ok=True,
            returncode=0,
            stdout=json.dumps(card, ensure_ascii=False, default=str),
            command=["retry-terminal", task_id],
        )
        result["idempotent"] = True
        result["retried_request_id"] = request_id
        result["terminal_substatus"] = terminal_substatus
        return result

    lifecycle = _lifecycle_state(card)
    worker_status = str(card.get("worker_status") or "")
    actual_substatus = str(card.get("terminal_substatus") or worker_status).strip()
    terminal_failure = card.get("terminal_failure")
    evidence = (
        terminal_failure.get("evidence")
        if isinstance(terminal_failure, dict)
        else None
    )
    actual_request_id = str(
        (evidence.get("request_id") if isinstance(evidence, dict) else "")
        or card.get("launch_request_id")
        or ""
    ).strip()
    if lifecycle != "blocked":
        return _lifecycle_error(f"terminal_retry_not_blocked:current={lifecycle}")
    if worker_status != terminal_substatus or actual_substatus != terminal_substatus:
        return _lifecycle_error(
            "terminal_retry_substatus_mismatch:"
            f"expected={actual_substatus or worker_status}:got={terminal_substatus}"
        )
    if actual_request_id != request_id:
        return _lifecycle_error(
            f"terminal_retry_request_mismatch:expected={actual_request_id}:got={request_id}"
        )

    command = [
        "retry-terminal",
        task_id,
        "--request-id",
        request_id,
        "--terminal-substatus",
        terminal_substatus,
    ]
    gate = _canonical_write_gate(
        "retry-terminal",
        runner=CODEX_RUNNER,
        topic=live_topic,
        coordinator_capability=True,
        task_id=task_id,
    )
    if gate is not None:
        return gate

    now = datetime.now(timezone.utc).isoformat()
    semantic_card = task_store.persistable_card_payload(card)
    prior_episode = task_store.begin_claim_episode(semantic_card)
    for key in (
        "launch_request_id",
        "terminal_failure",
        "blocked_at",
        "blocked_by",
        "completed_at",
        "claimed_at",
        "started_at",
    ):
        semantic_card.pop(key, None)
    semantic_card.update(
        status="pending",
        worker_status="unclaimed",
        claimed_by=None,
        terminal_retry={
            "schema_id": "aiworkhub.terminal_retry.v1",
            "request_id": request_id,
            "terminal_substatus": terminal_substatus,
            "reason": bounded_reason,
            "retried_at": now,
        },
    )
    encoded_card = json.dumps(semantic_card, ensure_ascii=False, sort_keys=True)
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        row = conn.execute(
            "SELECT status, worker_status, card_json FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if (
            row is None
            or str(row["status"] or "") != "blocked"
            or str(row["worker_status"] or "") != terminal_substatus
        ):
            conn.rollback()
            return _canonical_result(
                ok=False,
                returncode=1,
                stderr=f"terminal_retry_transition_conflict:task_id={task_id}",
                command=command,
            )
        raw_card_json = str(row["card_json"] or "{}")
        cur = conn.execute(
            "UPDATE tasks SET status='pending', worker_status='unclaimed', "
            "claimed_by=NULL, claimed_at=NULL, started_at=NULL, completed_at=NULL, "
            "card_json=?, updated_at=? "
            "WHERE task_id=? AND status='blocked' AND worker_status=? AND card_json=?",
            (
                encoded_card,
                now,
                task_id,
                terminal_substatus,
                raw_card_json,
            ),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return _canonical_result(
                ok=False,
                returncode=1,
                stderr=f"terminal_retry_transition_conflict:task_id={task_id}",
                command=command,
            )
        conn.execute(
            "INSERT INTO task_events "
            "(task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id,
                "retry_terminal",
                CODEX_RUNNER,
                json.dumps(
                    {
                        "topic": live_topic,
                        "request_id": request_id,
                        "terminal_substatus": terminal_substatus,
                        "reason": bounded_reason,
                        "transition": "blocked->pending",
                        "prior_episode": prior_episode,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    card2 = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
    result = _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
    result["retried_request_id"] = request_id
    result["terminal_substatus"] = terminal_substatus
    return _reconcile_retained_workspaces(result)


def archive_task(task_id: str, reason: str = "", topic: str | None = None) -> dict[str, Any]:
    """Coordinator-only: archive a stale task (Issue 3).

    Atomically sets ``archived_at`` + mirrors it into ``card_json`` and writes
    an ``archived`` ``task_events`` row through ``task_store.archive_task`` --
    no direct SQLite patching. Runs the FULL coordinator-capability write gate
    (unlike the bare ``writes_allowed()`` manager tool). A ``processing`` card
    is refused here (use ``supersede_task`` for an active/orphaned card)."""
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_topic = str(card.get("topic") or "")
    if topic is not None and topic != live_topic:
        return _lifecycle_error(f"topic mismatch expected={live_topic} got={topic}")
    command = ["archive", task_id, "--runner", CODEX_RUNNER, "--reason", reason]
    gate = _canonical_write_gate(
        "archive", runner=CODEX_RUNNER, topic=live_topic, coordinator_capability=True
    )
    if gate is not None:
        return gate
    ok, state = task_store.archive_task(
        repo_root(), task_id, actor=CODEX_RUNNER, reason=str(reason)[:200], operation="archived"
    )
    if not ok:
        return _canonical_result(ok=False, returncode=1, stderr=f"archive_failed:{state}", command=command)
    card2 = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
    return _reconcile_retained_workspaces(
        _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
    )


def supersede_task(
    task_id: str, reason: str = "", by: str = "", topic: str | None = None
) -> dict[str, Any]:
    """Coordinator-only: supersede a stale/active task with a replacement (Issue 3).

    Archives the card as ``superseded`` (allowed even from ``processing``, for a
    stale/orphaned card) and records the optional replacement ``--by`` task id
    in the audit reason. Same atomic archive backend + coordinator-capability
    gate as ``archive_task``; no direct SQLite patching."""
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_topic = str(card.get("topic") or "")
    if topic is not None and topic != live_topic:
        return _lifecycle_error(f"topic mismatch expected={live_topic} got={topic}")
    by = str(by or "").strip()
    command = ["supersede", task_id, "--runner", CODEX_RUNNER, "--reason", reason]
    if by:
        command += ["--by", by]
        try:
            task_plan.normalize_depends_on([by])
        except task_plan.TaskPlanError as exc:
            return _lifecycle_error(str(exc), 2)
        if by == task_id:
            return _lifecycle_error("superseded_replacement_self_reference", 2)
        try:
            cards = {str(item["task_id"]): item for item in _full_cards_for_plan()}
        except task_store.TaskStoreError as exc:
            return _lifecycle_error(str(exc), 1)
        if by not in cards:
            return _lifecycle_error(
                f"superseded_replacement_task_not_found:{by}",
                2,
            )
        probe_cards = dict(cards)
        probe_cards[task_id] = {
            **card,
            "status": "archived",
            "archived_at": "pending-supersede",
            "archive_operation": "superseded",
            "superseded_by": by,
        }
        before_snapshot = task_plan.build_snapshot(list(cards.values()))
        after_snapshot = task_plan.build_snapshot(list(probe_cards.values()))
        new_cycle_nodes = sorted(
            set(after_snapshot["cycle_nodes"]) - set(before_snapshot["cycle_nodes"])
        )
        if new_cycle_nodes:
            return _lifecycle_error(
                "superseded_replacement_cycle_detected:"
                + ",".join(new_cycle_nodes),
                2,
            )
    gate = _canonical_write_gate(
        "archive", runner=CODEX_RUNNER, topic=live_topic, coordinator_capability=True
    )
    if gate is not None:
        return gate
    full_reason = (f"superseded_by:{by}; {reason}" if by else str(reason))[:200]
    ok, state = task_store.archive_task(
        repo_root(), task_id, actor=CODEX_RUNNER, reason=full_reason,
        allow_processing=True, operation="superseded", superseded_by=by,
    )
    if not ok:
        return _canonical_result(ok=False, returncode=1, stderr=f"supersede_failed:{state}", command=command)
    card2 = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
    return _reconcile_retained_workspaces(
        _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
    )


def release_launch(
    task_id: str,
    claimed_by: str,
    reason: str,
    topic: str | None = None,
) -> dict[str, Any]:
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_topic = card.get("topic")
    if not live_topic:
        return _lifecycle_error("task has no exact topic identity")
    if topic is not None and topic != live_topic:
        return _lifecycle_error(f"topic mismatch expected={live_topic} got={topic}")
    command = [
        "release-launch", task_id, "--runner", CODEX_RUNNER, "--topic", str(live_topic),
        "--claimed-by", claimed_by, "--reason", reason,
    ]
    blocked = _canonical_write_gate(
        "release-launch", runner=CODEX_RUNNER, topic=str(live_topic), coordinator_capability=True
    )
    if blocked is not None:
        return blocked
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        callback_store.init_db(conn)
        persisted_card = task_store.persistable_card_payload(card)
        persisted_card.update(
            {
                "terminal_outcome": reason,
                "terminal_review_reason": reason,
                "terminal_worker": claimed_by,
            }
        )
        cur = conn.execute(
            "UPDATE tasks SET worker_status='review', status='review', card_json=?, updated_at=? "
            "WHERE task_id=? AND claimed_by=? AND worker_status IN ('claimed','in_progress')",
            (json.dumps(persisted_card, ensure_ascii=False, sort_keys=True), now, task_id, claimed_by),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return _canonical_result(
                ok=False,
                returncode=1,
                stderr=f"release_claimed_by_mismatch:task_id={task_id}",
                command=command,
            )
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id,
                "terminal_review",
                CODEX_RUNNER,
                json.dumps(
                    {
                        "topic": live_topic,
                        "claimed_by": claimed_by,
                        "terminal_outcome": reason,
                    },
                    ensure_ascii=False,
                ),
                now,
            ),
        )
        # Immutable task origin -- see the matching comment in the review
        # transition above. The releasing manager's own session must never
        # overwrite the task's persisted callback owner.
        origin_thread_id = callback_store.read_origin_thread(conn, task_id)
        if not origin_thread_id:
            origin_thread_id = str(card.get("origin_thread_id") or "").strip()
        callback_provider = _current_chat_provider(card)
        callback_enqueued = callback_store.enqueue_callback(
            conn,
            task_id,
            origin_thread_id or "",
            "review_ready",
            provider=callback_provider,
        )
        conn.commit()
    finally:
        conn.close()
    card2 = task_store.get_task(repo_root(), task_id)
    stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
    result = _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
    result["callback_enqueued"] = callback_enqueued
    result["terminal_outcome"] = reason
    return result


def _active_cards_for_collision_guard() -> list[dict[str, Any]]:
    """Non-terminal cards that can still own or acquire write authority.

    Blocked cards have no live worker and no promotable candidate workspace,
    so retaining their historical ``allowed_writes`` as an active lock makes a
    dependency repair impossible.  Pending, processing, and review cards stay
    conservative here; only an explicit supersede/requeue/review transition
    releases those scopes.

    Cards are returned with ``card_json``
    merged in, sourced purely from a SQL scan of the canonical ``tasks``
    table -- no taskctl/taskdb subprocess."""
    conn = _canonical_connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT task_id, runner, topic, status, worker_status, card_json, archived_at FROM tasks"
        ).fetchall()
    finally:
        conn.close()
    active: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        bucket = task_store.canonical_status(item)
        if bucket not in {"pending", "processing", "review"}:
            continue
        try:
            card_json = json.loads(item.get("card_json") or "{}")
        except json.JSONDecodeError:
            card_json = {}
        card = {**card_json, **{k: v for k, v in item.items() if k != "card_json"}}
        card["status"] = bucket
        active.append(card)
    return active


def _normalize_allowed_write_path(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("./")


def _allowed_write_paths_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    if any(ch in left + right for ch in "*?[]"):
        # Glob semantics are intentionally fail-safe but non-expansive here:
        # exact glob-vs-glob overlap is undecidable without a filesystem walk,
        # and collision guard must remain repo-local, cheap, and deterministic.
        return False
    left_dir = left.endswith("/")
    right_dir = right.endswith("/")
    if left_dir and right.startswith(left):
        return True
    if right_dir and left.startswith(right):
        return True
    return False


def _scan_aiworkhub_collisions(cards: list[dict[str, Any]]) -> dict[str, Any]:
    entries: list[tuple[str, str]] = []
    for card in cards:
        task_id = str(card.get("task_id") or "?")
        for raw_path in card.get("allowed_writes") or []:
            normalized = _normalize_allowed_write_path(str(raw_path))
            if normalized:
                entries.append((normalized, task_id))

    collisions: dict[str, set[str]] = defaultdict(set)
    for idx, (path_a, task_a) in enumerate(entries):
        for path_b, task_b in entries[idx + 1 :]:
            if task_a == task_b:
                continue
            if _allowed_write_paths_overlap(path_a, path_b):
                key = path_a if path_a == path_b else f"{path_a} <-> {path_b}"
                collisions[key].update((task_a, task_b))

    file_collisions = [
        {"file": path, "conflicting_tasks": sorted(tasks)}
        for path, tasks in sorted(collisions.items())
    ]
    root = repo_root()
    storage = task_store.storage_readiness(root)
    return {
        "schema_id": "aiworkhub.task_collision_report.v1",
        "source": "canonical_task_store",
        "repo": str(root),
        "cards_source": storage.canonical_db,
        "collision_free": not file_collisions,
        "active_cards": len(cards),
        "collision_count": len(file_collisions),
        "file_collisions": file_collisions,
        "coordination_commands": [],
    }


def collision_guard(print_json: bool = True) -> dict[str, Any]:
    command = ["collision-guard"]
    if print_json:
        command.append("--print")
    try:
        cards = _active_cards_for_collision_guard()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    if not cards:
        return _canonical_result(ok=True, returncode=0, stdout="No cards to scan.", command=command)

    result = _scan_aiworkhub_collisions(cards)
    lines: list[str] = []
    if result["collision_free"]:
        lines.append(
            f"collision_free=true — no overlapping allowed_writes ({result['active_cards']} active cards)"
        )
    else:
        lines.append(
            f"COLLISION: {result['collision_count']} file collision(s) among {result['active_cards']} active cards"
        )
        for fc in result.get("file_collisions", []) or []:
            lines.append(f"   {fc['file']}: {', '.join(fc['conflicting_tasks'])}")
        for cmd_line in result.get("coordination_commands", []) or []:
            lines.append(f"   {cmd_line}")
    if print_json:
        lines.append("")
        lines.append(json.dumps(result, indent=2, ensure_ascii=False))
    ok = bool(result["collision_free"])
    return _canonical_result(ok=ok, returncode=0 if ok else 1, stdout="\n".join(lines), command=command)


def launch_collision_guard(
    *, task_id: str, print_json: bool = True
) -> dict[str, Any]:
    """Check write-scope collisions that can block one exact launch.

    The dashboard collision report intentionally includes every pending card
    so managers can see future coordination needs.  A launcher must be more
    precise: an unrelated planned collision cannot freeze the entire queue,
    and a dependency-blocked pending card does not yet own write authority.
    Processing/review cards always retain their scopes.  Among dependency-
    ready pending contenders, a deterministic priority/task-id order admits
    one winner so concurrent launch attempts cannot both pass preflight.
    """
    command = ["launch-collision-guard", task_id]
    if print_json:
        command.append("--print")
    try:
        cards = _active_cards_for_collision_guard()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)

    by_id = {str(card.get("task_id") or ""): card for card in cards}
    candidate = by_id.get(task_id)
    if candidate is None:
        return _canonical_result(
            ok=False,
            returncode=1,
            stderr=f"collision_candidate_not_found:{task_id}",
            command=command,
        )

    def dependencies_ready(card: dict[str, Any]) -> bool:
        dependencies = card.get("depends_on") or []
        if not isinstance(dependencies, list):
            return False
        for raw_dependency in dependencies:
            dependency = by_id.get(str(raw_dependency or ""))
            if dependency is None:
                # Finished dependencies are absent from the active-card map;
                # confirm them from canonical storage before declaring ready.
                dependency = task_store.get_task(repo_root(), str(raw_dependency or ""))
            if dependency is None or task_store.canonical_status(dependency) != "finished":
                return False
        return True

    priority_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "": 4}

    def order_key(card: dict[str, Any]) -> tuple[int, str]:
        priority = str(card.get("priority") or "").strip().lower()
        return priority_rank.get(priority, 4), str(card.get("task_id") or "")

    candidate_paths = [
        normalized
        for raw_path in candidate.get("allowed_writes") or []
        if (normalized := _normalize_allowed_write_path(str(raw_path)))
    ]
    blockers: list[dict[str, Any]] = []
    for other_id, other in by_id.items():
        if other_id == task_id:
            continue
        other_paths = [
            normalized
            for raw_path in other.get("allowed_writes") or []
            if (normalized := _normalize_allowed_write_path(str(raw_path)))
        ]
        overlaps = sorted(
            {
                left if left == right else f"{left} <-> {right}"
                for left in candidate_paths
                for right in other_paths
                if _allowed_write_paths_overlap(left, right)
            }
        )
        if not overlaps:
            continue
        lifecycle = task_store.canonical_status(other)
        owns_scope = lifecycle in {"processing", "review"}
        if lifecycle == "pending" and dependencies_ready(other):
            owns_scope = order_key(other) < order_key(candidate)
        if owns_scope:
            blockers.append(
                {"task_id": other_id, "lifecycle": lifecycle, "paths": overlaps}
            )

    result = {
        "schema_id": "aiworkhub.task_launch_collision_report.v1",
        "repo": str(repo_root()),
        "task_id": task_id,
        "collision_free": not blockers,
        "blockers": blockers,
    }
    stdout = json.dumps(result, ensure_ascii=False, sort_keys=True) if print_json else ""
    return _canonical_result(
        ok=not blockers,
        returncode=0 if not blockers else 1,
        stdout=stdout,
        command=command,
    )


def _reconcile_task_is_live(root: Path, task_id: str) -> bool:
    """Verified-liveness probe for archive reconciliation.

    A half-archived row is treated as live only when the repo's one live mux is
    still driving this task's origin thread -- the same PID-reuse-safe evidence
    the launch path trusts.  Absent that proof it returns ``False`` (the row is
    a candidate for repair), never a bare status guess.
    """
    try:
        card = task_store.get_task(root, task_id)
    except task_store.TaskStoreError:
        return False
    if not card:
        return False
    origin = str(card.get("origin_thread_id") or "").strip()
    if not origin:
        return False
    try:
        return _live_mux_active_thread(root, card) == origin
    except (OSError, RuntimeError, TypeError, ValueError, task_store.TaskStoreError):
        return False


def archive_reconciliation_report(print_json: bool = True) -> dict[str, Any]:
    """Read-only operator report of rows whose ``archived_at`` and ``status``
    disagree, with a count and the three reconciliation numbers.

    This is the operator-reachable surface that keeps the half-archived class of
    drift from ever again being invisible until someone queries SQLite by hand.
    Pure read; never mutates.
    """
    command = ["archive-reconciliation-report"]
    root = repo_root()
    try:
        report = task_store.archive_inconsistency_report(root)
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    stdout = json.dumps(report, ensure_ascii=False, sort_keys=True) if print_json else ""
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def repair_archive_inconsistencies(
    *, actor: str = "operator", print_json: bool = True
) -> dict[str, Any]:
    """Operator-reachable, transactional repair of half-archived rows.

    Wraps :func:`task_store.repair_archive_inconsistencies` with the verified
    process-liveness probe, and reports the non-terminal count, collision-guard
    holder counts and pinned-worktree bytes before and after so the effect is
    auditable.  Rows excluded for liveness, rework-predecessor pins or a missing
    archive event are named, never touched.
    """
    command = ["repair-archive-inconsistencies", "--actor", actor]
    root = repo_root()
    try:
        result = task_store.repair_archive_inconsistencies(
            root,
            actor=actor,
            is_task_live=lambda task_id: _reconcile_task_is_live(root, task_id),
        )
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    stdout = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str) if print_json else ""
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)

def callback_outbox_status() -> dict[str, Any]:
    """Read-only, redacted-safe callback outbox health (bound/unbound task
    counts, pending/inflight/delivered/dead-letter counts). Sourced from
    ``task_store.callback_bridge_health`` -- never a full origin_thread_id,
    never taskctl/taskdb."""
    command = ["callback-outbox-status"]
    try:
        stats = task_store.callback_bridge_health(repo_root())
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    stdout = json.dumps(stats, ensure_ascii=False, sort_keys=True)
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def record_usage(
    task_id: str,
    *,
    runner: str,
    topic: str | None = None,
    model: str | None = None,
    requested_model: str | None = None,
    observed_model: str | None = None,
    model_observed: bool = False,
    role: str = "worker",
    provider: str | None = None,
    source: str | None = None,
    note: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    visible_output_tokens: int = 0,
    reasoning_output_tokens: int = 0,
    total_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_write_input_tokens: int = 0,
    usage_observed: bool | None = None,
    telemetry_reason: str | None = None,
    cache_metrics_observed: bool = False,
    cost_usd: float = 0.0,
    cost_observed: bool = False,
) -> dict[str, Any]:
    """Append one native usage event to the canonical task store.

    This is the standalone replacement for the historical
    ``taskctl.py usage`` subprocess path used by the launcher. It preserves
    the same event shape consumed by ``usage_report`` while staying fully
    repo-local under ``.aiworkhub``.
    """
    command = ["usage", task_id]
    if runner:
        command.extend(["--runner", runner])
    if topic:
        command.extend(["--topic", topic])
    blocked = _canonical_write_gate("usage", runner=runner or None, topic=topic, task_id=task_id)
    if blocked is not None:
        return {**blocked, "command": command}
    card, error = _live_card(task_id)
    if error:
        return error
    assert card is not None
    live_topic = str(card.get("topic") or topic or "")
    measured_usage = (
        bool(usage_observed)
        if usage_observed is not None
        else bool(
            input_tokens
            or output_tokens
            or reasoning_output_tokens
            or total_tokens
            or cached_input_tokens
            or cache_creation_input_tokens
            or cache_write_input_tokens
            or cache_metrics_observed
            or cost_observed
        )
    )
    normalized_role = str(role or "worker").strip().lower()
    if normalized_role not in {"worker", "reviewer"}:
        return _canonical_result(
            ok=False,
            returncode=2,
            stderr="usage_role_invalid",
            command=command,
        )
    payload = {
        "runner": runner,
        "topic": live_topic,
        "status": card.get("status"),
        "worker_status": card.get("worker_status"),
        "model": model or "",
        "requested_model": requested_model or model or "",
        "observed_model": observed_model or "",
        "model_observed": bool(model_observed and observed_model),
        "role": normalized_role,
        "provider": provider or "",
        "source": source or "",
        "note": note or "",
        "records": 1,
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "visible_output_tokens": int(visible_output_tokens or 0),
        "reasoning_output_tokens": int(reasoning_output_tokens or 0),
        "total_tokens": int(total_tokens or (int(input_tokens or 0) + int(output_tokens or 0))),
        "cached_input_tokens": int(cached_input_tokens or 0),
        "cache_creation_input_tokens": int(cache_creation_input_tokens or 0),
        "cache_write_input_tokens": int(cache_write_input_tokens or 0),
        "usage_observed": measured_usage,
        "telemetry_reason": str(telemetry_reason or "")[:160],
        "cache_metrics_observed": bool(cache_metrics_observed),
        "cost_usd": float(cost_usd or 0.0),
        "cost_observed": bool(cost_observed),
    }
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _canonical_connect()
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (task_id, "usage_record", runner, json.dumps(payload, ensure_ascii=False), now),
        )
        conn.commit()
    finally:
        conn.close()
    return _canonical_result(ok=True, returncode=0, stdout=f"usage recorded: {task_id}", command=command)


def usage_report(runner: str | None = None, topic: str | None = None, status: str | None = None) -> dict[str, Any]:
    """Read-only aggregate over ``task_events`` rows with ``event='usage_record'``.

    Observed rows retain the historical pipe format. Missing provider usage or
    price telemetry is rendered as ``unknown`` rather than a fabricated zero;
    every launcher attempt therefore remains visible without pretending that
    VS Code LM exposed token or cost figures it did not report.
    """
    command = ["usage-report"]
    if runner:
        command.extend(["--runner", runner])
    if topic:
        command.extend(["--topic", topic])
    if status:
        command.extend(["--status", status])
    try:
        conn = _canonical_connect(readonly=True)
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    try:
        rows = conn.execute(
            "SELECT task_id, runner, payload_json, created_at FROM task_events WHERE event='usage_record'"
        ).fetchall()
    finally:
        conn.close()
    records: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        rec = {**payload, "task_id": row["task_id"], "runner": row["runner"], "created_at": row["created_at"]}
        if "usage_observed" not in rec:
            rec["usage_observed"] = bool(
                rec.get("input_tokens")
                or rec.get("output_tokens")
                or rec.get("reasoning_output_tokens")
                or rec.get("total_tokens")
                or rec.get("cached_input_tokens")
                or rec.get("cache_creation_input_tokens")
                or rec.get("cache_write_input_tokens")
                or rec.get("cache_metrics_observed")
                or rec.get("cost_observed")
            )
        if "cost_observed" not in rec:
            # Historical events predate the explicit observation bit. A
            # positive persisted amount is evidence that a provider reported
            # cost; a missing/zero amount remains unknown, never free.
            rec["cost_observed"] = float(rec.get("cost_usd") or 0.0) > 0.0
        if runner and rec.get("runner") != runner:
            continue
        if topic and rec.get("topic") != topic:
            continue
        if status and rec.get("status") != status:
            continue
        records.append(rec)

    if not records:
        return _canonical_result(ok=True, returncode=0, stdout="No usage records.", command=command)

    lines = ["=== Task Usage Report ==="]
    for rec in records:
        usage_is_observed = bool(rec.get("usage_observed"))
        cost_is_observed = bool(rec.get("cost_observed"))
        tokens = str(int(rec.get("total_tokens") or 0)) if usage_is_observed else "unknown"
        input_tokens = str(int(rec.get("input_tokens") or 0)) if usage_is_observed else "unknown"
        output_tokens = str(int(rec.get("output_tokens") or 0)) if usage_is_observed else "unknown"
        cost = f"${float(rec.get('cost_usd') or 0.0):.4f}" if cost_is_observed else "unknown"
        lines.append(
            f"{rec.get('task_id')} | runner={rec.get('runner', '?')} | topic={rec.get('topic', '?')} | "
            f"records={int(rec.get('records') or 1)} | tokens={tokens} | "
            f"in={input_tokens} out={output_tokens} | cost={cost} | "
            f"usage_observed={str(usage_is_observed).lower()} "
            f"cost_observed={str(cost_is_observed).lower()} "
            f"telemetry_reason={rec.get('telemetry_reason') or '-'}"
        )
    stdout = "\n".join(lines)
    result = _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)
    result.update({
        "schema_id": "aiworkhub.usage_report.v2",
        "record_count": len(records),
        "usage_observed_records": sum(bool(rec.get("usage_observed")) for rec in records),
        "usage_unknown_records": sum(not bool(rec.get("usage_observed")) for rec in records),
        "cost_observed_records": sum(bool(rec.get("cost_observed")) for rec in records),
        "cost_unknown_records": sum(not bool(rec.get("cost_observed")) for rec in records),
    })
    return result


def export_jsonl(runner: str | None = None, topic: str | None = None) -> dict[str, Any]:
    """Write-gated bounded export of the canonical queue to a JSONL file
    under this repository's canonical ``.aiworkhub/tasking`` directory
    (never a package-install or historical monorepo data path).
    ``runner``/``topic`` are optional B119 allowlist identity hints (default
    None preserves prior behavior for callers that do not supply them)."""
    command = ["export-jsonl"]
    blocked = _canonical_write_gate("export-jsonl", runner=runner, topic=topic)
    if blocked is not None:
        return blocked
    try:
        rows = task_store.list_tasks(repo_root(), status=None, limit=5000)
    except task_store.TaskStoreError as exc:
        return _canonical_result(ok=False, returncode=1, stderr=str(exc), command=command)
    out_dir = (
        repo_root() / repository_state.HUB_DIRNAME / repository_state.DURABLE_LAYOUT["tasking"]
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "canonical_task_export.jsonl"
    lines = [json.dumps(r, ensure_ascii=False, default=str) for r in rows]
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    stdout = f"exported {len(rows)} rows to {out_path}"
    return _canonical_result(ok=True, returncode=0, stdout=stdout, command=command)


def read_audit_log(
    max_entries: int = 100,
    *,
    repo: Path | None = None,
) -> dict[str, Any]:
    """Read-only: return a summary of the write-gate audit log.

    Never writes, never enables writes, never launches subprocesses.
    Returns counts by tool/action and the last N entries (sanitized —
    env var VALUES are never present in audit entries by construction,
    but this function adds no new secrets exposure).
    """
    log_path = _audit_log_path(repo)
    result: dict[str, Any] = {
        "ok": True,
        "audit_log_path": str(log_path),
        "audit_log_exists": log_path.exists(),
        "total_entries": 0,
        "entries_by_tool": {},
        "entries_by_action": {},
        "last_entries": [],
        "file_size_bytes": 0,
        "writes_allowed": writes_allowed(),
        "authority_flags": {
            "write_gate_enabled": not writes_allowed(),
            "workflow_switch": False,
            "process_launch": False,
            "agent_launch": False,
        },
    }

    if not log_path.exists():
        return result

    try:
        raw = log_path.read_text(encoding="utf-8")
        result["file_size_bytes"] = log_path.stat().st_size
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        entries: list[dict[str, Any]] = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        result["total_entries"] = len(entries)

        tool_counts: dict[str, int] = {}
        for e in entries:
            tn = e.get("tool_name", "unknown")
            tool_counts[tn] = tool_counts.get(tn, 0) + 1
        result["entries_by_tool"] = tool_counts

        action_counts: dict[str, int] = {}
        for e in entries:
            a = e.get("action", "unknown")
            action_counts[a] = action_counts.get(a, 0) + 1
        result["entries_by_action"] = action_counts

        recent = entries[-max_entries:] if max_entries > 0 else []
        result["last_entries"] = recent
    except OSError as exc:
        result["ok"] = False
        result["error"] = str(exc)

    return result


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


# ---------------------------------------------------------------------------
# B11 (retry of stale-claimed B10): read-only supervisor-loop STATUS derivation.
#
# Implements the 7-step derivation algorithm frozen by the supervisor-loop
# status contract
# verbatim. Pure composition of pre-existing read-only helpers already defined
# above (show_task, check_runner_topic_allowlist, collision_guard,
# _lifecycle_state) -- no NEW subprocess/exec/network/daemon-launch code is
# introduced here, and no write-mode taskctl action (allow_write=True) is ever
# invoked. show_task/collision_guard only ever call run_taskctl with
# allow_write defaulting to False, so no audit entry is ever appended by this
# function (write_audit_entry is only reached from the write-command branches
# of run_taskctl). This is a deterministic safety/derivation classifier per
# CLAUDE.md's neural-control-first carve-out (controller/verifier layers are
# valid when they are correctness/support-boundary checks) -- not a learned
# routing decision, and it introduces no new cue-router cognition.
# ---------------------------------------------------------------------------

LIFECYCLE_TO_SUPERVISOR_STATE: dict[str, str] = {
    "pending": "planned",
    "processing": "dispatched_dryrun",
    "review": "worker_review_ready",
    "finished": "codex_reviewed",
    "blocked": "blocked",
    # Terminal retirements the lifecycle itself produces. Archiving is the only
    # closure available for a card wedged in a non-operational terminal
    # substatus, so archived cards are common; a superseded card was explicitly
    # replaced by its successor. A read-only status surface must name these,
    # never raise on a state the system itself emits.
    "archived": "archived",
    "superseded": "superseded",
}


def _parse_show_task_card(stdout: str) -> dict[str, Any] | None:
    """Parse a task card dict out of ``show_task``'s stdout, or ``None`` if
    the task was not found.

    ``AITools/taskctl.py::cmd_show`` always exits 0: on a hit it prints one
    pretty-printed JSON object via plain ``print()``; on a miss it prints the
    literal string ``f"Task not found: {task_id}"`` (also via plain
    ``print()``, no ``sys.exit``). Miss-detection must therefore inspect
    stdout content, never returncode. Tolerant parse -- never raises.
    """
    text = (stdout or "").strip()
    if not text or "Task not found:" in text:
        return None
    try:
        card = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(card, dict):
        return None
    return card


def _extract_collision_report(stdout: str) -> dict[str, Any] | None:
    """Extract the ``scan_collisions``-shaped JSON object out of
    ``collision_guard``'s stdout.

    The real ``cmd_collision_guard --print`` output is a human-readable
    banner line, a blank line, then a pretty-printed JSON blob -- not a bare
    JSON document -- so this slices from the first ``{`` rather than parsing
    stdout directly. Tolerant parse -- never raises, returns ``None`` on any
    failure so a parse hiccup degrades to "no collision detected" instead of
    crashing this read-only tool.
    """
    text = stdout or ""
    idx = text.find("{")
    if idx == -1:
        return None
    try:
        return json.loads(text[idx:])
    except json.JSONDecodeError:
        return None


def supervisor_loop_status(
    task_id: str,
    runner: str | None = None,
    topic: str | None = None,
    supervisor_request_id: str | None = None,
    previous_snapshot: dict[str, Any] | None = None,
    reported_validation_verdict: str | None = None,
) -> dict[str, Any]:
    """READ-ONLY: derive ``supervisor_state`` + error taxonomy for a task_id.

    Pure read/derive composition of pre-existing core.py helpers
    (``show_task``, ``check_runner_topic_allowlist``, ``collision_guard``,
    ``_lifecycle_state``) implementing the 7 ``ordered_steps`` frozen by
    ``task_mcp_supervisor_loop_status_tool_b08_v1.json`` exactly:
    fetch_status -> runner_topic_mismatch_check -> missing_artifact_check ->
    collision_check -> stale_task_check -> failed_validation_check ->
    clean_mapping.

    FROZEN READ-ONLY CONTRACT -- no write-gate toggle, no mutation
    parameters. Never calls taskctl done/review/start/auto-pickup/add-card
    and never invokes ``run_taskctl`` with ``allow_write=True``. Never
    mutates queue or audit state, never launches agents/daemons/processes,
    performs no network I/O. Writes remain default-off regardless of input.
    ``previous_snapshot``/``reported_validation_verdict`` are caller-supplied
    out-of-band observations relayed for comparison/reporting only -- this
    function keeps no server-side snapshot store and never independently
    runs validation itself.
    """
    base: dict[str, Any] = {
        "task_id": task_id,
        "supervisor_request_id": supervisor_request_id,
        "process_state": "no_process_local_only_mvp",
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    # Step 1: fetch_status
    taskctl_status = show_task(task_id)
    card = _parse_show_task_card(taskctl_status.get("stdout", ""))
    if card is None:
        base["state"] = "not_found"
        base["taskctl_status"] = None
        base["supervisor_state"] = None
        base["error"] = None
        return base

    base["taskctl_status"] = taskctl_status

    # Step 2: runner_topic_mismatch_check
    if runner is not None or topic is not None:
        decision = check_runner_topic_allowlist(runner, topic, "review")
        if not decision["allowed"]:
            base["state"] = "blocked"
            base["supervisor_state"] = "blocked"
            base["error"] = {
                "code": "runner_topic_mismatch",
                "reason": f"runner_topic_mismatch:{decision['reason']}",
                "detected_at_leg": 1,
                "blocked": True,
            }
            return base

    # Step 3: missing_artifact_check (pure filesystem existence check only,
    # never a read of file contents)
    root = repo_root()
    candidate_paths = list(card.get("read_first") or []) + list(card.get("allowed_writes") or [])
    for rel_path in candidate_paths:
        if not (root / str(rel_path)).exists():
            base["state"] = "blocked"
            base["supervisor_state"] = "blocked"
            base["error"] = {
                "code": "missing_artifact",
                "reason": f"missing_artifact:{rel_path}",
                "detected_at_leg": 1,
                "blocked": True,
            }
            return base

    # Step 4: collision_check
    guard = collision_guard(print_json=True)
    report = _extract_collision_report(guard.get("stdout", ""))
    if report is not None and not report.get("collision_free", True):
        for fc in report.get("file_collisions", []) or []:
            if task_id in (fc.get("conflicting_tasks") or []):
                base["state"] = "blocked"
                base["supervisor_state"] = "blocked"
                base["error"] = {
                    "code": "collision",
                    "reason": f"collision:{fc.get('file')}:{task_id}",
                    "detected_at_leg": 1,
                    "blocked": True,
                }
                return base

    # Step 5: stale_task_check
    if previous_snapshot is not None:
        cur_snapshot = {"status": card.get("status"), "worker_status": card.get("worker_status")}
        if cur_snapshot != previous_snapshot:
            base["state"] = "blocked"
            base["supervisor_state"] = "blocked"
            base["error"] = {
                "code": "stale_task",
                "reason": (
                    f"stale_task:worker_status_changed:{task_id}:"
                    f"{previous_snapshot}->{cur_snapshot}"
                ),
                "detected_at_leg": 4,
                "blocked": True,
            }
            return base

    # Step 6: failed_validation_check (schema-reserved relay-only signal --
    # this tool never runs validation itself, only relays a caller-reported
    # out-of-band verdict, per B07 leg-3 reality_in_this_contract)
    if reported_validation_verdict == "failed":
        base["state"] = "blocked"
        base["supervisor_state"] = "blocked"
        base["error"] = {
            "code": "failed_validation",
            "reason": f"failed_validation:reported_by_caller:{task_id}",
            "detected_at_leg": 4,
            "blocked": True,
        }
        return base

    # Step 7: clean_mapping
    base["state"] = "planned"
    base["error"] = None
    lifecycle = _lifecycle_state(card)
    # A lifecycle the map has not been taught degrades to a named unknown rather
    # than raising KeyError on this read-only surface (the archived-card bug).
    base["supervisor_state"] = LIFECYCLE_TO_SUPERVISOR_STATE.get(
        lifecycle, f"unknown:{lifecycle}"
    )
    return base


# ---------------------------------------------------------------------------
# B857: repository-bound callback dispatcher lifecycle wiring.
#
# This section is the in-process bridge between (a) the VS Code extension's
# already-idempotent one-process-per-repository lifecycle (activate/reload/
# tab-deserialize/workspace-switch/deactivate -- see McpStdioClient in
# vscode-extension/extension.js) and (b) callback_bridge.py's dispatcher
# registry. It only ever reads the per-repo canonical
# ``.aiworkhub/tasking/task_queue.sqlite`` (via task_store.storage_readiness)
# and the per-repo ``.aiworkhub/config/routing/coordinator-targets.json``
# route-selection file the extension already writes
# (readCoordinatorTargets/setCoordinatorTarget) -- never
# AITools/taskctl.py or AITools/taskdb.py directly. The actual delivery
# engine (callback_bridge.CallbackBridge) keeps using its own existing,
# already-durable AITools/taskdb.py outbox unchanged; only THIS status/
# lifecycle surface is bound to the canonical in-process task_store per the
# B852 doctrine above.
# ---------------------------------------------------------------------------

COORDINATOR_ROUTE_STATE_REL = Path("config") / "routing" / "coordinator-targets.json"


def _callback_bridge_module():
    from . import callback_bridge as _callback_bridge

    return _callback_bridge


def _coordinator_route_state_path(root: Path | None = None) -> Path:
    return (root or repo_root()) / ".aiworkhub" / COORDINATOR_ROUTE_STATE_REL


def _live_mux_active_thread(root: Path, target: dict[str, Any]) -> str:
    """Return the active thread observed by this repo/window's one live mux.

    The mux registry is written from actual extension->Codex App Server
    traffic. Matching requires the immutable repo_id and the exact extension
    host PID recorded by this route; zero or multiple candidates fail closed.
    """

    try:
        from . import app_server_mux

        readiness = task_store.storage_readiness(root)
        repo_id = str(readiness.repo_id or "")
        extension_host_pid = int(target.get("extension_host_pid") or 0)
        if not readiness.ready or not repo_id or extension_host_pid <= 1:
            return ""
        matches = [
            instance
            for instance in app_server_mux.list_live_sideband_instances(
                app_server_mux.default_sideband_dir()
            )
            if instance.repo_id == repo_id
            and instance.parent_pid == extension_host_pid
            and instance.is_owner_fresh
            and instance.ready
            and _UUID_RE.fullmatch(instance.active_thread_id or "")
        ]
    except (OSError, RuntimeError, TypeError, ValueError, task_store.TaskStoreError):
        return ""
    if len(matches) != 1:
        return ""
    return str(matches[0].active_thread_id)


def read_selected_coordinator_target(root: Path | None = None) -> dict[str, Any]:
    """Read the explicitly selected coordinator provider ("codex"/"claude"/
    "copilot") from the same file the VS Code extension writes
    (``.aiworkhub/config/routing/coordinator-targets.json``). Never invokes
    AITools; missing/corrupt/unrecognized state fails safe to the same
    "codex" default the extension's own ``defaultCoordinatorTargets()``
    uses -- never a guess at a different provider.

    A verified live Claude manager identity (this process's own confirmed
    route) is stronger, fresher evidence of the active route than this
    persisted/default file, which may still say "codex" (its own default)
    long after the chat that wrote it ended, or before the extension has
    ever written it. Whenever that identity is observed, every return path
    below -- default, corrupt/unrecognized payload, and the normal merged
    result -- resolves the active route to "claude" instead of leaving a
    Codex-only route_pending/reason pinned on a manager route that is
    provably Claude. When no such identity is observed, every return path
    is byte-for-byte unchanged."""
    claude_identity = _claude_manager_identity()

    def _resolved(d: dict[str, Any]) -> dict[str, Any]:
        if claude_identity is None:
            return d
        result = dict(d)
        result["selected_provider"] = "claude"
        targets = result.get("targets")
        claude_target = targets.get("claude") if isinstance(targets, dict) else None
        next_claude_target = dict(claude_target) if isinstance(claude_target, dict) else {}
        next_claude_target["route"] = {
            "thread_id": "",
            "session_id": str(claude_identity.get("session_id") or ""),
            "window_id": str(claude_identity.get("window_id") or ""),
        }
        next_claude_target["capability_state"] = "available"
        next_claude_target["wake"] = {
            "mode": "mcp_callback_wait",
            "supported": True,
        }
        result["targets"] = {
            **(targets if isinstance(targets, dict) else {}),
            "claude": next_claude_target,
        }
        return result

    resolved_root = root or repo_root()
    path = _coordinator_route_state_path(resolved_root)
    default = {"schema_id": "aiworkhub.coordinator_targets.v1", "selected_provider": "codex"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _resolved(default)
    if not isinstance(payload, dict):
        return _resolved(default)
    provider = str(payload.get("selected_provider") or "").strip().lower()
    if provider not in ("codex", "claude", "copilot"):
        return _resolved(default)
    merged = {**default, **payload, "selected_provider": provider}
    targets = merged.get("targets")
    if isinstance(targets, dict):
        codex_target = targets.get("codex")
        if isinstance(codex_target, dict):
            route = codex_target.get("route")
            if isinstance(route, dict):
                thread_id = str(route.get("thread_id") or "").strip()
                # Repo/window aliases and empty values are UI labels, not
                # callback-capable Codex thread UUIDs. Older route files may
                # still say capability_state=available with a synthetic
                # codex:window_* value; normalize that stale state on every
                # read so no manager/dispatcher path treats it as routable.
                if not _UUID_RE.fullmatch(thread_id):
                    thread_id = _live_mux_active_thread(resolved_root, merged)
                if _UUID_RE.fullmatch(thread_id):
                    next_route = dict(route)
                    next_route["thread_id"] = thread_id
                    next_route["session_id"] = thread_id
                    next_target = dict(codex_target)
                    next_target["route"] = next_route
                    next_target["capability_state"] = "available"
                    next_target["wake"] = {
                        "mode": "app_server_sideband",
                        "supported": True,
                    }
                    merged["targets"] = {**targets, "codex": next_target}
                else:
                    next_route = dict(route)
                    next_route["thread_id"] = ""
                    next_route["session_id"] = str(merged.get("claim_episode") or route.get("session_id") or "")
                    next_target = dict(codex_target)
                    next_target["route"] = next_route
                    next_target["capability_state"] = "route_pending"
                    next_target["wake"] = {
                        "mode": "direct_api_or_callback_inbox",
                        "supported": False,
                        "reason": "codex_thread_id_not_observed",
                    }
                    merged["targets"] = {**targets, "codex": next_target}
    return _resolved(merged)


def dispatcher_ensure_started() -> dict[str, Any]:
    """Idempotently ensure exactly one callback dispatcher is running for
    the active repository, bound to the currently-selected coordinator
    target. Safe to call repeatedly (activation, tab-deserialization,
    reload, explicit coordinator-target switch) -- never starts a second
    thread and never fabricates a running dispatcher for an uninitialized
    repository."""
    root = repo_root()
    readiness = task_store.storage_readiness(root)
    if not readiness.ready:
        return {
            "ok": True,
            "status": "uninitialized",
            "dispatcher_started": False,
            "reason": readiness.reason,
            "repo": str(root),
        }
    target = read_selected_coordinator_target(root)
    claude_identity = _claude_manager_identity()
    callback_transport = os.environ.get("AIWORKHUB_CALLBACK_TRANSPORT", "").strip().lower()
    if callback_transport == "sideband":
        provider = "codex"
        claude_identity = None
    elif claude_identity is not None:
        provider = "claude"
    else:
        provider = target["selected_provider"]
    target_codex = target.get("targets", {}).get("codex", {}) if isinstance(target.get("targets"), dict) else {}
    target_codex_wake = target_codex.get("wake", {}) if isinstance(target_codex, dict) else {}
    sideband_route_available = bool(
        isinstance(target_codex_wake, dict)
        and target_codex_wake.get("supported")
        and target_codex_wake.get("mode") == "app_server_sideband"
    )
    # The extension child starts conservatively in manager_inbox mode so a
    # split-host Remote-SSH window can never try a foreign/local socket. Once
    # the repo/window-scoped mux has published an exact live Codex thread,
    # promote this ensure call to direct sideband delivery. This decision is
    # re-evaluated on every watchdog/renewal call instead of being frozen in
    # the MCP child's startup environment.
    if callback_transport == "manager_inbox" and provider == "codex" and sideband_route_available:
        callback_transport = "sideband"
    window_id = os.environ.get("AIWORKHUB_WINDOW_ID", "").strip()
    if not window_id and claude_identity is not None:
        window_id = claude_identity["window_id"]
    if callback_transport == "manager_inbox":
        bridge = _callback_bridge_module()
        bridge.stop_dispatcher(root)
        manager_identity = _codex_manager_identity() if provider == "codex" else None
        manager_origin_thread_id = ""
        if isinstance(manager_identity, dict):
            candidate = str(
                manager_identity.get("thread_id")
                or manager_identity.get("session_id")
                or ""
            ).strip()
            if _valid_origin_thread_id(candidate):
                manager_origin_thread_id = candidate
        if not manager_origin_thread_id:
            provider_target = (
                target.get("targets", {}).get(provider, {})
                if isinstance(target.get("targets"), dict) else {}
            )
            route = provider_target.get("route", {}) if isinstance(provider_target, dict) else {}
            candidate = str(
                route.get("thread_id") or route.get("session_id") or ""
            ).strip() if isinstance(route, dict) else ""
            if _valid_origin_thread_id(candidate):
                manager_origin_thread_id = candidate
        conn = _canonical_connect()
        try:
            rebound_count = 0
            if manager_origin_thread_id:
                rebound_count = callback_store.rebind_pending_callbacks(
                    conn,
                    provider=provider,
                    origin_thread_id=manager_origin_thread_id,
                )
            seeded_review_callback_count = callback_store.seed_missing_review_callbacks(
                conn,
                provider=provider,
                origin_thread_id=manager_origin_thread_id or None,
            )
        finally:
            conn.close()
        return {
            "ok": True,
            "healthy": True,
            "status": "manager_inbox",
            "dispatcher_started": False,
            "repo": str(root),
            "provider": provider,
            "reason": "native_codex_uses_cooperative_manager_inbox",
            "manager_route": manager_identity or {},
            "rebound_callback_count": rebound_count,
            "seeded_review_callback_count": seeded_review_callback_count,
        }
    if claude_identity is not None and provider == "claude":
        # A second ``claude --resume --print`` process cannot wake an already
        # open Claude Code webview.  The verified manager instead owns a
        # two-phase MCP long-poll inbox (callback_wait/callback_ack).  Do not
        # start the old CLI dispatcher and do not consume its retry budget.
        bridge = _callback_bridge_module()
        bridge.stop_dispatcher(root)
        conn = _canonical_connect()
        try:
            rebound_count = callback_store.rebind_pending_callbacks(
                conn,
                provider="claude",
                origin_thread_id=claude_identity["session_id"],
            )
            seeded_review_callback_count = callback_store.seed_missing_review_callbacks(
                conn,
                provider="claude",
                origin_thread_id=claude_identity["session_id"],
            )
        finally:
            conn.close()
        return {
            "ok": True,
            "status": "manager_inbox",
            "dispatcher_started": False,
            "repo": str(root),
            "provider": "claude",
            "manager_route": claude_identity,
            "reason": "claude_manager_uses_mcp_callback_wait",
            "rebound_callback_count": rebound_count,
            "seeded_review_callback_count": seeded_review_callback_count,
        }
    if not window_id:
        # Headless worker MCP processes never own callback dispatch. They
        # may claim work and mark it for review, but only the repository-
        # bound VS Code extension child has the window identity and lifecycle
        # needed to wake a coordinator UI. Report the role boundary as a
        # normal state instead of attempting a doomed transport launch.
        return {
            "ok": True,
            "status": "headless_worker",
            "dispatcher_started": False,
            "reason": "dispatcher_owned_by_vscode_extension",
            "repo": str(root),
            "provider": provider,
        }
    if not readiness.repo_id:
        # Requirement: register repo identity BEFORE starting a dispatcher. An
        # extension-owned coordinator process (it exports a window id) with no
        # resolved repo_id must fail loudly and recoverably -- never start a
        # dispatcher bound to an empty repo_id (which then fails closed inside
        # the sideband transport and merely looks "stopped" with no cause).
        return {
            "ok": False,
            "status": "repo_id_unavailable",
            "dispatcher_started": False,
            "healthy": False,
            "recoverable": True,
            "reason": "repository_identity_unregistered_reinit_required",
            "repo": str(root),
            "provider": provider,
        }
    bridge = _callback_bridge_module()
    bridge_kwargs: dict[str, Any] = {}
    if provider == "codex":
        if callback_transport:
            bridge_kwargs["transport"] = callback_transport
    route_origin_thread_id = ""
    target_provider = target.get("targets", {}).get(provider, {}) if isinstance(target.get("targets"), dict) else {}
    if isinstance(target_provider, dict):
        route = target_provider.get("route", {})
        if isinstance(route, dict):
            candidate_thread = str(route.get("thread_id") or route.get("session_id") or "").strip()
            if _valid_origin_thread_id(candidate_thread):
                route_origin_thread_id = candidate_thread
    rebound_count = 0
    seeded_review_callback_count = 0
    # Reconcile the durable review queue on every idempotent ensure, even
    # when the currently-observed manager route is temporarily unavailable.
    # Normal transitions enqueue immediately, but a child/reload race can
    # leave an already-reviewable task without an outbox row.  Such a task
    # still carries its persisted originating thread, which
    # seed_missing_review_callbacks() can safely reuse.  Waiting for a fresh
    # route here was the reason those rows only woke the manager after a
    # later Reload Window.
    conn = _canonical_connect()
    try:
        if route_origin_thread_id:
            rebound_count = callback_store.rebind_pending_callbacks(
                conn,
                provider=provider,
                origin_thread_id=route_origin_thread_id,
            )
        seeded_review_callback_count = callback_store.seed_missing_review_callbacks(
            conn,
            provider=provider,
            origin_thread_id=route_origin_thread_id or None,
        )
    finally:
        conn.close()
    dispatcher = bridge.ensure_dispatcher(
        root,
        provider,
        repo_id=readiness.repo_id,
        window_id=window_id,
        bridge_kwargs=bridge_kwargs,
    )
    health = dispatcher.health()
    running = bool(health.get("dispatcher_running"))
    return {
        **health,
        "ok": running,
        "status": "started" if running else "start_failed",
        "dispatcher_started": running,
        "healthy": running,
        "recoverable": not running,
        "repo": str(root),
        "manager_route": claude_identity or {},
        "rebound_callback_count": rebound_count,
        "seeded_review_callback_count": seeded_review_callback_count,
    }


def claude_callback_wait(timeout_seconds: int = 240) -> dict[str, Any]:
    """Wait for one callback batch belonging to this exact Claude manager.

    Returning the batch wakes the already-open Claude turn through its own
    MCP request.  Delivery remains inflight until ``claude_callback_ack``;
    if the tool response is lost, the normal lease reclaim path retries it.
    """
    identity = _claude_manager_identity()
    if identity is None:
        return {"ok": False, "reason": "verified_claude_manager_required"}
    timeout = max(1, min(int(timeout_seconds), 300))
    deadline = time.monotonic() + timeout
    while True:
        conn = _canonical_connect()
        try:
            callback_store.seed_missing_review_callbacks(
                conn,
                provider="claude",
                origin_thread_id=identity["session_id"],
            )
            # Claim at route level, not just provider level: pass this verified
            # manager's own session identity through so a second manager on the
            # same repository can never lease (and then park) a batch belonging
            # to another route. The store's ``origin_thread_id`` scope is exactly
            # this guarantee; passing only ``provider`` left it unused.
            batch = callback_store.claim_pending_callback_batch(
                conn,
                lease_seconds=max(120, timeout + 30),
                provider="claude",
                origin_thread_id=identity["session_id"],
            )
        finally:
            conn.close()
        if batch is not None:
            if batch.get("origin_thread_id") != identity["session_id"]:
                conn = _canonical_connect()
                try:
                    callback_store.defer_batch_busy(
                        conn,
                        str(batch["batch_id"]),
                        "callback_belongs_to_different_claude_session",
                        str(batch["lease_id"]),
                        delay_seconds=30.0,
                    )
                finally:
                    conn.close()
            else:
                members = [
                    {
                        "task_id": str(member.get("task_id") or ""),
                        "state": str(member.get("transition") or ""),
                        "event_id": str(member.get("event_id") or ""),
                        "request_id": str(member.get("request_id") or ""),
                    }
                    for member in batch.get("members", [])
                ]
                return {
                    "ok": True,
                    "status": "callback_ready",
                    "provider": "claude",
                    "batch_id": batch["batch_id"],
                    "lease_id": batch["lease_id"],
                    "origin_thread_id": batch["origin_thread_id"],
                    "members": members,
                }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "ok": True,
                "status": "timeout_no_callback",
                "provider": "claude",
                "waited_seconds": timeout,
            }
        time.sleep(min(0.5, remaining))


def claude_callback_ack(batch_id: str, lease_id: str) -> dict[str, Any]:
    """Durably acknowledge a callback returned by ``claude_callback_wait``."""
    identity = _claude_manager_identity()
    if identity is None:
        return {"ok": False, "reason": "verified_claude_manager_required"}
    conn = _canonical_connect()
    try:
        acknowledged = callback_store.acknowledge_callback_batch(
            conn,
            batch_id,
            lease_id,
            provider="claude",
            origin_thread_id=identity["session_id"],
        )
    finally:
        conn.close()
    return {
        "ok": acknowledged,
        "status": "delivered" if acknowledged else "ack_rejected",
        "provider": "claude",
        "batch_id": batch_id,
    }


def dispatcher_health() -> dict[str, Any]:
    """Read-only dispatcher health for the active repository.

    An uninitialized repository and a bare headless worker (a child that owns
    no dispatch) are normal, non-degraded states. But when this process is the
    extension-owned coordinator (it exports ``AIWORKHUB_WINDOW_ID``) or a
    verified interactive Claude manager -- i.e. dispatch is EXPECTED here -- a
    dispatcher that is unregistered, stopped, carries an empty/mismatched
    ``repo_id``, or reports a start error is a HARD, recoverable health failure
    (``ok=False``, ``healthy=False``, with an explicit ``problems`` list), never
    a silent ``ok=True/stopped``. The canonical ``repo_id`` is always the
    storage-registry truth and is never overwritten by a dispatcher's empty one.
    """
    root = repo_root()
    readiness = task_store.storage_readiness(root)
    if not readiness.ready:
        return {
            "ok": True,
            "status": "uninitialized",
            "dispatcher_running": False,
            "healthy": True,
            "dispatch_expected": False,
            "problems": [],
            "reason": readiness.reason,
            "repo": str(root),
        }
    bridge = _callback_bridge_module()
    health = bridge.dispatcher_health(root)
    target = read_selected_coordinator_target(root)
    window_id = os.environ.get("AIWORKHUB_WINDOW_ID", "").strip()
    claude_identity = _claude_manager_identity()
    callback_transport = os.environ.get("AIWORKHUB_CALLBACK_TRANSPORT", "").strip().lower()
    target_codex = target.get("targets", {}).get("codex", {}) if isinstance(target.get("targets"), dict) else {}
    target_codex_wake = target_codex.get("wake", {}) if isinstance(target_codex, dict) else {}
    sideband_route_available = bool(
        isinstance(target_codex_wake, dict)
        and target_codex_wake.get("supported")
        and target_codex_wake.get("mode") == "app_server_sideband"
    )
    sideband = callback_transport == "sideband" or (
        callback_transport == "manager_inbox"
        and target.get("selected_provider") == "codex"
        and sideband_route_available
    )
    # A verified Claude manager delivers through the MCP long-poll inbox, not a
    # background dispatcher thread, so it is healthy WITHOUT a registered one.
    manager_inbox = (callback_transport == "manager_inbox" and not sideband) or (
        claude_identity is not None and not sideband
        and target["selected_provider"] == "claude"
    )
    # Dispatch is expected only in the extension-owned coordinator process (it
    # exports a window id) or a verified interactive Claude manager. A bare
    # headless worker MCP child owns no dispatch -- a role boundary, not a fault.
    dispatch_expected = bool(window_id) or claude_identity is not None
    running = bool(health.get("dispatcher_running"))
    registered = bool(health.get("registered"))
    dispatcher_repo_id = str(health.get("repo_id") or "")
    problems: list[str] = []
    if dispatch_expected and not manager_inbox:
        if not registered:
            problems.append("dispatcher_unregistered")
        elif not running:
            problems.append("dispatcher_stopped")
        if not readiness.repo_id:
            problems.append("repo_id_unavailable")
        elif registered and not dispatcher_repo_id:
            problems.append("dispatcher_repo_id_empty")
        elif dispatcher_repo_id and dispatcher_repo_id != readiness.repo_id:
            problems.append("dispatcher_repo_id_mismatch")
        start_error = str(health.get("last_start_error") or "")
        if start_error:
            problems.append(f"start_error:{start_error}")
    healthy = not problems
    status = "manager_inbox" if manager_inbox else ("running" if running else "stopped")
    return {
        **health,
        "ok": healthy,
        "healthy": healthy,
        "status": status,
        "repo": str(root),
        "repo_id": readiness.repo_id,
        "dispatcher_repo_id": dispatcher_repo_id,
        "selected_provider": target["selected_provider"],
        "dispatch_expected": dispatch_expected,
        "recoverable": bool(problems),
        "problems": problems,
    }


def dispatcher_watchdog() -> dict[str, Any]:
    """Detect a down-but-expected dispatcher and recover it in place.

    Intended to run on the extension's periodic refresh so a dispatcher that was
    never registered, crashed, lost its ``repo_id``, or reports a start error is
    automatically re-ensured instead of silently staying stopped until the next
    manual reload. A healthy dispatcher, a manager-inbox session, or a process
    where dispatch is not expected is left untouched.
    """
    health = dispatcher_health()
    if not health.get("dispatch_expected"):
        return {
            "ok": True,
            "recovered": False,
            "status": health.get("status"),
            "reason": "dispatch_not_expected",
            "health": health,
        }
    # ``ensure`` is deliberately idempotent.  Besides restarting a dead
    # dispatcher it reconciles the durable review queue and seeds any missing
    # callback outbox rows.  Run it even while the dispatcher thread itself is
    # healthy: a terminal-transition/outbox race is independent from thread
    # liveness and otherwise remains invisible until a manual reload.
    recovery = dispatcher_ensure_started()
    after = dispatcher_health()
    healthy_before = bool(health.get("healthy"))
    healthy_after = bool(after.get("healthy"))
    seeded = int(recovery.get("seeded_review_callback_count") or 0)
    rebound = int(recovery.get("rebound_callback_count") or 0)
    return {
        "ok": healthy_after,
        "recovered": (not healthy_before and healthy_after),
        "reconciled": True,
        "seeded_review_callback_count": seeded,
        "rebound_callback_count": rebound,
        "status": after.get("status"),
        "problems_before": health.get("problems", []),
        "recovery": recovery,
        "health": after,
    }


def dispatcher_stop() -> dict[str, Any]:
    """Stop and unregister the active repository's dispatcher, if any.
    Used on workspace/repository switch and extension deactivation so no
    cross-repository read or delivery can happen afterward."""
    root = repo_root()
    bridge = _callback_bridge_module()
    stopped = bridge.stop_dispatcher(root)
    return {"ok": True, "stopped": stopped, "repo": str(root)}


# ---------------------------------------------------------------------------
# 0.6.30: Source Graph automatic indexing lifecycle (repo-bound, one daemon
# per canonical repository -- see source_graph_daemon.py). Mirrors the
# dispatcher_ensure_started/health/stop shape above.
# ---------------------------------------------------------------------------

_SOURCE_GRAPH_REFRESH_SECONDS_ENV = "AIWORKHUB_SOURCE_GRAPH_REFRESH_SECONDS"


def _source_graph_daemon_module():
    from . import source_graph_daemon

    return source_graph_daemon


def _configured_source_graph_refresh_seconds():
    module = _source_graph_daemon_module()
    raw = os.environ.get(_SOURCE_GRAPH_REFRESH_SECONDS_ENV, "").strip()
    if not raw:
        return module.DEFAULT_REFRESH_INTERVAL_SECONDS
    try:
        return float(raw)
    except ValueError:
        return module.DEFAULT_REFRESH_INTERVAL_SECONDS


def source_graph_ensure_started() -> dict[str, Any]:
    """Idempotently ensure exactly one Source Graph indexing daemon is
    running for the active repository. Safe to call repeatedly (InitRepo,
    activation, tab-deserialization, reload) -- never starts a second
    thread and never fabricates a running daemon for an uninitialized
    repository."""
    root = repo_root()
    readiness = task_store.storage_readiness(root)
    if not readiness.ready:
        return {
            "ok": True,
            "status": "uninitialized",
            "daemon_started": False,
            "reason": readiness.reason,
            "repo": str(root),
        }
    from . import feature_settings

    if not feature_settings.enabled(root, "source_graph"):
        stopped = _source_graph_daemon_module().stop_daemon(root)
        return {
            "ok": True,
            "status": "disabled",
            "daemon_started": False,
            "stopped": stopped,
            "reason": "disabled_by_repository_settings",
            "repo": str(root),
        }
    module = _source_graph_daemon_module()
    module.ensure_started(root, refresh_interval_seconds=_configured_source_graph_refresh_seconds())
    # One canonical projection: ensure_started and source_graph_health must
    # hydrate generation metadata from the same committed database snapshot.
    health = module.daemon_health(root)
    return {
        "ok": True,
        "status": "started" if health.get("running") else "start_failed",
        "daemon_started": bool(health.get("running")),
        "repo": str(root),
        **health,
    }


def source_graph_health() -> dict[str, Any]:
    """READ-ONLY: Source Graph indexing daemon health for the active
    repository (indexing/ready/degraded, last report/error/time)."""
    root = repo_root()
    from . import feature_settings

    if not feature_settings.enabled(root, "source_graph"):
        return {
            "ok": True,
            "status": "disabled",
            "running": False,
            "reason": "disabled_by_repository_settings",
            "repo": str(root),
        }
    return _source_graph_daemon_module().daemon_health(root)


def source_graph_refresh_now() -> dict[str, Any]:
    """Force one bounded incremental (or first-ever full) build now,
    non-overlapping with the periodic loop. Starts the daemon first if it
    is not registered yet, but only for an already-initialized repository."""
    root = repo_root()
    from . import feature_settings

    if not feature_settings.enabled(root, "source_graph"):
        return {
            "ok": True,
            "status": "disabled",
            "triggered": False,
            "reason": "disabled_by_repository_settings",
            "repo": str(root),
        }
    readiness = task_store.storage_readiness(root)
    if not readiness.ready:
        return {
            "ok": True,
            "status": "uninitialized",
            "triggered": False,
            "reason": readiness.reason,
            "repo": str(root),
        }
    module = _source_graph_daemon_module()
    daemon = module.get_daemon(root)
    if daemon is None:
        daemon = module.ensure_started(root, refresh_interval_seconds=_configured_source_graph_refresh_seconds())
    # Never run repository parsing on the MCP stdio request thread. The
    # daemon coalesces repeated requests and executes the build in its own
    # background loop/subprocess while health, callbacks and dashboard calls
    # remain responsive.
    return daemon.request_refresh()


def source_graph_stop() -> dict[str, Any]:
    """Stop and unregister the active repository's Source Graph indexing
    daemon, if any. Called on workspace/repository switch and extension
    deactivation."""
    root = repo_root()
    stopped = _source_graph_daemon_module().stop_daemon(root)
    return {
        "ok": True,
        "stopped": stopped,
        "status": "stopped" if stopped else "idle",
        "repo": str(root),
    }
# --- NeedFix manager API (additive; NeedFix is separate from task state) ---


def _needfix_store_module():
    from . import needfix_store

    return needfix_store


def _needfix_ingest_module():
    from . import needfix_ingest

    return needfix_ingest


class NeedfixListing(list):
    """A NeedFix listing that also states whether read-time derivation ran.

    Subclasses ``list`` so every existing caller -- the ``needfix_list`` MCP
    tool typed ``list[dict]`` and the dashboard webview that iterates the rows
    -- keeps working unchanged, while ``derived``/``underived_reason`` tell a
    caller when the linked-card active state could NOT be resolved (no ready
    task store) instead of passing raw stored rows off as an authoritative
    active set. See ``needfix_store.ACTIVE_STATE_DEFINITION``.
    """

    __slots__ = ("derived", "underived_reason")

    def __init__(self, items, *, derived: bool, underived_reason: str | None):
        super().__init__(items)
        self.derived = bool(derived)
        self.underived_reason = underived_reason


class NeedfixCount(int):
    """A NeedFix count that also states whether read-time derivation ran.

    Subclasses ``int`` so the ``needfix_count`` MCP tool typed ``-> int`` and
    every arithmetic caller keep working, while ``derived``/``underived_reason``
    distinguish a derived active count from a raw fallback total taken when no
    ready task store could resolve the linked-card state.
    """

    def __new__(cls, value: int, *, derived: bool, underived_reason: str | None):
        obj = super().__new__(cls, int(value))
        obj.derived = bool(derived)
        obj.underived_reason = underived_reason
        return obj


def needfix_list(
    status: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
    order_by: str = "created_at",
    order_dir: str = "DESC",
) -> "NeedfixListing":
    """Operator-facing NeedFix listing, derived at read time by default.

    Routes through ``needfix_ingest.list_active`` so a record whose linked card
    has landed (or is owned by an in-flight task) is hidden without any caller
    remembering to pass task-store hooks -- derivation is the default door, not
    an opt-in a surface can forget. The return is a ``list`` subclass so the
    ``needfix_list`` MCP tool and the dashboard webview keep receiving an
    iterable list of rows, while ``derived``/``underived_reason`` on it say when
    the active state could not be resolved (no ready task store) rather than
    silently presenting raw rows as an authoritative active set.

    An explicit ``status``/``kind``/``severity`` filter is a raw field browse
    (a terminal status is never in the derived active set), so it returns the
    raw filtered rows marked ``derived=False`` -- not silently, the reason says
    so -- rather than an empty derived page.
    """
    if status is not None or kind is not None or severity is not None:
        ns = _needfix_store_module()
        rows = ns.list_needfix(
            repo_root(),
            status=status,
            kind=kind,
            severity=severity,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
            order_by=order_by,
            order_dir=order_dir,
        )
        return NeedfixListing(
            rows,
            derived=False,
            underived_reason="explicit_status_kind_severity_filter_returns_raw_rows",
        )
    ni = _needfix_ingest_module()
    report = ni.list_active(
        repo_root(),
        include_archived=include_archived,
        limit=limit,
        offset=offset,
        order_by=order_by,
        order_dir=order_dir,
    )
    return NeedfixListing(
        report["items"],
        derived=report.get("derived", False),
        underived_reason=report.get("underived_reason"),
    )


def needfix_show(needfix_id: str) -> dict[str, Any]:
    ns = _needfix_store_module()
    return ns.get_needfix(repo_root(), needfix_id)


def needfix_add(
    title: str,
    description: str,
    scope: str | None = None,
    provenance: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    status: str = "captured",
    kind: str = "other",
    severity: str = "medium",
    tags: list[str] | None = None,
    scope_files: list[str] | None = None,
    scope_symbols: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    readiness_score: int = 0,
) -> dict[str, Any]:
    """Manager mutation entry point. Distinct from worker capture authority."""
    ns = _needfix_store_module()
    return ns.add_needfix(
        repo_root(),
        title=title,
        description=description,
        scope=scope,
        provenance=provenance,
        evidence=evidence,
        status=status,
        kind=kind,
        severity=severity,
        tags=tags,
        scope_files=scope_files,
        scope_symbols=scope_symbols,
        evidence_refs=evidence_refs,
        readiness_score=readiness_score,
    )


def needfix_capture(
    title: str,
    description: str,
    scope: str | None = None,
    provenance: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    kind: str = "other",
    severity: str = "medium",
    tags: list[str] | None = None,
    scope_files: list[str] | None = None,
    scope_symbols: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    readiness_score: int = 0,
) -> dict[str, Any]:
    """Worker/unverified proposal entry point. Always lands as captured."""
    ns = _needfix_store_module()
    return ns.capture_proposal(
        repo_root(),
        title=title,
        description=description,
        scope=scope,
        provenance=provenance,
        evidence=evidence,
        kind=kind,
        severity=severity,
        tags=tags,
        scope_files=scope_files,
        scope_symbols=scope_symbols,
        evidence_refs=evidence_refs,
        readiness_score=readiness_score,
    )


def needfix_triage(needfix_id: str, *, readiness_score: int | None = None, triage_note: str | None = None) -> dict[str, Any]:
    ns = _needfix_store_module()
    return ns.triage_needfix(repo_root(), needfix_id, readiness_score=readiness_score, triage_note=triage_note)


def needfix_accept(needfix_id: str, *, readiness_score: int | None = None) -> dict[str, Any]:
    ns = _needfix_store_module()
    return ns.accept_needfix(repo_root(), needfix_id, readiness_score=readiness_score)


def needfix_reject(needfix_id: str, *, reason: str) -> dict[str, Any]:
    ns = _needfix_store_module()
    return ns.reject_needfix(repo_root(), needfix_id, reason=reason)


def needfix_mark_duplicate(needfix_id: str, duplicate_parent_id: str, *, reason: str | None = None) -> dict[str, Any]:
    ns = _needfix_store_module()
    return ns.mark_duplicate(repo_root(), needfix_id, duplicate_parent_id, reason=reason)


def needfix_defer(needfix_id: str, *, reason: str | None = None) -> dict[str, Any]:
    ns = _needfix_store_module()
    return ns.defer_needfix(repo_root(), needfix_id, reason=reason)


def needfix_mark_task_planned(needfix_id: str) -> dict[str, Any]:
    ns = _needfix_store_module()
    return ns.mark_task_planned(repo_root(), needfix_id)


def needfix_resolve(needfix_id: str, *, resolution_note: str | None = None) -> dict[str, Any]:
    ns = _needfix_store_module()
    return ns.resolve_needfix(repo_root(), needfix_id, resolution_note=resolution_note)


def needfix_resolve_verified(needfix_id: str, *, resolution_note: str) -> dict[str, Any]:
    ns = _needfix_store_module()
    return ns.resolve_verified_needfix(
        repo_root(), needfix_id, resolution_note=resolution_note
    )


def needfix_update(
    needfix_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    scope: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    tags: list[str] | None = None,
    scope_files: list[str] | None = None,
    scope_symbols: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
    evidence_refs: list[str] | None = None,
    readiness_score: int | None = None,
) -> dict[str, Any]:
    """Manager update of mutable NeedFix fields."""
    ns = _needfix_store_module()
    return ns.update_needfix(
        repo_root(),
        needfix_id,
        title=title,
        description=description,
        scope=scope,
        kind=kind,
        severity=severity,
        tags=tags,
        scope_files=scope_files,
        scope_symbols=scope_symbols,
        evidence=evidence,
        evidence_refs=evidence_refs,
        readiness_score=readiness_score,
    )


def needfix_archive(needfix_id: str, reason: str | None = None) -> dict[str, Any]:
    ns = _needfix_store_module()
    return ns.archive_needfix(repo_root(), needfix_id, reason=reason)


def needfix_restore(needfix_id: str, target_status: str = "captured") -> dict[str, Any]:
    ns = _needfix_store_module()
    return ns.restore_needfix(repo_root(), needfix_id, target_status=target_status)


def needfix_purge(needfix_id: str, audit_reason: str) -> dict[str, Any]:
    ns = _needfix_store_module()
    return ns.purge_needfix(repo_root(), needfix_id, audit_reason=audit_reason)


def needfix_count(status: str | None = None, kind: str | None = None, severity: str | None = None) -> "NeedfixCount":
    """Operator-facing NeedFix count, derived at read time by default.

    Uses ``needfix_ingest.count_active`` -- the same resolved hooks and default
    filter as :func:`needfix_list` -- so the derived count and the derived list
    describe the same active set. An explicit ``status``/``kind``/``severity``
    filter is a raw field browse and returns the raw count marked
    ``derived=False``. When no ready task store can resolve the linked-card
    state the count is the raw total, marked underived (with a reason) so it is
    never mistaken for an authoritative active count.
    """
    if status is not None or kind is not None or severity is not None:
        ns = _needfix_store_module()
        raw = ns.count_needfix(repo_root(), status=status, kind=kind, severity=severity)
        return NeedfixCount(
            raw,
            derived=False,
            underived_reason="explicit_status_kind_severity_filter_returns_raw_rows",
        )
    ni = _needfix_ingest_module()
    report = ni.count_active(repo_root())
    if report.get("count") is None:
        return NeedfixCount(
            report.get("raw_total") or 0,
            derived=False,
            underived_reason=report.get("underived_reason"),
        )
    return NeedfixCount(report["count"], derived=True, underived_reason=None)


def needfix_events(needfix_id: str, limit: int = 100) -> list[dict[str, Any]]:
    ns = _needfix_store_module()
    return ns.list_events(repo_root(), needfix_id, limit=limit)


def needfix_preview_convert(
    needfix_id: str, *, task_plan: dict[str, Any] | None = None
) -> dict[str, Any]:
    ns = _needfix_store_module()
    return ns.preview_convert(repo_root(), needfix_id, task_plan=task_plan)


def needfix_convert(
    needfix_id: str,
    *,
    task_plan: dict[str, Any] | None = None,
    plan_digest: str | None = None,
) -> dict[str, Any]:
    """Explicit conversion using the existing authoritative create_task.

    ``task_plan`` must explicitly bind the workforce-selected ``runner`` and
    ``topic``; writable cards must also declare non-empty
    ``required_outputs``. Scope-derived defaults never guess either decision.
    The plan is strictly validated and overrides the scope-derived base field
    by field. Never infers ``max_live_tokens`` on the caller's behalf -- the
    created task stays uncapped unless ``task_plan`` explicitly sets it.
    Every confirmed non-idempotent
    conversion must bind to a ``plan_digest`` the matching preview
    returned, so any NeedFix drift between preview and commit
    fails closed instead of silently committing a different plan.
    Idempotent already-task_created retries short-circuit before this
    enforcement, preserving reconciliation without a new create.

    The store mints a deterministic, collision-free ``task_id`` from the
    NeedFix's current ``reopen_generation``: ``needfix-{NF-ID}`` for the
    original conversion and ``needfix-{NF-ID}-rN`` after the Nth verified
    reopen of an archived superseded task link.
    """
    ns = _needfix_store_module()

    def _create_task_fn(card: dict[str, Any]) -> dict[str, Any]:
        def _require_str(key: str) -> str:
            """Extract a required non-empty string from the conversion card.

            Raises the typed ``NeedFixError`` (never a raw ``KeyError``) when
            the field is missing, not a string, or empty/whitespace-only.
            """
            value = card.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ns.NeedFixError(
                    f"conversion card is missing required field {key!r}"
                )
            return value

        return create_task(
            task_id=_require_str("task_id"),
            title=_require_str("title"),
            runner=card.get("runner", "claude"),
            topic=card.get("topic", "needfix_conversion"),
            objective=_require_str("objective"),
            acceptance=card.get("acceptance", ["Resolve the reported NeedFix"]),
            allowed_writes=card.get("allowed_writes", []),
            forbidden=card.get("forbidden"),
            required_outputs=card.get("required_outputs"),
            allow_empty_required_outputs=card.get("allow_empty_required_outputs"),
            allow_unchanged_required_outputs=card.get("allow_unchanged_required_outputs"),
            validation=card.get("validation"),
            priority=card.get("priority", "normal"),
            callback_required=card.get("callback_required", True),
            task_type=card.get("task_type", "code"),
            depends_on=card.get("depends_on"),
            read_first=card.get("read_first"),
            immutable_inputs=card.get("immutable_inputs"),
            read_only=card.get("read_only", False),
            max_live_tokens=card.get("max_live_tokens"),
        )

    def _task_card_builder(needfix_snapshot: dict[str, Any]) -> dict[str, Any]:
        if plan_digest is None:
            raise ns.NeedFixError(
                "plan_digest is required to bind a confirmed conversion to "
                "the previewed plan; re-preview before committing"
            )
        card = ns.normalize_task_plan(needfix_snapshot, task_plan)
        actual_digest = ns.plan_digest(card)
        if actual_digest != plan_digest:
            raise ns.NeedFixConflictError(
                "task_plan digest mismatch -- the previewed plan no longer "
                "matches the current NeedFix state; re-preview before committing"
            )
        return card

    return ns.convert_needfix(
        repo_root(), needfix_id, _create_task_fn, task_card_builder=_task_card_builder
    )


def needfix_link_existing_task(needfix_id: str, existing_task_id: str) -> dict[str, Any]:
    """Explicit manager-only link of a NeedFix to an already-existing,
    same-repository canonical task that is manager-accepted and finished."""
    ns = _needfix_store_module()

    def _get_task_fn(task_id: str) -> dict[str, Any] | None:
        return task_store.get_task(repo_root(), task_id)

    return ns.link_existing_task(
        repo_root(),
        needfix_id,
        existing_task_id,
        _get_task_fn,
        task_store.canonical_status,
    )


def needfix_reopen_superseded_task_link(
    needfix_id: str, reason: str
) -> dict[str, Any]:
    """Manager-only reconciliation of an archived superseded task link.

    Each verified reopen increments the store's ``reopen_generation`` so a
    later conversion mints a deterministic ``-rN`` successor task_id.
    """
    ns = _needfix_store_module()

    def _get_task_fn(task_id: str) -> dict[str, Any] | None:
        return task_store.get_task(repo_root(), task_id)

    return ns.reopen_superseded_task_link(
        repo_root(),
        needfix_id,
        get_task_fn=_get_task_fn,
        canonical_status_fn=task_store.canonical_status,
        reason=reason,
    )


# --- Roadmap manager API (durable layer between NeedFix and Task DAG) ---


def _roadmap_store_module():
    from . import roadmap_store

    return roadmap_store


def roadmap_list(
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    return _roadmap_store_module().list_items(
        repo_root(),
        status=status,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )


def roadmap_show(roadmap_id: str) -> dict[str, Any]:
    return _roadmap_store_module().get_item(repo_root(), roadmap_id)


def roadmap_events(roadmap_id: str, limit: int = 100) -> list[dict[str, Any]]:
    return _roadmap_store_module().list_events(
        repo_root(), roadmap_id, limit=limit
    )


def roadmap_add(
    title: str,
    outcome: str,
    *,
    priority: str = "medium",
    milestone: str = "",
    acceptance: list[str] | None = None,
    needfix_ids: list[str] | None = None,
    depends_on: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Create a proposed roadmap outcome without creating or launching tasks.

    Any linked NeedFix must already be manager-accepted or further along its
    audited lifecycle. Captured hypotheses cannot silently become roadmap
    commitments.
    """
    roadmap_ns = _roadmap_store_module()
    needfix_ns = _needfix_store_module()
    normalized_needfix = list(dict.fromkeys(needfix_ids or []))
    allowed_needfix_statuses = {
        "accepted",
        "task_planned",
        "task_created",
        "resolved",
    }
    for needfix_id in normalized_needfix:
        item = needfix_ns.get_needfix(repo_root(), needfix_id)
        if item["status"] not in allowed_needfix_statuses:
            raise roadmap_ns.RoadmapConflictError(
                f"roadmap requires accepted NeedFix authority: "
                f"{needfix_id} status={item['status']!r}"
            )
    manager_provenance = dict(provenance or {})
    manager_provenance.setdefault("origin", "manager_roadmap_add")
    manager_provenance["verified"] = True
    return roadmap_ns.add_item(
        repo_root(),
        title=title,
        outcome=outcome,
        priority=priority,
        milestone=milestone,
        acceptance=acceptance,
        needfix_ids=normalized_needfix,
        depends_on=depends_on,
        provenance=manager_provenance,
        evidence_refs=evidence_refs,
    )


def roadmap_transition(
    roadmap_id: str, target_status: str, *, reason: str
) -> dict[str, Any]:
    roadmap_ns = _roadmap_store_module()
    current = roadmap_ns.get_item(repo_root(), roadmap_id)
    if target_status == "completed":
        if not current["acceptance"]:
            raise roadmap_ns.RoadmapConflictError(
                "roadmap completion requires explicit acceptance criteria"
            )
        unfinished: list[str] = []
        missing: list[str] = []
        for task_id in current["task_ids"]:
            card = task_store.get_task(repo_root(), task_id)
            if card is None:
                missing.append(task_id)
            elif task_store.canonical_status(card) != "finished":
                unfinished.append(task_id)
        if missing or unfinished:
            raise roadmap_ns.RoadmapConflictError(
                "roadmap completion blocked by task authority: "
                f"missing={missing!r} unfinished={unfinished!r}"
            )
        if not current["task_ids"] and not current["evidence_refs"]:
            raise roadmap_ns.RoadmapConflictError(
                "roadmap completion requires linked task evidence or evidence_refs"
            )
    return roadmap_ns.transition_item(
        repo_root(), roadmap_id, target_status, reason=reason
    )


def roadmap_link_task(roadmap_id: str, task_id: str) -> dict[str, Any]:
    roadmap_ns = _roadmap_store_module()
    if task_store.get_task(repo_root(), task_id) is None:
        raise roadmap_ns.RoadmapNotFoundError(f"task not found: {task_id}")
    return roadmap_ns.link_task(repo_root(), roadmap_id, task_id)


def roadmap_snapshot(
    *, limit: int = 200, include_archived: bool = False
) -> dict[str, Any]:
    """Return bounded roadmap truth joined to canonical task lifecycle."""
    items = roadmap_list(include_archived=include_archived, limit=limit)
    status_counts = _roadmap_store_module().count_items_by_status(
        repo_root(), include_archived=include_archived
    )
    total = sum(status_counts.values())
    rows: list[dict[str, Any]] = []
    for item in items:
        tasks: list[dict[str, str]] = []
        for task_id in item["task_ids"]:
            card = task_store.get_task(repo_root(), task_id)
            tasks.append(
                {
                    "task_id": task_id,
                    "status": task_store.canonical_status(card)
                    if card is not None
                    else "missing",
                }
            )
        dependency_blockers = [
            dependency
            for dependency in item["depends_on"]
            if _roadmap_store_module().get_item(repo_root(), dependency)["status"]
            != "completed"
        ]
        rows.append(
            {
                **item,
                "tasks": tasks,
                "dependency_blockers": dependency_blockers,
                "dependency_ready": not dependency_blockers,
            }
        )
    return {
        "schema_id": "aiworkhub.roadmap_snapshot.v1",
        "available": True,
        "total": total,
        "active": sum(
            count
            for status, count in status_counts.items()
            if status not in {"completed", "archived"}
        ),
        "status_counts": status_counts,
        "items": rows,
        "truncated": total > len(rows),
    }
