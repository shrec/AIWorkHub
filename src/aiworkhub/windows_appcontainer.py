"""Fail-closed Windows AppContainer launch foundation for native workers.

This module provides a standalone, dependency-free way to launch a native
AIWorkHub worker (Claude CLI or Grok/Kilo CLI) inside a repo-scoped Windows
AppContainer.  It derives a deterministic AppContainer identity, builds the
``SECURITY_CAPABILITIES`` / ``STARTUPINFOEX`` attributes, launches the exact
argv without any shell parsing, assigns the child to a kill-on-close Job
Object *before* the launch is treated as successful, and returns structured
handles plus cleanup evidence.

The module is import-safe on non-Windows hosts: no Windows-only symbol is
resolved at import time.  ``platform_supported`` and ``probe`` report the
platform as unsupported without touching ``ctypes.WinDLL``.  The orchestration
in :func:`launch_appcontainer` talks to a small :class:`Win32Api` boundary so
that every partial-initialization failure can be exercised with mocked Windows
APIs and every SID / attribute-list / job / process / thread handle is unwound
on failure, never leaving a child outside its job.

This foundation intentionally does *not* remove or bypass the existing runtime
gate; it will be wired in only after independent acceptance.
"""

from __future__ import annotations

import base64
import ctypes
import enum
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, cast

try:
    from .windows_job_structures import JOBOBJECT_EXTENDED_LIMIT_INFORMATION
except ImportError:  # direct-script entrypoint
    from windows_job_structures import JOBOBJECT_EXTENDED_LIMIT_INFORMATION

__all__ = [
    "AppContainerError",
    "AppContainerLaunch",
    "AppContainerLifecycleResult",
    "AppContainerLifecycleState",
    "AppContainerProbe",
    "AppContainerReason",
    "AppContainerRequest",
    "AclAce",
    "AclSnapshot",
    "AclSnapshotError",
    "ContainerGrant",
    "DaclState",
    "Win32Api",
    "build_command_line",
    "derive_container_identity",
    "launch_appcontainer",
    "platform_supported",
    "probe",
    "snapshot_filesystem_acl",
]


# ---------------------------------------------------------------------------
# Structured reason taxonomy
# ---------------------------------------------------------------------------


class AppContainerReason(str, enum.Enum):
    """Exact, structured failure reasons suitable for Preflight routing.

    The string values are stable identifiers; callers may compare against the
    enum members or their string values interchangeably.
    """

    PLATFORM_UNSUPPORTED = "platform_unsupported"
    INVALID_REQUEST = "invalid_request"
    INVALID_ARGV = "invalid_argv"
    INVALID_ENVIRONMENT = "invalid_environment"
    CAPABILITY_DERIVATION_FAILED = "capability_derivation_failed"
    PROFILE_CREATION_FAILED = "profile_creation_failed"
    SECURITY_CAPABILITIES_FAILED = "security_capabilities_failed"
    ATTRIBUTE_LIST_INIT_FAILED = "attribute_list_init_failed"
    ATTRIBUTE_LIST_UPDATE_FAILED = "attribute_list_update_failed"
    JOB_CREATE_FAILED = "job_object_create_failed"
    JOB_CONFIGURE_FAILED = "job_object_configure_failed"
    JOB_ASSIGNMENT_FAILED = "job_assignment_failed"
    ACCESS_DENIED = "access_denied"
    PROCESS_LAUNCH_FAILED = "process_launch_failed"
    LAUNCH_FAILED = "launch_failed"
    FILESYSTEM_GRANT_FAILED = "filesystem_grant_failed"


# Maps a low-level Win32 operation name to its structured reason.  The
# operation names are also the boundary method call-sites, which keeps the
# taxonomy in exactly one place.
_OPERATION_REASON: dict[str, AppContainerReason] = {
    "grant_path_access": AppContainerReason.FILESYSTEM_GRANT_FAILED,
    "create_appcontainer_profile": AppContainerReason.PROFILE_CREATION_FAILED,
    "derive_appcontainer_sid": AppContainerReason.CAPABILITY_DERIVATION_FAILED,
    "derive_capability_sids": AppContainerReason.CAPABILITY_DERIVATION_FAILED,
    "build_security_capabilities":
        AppContainerReason.SECURITY_CAPABILITIES_FAILED,
    "create_job_object": AppContainerReason.JOB_CREATE_FAILED,
    "configure_job_object": AppContainerReason.JOB_CONFIGURE_FAILED,
    "init_attribute_list": AppContainerReason.ATTRIBUTE_LIST_INIT_FAILED,
    "set_security_capabilities":
        AppContainerReason.ATTRIBUTE_LIST_UPDATE_FAILED,
    "set_inherited_handles": AppContainerReason.ATTRIBUTE_LIST_UPDATE_FAILED,
    "create_process": AppContainerReason.PROCESS_LAUNCH_FAILED,
    "assign_process_to_job": AppContainerReason.JOB_ASSIGNMENT_FAILED,
    "resume_thread": AppContainerReason.PROCESS_LAUNCH_FAILED,
}


