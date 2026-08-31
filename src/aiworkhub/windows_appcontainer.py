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

import ctypes
import enum
import hashlib
import os
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

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


# Maps a low-level Win32 operation name to its structured reason.  The
# operation names are also the boundary method call-sites, which keeps the
# taxonomy in exactly one place.
_OPERATION_REASON: dict[str, AppContainerReason] = {
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
        """Release the process and job handles.  Idempotent."""
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
    (``free_*``, ``delete_*``, ``close_*``, ``terminate_*``) must be tolerant
    of being invoked during unwind and must not raise.
    """

    def derive_identity(
        self, name: str, display_name: str, description: str
    ) -> _Identity: ...

    def free_identity(self, identity: _Identity) -> None: ...

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


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


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

    if api is None:
        if not platform_supported():
            raise AppContainerError(
                AppContainerReason.PLATFORM_UNSUPPORTED,
                detail="AppContainer launch requires Windows (os.name=='nt').",
            )
        api = _load_win32()

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
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        k = self._kernel32
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

        k.DeriveCapabilitySidsFromName.restype = wintypes.BOOL
        k.DeriveCapabilitySidsFromName.argtypes = [
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
        ok = self._kernel32.DeriveCapabilitySidsFromName(
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
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
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
