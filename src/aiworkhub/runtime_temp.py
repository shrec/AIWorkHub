"""Repository-owned request-scoped temp authority under ``.aiworkhub/temp``.

This module is the single authority for *disposable* per-request state:
validation exec scratch, per-request HOME/TMPDIR/mypy cache and stdio spools.
Every directory it creates is rooted at ``<repo>/.aiworkhub/temp`` and
namespaced by request identity plus a compact owner manifest carrying the
creating process's PID and Linux start-time, so two repositories and
concurrent instances never share mutable temp state.

Cleanup policy (acceptance boundaries):

* Accepted/promoted and explicitly discarded attempts call
  :func:`dispose_request_temp` immediately.
* Review/rework/validation-failed candidates remain pinned until explicit
  disposition; nothing here deletes them.
* Startup/periodic GC is performed *only* by ``terminal_log_retention`` (the
  sole cleanup authority).  It consumes :func:`identify_dead_owner_dirs`,
  which returns exact dead-owner directories identified by PID/start identity
  and never a live or unknown owner.  This module performs no background GC.

Linux abstract ``AF_UNIX`` sockets remain transport-only; registry/capability/
metadata always live under the repository-local authority, never in shared
system temp.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

# ``worker_supervisor`` runs as a DIRECT SCRIPT, not as a package module, and it
# imports this module through the same fallback below.  In that mode this module
# is itself top-level, so a relative import here fails and takes the supervisor
# down with it - the one process whose job is to stop workers being orphaned.
# Mirror the supervisor's own pattern rather than assuming package context.
try:
    from .platform_io import process_is_alive
except ImportError:  # direct-script entrypoint (see worker_supervisor)
    from platform_io import process_is_alive  # type: ignore[no-redef]

TEMP_RELATIVE_PATH = Path(".aiworkhub/temp")
TEMP_ROOT_ENV = "AIWORKHUB_TEMP_ROOT"
SCHEMA_ID = "aiworkhub.runtime_temp.v1"
OWNER_SCHEMA_ID = "aiworkhub.runtime_temp_owner.v1"
OWNER_MANIFEST_NAME = "owner.json"

WORKER_NAMESPACE = "worker"
VALIDATION_NAMESPACE = "validation"
STDIO_NAMESPACE = "stdio"
_ALLOWED_NAMESPACES = frozenset({WORKER_NAMESPACE, VALIDATION_NAMESPACE, STDIO_NAMESPACE})

# Bounded per-repo retention/quota.  Long-term state is compact manifests and
# hashes; these ceilings are a backstop, not a daily driver.
DEFAULT_MAX_DIRS_PER_REPO = 4096
DEFAULT_MAX_BYTES_PER_REPO = 4 * 1024 * 1024 * 1024  # 4 GiB
MAX_MANIFEST_BYTES = 16 * 1024

_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
# Request ids are coordinator-chosen and include "needfix-NF-...", "union_...",
# and 32-hex terminal ids; keep the contract permissive but bounded.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")

# Deterministic request-scoped subdirectories provisioned by this authority.
HOME_SUBDIR = "home"
TMP_SUBDIR = "tmp"
MYPY_CACHE_SUBDIR = "mypy_cache"
STDIO_SUBDIR = "stdio"
_DEFAULT_SUBDIRS = (HOME_SUBDIR, TMP_SUBDIR, MYPY_CACHE_SUBDIR, STDIO_SUBDIR)


class RuntimeTempError(RuntimeError):
    pass


def _default_now_utc() -> datetime:
    return datetime.now(timezone.utc)


# Injectable reference clock. Tests monkeypatch ``now_utc`` (or reassign
# ``_now_func``) to make manifest/retention timestamps deterministic without
# mutating filesystem mtimes. Production uses the real UTC clock.
_now_func: Callable[[], datetime] = _default_now_utc


def now_utc() -> datetime:
    """Current UTC time via the injectable clock."""
    return _now_func()


@dataclass(frozen=True)
class RequestTemp:
    """The request-scoped directory layout owned by this authority."""

    root: Path
    home: Path
    tmp: Path
    mypy_cache: Path
    stdio: Path
    request_id: str
    namespace: str
    repo: Path


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_namespace(namespace: str) -> str:
    if namespace not in _ALLOWED_NAMESPACES and not _NAMESPACE_RE.fullmatch(namespace):
        raise RuntimeTempError(f"runtime_temp_namespace_invalid:{namespace}")
    return namespace


def _validate_request_id(request_id: str) -> str:
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise RuntimeTempError(f"runtime_temp_request_id_invalid:{request_id!r}")
    return request_id


def temp_root(repo: Path | None = None) -> Path:
    """Resolve ``<repo>/.aiworkhub/temp``, symlink/escape fail-closed.

    An explicit ``AIWORKHUB_TEMP_ROOT`` override is authoritative for operator
    pinning; otherwise the repository-owned path is used.  Never returns a
    shared system temp location and never follows a symlink.
    """
    override = os.environ.get(TEMP_ROOT_ENV, "").strip()
    if override:
        root = Path(override).expanduser()
        if root.is_symlink():
            raise RuntimeTempError(f"runtime_temp_root_symlink_forbidden:{root}")
        return root.resolve(strict=False)
    if repo is None:
        raise RuntimeTempError("runtime_temp_root_requires_repository_identity")
    repo = Path(repo).resolve()
    hub = repo / ".aiworkhub"
    raw = hub / "temp"
    if hub.is_symlink() or raw.is_symlink():
        raise RuntimeTempError(f"runtime_temp_root_symlink_forbidden:{raw}")
    return raw


def namespace_root(repo: Path, namespace: str) -> Path:
    return temp_root(repo) / _validate_namespace(namespace)


def request_dir(repo: Path, request_id: str, namespace: str) -> Path:
    return namespace_root(repo, namespace) / _validate_request_id(request_id)


def _proc_stat_starttime(pid: int) -> int | None:
    """Return the Linux ``/proc/<pid>/stat`` starttime (clock ticks), or None."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return None
    close = data.rfind(")")
    if close < 0:
        return None
    fields = data[close + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        # Field 22 (1-indexed) is starttime; after the parenthesised comm it
        # lands at index 19 of the remaining whitespace-split fields.
        return int(fields[19])
    except (ValueError, IndexError):
        return None


def _libc_with_sysctl() -> Any | None:
    """Return a libc handle exposing ``sysctl``, or None when unavailable."""
    for name in ("libc.dylib", "libSystem.dylib", None):
        try:
            lib = ctypes.CDLL(name, use_errno=True)
        except (OSError, TypeError):
            continue
        if hasattr(lib, "sysctl"):
            return lib
    return None


def _darwin_start_ticks_from_kinfo(raw: bytes) -> int | None:
    """Parse ``kp_proc.p_starttime`` from the head of a ``kinfo_proc`` record.

    ``p_starttime`` is a ``struct timeval`` at offset 0 of ``kinfo_proc`` on
    64-bit Darwin -- it is the leading union member of ``struct extern_proc``:
    an 8-byte little-endian ``tv_sec`` followed by a 4-byte ``tv_usec``.  Only
    this 12-byte prefix is read, so the full, version-sensitive struct layout
    is never depended upon.  Returns microseconds since the epoch, or None when
    the bytes are too short or not a plausible wall-clock time.  Kept as a pure
    function so the byte parsing is testable on any platform.
    """
    if len(raw) < 12:
        return None
    tv_sec = int.from_bytes(raw[0:8], "little", signed=True)
    tv_usec = int.from_bytes(raw[8:12], "little", signed=True)
    if tv_sec <= 0 or not (0 <= tv_usec < 1_000_000):
        return None
    return tv_sec * 1_000_000 + tv_usec


def _darwin_start_ticks(pid: int) -> int | None:
    """Darwin process creation time via ``sysctl kern.proc.pid`` (no /proc).

    Dependency-free: reads ``kp_proc.p_starttime`` straight from the kernel
    through libc's ``sysctl`` with mib ``[CTL_KERN, KERN_PROC, KERN_PROC_PID,
    pid]``.  No third-party module and no subprocess.  Returns None for an
    unknown/dead pid or when the syscall is unavailable, so callers fail
    closed rather than treating an absence as a match.
    """
    libc = _libc_with_sysctl()
    if libc is None:
        return None
    libc.sysctl.restype = ctypes.c_int
    libc.sysctl.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    ctl_kern, kern_proc, kern_proc_pid = 1, 14, 1
    mib = (ctypes.c_int * 4)(ctl_kern, kern_proc, kern_proc_pid, int(pid))
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0:
        return None
    if size.value < 12:
        return None
    buf = (ctypes.c_char * size.value)()
    if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0:
        return None
    if size.value < 12:
        return None  # process vanished; the kernel returned an empty record
    return _darwin_start_ticks_from_kinfo(bytes(buf.raw[:12]))


def _windows_start_ticks(pid: int) -> int | None:
    """Windows process creation ``FILETIME`` via ``GetProcessTimes``."""
    class _FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    creation = _FileTime()
    exit_time = _FileTime()
    kernel = _FileTime()
    user = _FileTime()
    try:
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        return (int(creation.high) << 32) | int(creation.low)
    finally:
        kernel32.CloseHandle(handle)


def process_start_ticks(pid: int) -> int | None:
    """Stable per-process creation stamp, or None when unavailable.

    This is the single cross-platform process-identity primitive shared by the
    launcher, the standalone supervisor, and this module's temp-owner GC, so
    those paths can never drift.  The value is an opaque integer compared only
    for equality against another value read the same way on the same host; its
    units differ by platform and must never be interpreted across platforms or
    used in arithmetic:

    * Linux   -- field 22 of ``/proc/<pid>/stat`` (clock ticks since boot).
    * Darwin  -- ``kp_proc.p_starttime`` via ``sysctl kern.proc.pid``
      (microseconds since the epoch); Darwin has no ``/proc``.
    * Windows -- process creation ``FILETIME`` via ``GetProcessTimes``.

    Returns None only where the host genuinely cannot supply a creation time
    (an unknown/dead pid, or a future platform with no supported source).
    Callers must treat None as "identity unknown", fail closed, and never as a
    match -- a bare pid is not an identity because the OS recycles pids.
    """
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            return _windows_start_ticks(pid)
        except OSError:
            return None
    if sys.platform == "darwin":
        return _darwin_start_ticks(pid)
    return _proc_stat_starttime(pid)


def _self_starttime() -> int:
    value = process_start_ticks(os.getpid())
    return value if value is not None else 0


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (json.dumps(dict(value), sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_MANIFEST_BYTES:
        raise RuntimeTempError("runtime_temp_manifest_too_large")
    fd, name = tempfile.mkstemp(prefix=".runtime-temp-", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        # mkstemp already opens 0600; the explicit chmod is best-effort
        # defense-in-depth and must not fail on chmod-restricted filesystems.
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        temp.unlink(missing_ok=True)


def _repo_fingerprint(repo: Path) -> str:
    return hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:16]


def write_owner_manifest(root: Path, request_id: str, repo: Path) -> dict[str, Any]:
    """Write the compact PID/start-time owner stamp for one request dir."""
    manifest = {
        "schema_id": OWNER_SCHEMA_ID,
        "request_id": _validate_request_id(request_id),
        "repo_id": _repo_fingerprint(Path(repo).resolve()),
        "pid": os.getpid(),
        "starttime": _self_starttime(),
        "created_at": now_utc().isoformat(),
        "namespace": root.parent.name if root.parent else "",
    }
    _atomic_json(root / OWNER_MANIFEST_NAME, manifest)
    return manifest


def _manifest_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_nlink),
        int(info.st_uid),
        int(info.st_gid),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def read_owner_manifest(directory: Path) -> dict[str, Any] | None:
    """Return a valid owner manifest, or None for an unknown/invalid owner."""
    path = Path(directory) / OWNER_MANIFEST_NAME
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size > MAX_MANIFEST_BYTES
        ):
            return None

        pre_identity = _manifest_identity(info)
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        after = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or _manifest_identity(opened) != pre_identity
            or _manifest_identity(after) != pre_identity
            or opened.st_size > MAX_MANIFEST_BYTES
        ):
            return None

        raw = os.read(fd, MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            return None

        reread_opened = os.fstat(fd)
        reread_path = path.lstat()
        if (
            not stat.S_ISREG(reread_opened.st_mode)
            or not stat.S_ISREG(reread_path.st_mode)
            or _manifest_identity(reread_opened) != pre_identity
            or _manifest_identity(reread_path) != pre_identity
            or reread_opened.st_size > MAX_MANIFEST_BYTES
        ):
            return None

        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if not isinstance(value, dict) or value.get("schema_id") != OWNER_SCHEMA_ID:
        return None
    return value


# Liveness is one function, imported -- never a private copy. This module used
# to keep its own probe that answered ``None`` for PID 1 (a third answer to the
# one question). ``platform_io.process_is_alive`` is the shared implementation
# (EPERM == alive). Bound to ``_pid_alive`` so ``owner_alive`` and the tests
# that monkeypatch this name resolve to the shared function.
_pid_alive = process_is_alive


def owner_alive(manifest: Mapping[str, Any]) -> bool:
    """True unless the recorded owner process is provably dead.

    Fail-closed on any absence of identity.  An owner we cannot identify --
    no usable PID at all -- must never have its runtime directory reclaimed,
    so it answers *alive*; absence of identity is not evidence of death.  A
    live PID (or an undeterminable one) also fails closed to *alive*.  Only a
    PID that is provably dead reclaims, and even then we guard against PID
    reuse by comparing the recorded start-time identity with the current
    owner's -- a mismatch means the PID was recycled and the directory is
    foreign, so it stays.
    """
    try:
        pid = int(manifest.get("pid") or 0)
        starttime = int(manifest.get("starttime") or 0)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True  # no identifiable owner -> fail closed, never delete
    # Only a definitive ``False`` (provably dead) may reclaim; alive and every
    # undeterminable answer fail closed to alive so a live or unknown owner is
    # never deleted. (A monkeypatched probe may still return None.)
    if _pid_alive(pid) is not False:
        return True
    # The PID is currently dead.  If we can still read a start-time for a
    # recycled PID that differs from ours, treat the directory as foreign.
    current = process_start_ticks(pid)
    if current is not None and current != starttime:
        return True
    return False


def _iter_request_dirs(root: Path) -> list[Path]:
    """Enumerate request dirs under ``<root>/<namespace>/<request>``, symlink-free."""
    result: list[Path] = []
    if not root.is_dir() or root.is_symlink():
        return result
    try:
        namespace_entries = sorted(root.iterdir())
    except OSError:
        return result
    for namespace_dir in namespace_entries:
        if namespace_dir.is_symlink() or not namespace_dir.is_dir():
            continue
        if not _NAMESPACE_RE.fullmatch(namespace_dir.name):
            continue
        try:
            request_entries = sorted(namespace_dir.iterdir())
        except OSError:
            continue
        for request_dir in request_entries:
            if request_dir.is_symlink() or not request_dir.is_dir():
                continue
            result.append(request_dir)
    return result


def identify_dead_owner_dirs(
    repo: Path, *, namespace: str | None = None
) -> list[dict[str, Any]]:
    """Return exact dead-owner request directories (read-only identification).

    A directory without a valid owner manifest is an *unknown* owner and is
    never returned.  A live (or undeterminable) owner is never returned.
    """
    root = temp_root(repo)
    result: list[dict[str, Any]] = []
    for request_dir in _iter_request_dirs(root):
        if namespace is not None and request_dir.parent.name != namespace:
            continue
        manifest = read_owner_manifest(request_dir)
        if manifest is None:
            continue
        if owner_alive(manifest):
            continue
        result.append({
            "path": request_dir,
            "request_id": str(manifest.get("request_id") or request_dir.name),
            "namespace": str(manifest.get("namespace") or request_dir.parent.name),
            "pid": int(manifest.get("pid") or 0),
            "starttime": int(manifest.get("starttime") or 0),
            "bytes": _dir_size(request_dir),
        })
    return result


def _dir_size(directory: Path) -> int:
    total = 0
    try:
        for parent, dirnames, filenames in os.walk(directory, followlinks=False):
            parent_path = Path(parent)
            dirnames[:] = [
                name for name in dirnames if not (parent_path / name).is_symlink()
            ]
            for name in filenames:
                path = parent_path / name
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    continue
                total += int(info.st_size)
    except OSError:
        return 0
    return total


def dispose_request_temp(
    path: Path,
    *,
    repo: Path | None = None,
    expected_request_id: str | None = None,
) -> bool:
    """Immediate cleanup for accepted/discarded attempts.  Fail-closed.

    Never follows a symlink and never deletes outside the repository-owned
    temp authority.  Returns True only when the exact request directory was
    removed.
    """
    if path is None:
        return False
    path = Path(path)
    if path.is_symlink():
        return False
    resolved = path.resolve(strict=False)
    if repo is not None:
        root = temp_root(repo)
        if not _path_is_relative_to(resolved, root):
            raise RuntimeTempError(f"runtime_temp_dispose_escape:{path}")
    if expected_request_id is not None:
        manifest = read_owner_manifest(path)
        recorded = (manifest or {}).get("request_id") or path.name
        if recorded != expected_request_id:
            raise RuntimeTempError(
                f"runtime_temp_dispose_identity_mismatch:{recorded}"
            )
    if not resolved.is_dir():
        return False
    shutil.rmtree(resolved, ignore_errors=False)
    return True


def provision_request_temp(
    repo: Path,
    request_id: str,
    *,
    namespace: str = WORKER_NAMESPACE,
    subdirs: tuple[str, ...] = _DEFAULT_SUBDIRS,
) -> RequestTemp:
    """Provision (or reuse) one request-scoped temp layout under the authority.

    Fail-closed on symlink/escape, namespaced by request identity, and stamped
    with a compact PID/start-time owner manifest for the sole GC authority.
    """
    repo = Path(repo).resolve()
    request_id = _validate_request_id(request_id)
    namespace = _validate_namespace(namespace)
    root = request_dir(repo, request_id, namespace)
    if root.is_symlink():
        raise RuntimeTempError(f"runtime_temp_request_dir_symlink_forbidden:{root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    resolved = root.resolve()
    if resolved.is_symlink() or not resolved.is_dir():
        raise RuntimeTempError(f"runtime_temp_request_dir_invalid:{root}")
    paths: dict[str, Path] = {}
    for sub in subdirs:
        if not _NAMESPACE_RE.fullmatch(sub):
            raise RuntimeTempError(f"runtime_temp_subdir_invalid:{sub}")
        sub_path = resolved / sub
        if sub_path.is_symlink():
            raise RuntimeTempError(f"runtime_temp_subdir_symlink_forbidden:{sub_path}")
        sub_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(sub_path, 0o700)
        except OSError:
            pass
        paths[sub] = sub_path
    write_owner_manifest(resolved, request_id, repo)
    return RequestTemp(
        root=resolved,
        home=paths.get(HOME_SUBDIR, resolved),
        tmp=paths.get(TMP_SUBDIR, resolved),
        mypy_cache=paths.get(MYPY_CACHE_SUBDIR, resolved),
        stdio=paths.get(STDIO_SUBDIR, resolved),
        request_id=request_id,
        namespace=namespace,
        repo=repo,
    )


def quota(repo: Path) -> dict[str, Any]:
    """Bounded per-repo accounting: compact counts and byte totals only."""
    root = temp_root(repo)
    request_dirs = _iter_request_dirs(root)
    dirs = len(request_dirs)
    total_bytes = sum(_dir_size(directory) for directory in request_dirs)
    return {
        "schema_id": SCHEMA_ID,
        "dirs": dirs,
        "bytes": total_bytes,
        "max_dirs": DEFAULT_MAX_DIRS_PER_REPO,
        "max_bytes": DEFAULT_MAX_BYTES_PER_REPO,
        "within_quota": (
            dirs <= DEFAULT_MAX_DIRS_PER_REPO
            and total_bytes <= DEFAULT_MAX_BYTES_PER_REPO
        ),
    }


__all__ = [
    "HOME_SUBDIR",
    "MYPY_CACHE_SUBDIR",
    "OWNER_MANIFEST_NAME",
    "OWNER_SCHEMA_ID",
    "RequestTemp",
    "RuntimeTempError",
    "SCHEMA_ID",
    "STDIO_NAMESPACE",
    "STDIO_SUBDIR",
    "TEMP_RELATIVE_PATH",
    "TEMP_ROOT_ENV",
    "TMP_SUBDIR",
    "VALIDATION_NAMESPACE",
    "WORKER_NAMESPACE",
    "dispose_request_temp",
    "identify_dead_owner_dirs",
    "namespace_root",
    "now_utc",
    "owner_alive",
    "process_start_ticks",
    "provision_request_temp",
    "quota",
    "read_owner_manifest",
    "request_dir",
    "temp_root",
    "write_owner_manifest",
]