class AppContainerError(RuntimeError):
    """Raised when an AppContainer launch cannot complete.

    Carries the structured :class:`AppContainerReason`, the offending Win32
    operation, and the underlying ``GetLastError``/``HRESULT`` value when one
    is available.
    """

    def __init__(
        self,
        reason: AppContainerReason,
        *,
        detail: str = "",
        operation: str | None = None,
        win_error: int | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.operation = operation
        self.win_error = win_error
        message = reason.value if not detail else f"{reason.value}: {detail}"
        super().__init__(message)


class _Win32Failure(Exception):
    """Low-level failure raised by the Win32 boundary.

    ``operation`` identifies the failing call so the orchestrator can map it to
    a structured :class:`AppContainerReason`.  This exception never escapes the
    module; :func:`launch_appcontainer` translates it into an
    :class:`AppContainerError`.
    """

    def __init__(
        self, win_error: int, operation: str, detail: str = ""
    ) -> None:
        self.win_error = win_error
        self.operation = operation
        self.detail = detail
        super().__init__(f"{operation} failed (win_error={win_error})")


def _map_reason(operation: str | None, win_error: int | None) -> AppContainerReason:
    if (
        operation == "create_process"
        and win_error == _ERROR_ACCESS_DENIED
    ):
        return AppContainerReason.ACCESS_DENIED
    if operation is None:
        return AppContainerReason.LAUNCH_FAILED
    return _OPERATION_REASON.get(operation, AppContainerReason.LAUNCH_FAILED)


# ---------------------------------------------------------------------------
# Public request / result data
# ---------------------------------------------------------------------------


# Access masks written into the container's ACE.  "read_execute" is icacls RX
# (FILE_GENERIC_READ | FILE_GENERIC_EXECUTE); "modify" is icacls M, which adds
# FILE_GENERIC_WRITE and DELETE but never WRITE_DAC or WRITE_OWNER, so the
# container can use a granted tree and never re-permission it.
_GRANT_ACCESS_MASKS: dict[str, int] = {
    "read_execute": 0x001200A9,
    "modify": 0x001301BF,
}


@dataclass(frozen=True)
class ContainerGrant:
    """One filesystem path this launch's own container SID may use.

    ``access`` is ``"read_execute"`` or ``"modify"``.  A grant is revoked --
    this container SID's explicit ACEs removed from the path again -- when the
    launch closes or fails, unless ``persistent`` is set (read only; see
    :func:`launch_appcontainer` for when that is the right choice).
    """

    path: str
    access: str
    persistent: bool = False


# Where a launcher names a request's isolated HOME and its request temp.
_REQUEST_SCOPED_ENV_KEYS = ("HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR")


def request_scoped_grants(
    environment: Mapping[str, str], *paths: str
) -> list[ContainerGrant]:
    """Revocable modify grants on ``paths`` and on the HOME / temp directories
    ``environment`` names -- one per distinct path, in that order."""
    grants: list[ContainerGrant] = []
    seen: set[str] = set()
    for value in (*paths, *(environment.get(k, "") for k in _REQUEST_SCOPED_ENV_KEYS)):
        key = os.path.normcase(os.path.normpath(value)) if value else ""
        if key and key not in seen:
            seen.add(key)
            grants.append(ContainerGrant(value, "modify"))
    return grants


def outside_system_trees(grants: list[ContainerGrant]) -> list[ContainerGrant]:
    """``grants`` minus each one equal to or inside a system tree
    (%SystemRoot%, the Program Files roots -- :func:`_sensitive_roots`).

    Windows gives ALL APPLICATION PACKAGES read/execute there by default so
    that every AppContainer can load from them, and :func:`_validate_grants`
    refuses to touch them.  Omitting such a grant only ever withholds access,
    never widens it: on a hardened host without that default the child fails
    with access denied, which is fail-closed.
    """
    if not grants:
        return grants
    system = _sensitive_roots()[1]
    return [
        grant
        for grant in grants
        if not any(
            _within(os.path.normcase(os.path.normpath(grant.path)), root) for root in system
        )
    ]


# A directory holding only a ``sitecustomize`` that makes ``os.mkdir(path,
# 0o700)`` usable inside an AppContainer; a lane puts it first on a Python
# child's PYTHONPATH.  Why, and what it changes: appcontainer_site/sitecustomize.py.
APPCONTAINER_PYTHON_SITE = str(Path(__file__).absolute().with_name("appcontainer_site"))
# Carries :func:`ancestor_stat_facts` to that shim.
APPCONTAINER_ANCESTORS_ENV = "AIWORKHUB_APPCONTAINER_ANCESTORS"


def ancestor_stat_facts(path: str) -> str:
    """JSON: the host's ``lstat`` of every directory above ``path``, keyed by
    normcased path, as the fields ``os.stat_result`` is rebuilt from.

    A container cannot stat them -- measured: ``D:\\``, ``D:\\Dev`` and the
    worktree root WinError 5 -- and no grant can fix the drive root, so code
    that walks a path from its drive root (``repository_state``'s symlink
    check) refused every path.  ``path`` is a request's own directory, the
    lane granted it, and :func:`_validate_grants` proved nothing above it is a
    reparse point; the container cannot write any of them.
    """
    facts: dict[str, list[Any]] = {}
    for parent in Path(path).parents:
        st = os.lstat(parent)
        facts[os.path.normcase(str(parent))] = [
            st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
            st.st_size, st.st_atime, st.st_mtime, st.st_ctime,
            getattr(st, "st_file_attributes", 0), getattr(st, "st_reparse_tag", 0),
        ]
    return json.dumps(facts)


def is_python_executable(executable: str) -> bool:
    """A ``python*.exe`` -- the executables :func:`python_read_grants` serves."""
    exe = Path(executable)
    return exe.name.lower().startswith("python") and exe.suffix.lower() == ".exe"


def python_read_grants(
    executable: str, pythonpath: str = "", *, covered: Sequence[str] = ()
) -> list[ContainerGrant]:
    """Persistent read/execute on what a Python ``executable`` needs to run in
    a container; ``[]`` when it is no ``python*.exe``.

    A venv launcher needs its ``Scripts`` directory (itself, and the console
    scripts a ``-m`` tool runs: ``python -m ruff`` execs ``Scripts\\ruff.exe``),
    its ``pyvenv.cfg`` and ``Lib\\site-packages``, and the base interpreter's
    home that ``pyvenv.cfg`` names -- the launcher
    re-executes that interpreter, which loads its DLLs and standard library
    from there.  A plain interpreter needs its own install root.  Each
    absolute ``pythonpath`` entry is an import root too.  These are shared
    install roots, hence persistent (see :func:`launch_appcontainer`); one
    ALL APPLICATION PACKAGES can already read costs no write, and one in a
    system tree -- a Program Files install -- is omitted
    (:func:`outside_system_trees`).

    ``covered`` names what the request already grants -- the worktree, HOME
    and temp, all writable by the container.  An import root inside them is
    already reachable and is skipped.  But an interpreter or ``pyvenv.cfg``
    there, or a ``home`` it names there, is refused: a worker could plant
    them, and ``home`` would then steer a PERSISTENT grant anywhere.
    """
    exe = Path(executable)
    if not is_python_executable(executable):
        return []
    writable = [os.path.normcase(os.path.normpath(path)) for path in covered]

    def planted(path: Path) -> bool:
        key = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        return any(_within(key, root) for root in writable)

    config = exe.parent.parent / "pyvenv.cfg"
    roots: list[Path] = []
    if config.is_file():
        roots += [exe.parent, config, exe.parent.parent / "Lib" / "site-packages"]
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == "home" and value.strip():
                roots.append(Path(value.strip()))
    else:
        roots.append(exe.parent)
    if any(planted(root) for root in roots):
        raise AppContainerError(
            AppContainerReason.INVALID_REQUEST,
            detail=f"an interpreter the container can write is never granted: {executable!r}.",
        )
    roots += [Path(part) for part in pythonpath.split(os.pathsep) if os.path.isabs(part)]
    skip = list(writable)
    grants: list[ContainerGrant] = []
    for root in roots:
        key = os.path.normcase(os.path.normpath(str(root)))
        if root.exists() and not any(_within(key, other) for other in skip):
            skip.append(key)
            grants.append(ContainerGrant(str(root), "read_execute", persistent=True))
    return outside_system_trees(grants)


@dataclass(frozen=True)
class AppContainerRequest:
    """A fully specified, shell-free AppContainer launch request."""

    argv: Sequence[str]
    repo_id: str
    worker_kind: str
    executable: str | None = None
    working_directory: str | None = None
    environment: Mapping[str, str] | None = None
    stdin_handle: int | None = None
    stdout_handle: int | None = None
    stderr_handle: int | None = None
    capability_sids: Sequence[str] = ()
    create_no_window: bool = True
    filesystem_grants: Sequence[ContainerGrant] = ()
    # Protected directories beneath a revocable grant that stay closed to the
    # container: never granted, never walked (see _with_protected_descendants).
    withheld_directories: Sequence[str] = ()


@dataclass(frozen=True)
class AppContainerProbe:
    """Result of a side-effect-bounded capability probe."""

    available: bool
    reason: AppContainerReason | None
    detail: str


class AppContainerLifecycleState(str, enum.Enum):
    """Stable states returned by process lifecycle observations."""

    RUNNING = "running"
    EXITED = "exited"
    TIMEOUT = "timeout"
    CLOSED = "closed"
    ERROR = "error"


_TERMINATION_WAIT_MS = 5_000


@dataclass(frozen=True)
class AppContainerLifecycleResult:
    """One bounded observation of the exact process owned by a launch."""

    state: AppContainerLifecycleState
    exit_code: int | None = None
    win_error: int | None = None
    operation: str | None = None
    terminated: bool = False


@dataclass
class AppContainerLaunch:
    """A successfully launched, job-owned AppContainer child.

    The child is already assigned to a kill-on-close Job Object; the returned
    handles are owned by this object.  :meth:`close` releases them (closing the
    job handle tears down the whole tree), and :meth:`terminate` kills the tree
    immediately.  Both are idempotent.
    """

    pid: int
    process_id: int
    thread_id: int
    container_name: str
    container_sid: str
    creation_identity: str
    command_line: str
    api: "Win32Api" = field(repr=False)
    job: Any = field(repr=False)
    creation: "_ProcessCreation" = field(repr=False)
    # Revocable filesystem grants still in force, in the order applied.
    grants: list["_PathGrant"] = field(default_factory=list, repr=False)
    # Grants whose DACL restore failed on close: (path, win_error).
    grant_revoke_failures: list[tuple[str, int]] = field(
        default_factory=list, repr=False
    )
    # Persistent grants as applied: (path, satisfied_by or "granted").
    persistent_grants: list[tuple[str, str]] = field(default_factory=list, repr=False)
    closed: bool = field(default=False, repr=False)
    _process_handle_owned: bool = field(default=True, init=False, repr=False)
    _job_handle_owned: bool = field(default=True, init=False, repr=False)
    _termination_completed: bool = field(default=False, init=False, repr=False)
    _terminal_result: AppContainerLifecycleResult | None = field(
        default=None, init=False, repr=False
    )

    def poll(self) -> AppContainerLifecycleResult:
        """Observe the owned process without blocking."""
        return self.wait(0)

    def wait(
        self,
        timeout_ms: int,
        *,
        terminate_on_timeout: bool = False,
        terminate_exit_code: int = 1,
    ) -> AppContainerLifecycleResult:
        """Wait at most ``timeout_ms`` for the exact owned process handle."""
        if self._terminal_result is not None:
            return self._terminal_result
        if not self._process_handle_owned:
            return AppContainerLifecycleResult(AppContainerLifecycleState.CLOSED)
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
            raise TypeError("timeout_ms must be an integer")
        if not 0 <= timeout_ms <= _MAX_BOUNDED_WAIT_MS:
            raise ValueError("timeout_ms must be between 0 and 4294967294")
        try:
            signaled = self.api.wait_process(self.creation, timeout_ms)
            if signaled:
                result = AppContainerLifecycleResult(
                    AppContainerLifecycleState.EXITED,
                    exit_code=self.api.get_process_exit_code(self.creation),
                )
                self._terminal_result = result
                return result
        except _Win32Failure as exc:
            return AppContainerLifecycleResult(
                AppContainerLifecycleState.ERROR,
                win_error=exc.win_error,
                operation=exc.operation,
            )
        if terminate_on_timeout:
            self.cancel(terminate_exit_code)
            return AppContainerLifecycleResult(
                AppContainerLifecycleState.TIMEOUT,
                terminated=True,
            )
        state = (
            AppContainerLifecycleState.RUNNING
            if timeout_ms == 0
            else AppContainerLifecycleState.TIMEOUT
        )
        return AppContainerLifecycleResult(state)

    def exit_status(self) -> AppContainerLifecycleResult:
        """Return an exit code only after this same process handle signals."""
        return self.poll()

    def cancel(self, exit_code: int = 1) -> AppContainerLifecycleResult:
        """Explicitly kill the job-owned process tree and close resources."""
        return self.terminate(exit_code)

    def terminate(self, exit_code: int = 1) -> AppContainerLifecycleResult:
        """Kill the whole child tree without hiding its terminal outcome.

        The process handle remains owned until :meth:`wait` observes the
        native terminal state (or the caller explicitly closes the launch).
        This is deliberate: caching the requested termination code, or closing
        the process handle here, would let a Popen-shaped caller bypass the
        authoritative wait result.
        """
        if self.closed:
            return self.wait(0)
        first_error: Exception | None = None
        result: AppContainerLifecycleResult | None = None
        if self._job_handle_owned and not self._termination_completed:
            try:
                self.api.terminate_job(self.job, exit_code)
                self._termination_completed = True
                result = self.wait(_TERMINATION_WAIT_MS)
            except Exception as exc:
                first_error = exc
        try:
            self.close()
        except Exception as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error
        if result is None:
            result = self.wait(0)
        return result

    def close(self) -> None:
        """Release the process and job handles, then revoke the filesystem
        grants.  Idempotent."""
        if self.closed:
            return
        first_error: Exception | None = None
        if self._process_handle_owned:
            try:
                self.api.close_process_handle(self.creation)
                self._process_handle_owned = False
            except Exception as exc:
                first_error = exc
        if self._job_handle_owned:
            try:
                self.api.close_job(self.job)
                self._job_handle_owned = False
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        # Revoked even when a handle close failed: narrowing a possibly live
        # tree's access is always the safer direction.  Popping in reverse
        # order restores nested paths LIFO and makes a second close a no-op.
        while self.grants:
            grant = self.grants.pop()
            try:
                self.api.revoke_path_access(grant)
            except Exception:
                pass  # the boundary contract is never to raise; belt and braces
            if grant.revoke_error is not None:
                self.grant_revoke_failures.append((grant.path, grant.revoke_error))
        self.closed = not self._process_handle_owned and not self._job_handle_owned
        if first_error is not None:
            raise first_error

    def cleanup_evidence(self) -> dict[str, object]:
        """Structured, serializable evidence about the owned resources."""
        return {
            "pid": self.pid,
            "process_id": self.process_id,
            "thread_id": self.thread_id,
            "creation_identity": self.creation_identity,
            "container_name": self.container_name,
            "container_sid": self.container_sid,
            "closed": self.closed,
            "outstanding_grants": [grant.path for grant in self.grants],
            "grant_revoke_failures": [
                {"path": path, "win_error": error}
                for path, error in self.grant_revoke_failures
            ],
            "persistent_grants": [
                {"path": path, "satisfied_by": how} for path, how in self.persistent_grants
            ],
        }


# ---------------------------------------------------------------------------
# Opaque native records shared by the real and mocked Win32 boundary
# ---------------------------------------------------------------------------


@dataclass
class _Identity:
    name: str
    display_name: str
    sid_string: str
    sid_token: Any
    created_profile: bool


@dataclass
class _SecurityCapabilities:
    sid_string: str
    native: Any


@dataclass
class _AttributeList:
    native: Any
    keepalive: list[Any]
    attribute_count: int


@dataclass
class _ProcessCreation:
    process_id: int
    thread_id: int
    process_handle: Any
    thread_handle: Any


@dataclass
class _PathGrant:
    """One applied filesystem grant.  ``restore`` is the boundary's opaque
    undo state (for the ctypes boundary: the container SID whose explicit
    ACEs a revoke removes); ``None`` means there is nothing (left) to undo."""

    path: str
    access: str
    restore: Any = None
    revoke_error: int | None = None
    # A persistent grant the DACL already satisfied (nothing was written):
    # "container_sid" or "all_application_packages".  See _satisfying_trustee.
    satisfied_by: str = ""


@dataclass
class _ProcessSpec:
    executable: str | None
    command_line: str
    working_directory: str | None
    environment: Mapping[str, str] | None
    attribute_list: _AttributeList
    std_input: int | None
    std_output: int | None
    std_error: int | None
    creation_flags: int
    inherit_handles: bool


# ---------------------------------------------------------------------------
# Win32 boundary protocol (real and mocked implementations satisfy this)
# ---------------------------------------------------------------------------


class Win32Api(Protocol):
    """The bounded set of Windows operations used by the launcher.

    Every mutating call raises :class:`_Win32Failure` on error carrying the
    ``GetLastError``/``HRESULT`` value and the operation name.  Cleanup calls
    (``free_*``, ``delete_*``, ``close_*``, ``terminate_*``, ``revoke_*``) must
    be tolerant of being invoked during unwind and must not raise.
    """

    def derive_identity(
        self, name: str, display_name: str, description: str
    ) -> _Identity: ...

    def free_identity(self, identity: _Identity) -> None: ...

    def grant_path_access(
        self,
        identity: _Identity,
        path: str,
        access: str,
        *,
        persistent: bool = False,
    ) -> _PathGrant: ...

    def revoke_path_access(self, grant: _PathGrant) -> None: ...

    def dacl_protected(self, path: str) -> bool: ...

    def build_security_capabilities(
        self, identity: _Identity, capability_sids: Sequence[str]
    ) -> _SecurityCapabilities: ...

    def free_security_capabilities(
        self, sec_caps: _SecurityCapabilities
    ) -> None: ...

    def create_job_object(self, name: str) -> Any: ...

    def configure_job_object(self, job: Any) -> None: ...

    def init_attribute_list(self, attribute_count: int) -> _AttributeList: ...

    def set_security_capabilities(
        self, attrs: _AttributeList, sec_caps: _SecurityCapabilities
    ) -> None: ...

    def set_inherited_handles(
        self, attrs: _AttributeList, handles: Sequence[int]
    ) -> None: ...

    def delete_attribute_list(self, attrs: _AttributeList) -> None: ...

    def create_process(self, spec: _ProcessSpec) -> _ProcessCreation: ...

    def assign_process_to_job(
        self, job: Any, creation: _ProcessCreation
    ) -> None: ...

    def resume_thread(self, creation: _ProcessCreation) -> None: ...

    def terminate_process(self, creation: _ProcessCreation) -> None: ...

    def close_thread_handle(self, creation: _ProcessCreation) -> None: ...

    def close_process_handle(self, creation: _ProcessCreation) -> None: ...

    def wait_process(
        self, creation: _ProcessCreation, timeout_ms: int
    ) -> bool: ...

    def get_process_exit_code(self, creation: _ProcessCreation) -> int: ...

    def terminate_job(self, job: Any, exit_code: int = 1) -> None: ...

    def close_job(self, job: Any) -> None: ...


# ---------------------------------------------------------------------------
# Win32 constants (plain integers; safe to define on any platform)
# ---------------------------------------------------------------------------


CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_SE_GROUP_ENABLED = 0x00000004
_HANDLE_FLAG_INHERIT = 0x00000001
_ERROR_ACCESS_DENIED = 5
# HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS=183); 0x800700B7 as signed c_long.
_HRESULT_ALREADY_EXISTS = -0x7FF8FF49
# Exit code applied to a child forcibly terminated during failure unwind.
_UNWIND_KILL_EXIT_CODE = 1
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_MAX_BOUNDED_WAIT_MS = 0xFFFFFFFE


# ---------------------------------------------------------------------------
# ctypes structures (definitions only; no DLL is loaded at import time)
# ---------------------------------------------------------------------------


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", wintypes.LPVOID),
        ("Attributes", wintypes.DWORD),
    ]


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", wintypes.LPVOID),
        ("Capabilities", ctypes.POINTER(_SID_AND_ATTRIBUTES)),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _TRUSTEE_W(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", wintypes.LPVOID),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", wintypes.LPVOID),
    ]


class _EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", _TRUSTEE_W),
    ]


_GRANT_ACCESS = 1  # ACCESS_MODE.GRANT_ACCESS
_REVOKE_ACCESS = 4  # ACCESS_MODE.REVOKE_ACCESS: drop the trustee's allow ACEs
_TRUSTEE_IS_SID = 0
_SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3  # OBJECT_INHERIT | CONTAINER_INHERIT
_SE_DACL_PROTECTED = 0x1000
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
_ACCESS_ALLOWED_ACE_TYPE = 0
# OI | CI | NO_PROPAGATE | INHERIT_ONLY | INHERITED: an explicit, fully
# propagating ACE of ours has exactly the requested inheritance bits set here.
_ACE_INHERITANCE_FLAGS = 0x1F
_ACCESS_DENIED_ACE_TYPE = 1
_NO_PROPAGATE_INHERIT_ACE = 0x04
_INHERIT_ONLY_ACE = 0x08
# S-1-15-2-1, APPLICATION PACKAGE AUTHORITY\ALL APPLICATION PACKAGES.
_ALL_APPLICATION_PACKAGES_SID = b"\x01\x02\x00\x00\x00\x00\x00\x0f\x02\x00\x00\x00\x01\x00\x00\x00"


# Rights that let a holder change what a path is or what it holds: write and
# append data, write EA and attributes, delete child, DELETE, WRITE_DAC,
# WRITE_OWNER, GENERIC_ALL and GENERIC_WRITE.
_ANY_WRITE_RIGHTS = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x50000000
_PACKAGE_SID_PREFIX = b"\x00\x00\x00\x00\x00\x0f\x02\x00\x00\x00"  # S-1-15-2-*


def appcontainer_writers(path: str) -> list[str]:
    """The package SIDs -- an AppContainer, or ALL APPLICATION PACKAGES
    (S-1-15-2-*) -- that an allow ACE on ``path``, explicit or inherited,
    inherit-only or not, gives any right to change it or what it holds.
    ``[]`` off Windows.  A launch's revocable modify grant is exactly such an
    ACE until :meth:`AppContainerLaunch.close` -- which first closes the
    kill-on-close job -- revokes it."""
    if os.name != "nt":
        return []
    writers = []
    for ace in snapshot_filesystem_acl(path).aces:
        sid = ace.sid
        if (
            ace.ace_type == _ACCESS_ALLOWED_ACE_TYPE
            and ace.mask & _ANY_WRITE_RIGHTS
            and sid[2:12] == _PACKAGE_SID_PREFIX
        ):
            subauthorities = (
                int.from_bytes(sid[8 + 4 * i: 12 + 4 * i], "little") for i in range(sid[1])
            )
            writers.append("S-1-15-" + "-".join(map(str, subauthorities)))
    return writers


def _satisfying_trustee(
    aces: Sequence["AclAce"], sid: bytes, mask: int, inherit: int
) -> str:
    """Who already allows ``mask`` with ``inherit`` on this DACL: ``"container_sid"``
    (this SID's own explicit ACE), ``"all_application_packages"`` (an ALL
    APPLICATION PACKAGES ACE, explicit or inherited, that is not inherit-only
    and propagates at least as far as ``inherit`` asks), or ``""``.

    ACEs are read in stored order, as the kernel evaluates them: a deny ACE
    for this SID or for ALL APPLICATION PACKAGES that overlaps ``mask`` and
    comes before a satisfying allow means not satisfied.
    """
    for ace in aces:
        if (
            ace.ace_type == _ACCESS_DENIED_ACE_TYPE
            and ace.sid in (sid, _ALL_APPLICATION_PACKAGES_SID)
            and ace.mask & mask
        ):
            return ""
        if ace.ace_type != _ACCESS_ALLOWED_ACE_TYPE or ace.mask & mask != mask:
            continue
        if ace.sid == sid and ace.flags & _ACE_INHERITANCE_FLAGS == inherit:
            return "container_sid"
        if (
            ace.sid == _ALL_APPLICATION_PACKAGES_SID
            and not ace.flags & (_INHERIT_ONLY_ACE | _NO_PROPAGATE_INHERIT_ACE)
            and ace.flags & inherit == inherit
        ):
            return "all_application_packages"
    return ""


# The supervisor keeps 500 chars of "AppContainerError:filesystem_grant_failed:
# <detail>"; a hint this long survives that with room to spare.
_GRANT_HINT_MAX_CHARS = 400


def _all_packages_grant_hint(path: str) -> str:
    """The failure detail when this user lacks WRITE_DAC on a persistent read
    grant's install root: the one-time command an administrator runs so ALL
    APPLICATION PACKAGES can read it, after which the grant is satisfied and
    writes nothing.  Past the bound only the command itself is kept."""
    target = os.path.normpath(path)  # a trailing \ would escape the closing quote
    if os.path.isdir(target):
        command = f'icacls "{target}" /grant "*S-1-15-2-1:(OI)(CI)(RX)" /T'
    else:  # measured: on a file icacls drops an (OI)(CI) ACE yet reports success
        command = f'icacls "{target}" /grant "*S-1-15-2-1:(RX)"'
    detail = (
        f"cannot add AppContainer read access to {target}: this user lacks WRITE_DAC "
        f"there (win_error 5). Run once from an elevated shell: {command}"
    )
    return detail if len(detail) <= _GRANT_HINT_MAX_CHARS else command



# ---------------------------------------------------------------------------
# Argv-preserving Windows command line construction (no shell parsing)
# ---------------------------------------------------------------------------


def build_command_line(argv: Sequence[str]) -> str:
    """Build a Windows command line that round-trips ``argv`` verbatim.

    Uses the MSVCRT / ``CommandLineToArgvW`` quoting rules so the child process
    observes exactly ``argv`` with no shell interpretation.  Raises
    :class:`ValueError` for an empty argv.
    """
    if not argv:
        raise ValueError("argv must contain at least the executable")
    if any("\x00" in str(arg) for arg in argv):
        # Defense in depth: a NUL would truncate the command line at the ctypes
        # boundary, silently dropping trailing arguments.  Reject it here too so
        # the public helper never emits a truncatable command line.
        raise ValueError("argv elements must not contain embedded NUL")
    return " ".join(_quote_argument(str(arg)) for arg in argv)


def _quote_argument(arg: str) -> str:
    if arg and not _needs_quoting(arg):
        return arg
    out: list[str] = ['"']
    backslashes = 0
    for char in arg:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            # Escape all pending backslashes and the quote itself.
            out.append("\\" * (backslashes * 2 + 1))
            out.append('"')
            backslashes = 0
            continue
        if backslashes:
            out.append("\\" * backslashes)
            backslashes = 0
        out.append(char)
    # Backslashes immediately before the closing quote must be doubled.
    out.append("\\" * (backslashes * 2))
    out.append('"')
    return "".join(out)


def _needs_quoting(arg: str) -> bool:
    return any(char in arg for char in ' \t\n\v"')


# ---------------------------------------------------------------------------
# Deterministic, repo-scoped AppContainer identity
# ---------------------------------------------------------------------------


# AppContainer monikers are bounded to 64 characters.  Keep the full digest
# (which encodes the entire repo/worker identity) and truncate only the
# human-readable label so an arbitrarily long ``worker_kind`` can never
# overflow the limit while distinct kinds still resolve to distinct monikers.
_MONIKER_MAX_LENGTH = 64
_MONIKER_PREFIX = "aiworkhub."
_MONIKER_DIGEST_LENGTH = 20


def appcontainer_worker_kind(adapter_id: str) -> str:
    """Normalize an adapter id the way the supervisor does before deriving a SID.

    ``process_launcher_launch_isolated._appcontainer_supervisor_identity``
    applies exactly this transform to ``adapter_id`` and passes the result on
    as ``worker_kind``.  :func:`derive_container_identity` digests the *raw*
    string it is handed, so anything that has to land inside the worker's own
    AppContainer -- a validation command, say -- must normalize identically
    rather than pass an adapter id that merely happens to already be in normal
    form.  ``tests/test_process_launcher_appcontainer_spec.py`` pins the two
    against each other so they cannot drift apart silently.
    """
    return "_".join(adapter_id.lower().replace("-", "").split())


_LOCAL_APPDATA_ENV = "LOCALAPPDATA"


class _KnownFolderId(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


_FolderId = tuple[int, int, int, tuple[int, ...]]
_FOLDERID_LOCAL_APPDATA: _FolderId = (
    0xF1B32785, 0x6FBA, 0x4FCF, (0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91)
)
_FOLDERID_ROAMING_APPDATA: _FolderId = (
    0x3EB685DB, 0x65F9, 0x4CF6, (0xA0, 0x3A, 0xE3, 0xEF, 0x65, 0x72, 0x9F, 0x3D)
)
_FOLDERID_PROGRAM_FILES: _FolderId = (
    0x905E63B6, 0xC1BF, 0x494E, (0xB2, 0x9C, 0x65, 0xB7, 0x32, 0xD3, 0xD2, 0x1A)
)
_FOLDERID_PROGRAM_FILES_X86: _FolderId = (
    0x7C5A40EF, 0xA0FB, 0x4BFC, (0x87, 0x4A, 0xC0, 0xF2, 0xE0, 0xB9, 0xFA, 0x8E)
)


def _known_folder_local_appdata() -> str:
    """Resolve FOLDERID_LocalAppData without consulting LOCALAPPDATA.

    Measured: the shell still expands ``%USERPROFILE%\\AppData\\Local`` from
    the process environment, so under an isolated USERPROFILE this is ``""``.
    """
    return _known_folder_path(_FOLDERID_LOCAL_APPDATA)


def _known_folder_path(folder_id: _FolderId) -> str:
    """SHGetKnownFolderPath for ``folder_id``, or ``""``."""
    data1, data2, data3, data4 = folder_id
    folder = _KnownFolderId(data1, data2, data3, (ctypes.c_ubyte * 8)(*data4))
    shell32 = _load_windows_dll("shell32")
    ole32 = _load_windows_dll("ole32")
    get_path = shell32.SHGetKnownFolderPath
    get_path.argtypes = [
        ctypes.POINTER(_KnownFolderId),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_path.restype = ctypes.c_long
    free = ole32.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    raw = ctypes.c_void_p()
    status = get_path(ctypes.byref(folder), 0, None, ctypes.byref(raw))
    try:
        if status != 0 or not raw.value:
            return ""
        return ctypes.wstring_at(raw.value)
    finally:
        if raw.value:
            free(raw)


def resolve_local_appdata() -> str:
    """Return this user's LocalAppData directory, or ``""`` if unresolvable.

    The process environment is preferred; the known-folder API is the fallback
    so that a caller running under a sanitized allowlist environment -- the
    worker supervisor is exactly that -- still resolves the real directory.
    """
    value = os.environ.get(_LOCAL_APPDATA_ENV, "").strip()
    if value:
        return value
    if os.name != "nt":
        return ""
    try:
        return _known_folder_local_appdata()
    except (OSError, AttributeError, ValueError):
        return ""


def _token_profile_directory() -> str:
    """The REAL user's profile root, from the process token.

    Never USERPROFILE: a launcher points that at the isolated request home.
    Measured: this still answers ``C:\\Users\\<user>`` under an isolated
    USERPROFILE with no SystemDrive, where the known-folder API does not.
    """
    kernel32 = _load_windows_dll("kernel32")
    advapi32 = _load_windows_dll("advapi32")
    userenv = _load_windows_dll("userenv")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    userenv.GetUserProfileDirectoryW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    userenv.GetUserProfileDirectoryW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        return ""
    try:
        size = wintypes.DWORD(0)
        userenv.GetUserProfileDirectoryW(token, None, ctypes.byref(size))
        buffer = ctypes.create_unicode_buffer(max(size.value, 1))
        if not userenv.GetUserProfileDirectoryW(token, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(token)


def _system_windows_directory() -> str:
    kernel32 = _load_windows_dll("kernel32")
    kernel32.GetSystemWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel32.GetSystemWindowsDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetSystemWindowsDirectoryW(buffer, len(buffer))
    return buffer.value if 0 < length < len(buffer) else ""


_TOKEN_QUERY = 0x0008


def _sensitive_roots() -> tuple[list[str], list[str]]:
    """``(protected, system)`` trees guarding every grant (normcased).

    No grant, at any access level, may equal or contain a protected tree:
    the real profile root, its default AppData\\Local (+ Temp) and
    AppData\\Roaming, and whatever LocalAppData / RoamingAppData actually
    resolve to.  Grants INSIDE those stay legal -- the npm install and the
    per-request directories live there.  A system tree -- %SystemRoot% and
    the Program Files roots -- may not be touched at all: the container
    already reads it through ALL APPLICATION PACKAGES, and a user without
    WRITE_DAC there could not grant anyway.

    Anchored on the process token and the system, never on USERPROFILE or
    TEMP, which a launcher points at the request's own directories.  Fails
    closed if the profile is unknown.
    """
    if os.name != "nt":
        return [], []
    try:
        profile = _token_profile_directory()
        roaming = _known_folder_path(_FOLDERID_ROAMING_APPDATA)
        system = [
            _known_folder_path(_FOLDERID_PROGRAM_FILES),
            _known_folder_path(_FOLDERID_PROGRAM_FILES_X86),
            _system_windows_directory(),
        ]
    except (OSError, AttributeError, ValueError) as exc:
        raise AppContainerError(
            AppContainerReason.INVALID_REQUEST,
            detail=f"cannot resolve the protected trees that guard grants: {exc}",
        ) from exc
    if not profile:
        raise AppContainerError(
            AppContainerReason.INVALID_REQUEST,
            detail="cannot resolve the real user profile that guards grants.",
        )
    local = os.path.join(profile, "AppData", "Local")
    local_appdata = resolve_local_appdata()
    roots = [
        profile,
        local,
        os.path.join(local, "Temp"),
        os.path.join(profile, "AppData", "Roaming"),
        roaming,
    ]
    if local_appdata:
        roots += [local_appdata, os.path.join(local_appdata, "Temp")]

    def _normalized(paths: list[str]) -> list[str]:
        return sorted({os.path.normcase(os.path.normpath(p)) for p in paths if p})

    return _normalized(roots), _normalized(system)


def appcontainer_child_environment(
    environment: Mapping[str, str] | None,
    *,
    local_appdata: str | None = None,
) -> Mapping[str, str] | None:
    """Return ``environment`` guaranteed to carry ``LOCALAPPDATA``.

    Measured on Windows 11: ``CreateProcessW`` for an AppContainer token fails
    with ``ERROR_ENVVAR_NOT_FOUND`` (203) when the *child's* environment block
    lacks ``LOCALAPPDATA`` -- the caller's own environment does not matter.
    The system maps the container's private storage beneath
    ``%LOCALAPPDATA%\\Packages\\<moniker>``, so the variable is load-bearing
    for process creation itself.  Sanitized worker and validation environments
    drop it, which made every AppContainer launch from them fail at
    ``create_process`` while the identical request succeeded from a full
    interactive environment.

    Supplying the real path discloses nothing new -- ``USERPROFILE`` already
    names the same profile -- and grants no access: the container still reaches
    only what its ACLs allow.  ``None`` (inherit the caller's environment) and
    an environment that already carries the key are returned unchanged, as is
    one for which no value can be resolved.
    """
    if environment is None:
        return None
    if any(key.upper() == _LOCAL_APPDATA_ENV for key in environment):
        return environment
    value = resolve_local_appdata() if local_appdata is None else local_appdata
    if not value:
        return environment
    merged = dict(environment)
    merged[_LOCAL_APPDATA_ENV] = value
    return merged


def derive_container_identity(
    repo_id: str, worker_kind: str
) -> tuple[str, str, str]:
    """Derive a deterministic ``(name, display_name, description)`` triple.

    The AppContainer moniker is a repo-scoped, stable identifier derived from
    ``repo_id`` and ``worker_kind`` so re-launches reuse the same profile.  The
    result honours the AppContainer moniker constraints (<= 64 chars, RFC1035
    label characters) for an arbitrarily long ``worker_kind``: the digest binds
    the full identity, so truncating the label never collides distinct kinds.
    """
    if not repo_id:
        raise ValueError("repo_id must be a non-empty string")
    if not worker_kind:
        raise ValueError("worker_kind must be a non-empty string")
    kind = _normalize_kind(worker_kind)
    # Digest binds the full repo_id and raw worker_kind so two long kinds that
    # share a truncated label still resolve to distinct, stable monikers.
    digest = hashlib.sha256(
        f"{repo_id}\x00{worker_kind}".encode()
    ).hexdigest()[:_MONIKER_DIGEST_LENGTH]
    label_budget = _MONIKER_MAX_LENGTH - len(_MONIKER_PREFIX) - 1 - len(digest)
    label = _bound_label(kind, label_budget)
    name = f"{_MONIKER_PREFIX}{label}.{digest}"
    display_name = f"AIWorkHub {kind} worker"
    description = (
        f"Repo-scoped AppContainer for the AIWorkHub {kind} native worker "
        f"(repo {repo_id})."
    )
    return name, display_name, description


def _normalize_kind(worker_kind: str) -> str:
    if not worker_kind:
        raise ValueError("worker_kind must be a non-empty string")
    cleaned = "".join(
        char if char.isalnum() else "-" for char in worker_kind.lower()
    ).strip("-")
    return cleaned or "worker"


def _bound_label(label: str, max_length: int) -> str:
    """Deterministically truncate ``label`` to ``max_length`` characters.

    Never leaves a trailing ``-`` so the moniker stays a valid RFC1035 label;
    uniqueness is carried by the digest, not the (possibly truncated) label.
    """
    if len(label) <= max_length:
        return label
    return label[:max_length].strip("-") or "worker"


# ---------------------------------------------------------------------------
# Platform / capability probing (side-effect free on non-Windows)
# ---------------------------------------------------------------------------


def platform_supported() -> bool:
    """Return ``True`` only on a Windows host.  No Windows symbol is touched."""
    return os.name == "nt"


def probe(
    *, api_loader: Callable[[], Win32Api] | None = None
) -> AppContainerProbe:
    """Report AppContainer availability without launching anything.

    On non-Windows this short-circuits to ``platform_unsupported`` before any
    Windows-only symbol is referenced.  On Windows it attempts to resolve the
    required APIs and maps any failure onto the structured taxonomy.
    """
    if not platform_supported():
        return AppContainerProbe(
            available=False,
            reason=AppContainerReason.PLATFORM_UNSUPPORTED,
            detail="AppContainer launch requires Windows (os.name == 'nt').",
        )
    loader = api_loader if api_loader is not None else _load_win32
    try:
        loader()
    except _Win32Failure as exc:
        return AppContainerProbe(
            available=False,
            reason=_map_reason(exc.operation, exc.win_error),
            detail=exc.detail or exc.operation,
        )
    except AttributeError as exc:
        # A required Win32 export (e.g. CreateAppContainerProfile) is absent on
        # this host: ctypes raises AttributeError when resolving the missing
        # function pointer.  Convert it into the structured taxonomy instead of
        # letting a raw AttributeError escape the probe.
        return AppContainerProbe(
            available=False,
            reason=AppContainerReason.CAPABILITY_DERIVATION_FAILED,
            detail=f"required Win32 export unavailable: {exc}",
        )
    except OSError as exc:
        return AppContainerProbe(
            available=False,
            reason=AppContainerReason.CAPABILITY_DERIVATION_FAILED,
            detail=str(exc),
        )
    return AppContainerProbe(
        available=True,
        reason=None,
        detail="AppContainer APIs resolved.",
    )


# ---------------------------------------------------------------------------
# Cleanup stack: ordered, idempotent, category-aware unwind
# ---------------------------------------------------------------------------


class _CleanupAction:
    __slots__ = ("fn", "always", "done")

    def __init__(self, fn: Callable[[], None], always: bool) -> None:
        self.fn = fn
        self.always = always
        self.done = False


class _CleanupStack:
    """Records cleanup callbacks and runs them at most once, in reverse order.

    ``always`` actions (free SID, delete attribute list, close thread handle)
    run on both success and failure.  Non-``always`` actions (terminate child,
    close job) run only on failure so the owned resources survive a successful
    launch.  Exceptions raised by a cleanup callback are swallowed so a single
    failing free never aborts the rest of the unwind.
    """

    def __init__(self) -> None:
        self._actions: list[_CleanupAction] = []

    def push_always(self, fn: Callable[[], None]) -> None:
        self._actions.append(_CleanupAction(fn, always=True))

    def push_on_failure(self, fn: Callable[[], None]) -> None:
        self._actions.append(_CleanupAction(fn, always=False))

    def run_failure(self) -> None:
        for action in reversed(self._actions):
            self._run(action)

    def run_success(self) -> None:
        for action in reversed(self._actions):
            if action.always:
                self._run(action)

    @staticmethod
    def _run(action: _CleanupAction) -> None:
        if action.done:
            return
        action.done = True
        try:
            action.fn()
        except Exception:
            # Cleanup must never re-raise or it would abort the rest of the
            # unwind and risk leaking a handle or an out-of-job child.
            pass


# ---------------------------------------------------------------------------
# Launch orchestration
# ---------------------------------------------------------------------------


def launch_appcontainer(
    request: AppContainerRequest, *, api: Win32Api | None = None
) -> AppContainerLaunch:
    """Launch ``request.argv`` inside a repo-scoped Windows AppContainer.

    On success the child is already assigned to a kill-on-close Job Object and
    an :class:`AppContainerLaunch` is returned.  On any failure an
    :class:`AppContainerError` with a structured reason is raised after every
    SID / attribute-list / job / process / thread handle has been unwound.  The
    child is never left running outside its job.

    ``api`` may be supplied to inject a mocked Windows boundary; when omitted a
    real ctypes-backed boundary is loaded lazily (Windows only).
    """
    _validate_request(request)
    # Every AppContainer launch -- worker supervisor, validation lane, anything
    # later -- passes through here, so the LOCALAPPDATA requirement is met once
    # at the chokepoint instead of being remembered by each caller.  It runs
    # after validation so hostile keys are still refused first.
    child_environment = appcontainer_child_environment(request.environment)
    if child_environment is not request.environment:
        request = replace(request, environment=child_environment)

    if api is None:
        if not platform_supported():
            raise AppContainerError(
                AppContainerReason.PLATFORM_UNSUPPORTED,
                detail="AppContainer launch requires Windows (os.name=='nt').",
            )
        api = _load_win32()
    grant_plan = _with_protected_descendants(
        request.filesystem_grants, api, request.withheld_directories
    )

    name, display_name, description = derive_container_identity(
        request.repo_id, request.worker_kind
    )
    command_line = build_command_line(request.argv)
    executable = request.executable or str(request.argv[0])
    std_handles = _std_handle_list(request)
    creation_flags = _creation_flags(request)

    cleanup = _CleanupStack()
    creation: _ProcessCreation | None = None
    try:
        identity = _step(
            "derive_appcontainer_sid",
            lambda: api.derive_identity(name, display_name, description),
        )
        cleanup.push_always(lambda: api.free_identity(identity))

        # Grants go to exactly this launch's container SID -- never to ALL
        # APPLICATION PACKAGES -- and are in force before the child exists.
        # A revocable grant is undone on any failure below (LIFO, so nested
        # paths restore correctly) and otherwise by AppContainerLaunch.close.
        #
        # A revoke removes this SID's explicit ACEs from the path's CURRENT
        # DACL, so a concurrent edit by anyone else survives it and an ACE a
        # crashed launch left behind is cleaned up; the price is that a
        # revocable grant owns every explicit ACE of its SID on that path, so
        # revocable (per-request) and persistent (install root) paths must
        # stay disjoint, as the supervisor's wiring keeps them.
        #
        # Persistent vs revoked, measured on Windows 11 26200 (inheritable
        # grant, then revoke, DACL byte-exact afterwards, 3 runs each):
        #   whole npm global dir, 210 entries ...... grant ~20 ms, revoke ~20 ms
        #   one npm package (opencode-ai), 14 ...... grant ~2 ms,  revoke ~2 ms
        #   a .cmd shim (file) ..................... grant <1 ms,  revoke <1 ms
        #   worktree-sized tree, 1364 entries ...... grant ~130 ms, revoke ~120 ms
        # Cost alone never justifies persistence.  Sharing does: every worker
        # of one repo+kind uses the same SID and the same install root, so a
        # revocable grant there would let the first launch to close remove the
        # ACE from under a still-running sibling.  Provider
        # install roots (read-only, public code) are therefore granted
        # persistent + idempotent; the per-request worktree, HOME and temp --
        # used by exactly one launch at a time -- are always revoked.
        # ponytail: two *different* SIDs persistently granting the same root
        # within the same few ms can lose one ACE (read-modify-write DACL); that
        # launch fails closed with access denied and the next one re-grants.
        # Serialize grants behind a machine-wide mutex if that is ever seen.
        #
        # ``grant_plan`` is the request's grants plus, after each revocable
        # directory, the protected directories beneath it
        # (:func:`_with_protected_descendants`): each is one more revocable
        # grant through this same loop, so failure unwind and close() revoke
        # them exactly like the directory they came from.
        grants: list[_PathGrant] = []
        persistent_grants: list[tuple[str, str]] = []
        for grant in grant_plan:
            applied: _PathGrant = _step(
                "grant_path_access",
                partial(
                    api.grant_path_access,
                    identity,
                    grant.path,
                    grant.access,
                    persistent=grant.persistent,
                ),
            )
            if grant.persistent:
                persistent_grants.append((grant.path, applied.satisfied_by or "granted"))
            else:
                grants.append(applied)
                cleanup.push_on_failure(partial(api.revoke_path_access, applied))

        sec_caps = _step(
            "build_security_capabilities",
            lambda: api.build_security_capabilities(
                identity, request.capability_sids
            ),
        )
        # The capability SIDs derived above are OS-allocated and owned by us;
        # free them on both success and failure.  As an ``always`` action this
        # runs after CreateProcess has consumed the struct on the success path
        # and during unwind on any failure path.
        cleanup.push_always(lambda: api.free_security_capabilities(sec_caps))

        job = _step("create_job_object", lambda: api.create_job_object(name))
        cleanup.push_on_failure(lambda: api.close_job(job))
        _step("configure_job_object", lambda: api.configure_job_object(job))

        attrs = _step(
            "init_attribute_list",
            lambda: api.init_attribute_list(_attribute_count(std_handles)),
        )
        cleanup.push_always(lambda: api.delete_attribute_list(attrs))
        _step(
            "set_security_capabilities",
            lambda: api.set_security_capabilities(attrs, sec_caps),
        )
        if std_handles:
            _step(
                "set_inherited_handles",
                lambda: api.set_inherited_handles(attrs, std_handles),
            )

        spec = _ProcessSpec(
            executable=executable,
            command_line=command_line,
            working_directory=request.working_directory,
            environment=request.environment,
            attribute_list=attrs,
            std_input=request.stdin_handle,
            std_output=request.stdout_handle,
            std_error=request.stderr_handle,
            creation_flags=creation_flags,
            inherit_handles=bool(std_handles),
        )
        creation = _step("create_process", lambda: api.create_process(spec))
        # Bind for the closures below without tripping "possibly unbound".
        launched = creation
        cleanup.push_on_failure(lambda: api.terminate_process(launched))
        cleanup.push_always(lambda: api.close_thread_handle(launched))

        # Assign to the kill-on-close job *before* resuming so the full child
        # tree is owned before the caller ever sees a running process.
        _step(
            "assign_process_to_job",
            lambda: api.assign_process_to_job(job, launched),
        )
        _step("resume_thread", lambda: api.resume_thread(launched))
    except AppContainerError:
        cleanup.run_failure()
        raise

    cleanup.run_success()
    # `_step` returns Any, so mypy cannot narrow `creation` from the
    # try block's control flow alone; assert it for the type checker (it is
    # always assigned here, since any failure above re-raises before this
    # point is reached).
    assert creation is not None
    return AppContainerLaunch(
        pid=creation.process_id,
        process_id=creation.process_id,
        thread_id=creation.thread_id,
        container_name=name,
        container_sid=identity.sid_string,
        creation_identity=_creation_identity(name, creation),
        command_line=command_line,
        api=api,
        job=job,
        creation=creation,
        grants=grants,
        persistent_grants=persistent_grants,
    )


def _step(operation: str, thunk: Callable[[], Any]) -> Any:
    try:
        return thunk()
    except _Win32Failure as exc:
        # Prefer the precise low-level operation reported by the boundary (e.g.
        # "create_appcontainer_profile" or "derive_capability_sids") over the
        # coarse orchestration step so the taxonomy stays exact; fall back to
        # the step name only when the boundary supplied none.
        failed = exc.operation or operation
        raise AppContainerError(
            _map_reason(failed, exc.win_error),
            detail=exc.detail or failed,
            operation=failed,
            win_error=exc.win_error,
        ) from exc


def _validate_request(request: AppContainerRequest) -> None:
    if not request.argv:
        raise AppContainerError(
            AppContainerReason.INVALID_ARGV,
            detail="argv must contain at least the executable path.",
        )
    if any(not isinstance(arg, str) for arg in request.argv):
        raise AppContainerError(
            AppContainerReason.INVALID_ARGV,
            detail="every argv element must be a string.",
        )
    if any("\x00" in arg for arg in request.argv):
        # An embedded NUL would be silently truncated at the ctypes boundary
        # (create_unicode_buffer stops at the first NUL), dropping every later
        # argument from the child's argument vector.  Fail closed here, before
        # build_command_line or any Win32 call, so no truncated command line
        # can reach CreateProcessW.
        raise AppContainerError(
            AppContainerReason.INVALID_ARGV,
            detail="argv elements must not contain embedded NUL.",
        )
    if not request.repo_id or not request.worker_kind:
        raise AppContainerError(
            AppContainerReason.INVALID_REQUEST,
            detail="repo_id and worker_kind are required.",
        )
    if request.environment is not None:
        _validate_environment(request.environment)
    _validate_grants(request.filesystem_grants)


def _validate_grants(grants: Sequence[ContainerGrant]) -> None:
    """Refuse any grant that could land somewhere other than the path it names.

    Runs before any grant or launch call.  SetNamedSecurityInfoW follows
    reparse points, so a symlink or junction -- at the leaf, or in an ancestor,
    which is what comparing against ``realpath`` exposes -- would re-permission
    a target the caller never named.  UNC, ``\\\\?\\``, ``\\\\.\\`` and
    admin-share spellings are refused outright: they alias local trees past
    every string comparison below, and AIWorkHub never needs one.  No grant,
    read or modify, persistent or not, may equal or contain a protected tree,
    nor lie anywhere inside a system tree (:func:`_sensitive_roots`): a read
    grant on the profile is as much a leak as a write grant.  Finally, a
    revocable grant may not overlap a persistent one, because its revoke
    removes every explicit ACE of the SID on its path.
    """
    protected: list[str] | None = None
    system: list[str] = []
    checked: list[tuple[str, bool]] = []
    for grant in grants:
        if not isinstance(grant, ContainerGrant) or not isinstance(grant.path, str):
            raise AppContainerError(
                AppContainerReason.INVALID_REQUEST,
                detail="filesystem grants must be ContainerGrant(path: str, ...).",
            )
        path = grant.path
        if grant.access not in _GRANT_ACCESS_MASKS:
            raise AppContainerError(
                AppContainerReason.INVALID_REQUEST,
                detail=f"unknown grant access {grant.access!r} for {path!r}.",
            )
        if grant.persistent and grant.access == "modify":
            # Only read access to shared install roots may outlive a launch.
            raise AppContainerError(
                AppContainerReason.INVALID_REQUEST,
                detail=f"a modify grant is always revoked, never persistent: {path!r}.",
            )
        if not path or "\x00" in path or not os.path.isabs(path):
            raise AppContainerError(
                AppContainerReason.INVALID_REQUEST,
                detail=f"grant path must be absolute: {path!r}.",
            )
        if path.startswith(("\\\\", "//")):
            # Checked before lstat so an admin share is never even touched.
            raise AppContainerError(
                AppContainerReason.INVALID_REQUEST,
                detail=f"UNC, device and \\\\?\\ paths are never granted: {path!r}.",
            )
        try:
            info = os.lstat(path)
        except OSError:
            raise AppContainerError(
                AppContainerReason.INVALID_REQUEST,
                detail=f"grant path does not exist: {path!r}.",
            ) from None
        reparse = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        canonical = os.path.normcase(os.path.normpath(path))
        if (
            stat.S_ISLNK(info.st_mode)
            or reparse
            or os.path.normcase(os.path.realpath(path)) != canonical
        ):
            raise AppContainerError(
                AppContainerReason.INVALID_REQUEST,
                detail=f"grant path is or passes through a reparse point: {path!r}.",
            )
        if protected is None:
            protected, system = _sensitive_roots()
        if (
            os.path.dirname(canonical) == canonical
            or any(_within(root, canonical) for root in (*protected, *system))
            or any(_within(canonical, root) for root in system)
        ):
            raise AppContainerError(
                AppContainerReason.INVALID_REQUEST,
                detail=(
                    "grant would equal or contain a protected tree (drive root, "
                    "user profile, AppData, user temp) or touch the Windows or "
                    f"Program Files trees: {path!r}."
                ),
            )
        checked.append((canonical, grant.persistent))
    for path, persistent in checked:
        if not persistent and any(
            other_persistent and (_within(path, other) or _within(other, path))
            for other, other_persistent in checked
        ):
            raise AppContainerError(
                AppContainerReason.INVALID_REQUEST,
                detail=f"revocable grant overlaps a persistent grant: {path!r}.",
            )


def _within(child: str, parent: str) -> bool:
    """``child`` equals ``parent`` or lies beneath it (both normcased)."""
    return (child.rstrip(os.sep) + os.sep).startswith(parent.rstrip(os.sep) + os.sep)


# Directories a protected-descendant walk may visit before it fails closed.
# Measured on Windows 11 26200 (scandir + one DACL read per directory): a
# request HOME, 6 dirs, 0.3 ms; a sparse request worktree, 6 dirs, 1.1 ms; a
# full source checkout, 55 dirs, 5.4 ms -- about 0.1 ms per directory, so the
# bound caps a pathological tree near two seconds.
_DESCENDANT_WALK_LIMIT = 20_000


def _plain_directory(entry: os.DirEntry[str]) -> bool:
    """A real directory: never a symlink, junction or other reparse point."""
    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
        return False
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return not attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT


def _protected_descendants(
    root: str,
    is_protected: Callable[[str], bool],
    withheld: frozenset[str] = frozenset(),
) -> list[str]:
    """Directories beneath ``root`` whose DACL is protected, parents first.

    Reparse points and ``withheld`` (normcased) directories are neither
    returned nor entered.  More than ``_DESCENDANT_WALK_LIMIT`` directories
    raises ``OSError``.
    """
    found: list[str] = []
    pending = [root]
    visited = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if not _plain_directory(entry) or (
                    os.path.normcase(os.path.normpath(entry.path)) in withheld
                ):
                    continue
                visited += 1
                if visited > _DESCENDANT_WALK_LIMIT:
                    raise OSError(
                        f"more than {_DESCENDANT_WALK_LIMIT} directories beneath {root!r}"
                    )
                if is_protected(entry.path):
                    found.append(entry.path)
                pending.append(entry.path)
    return found


def _with_protected_descendants(
    grants: Sequence[ContainerGrant],
    api: Win32Api,
    withheld_directories: Sequence[str] = (),
) -> list[ContainerGrant]:
    """``grants``, each revocable directory followed by the protected
    directories beneath it, granted the same access.

    A directory grant is one inheritable ACE, and a protected DACL stops
    inheritance.  AIWorkHub creates owner-private subdirectories inside its
    per-request directories -- measured: ``home\\task_mcp_worker_runtime``
    kept the container out of ``claude_mcp_config.json`` (EPERM) although
    HOME itself was granted -- so each protected directory gets its own ACE.
    Only revocable grants are walked: they name one request's own
    directories, while a persistent grant names a shared install root, whose
    protected corners are not the container's business.  Every added path
    passes :func:`_validate_grants` like the rest.

    ``withheld_directories`` stay closed: that same protection is what keeps
    them out of an inheritable grant above them, so each must be an existing,
    protected directory nobody asked to grant, or the launch fails closed.
    """
    seen = {os.path.normcase(os.path.normpath(grant.path)) for grant in grants}
    withheld = frozenset(os.path.normcase(os.path.normpath(p)) for p in withheld_directories)
    for path in withheld_directories:
        key = os.path.normcase(os.path.normpath(path))
        if key in seen or not os.path.isdir(path) or not _step(
            "grant_path_access", partial(api.dacl_protected, path)
        ):
            raise AppContainerError(
                AppContainerReason.INVALID_REQUEST,
                detail=f"a withheld directory must exist, be protected and not be granted: {path!r}.",
            )
    plan: list[ContainerGrant] = []
    for grant in grants:
        plan.append(grant)
        if grant.persistent or not os.path.isdir(grant.path):
            continue
        try:
            descendants: list[str] = _step(
                "grant_path_access",
                partial(_protected_descendants, grant.path, api.dacl_protected, withheld),
            )
        except OSError as exc:
            raise AppContainerError(
                AppContainerReason.FILESYSTEM_GRANT_FAILED,
                detail=f"cannot walk {grant.path!r} for protected directories: {exc}",
                operation="grant_path_access",
            ) from exc
        for path in descendants:
            key = os.path.normcase(os.path.normpath(path))
            if key not in seen:
                seen.add(key)
                plan.append(ContainerGrant(path, grant.access))
    if len(plan) > len(grants):
        _validate_grants(plan)
    return plan


def _validate_environment(environment: Mapping[str, str]) -> None:
    """Reject environments that could truncate or inject the child's env block.

    This runs before any ctypes call or boundary work so a hostile key/value
    (embedded NUL, ``=`` in a key, non-string, empty key) fails closed with a
    structured reason and never reaches CreateProcessW.
    """
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise AppContainerError(
                AppContainerReason.INVALID_ENVIRONMENT,
                detail="environment keys and values must be strings.",
            )
        if not key:
            raise AppContainerError(
                AppContainerReason.INVALID_ENVIRONMENT,
                detail="environment keys must be non-empty.",
            )
        if "\x00" in key or "\x00" in value:
            raise AppContainerError(
                AppContainerReason.INVALID_ENVIRONMENT,
                detail="environment keys/values must not contain embedded NUL.",
            )
        if "=" in key:
            raise AppContainerError(
                AppContainerReason.INVALID_ENVIRONMENT,
                detail="environment keys must not contain '='.",
            )


def _std_handle_list(request: AppContainerRequest) -> list[int]:
    handles: list[int] = []
    for handle in (
        request.stdin_handle,
        request.stdout_handle,
        request.stderr_handle,
    ):
        if handle is not None and handle not in handles:
            handles.append(handle)
    return handles


def _attribute_count(std_handles: Sequence[int]) -> int:
    # One attribute for SECURITY_CAPABILITIES, plus one for the handle list.
    return 1 + (1 if std_handles else 0)


def _creation_flags(request: AppContainerRequest) -> int:
    flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED
    if request.create_no_window:
        flags |= CREATE_NO_WINDOW
    if request.environment is not None:
        flags |= CREATE_UNICODE_ENVIRONMENT
    return flags


def native_handle(fd: int) -> int:
    """The Win32 HANDLE behind CRT descriptor ``fd``, for the std-handle
    fields of an :class:`AppContainerRequest`.  It lives here, inside the
    sanctioned OS-dependency boundary, so callers never import ``msvcrt``."""
    import msvcrt

    get_osfhandle = cast(Callable[[int], int], getattr(msvcrt, "get_osfhandle"))
    return int(get_osfhandle(fd))


def _creation_identity(name: str, creation: _ProcessCreation) -> str:
    raw = f"{name}|{creation.process_id}|{creation.thread_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Real ctypes-backed Win32 boundary (loaded lazily, Windows only)
# ---------------------------------------------------------------------------


def _load_windows_dll(name: str) -> "ctypes.CDLL":
    """Load a Windows system DLL with ``GetLastError`` capture enabled.

    ``ctypes.WinDLL`` is typed as Windows-only in typeshed, so the canonical
    mypy gate (which runs on a Linux host) would flag a direct reference as a
    missing attribute.  Resolving it through :func:`getattr` keeps the module
    type-clean without a blanket ``type: ignore`` or any loss of typing on the
    surrounding code.  ``WinDLL`` is the correct stdcall + last-error loader on
    Windows; the ``CDLL`` fallback is never reached there (this boundary is only
    constructed on Windows) and exists solely so the reference is well-typed
    off-platform.
    """
    loader = getattr(ctypes, "WinDLL", ctypes.CDLL)
    return loader(name, use_last_error=True)


# Where each Win32 export actually lives, most specific library first. Windows
# moved most of the Win32 base APIs into kernelbase.dll and publishes them
# through API sets; kernel32.dll forwards many of them but NOT the
# security-base ones. Measured on Windows 11 26200:
#   advapi32  ->  (no DeriveCapabilitySidsFromName)
#   kernel32  ->  (no DeriveCapabilitySidsFromName)
#   kernelbase, api-ms-win-security-base-l1-2-2  ->  exports it
# Binding it to kernel32 therefore raised AttributeError on a host that is
# perfectly capable of AppContainer confinement, the probe answered
# CAPABILITY_DERIVATION_FAILED, and every native CLI route was excluded as
# "windows_appcontainer_sandbox_unavailable" while the real cause was a lookup
# in the wrong library.
_WINDOWS_EXPORT_LIBRARIES: dict[str, tuple[str, ...]] = {
    "DeriveCapabilitySidsFromName": (
        "kernelbase",
        "api-ms-win-security-base-l1-2-2",
        "advapi32",
        "kernel32",
    ),
}


def _load_windows_export(name: str) -> tuple["ctypes.CDLL", str] | None:
    """Return the first loadable library that exports ``name``, or None.

    Fails closed rather than guessing: a caller that gets None reports the
    export as unavailable instead of binding a same-named symbol from a library
    that happens to load.
    """

    for library in _WINDOWS_EXPORT_LIBRARIES.get(name, ()):
        try:
            handle = _load_windows_dll(library)
        except OSError:
            continue
        try:
            getattr(handle, name)
        except AttributeError:
            continue
        return handle, library
    return None


def _last_win_error() -> int:
    """Return the last Win32 error code (``GetLastError``).

    ``ctypes.get_last_error`` is likewise typed Windows-only in typeshed;
    resolving it dynamically keeps the module type-clean on non-Windows hosts
    and lets the mocked tests substitute the value.  Off Windows (where the
    launcher never runs) it degrades to ``0``.
    """
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter is not None else 0


def _load_win32() -> Win32Api:
    """Construct the real ctypes Win32 boundary.

    Only invoked on Windows; resolving the DLLs and function pointers here
    keeps the module import side-effect free elsewhere.
    """
    return _CtypesWin32Api()


class _NativeSecurityCapabilities:
    """Holds the SECURITY_CAPABILITIES struct, its live capability array, and
    the OS-allocated capability SIDs retained for deterministic freeing."""

    __slots__ = ("struct", "keepalive", "capability_sids")

    def __init__(
        self, struct: Any, keepalive: list[Any], capability_sids: list[Any]
    ) -> None:
        self.struct = struct
        self.keepalive = keepalive
        self.capability_sids = capability_sids


class _CtypesWin32Api:
    """Real Windows boundary using exact, bounded ctypes signatures."""

    def __init__(self) -> None:
        # Resolved lazily via the platform shim; WinDLL is absent on non-Windows
        # typeshed, and this boundary is only ever constructed on Windows.
        self._kernel32 = _load_windows_dll("kernel32")
        self._userenv = _load_windows_dll("userenv")
        self._advapi32 = _load_windows_dll("advapi32")
        # DeriveCapabilitySidsFromName is a security-base export that kernel32
        # does not forward; resolve it where this host actually publishes it.
        resolved = _load_windows_export("DeriveCapabilitySidsFromName")
        if resolved is None:
            libraries = ", ".join(
                _WINDOWS_EXPORT_LIBRARIES["DeriveCapabilitySidsFromName"]
            )
            raise AppContainerError(
                AppContainerReason.CAPABILITY_DERIVATION_FAILED,
                detail=(
                    "required Win32 export unavailable: function "
                    f"'DeriveCapabilitySidsFromName' not found in {libraries}"
                ),
                operation="DeriveCapabilitySidsFromName",
            )
        self._security_base, self._security_base_library = resolved
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        k = self._kernel32
        s = self._security_base
        u = self._userenv
        a = self._advapi32

        u.CreateAppContainerProfile.restype = ctypes.c_long
        u.CreateAppContainerProfile.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.POINTER(_SID_AND_ATTRIBUTES),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
        ]
        u.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
        u.DeriveAppContainerSidFromAppContainerName.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.LPVOID),
        ]

        s.DeriveCapabilitySidsFromName.restype = wintypes.BOOL
        s.DeriveCapabilitySidsFromName.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.POINTER(wintypes.LPVOID)),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.POINTER(wintypes.LPVOID)),
            ctypes.POINTER(wintypes.DWORD),
        ]

        a.FreeSid.restype = wintypes.LPVOID
        a.FreeSid.argtypes = [wintypes.LPVOID]
        a.ConvertSidToStringSidW.restype = wintypes.BOOL
        a.ConvertSidToStringSidW.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPWSTR),
        ]

        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        k.TerminateJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]

        k.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        k.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        k.UpdateProcThreadAttribute.restype = wintypes.BOOL
        k.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        k.DeleteProcThreadAttributeList.restype = None
        k.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]

        k.CreateProcessW.restype = wintypes.BOOL
        k.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPCWSTR,
            ctypes.POINTER(_STARTUPINFOEXW),
            ctypes.POINTER(_PROCESS_INFORMATION),
        ]
        k.ResumeThread.restype = wintypes.DWORD
        k.ResumeThread.argtypes = [wintypes.HANDLE]
        k.TerminateProcess.restype = wintypes.BOOL
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k.CloseHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.LocalFree.restype = wintypes.HLOCAL
        k.LocalFree.argtypes = [wintypes.HLOCAL]
        # HANDLE-width-safe: wintypes.HANDLE is c_void_p, so a > 32-bit handle
        # is passed intact rather than truncated to a 32-bit int.
        k.SetHandleInformation.restype = wintypes.BOOL
        k.SetHandleInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
        ]

        # Filesystem grants reuse the snapshot boundary's security-info
        # signatures (GetNamedSecurityInfoW, GetLengthSid, LocalFree) and add
        # only the write side.
        _NativeAclSnapshotApi._configure_signatures(a, k)
        a.GetSecurityDescriptorControl.restype = wintypes.BOOL
        a.GetSecurityDescriptorControl.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        a.SetEntriesInAclW.restype = wintypes.DWORD
        a.SetEntriesInAclW.argtypes = [
            wintypes.ULONG,
            ctypes.POINTER(_EXPLICIT_ACCESS_W),
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPVOID),
        ]
        a.SetNamedSecurityInfoW.restype = wintypes.DWORD
        a.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]

    # -- identity -----------------------------------------------------------

    def derive_identity(
        self, name: str, display_name: str, description: str
    ) -> _Identity:
        sid = wintypes.LPVOID()
        hr = self._userenv.CreateAppContainerProfile(
            name, display_name, description, None, 0, ctypes.byref(sid)
        )
        created = True
        if hr == _HRESULT_ALREADY_EXISTS:
            created = False
            hr = self._userenv.DeriveAppContainerSidFromAppContainerName(
                name, ctypes.byref(sid)
            )
            if hr != 0:
                raise _Win32Failure(
                    hr & 0xFFFF,
                    "derive_appcontainer_sid",
                    f"hr=0x{hr & 0xFFFFFFFF:08x}",
                )
        elif hr != 0:
            raise _Win32Failure(
                hr & 0xFFFF,
                "create_appcontainer_profile",
                f"hr=0x{hr & 0xFFFFFFFF:08x}",
            )
        sid_string = self._sid_to_string(sid)
        return _Identity(name, display_name, sid_string, sid, created)

    def free_identity(self, identity: _Identity) -> None:
        if identity.sid_token:
            self._advapi32.FreeSid(identity.sid_token)
            identity.sid_token = None

    def _sid_to_string(self, sid: Any) -> str:
        out = wintypes.LPWSTR()
        ok = self._advapi32.ConvertSidToStringSidW(sid, ctypes.byref(out))
        if not ok:
            return ""
        try:
            return out.value or ""
        finally:
            self._kernel32.LocalFree(out)

    # -- filesystem grants --------------------------------------------------

    def grant_path_access(
        self,
        identity: _Identity,
        path: str,
        access: str,
        *,
        persistent: bool = False,
    ) -> _PathGrant:
        """Merge one GRANT_ACCESS ACE for exactly the container SID.

        A persistent grant the DACL already satisfies rewrites nothing and
        records who satisfies it (:func:`_satisfying_trustee`) -- this SID's
        own explicit ACE, or ALL APPLICATION PACKAGES, which an administrator
        may have granted an install root the user cannot re-permission.  An
        ALL APPLICATION PACKAGES ACE is only ever read here, never written.
        A revocable grant always writes and keeps a copy of the SID:
        :meth:`revoke_path_access` then removes this SID's explicit ACEs --
        including one a crashed launch left behind -- instead of restoring a
        snapshot that would clobber a concurrent DACL edit.
        """
        mask = _GRANT_ACCESS_MASKS[access]
        inherit = _SUB_CONTAINERS_AND_OBJECTS_INHERIT if os.path.isdir(path) else 0
        sid = ctypes.string_at(
            identity.sid_token, self._advapi32.GetLengthSid(identity.sid_token)
        )
        if persistent:
            satisfied_by = self._grant_already_satisfied(sid, path, mask, inherit)
            if satisfied_by:
                return _PathGrant(path, access, satisfied_by=satisfied_by)
        changed = self._set_sid_entry(
            path, sid, _GRANT_ACCESS, mask, inherit, "grant_path_access",
            denied_detail=(
                _all_packages_grant_hint(path)
                if persistent and access == "read_execute"
                else ""
            ),
        )
        return _PathGrant(path, access, sid if changed and not persistent else None)

    def revoke_path_access(self, grant: _PathGrant) -> None:
        """Remove this container SID's explicit ACEs from ``grant.path``.
        Idempotent; never raises; a failure is recorded on
        ``grant.revoke_error``."""
        if grant.restore is None:
            return
        sid, grant.restore = grant.restore, None
        try:
            self._set_sid_entry(grant.path, sid, _REVOKE_ACCESS, 0, 0, "revoke_path_access")
        except _Win32Failure as exc:
            grant.revoke_error = exc.win_error or -1
        except Exception:
            grant.revoke_error = -1

    def dacl_protected(self, path: str) -> bool:
        """Whether ``path``'s DACL is protected from inheritance (read only)."""
        a = self._advapi32
        descriptor = wintypes.LPVOID()
        dacl = wintypes.LPVOID()
        status = a.GetNamedSecurityInfoW(
            path, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
        )
        if status or not descriptor.value:
            raise _Win32Failure(int(status), "grant_path_access", f"read DACL {path}")
        try:
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not a.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            ):
                raise _Win32Failure(
                    _last_win_error(), "grant_path_access", f"read control {path}"
                )
            return bool(control.value & _SE_DACL_PROTECTED)
        finally:
            self._kernel32.LocalFree(descriptor)

    def _set_sid_entry(
        self, path: str, sid: bytes, mode: int, mask: int, inherit: int, operation: str,
        *, denied_detail: str = "",
    ) -> bool:
        """Read ``path``'s DACL, apply one EXPLICIT_ACCESS entry for ``sid``,
        write it back.  False (nothing written) for a NULL DACL: it already
        admits everyone, and merging into it would REPLACE it with a one-entry
        DACL that locks everyone else out.  ``denied_detail``, when given,
        replaces the failure detail if the write is refused with
        ERROR_ACCESS_DENIED (no WRITE_DAC)."""
        a = self._advapi32
        descriptor = wintypes.LPVOID()
        dacl = wintypes.LPVOID()
        status = a.GetNamedSecurityInfoW(
            path, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
        )
        if status or not descriptor.value:
            raise _Win32Failure(int(status), operation, f"read DACL {path}")
        try:
            if not dacl.value:
                return False
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not a.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            ):
                raise _Win32Failure(_last_win_error(), operation, f"read control {path}")
            # Name the current protection explicitly so a write can never flip
            # whether the DACL inherits.
            info = _DACL_SECURITY_INFORMATION | (
                _PROTECTED_DACL_SECURITY_INFORMATION
                if control.value & _SE_DACL_PROTECTED
                else _UNPROTECTED_DACL_SECURITY_INFORMATION
            )
            trustee = ctypes.create_string_buffer(sid, len(sid))
            entry = _EXPLICIT_ACCESS_W()
            entry.grfAccessPermissions = mask
            entry.grfAccessMode = mode
            entry.grfInheritance = inherit
            entry.Trustee.TrusteeForm = _TRUSTEE_IS_SID
            entry.Trustee.ptstrName = ctypes.addressof(trustee)
            merged = wintypes.LPVOID()
            status = a.SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(merged))
            if status:
                raise _Win32Failure(int(status), operation, f"merge ACE {path}")
            try:
                status = a.SetNamedSecurityInfoW(
                    path, _SE_FILE_OBJECT, info, None, None, merged, None
                )
            finally:
                self._kernel32.LocalFree(merged)
            if status:
                detail = (
                    denied_detail
                    if denied_detail and status == _ERROR_ACCESS_DENIED
                    else f"write DACL {path}"
                )
                raise _Win32Failure(int(status), operation, detail)
            return True
        finally:
            self._kernel32.LocalFree(descriptor)

    def _grant_already_satisfied(
        self, sid: bytes, path: str, mask: int, inherit: int
    ) -> str:
        try:
            snapshot = snapshot_filesystem_acl(path)
        except AclSnapshotError:
            return ""  # an ACL we cannot parse is simply re-granted
        return _satisfying_trustee(snapshot.aces, sid, mask, inherit)

    # -- security capabilities ---------------------------------------------

    def build_security_capabilities(
        self, identity: _Identity, capability_sids: Sequence[str]
    ) -> _SecurityCapabilities:
        struct = _SECURITY_CAPABILITIES()
        struct.AppContainerSid = identity.sid_token
        keepalive: list[Any] = []
        retained: list[Any] = []
        names = list(capability_sids)
        try:
            if names:
                entries = (_SID_AND_ATTRIBUTES * len(names))()
                for index, cap_name in enumerate(names):
                    cap_sid = self._derive_capability_sid(cap_name)
                    retained.append(cap_sid)
                    entries[index].Sid = ctypes.cast(
                        cap_sid, wintypes.LPVOID
                    )
                    entries[index].Attributes = _SE_GROUP_ENABLED
                keepalive.append(entries)
                struct.Capabilities = entries
                struct.CapabilityCount = len(names)
            else:
                struct.Capabilities = None
                struct.CapabilityCount = 0
        except _Win32Failure:
            # A later derivation failed after earlier ones succeeded; free the
            # SIDs retained so far so a partial SECURITY_CAPABILITIES leaks
            # nothing before the failure propagates.
            self._free_capability_sids(retained)
            raise
        native = _NativeSecurityCapabilities(struct, keepalive, retained)
        return _SecurityCapabilities(identity.sid_string, native)

    def _derive_capability_sid(self, cap_name: str) -> Any:
        group_sids = ctypes.POINTER(wintypes.LPVOID)()
        group_count = wintypes.DWORD(0)
        cap_sids = ctypes.POINTER(wintypes.LPVOID)()
        cap_count = wintypes.DWORD(0)
        ok = self._security_base.DeriveCapabilitySidsFromName(
            cap_name,
            ctypes.byref(group_sids),
            ctypes.byref(group_count),
            ctypes.byref(cap_sids),
            ctypes.byref(cap_count),
        )
        if not ok or cap_count.value < 1:
            win_error = _last_win_error()
            # The API may have LocalAlloc'd one array before failing; release
            # whatever it handed back so a failed derivation leaks nothing.
            self._free_sid_array(group_sids, group_count.value)
            self._free_sid_array(cap_sids, cap_count.value)
            raise _Win32Failure(
                win_error,
                "derive_capability_sids",
                f"capability={cap_name}",
            )
        # DeriveCapabilitySidsFromName LocalAllocs both arrays and every SID
        # element.  We retain exactly one capability SID (index 0) for the
        # SECURITY_CAPABILITIES entry and free everything else now: the whole
        # group array with its SIDs, and the capability array plus its
        # non-retained SID slots.  The retained SID is freed later by
        # :meth:`free_security_capabilities`.
        retained = cap_sids[0]
        self._free_sid_array(group_sids, group_count.value)
        self._free_sid_array(cap_sids, cap_count.value, keep=retained)
        return retained

    def _free_sid_array(
        self, array: Any, count: int, keep: Any = None
    ) -> None:
        """LocalFree each SID slot (except ``keep``) then the array itself."""
        if not array:
            return
        for index in range(count):
            element = array[index]
            if element and element != keep:
                self._kernel32.LocalFree(element)
        self._kernel32.LocalFree(ctypes.cast(array, wintypes.HLOCAL))

    def _free_capability_sids(self, sids: list[Any]) -> None:
        """LocalFree each retained capability SID exactly once, then clear.

        Draining the list in place makes repeated calls idempotent: a second
        pass finds nothing left to free and cannot double-free.
        """
        while sids:
            sid = sids.pop()
            if sid:
                self._kernel32.LocalFree(sid)

    def free_security_capabilities(
        self, sec_caps: _SecurityCapabilities
    ) -> None:
        """Free the retained capability SIDs.  Idempotent; never raises.

        The AppContainer SID is owned by the :class:`_Identity` and released by
        :meth:`free_identity`; only the capability SIDs retained from
        :meth:`_derive_capability_sid` are freed here.
        """
        sids = getattr(sec_caps.native, "capability_sids", None)
        if sids:
            self._free_capability_sids(sids)

    # -- job object ---------------------------------------------------------

    def create_job_object(self, name: str) -> Any:
        # Unnamed job so it cannot be opened by other processes by name.
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise _Win32Failure(
                _last_win_error(), "create_job_object"
            )
        return handle

    def configure_job_object(self, job: Any) -> None:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        ok = self._kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            raise _Win32Failure(
                _last_win_error(), "configure_job_object"
            )

    def terminate_job(self, job: Any, exit_code: int = 1) -> None:
        if job:
            ok = self._kernel32.TerminateJobObject(job, exit_code)
            if not ok:
                raise _Win32Failure(_last_win_error(), "terminate_job")

    def close_job(self, job: Any) -> None:
        if job:
            self._kernel32.CloseHandle(job)

    # -- attribute list -----------------------------------------------------

    def init_attribute_list(self, attribute_count: int) -> _AttributeList:
        size = ctypes.c_size_t(0)
        # First call returns the required buffer size (expected to "fail").
        self._kernel32.InitializeProcThreadAttributeList(
            None, attribute_count, 0, ctypes.byref(size)
        )
        buffer = (ctypes.c_byte * size.value)()
        attr_ptr = ctypes.cast(buffer, ctypes.c_void_p)
        ok = self._kernel32.InitializeProcThreadAttributeList(
            attr_ptr, attribute_count, 0, ctypes.byref(size)
        )
        if not ok:
            raise _Win32Failure(
                _last_win_error(), "init_attribute_list"
            )
        return _AttributeList(attr_ptr, [buffer], attribute_count)

    def set_security_capabilities(
        self, attrs: _AttributeList, sec_caps: _SecurityCapabilities
    ) -> None:
        native = sec_caps.native
        ok = self._kernel32.UpdateProcThreadAttribute(
            attrs.native,
            0,
            PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(native.struct),
            ctypes.sizeof(native.struct),
            None,
            None,
        )
        if not ok:
            raise _Win32Failure(
                _last_win_error(), "set_security_capabilities"
            )
        attrs.keepalive.append(native)

    def set_inherited_handles(
        self, attrs: _AttributeList, handles: Sequence[int]
    ) -> None:
        array = (wintypes.HANDLE * len(handles))(*handles)
        ok = self._kernel32.UpdateProcThreadAttribute(
            attrs.native,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            array,
            ctypes.sizeof(array),
            None,
            None,
        )
        if not ok:
            raise _Win32Failure(
                _last_win_error(), "set_inherited_handles"
            )
        attrs.keepalive.append(array)

    def delete_attribute_list(self, attrs: _AttributeList) -> None:
        if attrs.native:
            self._kernel32.DeleteProcThreadAttributeList(attrs.native)
            attrs.native = None
            attrs.keepalive.clear()

    # -- process ------------------------------------------------------------

    def create_process(self, spec: _ProcessSpec) -> _ProcessCreation:
        startup = _STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        startup.lpAttributeList = spec.attribute_list.native
        if (
            spec.std_input is not None
            or spec.std_output is not None
            or spec.std_error is not None
        ):
            startup.StartupInfo.dwFlags |= STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = spec.std_input or 0
            startup.StartupInfo.hStdOutput = spec.std_output or 0
            startup.StartupInfo.hStdError = spec.std_error or 0
            self._mark_inheritable(spec)

        env_block = _environment_block(spec.environment)
        command_buffer = ctypes.create_unicode_buffer(spec.command_line)
        info = _PROCESS_INFORMATION()
        ok = self._kernel32.CreateProcessW(
            spec.executable,
            command_buffer,
            None,
            None,
            spec.inherit_handles,
            spec.creation_flags,
            env_block,
            spec.working_directory,
            ctypes.byref(startup),
            ctypes.byref(info),
        )
        if not ok:
            raise _Win32Failure(_last_win_error(), "create_process")
        return _ProcessCreation(
            info.dwProcessId, info.dwThreadId, info.hProcess, info.hThread
        )

    def _mark_inheritable(self, spec: _ProcessSpec) -> None:
        # Every std handle exposed to the child must be explicitly marked
        # inheritable; a bare bInheritHandles=TRUE does not promote handles that
        # were opened non-inheritable.  Call the signature-configured export
        # directly (HANDLE-width-safe argtypes) and fail closed on a false
        # return *before* CreateProcessW, so a handle that cannot be made
        # inheritable can never yield a child with the wrong stdio.
        for handle in (spec.std_input, spec.std_output, spec.std_error):
            if handle is None:
                continue
            ok = self._kernel32.SetHandleInformation(
                handle, _HANDLE_FLAG_INHERIT, _HANDLE_FLAG_INHERIT
            )
            if not ok:
                raise _Win32Failure(
                    _last_win_error(),
                    "create_process",
                    f"SetHandleInformation(handle={handle:#x})",
                )

    def assign_process_to_job(
        self, job: Any, creation: _ProcessCreation
    ) -> None:
        ok = self._kernel32.AssignProcessToJobObject(
            job, creation.process_handle
        )
        if not ok:
            raise _Win32Failure(
                _last_win_error(), "assign_process_to_job"
            )

    def resume_thread(self, creation: _ProcessCreation) -> None:
        result = self._kernel32.ResumeThread(creation.thread_handle)
        if result == 0xFFFFFFFF:
            raise _Win32Failure(_last_win_error(), "resume_thread")

    def terminate_process(self, creation: _ProcessCreation) -> None:
        if creation.process_handle:
            self._kernel32.TerminateProcess(
                creation.process_handle, _UNWIND_KILL_EXIT_CODE
            )
            self._kernel32.CloseHandle(creation.process_handle)
            creation.process_handle = None

    def close_thread_handle(self, creation: _ProcessCreation) -> None:
        if creation.thread_handle:
            self._kernel32.CloseHandle(creation.thread_handle)
            creation.thread_handle = None

    def close_process_handle(self, creation: _ProcessCreation) -> None:
        if creation.process_handle:
            self._kernel32.CloseHandle(creation.process_handle)
            creation.process_handle = None

    def wait_process(
        self, creation: _ProcessCreation, timeout_ms: int
    ) -> bool:
        result = self._kernel32.WaitForSingleObject(
            creation.process_handle, timeout_ms
        )
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        error = _last_win_error() if result == _WAIT_FAILED else int(result)
        raise _Win32Failure(error, "wait_process")

    def get_process_exit_code(self, creation: _ProcessCreation) -> int:
        exit_code = wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(
            creation.process_handle, ctypes.byref(exit_code)
        ):
            raise _Win32Failure(_last_win_error(), "get_process_exit_code")
        return int(exit_code.value)


def _environment_block(environment: Mapping[str, str] | None) -> Any:
    if environment is None:
        return None
    parts: list[str] = []
    for key, value in environment.items():
        # Fail closed before touching ctypes: an embedded NUL in a key or
        # value would either truncate the block or splice an attacker-chosen
        # ``NAME=VALUE`` pair into the environment CreateProcessW receives.
        if "\x00" in key or "\x00" in value:
            raise ValueError(
                "environment keys and values must not contain embedded NUL"
            )
        parts.append(f"{key}={value}")
    block = "\x00".join(parts) + "\x00\x00"
    return ctypes.create_unicode_buffer(block)


# ---------------------------------------------------------------------------
# Read-only filesystem ACL snapshots
# ---------------------------------------------------------------------------


class DaclState(str, enum.Enum):
    """The three semantically distinct DACL states in a security descriptor."""

    ABSENT = "absent"
    NULL = "null"
    PRESENT = "present"


@dataclass(frozen=True)
class AclAce:
    """An immutable, ownership-free copy of one supported native ACE."""

    ace_type: int
    flags: int
    mask: int
    sid: bytes
    raw: bytes
    object_flags: int | None = None
    object_type: bytes | None = None
    inherited_object_type: bytes | None = None


@dataclass(frozen=True)
class AclSnapshot:
    """Immutable read-only copy; it contains no borrowed native pointers."""

    path: str
    dacl_state: DaclState
    defaulted: bool
    aces: tuple[AclAce, ...]
    raw_acl: bytes | None = None
    authentication: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.dacl_state is DaclState.PRESENT:
            if self.raw_acl is None:
                raise ValueError("a present DACL requires raw ACL bytes")
        elif self.raw_acl is not None:
            raise ValueError("an absent or NULL DACL cannot have raw ACL bytes")
        object.__setattr__(self, "authentication", self._authentication())

    def _authentication(self) -> bytes:
        digest = hashlib.sha256()
        encoded_path = self.path.encode("utf-8", "surrogatepass")
        digest.update(len(encoded_path).to_bytes(8, "little"))
        digest.update(encoded_path)
        digest.update(self.dacl_state.value.encode("ascii"))
        digest.update(bytes((self.defaulted,)))
        raw = self.raw_acl or b""
        digest.update(len(raw).to_bytes(8, "little"))
        digest.update(raw)
        return digest.digest()

    def verify_integrity(self) -> None:
        """Fail closed if authenticated fields or decoded ACE bytes drift."""
        if self.authentication != self._authentication():
            raise AclSnapshotError("snapshot_authentication")
        if self.dacl_state is not DaclState.PRESENT:
            if self.raw_acl is not None or self.aces:
                raise AclSnapshotError("snapshot_state")
            return
        raw = self.raw_acl
        if raw is None or len(raw) < 8:
            raise AclSnapshotError("snapshot_state")
        if b"".join(ace.raw for ace in self.aces) != raw[8:]:
            raise AclSnapshotError("snapshot_ace_partition")


class AclSnapshotError(RuntimeError):
    """A fail-closed native snapshot error, optionally with cleanup evidence."""

    def __init__(
        self,
        operation: str,
        win_error: int | None = None,
        *,
        cleanup_error: BaseException | None = None,
    ) -> None:
        self.operation = operation
        self.win_error = win_error
        self.cleanup_error = cleanup_error
        detail = operation if win_error is None else f"{operation} (win_error={win_error})"
        if cleanup_error is not None:
            detail += f"; cleanup failed: {cleanup_error}"
        super().__init__(detail)


class _AclSnapshotApi(Protocol):
    def get_named_security_info(self, path: str) -> int: ...
    def get_security_descriptor_dacl(self, descriptor: int) -> tuple[bool, int, bool]: ...
    def acl_information(self, dacl: int) -> tuple[int, int]: ...
    def acl_bytes(self, dacl: int, size: int) -> bytes: ...
    def get_ace(self, dacl: int, index: int, acl_end: int) -> tuple[int, bytes]: ...
    def sid_bytes(self, address: int, ace_end: int) -> bytes: ...
    def local_free(self, descriptor: int) -> None: ...


_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_ACL_SIZE_INFORMATION_CLASS = 2
_SIMPLE_ACE_TYPES = frozenset((0, 1, 2, 3))
_OBJECT_ACE_TYPES = frozenset((5, 6, 7, 8))
_ACE_OBJECT_TYPE_PRESENT = 0x1
_ACE_INHERITED_OBJECT_TYPE_PRESENT = 0x2
_SUPPORTED_ACL_REVISIONS = frozenset((2, 4))


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _NativeAclSnapshotApi:
    """Small read-only Advapi32 boundary; no write-side export is resolved."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise AclSnapshotError("platform_unsupported")
        self._advapi32 = _load_windows_dll("advapi32")
        self._kernel32 = _load_windows_dll("kernel32")
        self._configure_signatures(self._advapi32, self._kernel32)

    @staticmethod
    def _configure_signatures(advapi32: Any, kernel32: Any) -> None:
        advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
        ]
        advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.GetSecurityDescriptorDacl.argtypes = [
            wintypes.LPVOID, ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.BOOL),
        ]
        advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        advapi32.GetAclInformation.argtypes = [
            wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.c_int,
        ]
        advapi32.GetAclInformation.restype = wintypes.BOOL
        advapi32.GetAce.argtypes = [
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
        ]
        advapi32.GetAce.restype = wintypes.BOOL
        advapi32.IsValidSid.argtypes = [wintypes.LPVOID]
        advapi32.IsValidSid.restype = wintypes.BOOL
        advapi32.GetLengthSid.argtypes = [wintypes.LPVOID]
        advapi32.GetLengthSid.restype = wintypes.DWORD
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL

    def get_named_security_info(self, path: str) -> int:
        descriptor = wintypes.LPVOID()
        status = self._advapi32.GetNamedSecurityInfoW(
            path, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
            None, None, None, None, ctypes.byref(descriptor),
        )
        if status or not descriptor.value:
            raise AclSnapshotError("get_named_security_info", int(status))
        return int(descriptor.value)

    def get_security_descriptor_dacl(self, descriptor: int) -> tuple[bool, int, bool]:
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), wintypes.LPVOID()
        if not self._advapi32.GetSecurityDescriptorDacl(
            ctypes.c_void_p(descriptor), ctypes.byref(present),
            ctypes.byref(dacl), ctypes.byref(defaulted),
        ):
            raise AclSnapshotError("get_security_descriptor_dacl", _last_win_error())
        return bool(present.value), int(dacl.value or 0), bool(defaulted.value)

    def acl_information(self, dacl: int) -> tuple[int, int]:
        info = _ACL_SIZE_INFORMATION()
        if not self._advapi32.GetAclInformation(
            ctypes.c_void_p(dacl), ctypes.byref(info), ctypes.sizeof(info),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            raise AclSnapshotError("acl_information", _last_win_error())
        return int(info.AclBytesInUse), int(info.AceCount)

    def acl_bytes(self, dacl: int, size: int) -> bytes:
        return bytes(ctypes.string_at(dacl, size))

    def get_ace(self, dacl: int, index: int, acl_end: int) -> tuple[int, bytes]:
        ace = wintypes.LPVOID()
        if not self._advapi32.GetAce(ctypes.c_void_p(dacl), index, ctypes.byref(ace)):
            raise AclSnapshotError("get_ace", _last_win_error())
        address = int(ace.value or 0)
        if not address:
            raise AclSnapshotError("null_ace")
        if address < dacl or address > acl_end or acl_end - address < 4:
            raise AclSnapshotError("ace_out_of_range")
        header = ctypes.string_at(address, 4)
        size = int.from_bytes(header[2:4], "little")
        if size < 4:
            raise AclSnapshotError("invalid_ace_size")
        if size > acl_end - address:
            raise AclSnapshotError("ace_out_of_range")
        return address, bytes(ctypes.string_at(address, size))

    def sid_bytes(self, address: int, ace_end: int) -> bytes:
        if not address or address >= ace_end or ace_end - address < 8:
            raise AclSnapshotError("sid_out_of_range")
        header = bytes(ctypes.string_at(address, 8))
        if len(header) != 8:
            raise AclSnapshotError("sid_out_of_range")
        expected_length = 8 + 4 * header[1]
        if expected_length > ace_end - address:
            raise AclSnapshotError("sid_out_of_range")
        pointer = ctypes.c_void_p(address)
        if not self._advapi32.IsValidSid(pointer):
            raise AclSnapshotError("invalid_sid")
        length = int(self._advapi32.GetLengthSid(pointer))
        if length != expected_length:
            raise AclSnapshotError("sid_out_of_range")
        return bytes(ctypes.string_at(address, length))

    def local_free(self, descriptor: int) -> None:
        result = self._kernel32.LocalFree(ctypes.c_void_p(descriptor))
        if result:
            raise AclSnapshotError("local_free", _last_win_error())


def _copy_acl_ace(api: _AclSnapshotApi, address: int, raw: bytes) -> AclAce:
    if not address or len(raw) < 8:
        raise AclSnapshotError("invalid_ace_size")
    size = int.from_bytes(raw[2:4], "little")
    if size != len(raw) or size < 8:
        raise AclSnapshotError("invalid_ace_size")
    ace_type, flags = raw[0], raw[1]
    mask = int.from_bytes(raw[4:8], "little")
    object_flags = None
    object_type = inherited_type = None
    if ace_type in _SIMPLE_ACE_TYPES:
        sid_offset = 8
    elif ace_type in _OBJECT_ACE_TYPES:
        if size < 12:
            raise AclSnapshotError("invalid_object_ace")
        object_flags = int.from_bytes(raw[8:12], "little")
        if object_flags & ~3:
            raise AclSnapshotError("invalid_object_flags")
        sid_offset = 12
        if object_flags & _ACE_OBJECT_TYPE_PRESENT:
            if sid_offset + 16 > size:
                raise AclSnapshotError("invalid_object_guid")
            object_type = raw[sid_offset : sid_offset + 16]
            sid_offset += 16
        if object_flags & _ACE_INHERITED_OBJECT_TYPE_PRESENT:
            if sid_offset + 16 > size:
                raise AclSnapshotError("invalid_object_guid")
            inherited_type = raw[sid_offset : sid_offset + 16]
            sid_offset += 16
    else:
        raise AclSnapshotError("unsupported_ace_type")
    sid = api.sid_bytes(address + sid_offset, address + size)
    if not sid or sid_offset + len(sid) > size:
        raise AclSnapshotError("sid_out_of_range")
    return AclAce(ace_type, flags, mask, bytes(sid), bytes(raw), object_flags, object_type, inherited_type)


def snapshot_filesystem_acl(path: str, *, api: _AclSnapshotApi | None = None) -> AclSnapshot:
    """Read and copy a filesystem DACL without retaining native authority."""
    if not isinstance(path, str):
        raise TypeError("path must be a string")
    if not path or "\x00" in path:
        raise ValueError("path must be non-empty and contain no NUL")
    boundary: _AclSnapshotApi = api if api is not None else _NativeAclSnapshotApi()
    descriptor = boundary.get_named_security_info(path)
    if not descriptor:
        raise AclSnapshotError("null_security_descriptor")
    primary: BaseException | None = None
    try:
        present, dacl, defaulted = boundary.get_security_descriptor_dacl(descriptor)
        if not present:
            if dacl:
                raise AclSnapshotError("contradictory_dacl_state")
            return AclSnapshot(path, DaclState.ABSENT, defaulted, (), None)
        if not dacl:
            return AclSnapshot(path, DaclState.NULL, defaulted, (), None)
        acl_bytes, ace_count = boundary.acl_information(dacl)
        if acl_bytes < 8 or ace_count < 0 or ace_count > (acl_bytes - 8) // 4:
            raise AclSnapshotError("invalid_acl_bounds")
        uintptr_max = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
        if dacl > uintptr_max or acl_bytes > uintptr_max - dacl:
            raise AclSnapshotError("invalid_acl_bounds")
        acl_end = dacl + acl_bytes
        header = bytes(boundary.acl_bytes(dacl, 8))
        if len(header) != 8:
            raise AclSnapshotError("truncated_acl_header")
        revision = header[0]
        declared_size = int.from_bytes(header[2:4], "little")
        declared_count = int.from_bytes(header[4:6], "little")
        if revision not in _SUPPORTED_ACL_REVISIONS:
            raise AclSnapshotError("unsupported_acl_revision")
        if declared_size != acl_bytes or declared_count != ace_count:
            raise AclSnapshotError("invalid_acl_header")
        copied: list[AclAce] = []
        cursor = dacl + 8
        for index in range(ace_count):
            address, raw = boundary.get_ace(dacl, index, acl_end)
            if address != cursor or len(raw) < 4:
                raise AclSnapshotError("ace_traversal")
            ace_end = address + len(raw)
            if ace_end < address or ace_end > acl_end:
                raise AclSnapshotError("ace_out_of_range")
            copied.append(_copy_acl_ace(boundary, address, raw))
            cursor = ace_end
        if cursor != acl_end:
            raise AclSnapshotError("ace_count_traversal")
        raw_acl = bytes(boundary.acl_bytes(dacl, acl_bytes))
        if len(raw_acl) != acl_bytes or raw_acl[:8] != header:
            raise AclSnapshotError("truncated_acl_copy")
        if b"".join(ace.raw for ace in copied) != raw_acl[8:]:
            raise AclSnapshotError("ace_raw_mismatch")
        result = AclSnapshot(path, DaclState.PRESENT, defaulted, tuple(copied), raw_acl)
        result.verify_integrity()
        return result
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            boundary.local_free(descriptor)
        except BaseException as cleanup:
            if primary is None:
                raise
            if isinstance(primary, AclSnapshotError):
                primary.cleanup_error = cleanup
                primary.args = (f"{primary}; cleanup failed: {cleanup}",)
            else:
                primary.add_note(f"LocalFree failed: {cleanup}")


# ---------------------------------------------------------------------------
# Worker MCP bridge pipe (NF-2026-00034)
# ---------------------------------------------------------------------------
#
# The worker MCP server holds the request's audit key, reads canonical state
# and applies edits under its own authority checks, so it runs on the host
# and the contained worker reaches it through one named pipe.  Measured on
# Windows 11 26200 from a repo-scoped container with internetClient only:
#   * a host pipe whose DACL names the owner user and the container SID is
#     opened read/write from inside; with the container ACE removed the open
#     fails EPERM.  No mandatory label is needed (Low-IL clients connected
#     with and without S:(ML;;NW;;;LW)), so none is set.
#   * ``\\.\pipe\`` is LISTABLE from inside a container, and every request of
#     one repo + adapter shares one container SID -- so neither the name nor
#     the DACL keeps a sibling request out.  WorkerPipe.accept admits only a
#     client process inside this launch's own job.

_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_FILE_FLAG_OVERLAPPED = 0x40000000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_PIPE_BUFFER_BYTES = 65536
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_ERROR_PIPE_NOT_CONNECTED = 233
_ERROR_PIPE_CONNECTED = 535
_ERROR_OPERATION_ABORTED = 995
_ERROR_IO_PENDING = 997
_PIPE_GONE = frozenset(
    (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_PIPE_NOT_CONNECTED, _ERROR_OPERATION_ABORTED)
)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_USER_CLASS = 1
_WORKER_PIPE_PREFIX = "\\\\.\\pipe\\aiworkhub-worker-"
# Only these characters ever reach a pipe name, so the name is safe to embed
# in the shim's quoted script and in a JSON config verbatim.
_WORKER_PIPE_NAME = re.compile(
    r"\\\\\.\\pipe\\aiworkhub-worker-[A-Za-z0-9_-]{1,128}-[0-9a-f]{32}\Z"
)
_SID_STRING = re.compile(r"S-1-(?:\d+-){1,15}\d+\Z")


def new_worker_pipe_name(request_id: str) -> str:
    """A fresh, per-request pipe name.  Unguessable, but NOT secret -- see
    the section note: access rests on the DACL and the job check."""
    name = f"{_WORKER_PIPE_PREFIX}{request_id}-{secrets.token_hex(16)}"
    if not _WORKER_PIPE_NAME.match(name):
        raise ValueError(f"request_id cannot name a worker pipe: {request_id!r}")
    return name


# The contained end: the program the worker's MCP config starts, relaying its
# stdio to the pipe.  Measured from inside a container: node.exe under
# Program Files cannot be started there (its DACL has no ALL APPLICATION
# PACKAGES ACE; opening it fails EPERM, though a HOST-created top-level
# process of the same image runs), claude.exe has no script mode, and
# System32's Windows PowerShell 5.1 is readable by every AppContainer and
# starts as a child of a contained process in about 0.5 s.  The name reaches
# the script only through _WORKER_PIPE_NAME's alphabet, and -EncodedCommand
# keeps the script out of command-line quoting altogether.
#   No cmdlet is used: auto-loading one printed a CLIXML progress record on
# stderr ("Preparing modules for first use"), and .NET alone needs no module.
_PIPE_SHIM_SCRIPT = (
    "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
    "$p=[IO.Pipes.NamedPipeClientStream]::new('.','{name}',"
    "[IO.Pipes.PipeDirection]::InOut,[IO.Pipes.PipeOptions]::Asynchronous);"
    "$p.Connect(30000);"
    "$i=[Console]::OpenStandardInput();$o=[Console]::OpenStandardOutput();"
    "$up=$i.CopyToAsync($p);$down=$p.CopyToAsync($o);"
    "[void][Threading.Tasks.Task]::WaitAny(@($up,$down));$o.Flush()"
)


def worker_pipe_shim_argv(pipe_name: str) -> list[str]:
    """The contained stdio<->pipe shim for ``pipe_name`` (see above)."""
    if not _WORKER_PIPE_NAME.match(pipe_name):
        raise ValueError(f"not a worker pipe name: {pipe_name!r}")
    windows = _system_windows_directory() if os.name == "nt" else ""
    if not windows:
        raise AppContainerError(
            AppContainerReason.PLATFORM_UNSUPPORTED,
            detail="the worker MCP pipe shim needs the Windows directory.",
        )
    script = _PIPE_SHIM_SCRIPT.replace("{name}", pipe_name[len("\\\\.\\pipe\\"):])
    return [
        os.path.join(windows, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        "-NoLogo", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
    ]


def worker_pipe_sddl(owner_sid: str, container_sid: str) -> str:
    """The pipe's security descriptor: a protected DACL allowing the owner
    user everything and exactly one container SID read/write -- nothing for
    ALL APPLICATION PACKAGES, Everyone, or anyone else."""
    for sid in (owner_sid, container_sid):
        if not _SID_STRING.match(sid):
            raise ValueError(f"not a SID string: {sid!r}")
    return f"D:P(A;;GA;;;{owner_sid})(A;;GRGW;;;{container_sid})"


def container_sid(repo_id: str, worker_kind: str, *, api: Win32Api | None = None) -> str:
    """The SID :func:`launch_appcontainer` confines ``(repo_id, worker_kind)``
    to -- the same derivation, creating the profile if it is missing."""
    boundary = api if api is not None else _load_win32()
    identity: _Identity = _step(
        "derive_appcontainer_sid",
        lambda: boundary.derive_identity(*derive_container_identity(repo_id, worker_kind)),
    )
    try:
        return identity.sid_string
    finally:
        boundary.free_identity(identity)


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


def _pipe_libraries() -> tuple[Any, Any]:
    k = _load_windows_dll("kernel32")
    a = _load_windows_dll("advapi32")
    handle, dword, bool_ = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    overlapped = ctypes.POINTER(_OVERLAPPED)
    k.CreateNamedPipeW.restype = handle
    k.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, dword, dword, dword, dword, dword, dword,
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
    ]
    k.CreateEventW.restype = handle
    k.CreateEventW.argtypes = [wintypes.LPVOID, bool_, bool_, wintypes.LPCWSTR]
    k.ConnectNamedPipe.restype = bool_
    k.ConnectNamedPipe.argtypes = [handle, overlapped]
    k.DisconnectNamedPipe.restype = bool_
    k.DisconnectNamedPipe.argtypes = [handle]
    k.ReadFile.restype = bool_
    k.ReadFile.argtypes = [handle, wintypes.LPVOID, dword, wintypes.LPVOID, overlapped]
    k.WriteFile.restype = bool_
    k.WriteFile.argtypes = [handle, wintypes.LPCVOID, dword, wintypes.LPVOID, overlapped]
    k.GetOverlappedResult.restype = bool_
    k.GetOverlappedResult.argtypes = [handle, overlapped, ctypes.POINTER(dword), bool_]
    k.CancelIoEx.restype = bool_
    k.CancelIoEx.argtypes = [handle, overlapped]
    k.WaitForSingleObject.restype = dword
    k.WaitForSingleObject.argtypes = [handle, dword]
    k.GetNamedPipeClientProcessId.restype = bool_
    k.GetNamedPipeClientProcessId.argtypes = [handle, ctypes.POINTER(wintypes.ULONG)]
    k.OpenProcess.restype = handle
    k.OpenProcess.argtypes = [dword, bool_, dword]
    k.IsProcessInJob.restype = bool_
    k.IsProcessInJob.argtypes = [handle, handle, ctypes.POINTER(bool_)]
    k.GetCurrentProcess.restype = handle
    k.CloseHandle.restype = bool_
    k.CloseHandle.argtypes = [handle]
    k.LocalFree.restype = wintypes.HLOCAL
    k.LocalFree.argtypes = [wintypes.HLOCAL]
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = bool_
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, dword, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID,
    ]
    a.OpenProcessToken.restype = bool_
    a.OpenProcessToken.argtypes = [handle, dword, ctypes.POINTER(handle)]
    a.GetTokenInformation.restype = bool_
    a.GetTokenInformation.argtypes = [handle, ctypes.c_int, wintypes.LPVOID, dword, ctypes.POINTER(dword)]
    a.ConvertSidToStringSidW.restype = bool_
    a.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    return k, a


def _token_user_sid(k: Any, a: Any) -> str:
    """This process's user SID as a string, from its token."""
    token = wintypes.HANDLE()
    if not a.OpenProcessToken(k.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _Win32Failure(_last_win_error(), "create_worker_pipe", "OpenProcessToken")
    try:
        size = wintypes.DWORD(0)
        a.GetTokenInformation(token, _TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(max(size.value, ctypes.sizeof(_TOKEN_USER)))
        if not a.GetTokenInformation(
            token, _TOKEN_USER_CLASS, buffer, len(buffer), ctypes.byref(size)
        ):
            raise _Win32Failure(_last_win_error(), "create_worker_pipe", "GetTokenInformation")
        text = wintypes.LPWSTR()
        user = _TOKEN_USER.from_buffer(buffer)
        if not a.ConvertSidToStringSidW(user.User.Sid, ctypes.byref(text)):
            raise _Win32Failure(_last_win_error(), "create_worker_pipe", "ConvertSidToStringSidW")
        try:
            return text.value or ""
        finally:
            k.LocalFree(text)
    finally:
        k.CloseHandle(token)


class WorkerPipe:
    """Host end of one request's worker MCP pipe.

    One instance (``FILE_FLAG_FIRST_PIPE_INSTANCE``: a squatter makes creation
    fail), local clients only, overlapped so a read and a write can be in
    flight at once, DACL from :func:`worker_pipe_sddl`.  :meth:`accept` serves
    the first client whose process is in the given job and disconnects anyone
    else.  :meth:`shutdown` cancels every pending operation and makes later
    ones fail fast; :meth:`close` then releases the handle, which removes the
    pipe.  Reads return ``b""`` and writes raise ``BrokenPipeError`` once the
    pipe is gone or shut down.
    """

    def __init__(self, name: str, container_sid: str) -> None:
        if not _WORKER_PIPE_NAME.match(name):
            raise ValueError(f"not a worker pipe name: {name!r}")
        self.name = name
        self._k, a = _pipe_libraries()
        sddl = worker_pipe_sddl(_token_user_sid(self._k, a), container_sid)
        descriptor = wintypes.LPVOID()
        if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise _Win32Failure(_last_win_error(), "create_worker_pipe", "security descriptor")
        try:
            attributes = _SECURITY_ATTRIBUTES(
                ctypes.sizeof(_SECURITY_ATTRIBUTES), descriptor, False
            )
            handle = self._k.CreateNamedPipeW(
                name,
                _PIPE_ACCESS_DUPLEX | _FILE_FLAG_FIRST_PIPE_INSTANCE | _FILE_FLAG_OVERLAPPED,
                _PIPE_REJECT_REMOTE_CLIENTS,  # byte type, byte read mode, blocking
                1,
                _PIPE_BUFFER_BYTES,
                _PIPE_BUFFER_BYTES,
                0,
                ctypes.byref(attributes),
            )
        finally:
            self._k.LocalFree(descriptor)
        if not handle or handle == wintypes.HANDLE(-1).value:
            raise _Win32Failure(_last_win_error(), "create_worker_pipe", name)
        self._handle: Any = handle
        self._lock = threading.Condition()
        self._shut = False
        self._pending = 0

    def _io(
        self, start: Callable[[Any], bool], operation: str, deadline: float | None = None
    ) -> int:
        """One overlapped call, to completion: the byte count, or -1 when the
        pipe is gone, shut down, or ``deadline`` (monotonic) passed first.
        Issued under the lock that shutdown takes, so nothing can start after
        CancelIoEx and wait forever."""
        event = self._k.CreateEventW(None, True, False, None)
        if not event:
            raise _Win32Failure(_last_win_error(), operation, "CreateEventW")
        overlapped = _OVERLAPPED()
        overlapped.hEvent = event
        try:
            with self._lock:
                if self._shut:
                    return -1
                self._pending += 1
                try:
                    error = 0 if start(ctypes.byref(overlapped)) else _last_win_error()
                except BaseException:
                    self._pending -= 1
                    raise
            try:
                if error == _ERROR_PIPE_CONNECTED:
                    return 0
                if error in _PIPE_GONE:
                    return -1
                if error not in (0, _ERROR_IO_PENDING):
                    raise _Win32Failure(error, operation)
                if deadline is not None:
                    wait_ms = max(0, int((deadline - time.monotonic()) * 1000))
                    if self._k.WaitForSingleObject(event, wait_ms) != _WAIT_OBJECT_0:
                        # Cancel it; GetOverlappedResult below then reports the
                        # abort once the operation has really stopped.
                        self._k.CancelIoEx(self._handle, ctypes.byref(overlapped))
                done = wintypes.DWORD()
                if not self._k.GetOverlappedResult(
                    self._handle, ctypes.byref(overlapped), ctypes.byref(done), True
                ):
                    error = _last_win_error()
                    if error in _PIPE_GONE:
                        return -1
                    raise _Win32Failure(error, operation)
                return int(done.value)
            finally:
                with self._lock:
                    self._pending -= 1
                    self._lock.notify_all()
        finally:
            self._k.CloseHandle(event)

    def accept(self, job: Any, timeout: float | None = None) -> bool:
        """Wait for a client of ``job``; False once shut down or ``timeout``
        seconds have passed.

        A process of a sibling request (same SID, other job) that keeps
        reconnecting is dropped each time but can hold the only instance for
        a moment each round, delaying our own client until ``timeout``: a
        denial of service against that one request, never access to it.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            connected = self._io(
                lambda ov: self._k.ConnectNamedPipe(self._handle, ov),
                "connect_worker_pipe",
                deadline,
            )
            if connected < 0:
                return False
            if self._client_in_job(job):
                return True
            self._k.DisconnectNamedPipe(self._handle)  # not ours: never served

    def _client_in_job(self, job: Any) -> bool:
        pid = wintypes.ULONG()
        if not self._k.GetNamedPipeClientProcessId(self._handle, ctypes.byref(pid)):
            return False
        process = self._k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not process:
            return False
        try:
            inside = wintypes.BOOL()
            return bool(self._k.IsProcessInJob(process, job, ctypes.byref(inside))) and bool(
                inside.value
            )
        finally:
            self._k.CloseHandle(process)

    def read(self) -> bytes:
        buffer = ctypes.create_string_buffer(_PIPE_BUFFER_BYTES)
        count = self._io(
            lambda ov: self._k.ReadFile(self._handle, buffer, _PIPE_BUFFER_BYTES, None, ov),
            "read_worker_pipe",
        )
        return buffer.raw[:count] if count > 0 else b""

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            chunk = bytes(view[:_PIPE_BUFFER_BYTES])
            count = self._io(
                lambda ov: self._k.WriteFile(self._handle, chunk, len(chunk), None, ov),
                "write_worker_pipe",
            )
            if count <= 0:
                raise BrokenPipeError(self.name)
            view = view[count:]

    def shutdown(self) -> None:
        """Cancel every pending operation; later ones return at once."""
        with self._lock:
            if not self._shut:
                self._shut = True
                if self._handle is not None:
                    self._k.CancelIoEx(self._handle, None)

    def close(self, timeout: float = 5.0) -> bool:
        """shutdown(), then release the handle once no operation still uses
        it.  False leaves the handle to process exit rather than free it
        under a caller that has not returned yet."""
        self.shutdown()
        with self._lock:
            if self._handle is None:
                return True
            if not self._lock.wait_for(lambda: self._pending == 0, timeout):
                return False
            self._k.CloseHandle(self._handle)
            self._handle = None
            return True
