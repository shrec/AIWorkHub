"""Run every AppContainer launch inside a private NewCredentials logon session.

OWNER DECISION (NF-2026-01154): no AIWorkHub launch may ever add a DOS drive
letter to the owner's interactive logon session or Explorer, even temporarily.
``windows_appcontainer.sandbox_drive`` defines its letter with
``DefineDosDeviceW`` in the *caller's* logon session, so both production
callers -- the worker supervisor and the accept/finalize validation lane --
must run inside a logon session that is NOT the owner's before they ever reach
that call.

MEASURED (manager host, non-elevated): ``advapi32.CreateProcessWithLogonW``
with ``LOGON_NETCREDENTIALS_ONLY`` starts the *same* user/token in a *new*
logon session whose ``SECURITY_LOGON_SESSION_DATA.LogonType`` is ``9``
(``NewCredentials``; the owner's interactive session is ``2``), inherited by
every descendant.  A drive letter defined there is visible to it and its
children and never to the owner session, and it dies with the session.

This module provides three things, all import-safe on non-Windows hosts (no
Windows symbol is resolved at import time):

* :func:`in_private_logon_session` -- is *this* process already in a type-9
  session, read from its own token with typed ``ctypes`` structs;
* :func:`spawn_in_private_logon_session` -- the ``CreateProcessWithLogonW``
  spawn primitive: an explicit environment block, launched suspended, placed
  in a kill-on-close Job before ``ResumeThread``; any failure terminates the
  suspended process and raises, and the inner exit code is relayed;
* :func:`reexec_supervisor_if_owner_session` -- the worker-lane hop: a worker
  supervisor started in the owner session re-execs itself through the spawn
  primitive BEFORE it reads stdin or unlinks the spec, stays alive as the
  launcher-tracked pid, and exits with the inner code.  The inner copy is
  private, so it takes the direct path unchanged;
* :func:`run_appcontainer_validation_hopped` -- the validation-lane hop: an
  owner-session ``run_validations`` (the MCP server's finalize/accept lane)
  relays one AppContainer validation to a private helper that runs
  ``worker_workspace._run_appcontainer_validation`` there and hands back the
  same ``CompletedProcess`` / ``TimeoutExpired`` / ``OSError`` shapes.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Mapping

__all__ = [
    "PrivateLogonSessionError",
    "PrivateLogonSessionTimeout",
    "SECURITY_LOGON_TYPE_NEW_CREDENTIALS",
    "current_logon_type",
    "in_private_logon_session",
    "spawn_in_private_logon_session",
    "reexec_supervisor_if_owner_session",
    "run_appcontainer_validation_hopped",
    "validation_relay_main",
]


# SECURITY_LOGON_TYPE.NewCredentials: the type a LOGON_NETCREDENTIALS_ONLY
# CreateProcessWithLogonW session reports; the owner's interactive session is 2.
SECURITY_LOGON_TYPE_NEW_CREDENTIALS = 9

_LOGON_NETCREDENTIALS_ONLY = 0x00000002
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NO_WINDOW = 0x08000000
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x00000001
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_TOKEN_QUERY = 0x00000008
_TOKEN_STATISTICS_CLASS = 10
_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12
# seclogon never validates these with LOGON_NETCREDENTIALS_ONLY -- the child
# runs as the caller -- so the domain is local and the password is a placeholder.
_SECLOGON_DOMAIN = "."
_SECLOGON_PLACEHOLDER_PASSWORD = "-"


class PrivateLogonSessionError(RuntimeError):
    """A CreateProcessWithLogonW hop could not complete.

    Carries the offending ``win_error`` (``GetLastError``) when the OS supplied
    one, so a seclogon-disabled host surfaces as a named launch failure rather
    than a silent fall-through to the owner session.
    """

    def __init__(self, detail: str, *, win_error: int | None = None) -> None:
        self.detail = detail
        self.win_error = win_error
        suffix = f" win_error={win_error}" if win_error is not None else ""
        super().__init__(f"{detail}{suffix}")


class PrivateLogonSessionTimeout(PrivateLogonSessionError):
    """The hopped process outlived its deadline; it and its Job were killed."""


def _appcontainer() -> Any:
    """The ``windows_appcontainer`` module, under package or direct-script import.

    The worker supervisor runs as a top-level script, so a bare ``from .`` here
    raises ``ImportError: no known parent package``; both spellings are tried,
    exactly as the supervisor itself does.
    """

    try:
        from . import windows_appcontainer
    except ImportError:  # direct-script entrypoint
        import windows_appcontainer  # type: ignore[no-redef]
    return windows_appcontainer


def _job_limit_information() -> Any:
    try:
        from .windows_job_structures import JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    except ImportError:  # direct-script entrypoint
        from windows_job_structures import JOBOBJECT_EXTENDED_LIMIT_INFORMATION  # type: ignore[no-redef]
    return JOBOBJECT_EXTENDED_LIMIT_INFORMATION


# --------------------------------------------------------------------------- #
# Typed ctypes structures (classes carry no methods, so they are not Win32
# symbol references at import time and resolve fine off-platform).
# --------------------------------------------------------------------------- #


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _TOKEN_STATISTICS(ctypes.Structure):
    _fields_ = [
        ("TokenId", _LUID),
        ("AuthenticationId", _LUID),
        ("ExpirationTime", ctypes.c_longlong),
        ("TokenType", ctypes.c_int),
        ("ImpersonationLevel", ctypes.c_int),
        ("DynamicCharged", wintypes.DWORD),
        ("DynamicAvailable", wintypes.DWORD),
        ("GroupCount", wintypes.DWORD),
        ("PrivilegeCount", wintypes.DWORD),
        ("ModifiedId", _LUID),
    ]


class _LSA_UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _SECURITY_LOGON_SESSION_DATA(ctypes.Structure):
    _fields_ = [
        ("Size", wintypes.ULONG),
        ("LogonId", _LUID),
        ("UserName", _LSA_UNICODE_STRING),
        ("LogonDomain", _LSA_UNICODE_STRING),
        ("AuthenticationPackage", _LSA_UNICODE_STRING),
        ("LogonType", wintypes.ULONG),
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


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


# --------------------------------------------------------------------------- #
# Logon-type probe
# --------------------------------------------------------------------------- #


def current_logon_type() -> int | None:
    """Return this process token's ``SECURITY_LOGON_TYPE``, or ``None``.

    Reads ``TokenStatistics.AuthenticationId`` from the current process token
    and resolves it through ``LsaGetLogonSessionData``.  Fails closed to
    ``None`` -- off Windows, or on any failure to read the session -- because an
    unknown session must never be treated as the private one.
    """

    windows_appcontainer = _appcontainer()

    if not windows_appcontainer.platform_supported():
        return None
    try:
        kernel32 = windows_appcontainer._load_windows_dll("kernel32")
        advapi32 = windows_appcontainer._load_windows_dll("advapi32")
        secur32 = windows_appcontainer._load_windows_dll("secur32")
    except (OSError, AttributeError, ValueError):
        return None
    try:
        token = wintypes.HANDLE()
        process = ctypes.c_void_p(kernel32.GetCurrentProcess())
        if not advapi32.OpenProcessToken(
            process, _TOKEN_QUERY, ctypes.byref(token)
        ):
            return None
        try:
            statistics = _TOKEN_STATISTICS()
            returned = wintypes.DWORD(0)
            if not advapi32.GetTokenInformation(
                token,
                _TOKEN_STATISTICS_CLASS,
                ctypes.byref(statistics),
                ctypes.sizeof(statistics),
                ctypes.byref(returned),
            ):
                return None
            session = ctypes.POINTER(_SECURITY_LOGON_SESSION_DATA)()
            status = secur32.LsaGetLogonSessionData(
                ctypes.byref(statistics.AuthenticationId), ctypes.byref(session)
            )
            if status != 0 or not session:
                return None
            try:
                return int(session.contents.LogonType)
            finally:
                secur32.LsaFreeReturnBuffer(session)
        finally:
            kernel32.CloseHandle(token)
    except (OSError, AttributeError, ValueError):
        return None


def in_private_logon_session() -> bool:
    """Whether this process already runs in a private NewCredentials session."""

    return current_logon_type() == SECURITY_LOGON_TYPE_NEW_CREDENTIALS


# --------------------------------------------------------------------------- #
# CreateProcessWithLogonW spawn primitive
# --------------------------------------------------------------------------- #

# One table drives every signature, so the boundary is configured by a loop
# rather than a long uniform block of per-call assignments.
_SPAWN_SIGNATURES: tuple[tuple[str, str, list[Any], Any], ...] = (
    ("advapi32", "CreateProcessWithLogonW", [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, wintypes.LPVOID,
        wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
    ], wintypes.BOOL),
    ("kernel32", "GetStdHandle", [wintypes.DWORD], wintypes.HANDLE),
    ("kernel32", "CreateJobObjectW", [wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE),
    ("kernel32", "SetInformationJobObject",
     [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL),
    ("kernel32", "AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
    ("kernel32", "ResumeThread", [wintypes.HANDLE], wintypes.DWORD),
    ("kernel32", "TerminateProcess", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
    ("kernel32", "WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
    ("kernel32", "GetExitCodeProcess",
     [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
    ("kernel32", "SetHandleInformation",
     [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD], wintypes.BOOL),
    ("kernel32", "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
)


class _CtypesSpawnApi:
    """The real Win32 boundary for the spawn primitive.

    Built only on Windows; every signature is set from ``_SPAWN_SIGNATURES`` so
    ctypes never silently truncates a 64-bit HANDLE to a C ``int``.
    """

    def __init__(self) -> None:
        windows_appcontainer = _appcontainer()

        libraries = {
            "kernel32": windows_appcontainer._load_windows_dll("kernel32"),
            "advapi32": windows_appcontainer._load_windows_dll("advapi32"),
        }
        for library_name, export, argtypes, restype in _SPAWN_SIGNATURES:
            function = getattr(libraries[library_name], export)
            function.argtypes = argtypes
            function.restype = restype
        self._kernel32 = libraries["kernel32"]
        self._advapi32 = libraries["advapi32"]

    def std_handle(self, which: int) -> int:
        return int(self._kernel32.GetStdHandle(wintypes.DWORD(which)))

    def _win_error(self) -> int:
        return int(ctypes.get_last_error())

    def create_process_with_logon(
        self,
        username: str,
        command_line: str,
        creation_flags: int,
        environment_block: str | None,
        working_directory: str | None,
        stdin_handle: int | None,
        stdout_handle: int | None,
        stderr_handle: int | None,
    ) -> tuple[int, int, int]:
        startup = _STARTUPINFOW()
        startup.cb = ctypes.sizeof(startup)
        if stdin_handle is not None or stdout_handle is not None or stderr_handle is not None:
            startup.dwFlags |= _STARTF_USESTDHANDLES
            for handle in (stdin_handle, stdout_handle, stderr_handle):
                if handle is not None:
                    self._kernel32.SetHandleInformation(
                        wintypes.HANDLE(handle), _HANDLE_FLAG_INHERIT, _HANDLE_FLAG_INHERIT
                    )
            startup.hStdInput = stdin_handle
            startup.hStdOutput = stdout_handle
            startup.hStdError = stderr_handle
        block = None
        if environment_block is not None:
            block = ctypes.create_unicode_buffer(environment_block)
        info = _PROCESS_INFORMATION()
        command_buffer = ctypes.create_unicode_buffer(command_line)
        ok = self._advapi32.CreateProcessWithLogonW(
            username,
            _SECLOGON_DOMAIN,
            _SECLOGON_PLACEHOLDER_PASSWORD,
            _LOGON_NETCREDENTIALS_ONLY,
            None,
            command_buffer,
            creation_flags,
            ctypes.cast(block, wintypes.LPVOID) if block is not None else None,
            working_directory,
            ctypes.byref(startup),
            ctypes.byref(info),
        )
        if not ok:
            raise PrivateLogonSessionError(
                "create_process_with_logon_failed", win_error=self._win_error()
            )
        return int(info.hProcess or 0), int(info.hThread or 0), int(info.dwProcessId or 0)

    def create_kill_on_close_job(self) -> int:
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise PrivateLogonSessionError("create_job_failed", win_error=self._win_error())
        limits = _job_limit_information()()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            wintypes.HANDLE(handle),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = self._win_error()
            self._kernel32.CloseHandle(wintypes.HANDLE(handle))
            raise PrivateLogonSessionError("configure_job_failed", win_error=error)
        return int(handle)

    def assign_process_to_job(self, job: int, process: int) -> None:
        if not self._kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(job), wintypes.HANDLE(process)
        ):
            raise PrivateLogonSessionError("assign_process_failed", win_error=self._win_error())

    def resume_thread(self, thread: int) -> None:
        if self._kernel32.ResumeThread(wintypes.HANDLE(thread)) == 0xFFFFFFFF:
            raise PrivateLogonSessionError("resume_thread_failed", win_error=self._win_error())

    def terminate_process(self, process: int, exit_code: int = 1) -> None:
        self._kernel32.TerminateProcess(wintypes.HANDLE(process), wintypes.UINT(exit_code))

    def wait(self, process: int, timeout_ms: int) -> None:
        state = self._kernel32.WaitForSingleObject(
            wintypes.HANDLE(process), wintypes.DWORD(timeout_ms)
        )
        if state == _WAIT_TIMEOUT:
            raise PrivateLogonSessionTimeout("wait_process_timeout")
        if state != _WAIT_OBJECT_0:
            raise PrivateLogonSessionError("wait_process_failed", win_error=self._win_error())

    def exit_code(self, process: int) -> int:
        value = wintypes.DWORD(0)
        if not self._kernel32.GetExitCodeProcess(
            wintypes.HANDLE(process), ctypes.byref(value)
        ):
            raise PrivateLogonSessionError("exit_code_failed", win_error=self._win_error())
        return int(value.value)

    def close_handle(self, handle: int | None) -> None:
        if handle:
            self._kernel32.CloseHandle(wintypes.HANDLE(handle))


def _environment_block(environment: Mapping[str, str] | None) -> str | None:
    return _appcontainer()._environment_block_text(environment)


def spawn_in_private_logon_session(
    command_line: str,
    *,
    environment: Mapping[str, str] | None,
    working_directory: str | None = None,
    stdin_handle: int | None = None,
    stdout_handle: int | None = None,
    stderr_handle: int | None = None,
    username: str | None = None,
    api: Any | None = None,
    timeout_seconds: float | None = None,
) -> int:
    """Launch ``command_line`` in a new private logon session and relay its exit.

    The child is created suspended, placed in a kill-on-close Job, and only then
    resumed, so no grandchild can escape the Job even in the window before the
    first wait.  If assignment or resume fails the suspended process is
    terminated and every handle closed before the typed error is raised.  The
    inner exit code is returned; closing the Job here tears the inner process
    down with it.  With ``timeout_seconds`` a process still running at the
    deadline is terminated, its Job closed (killing every descendant), and
    :class:`PrivateLogonSessionTimeout` raised.
    """

    if api is None:
        api = _CtypesSpawnApi()
    if username is None:
        username = _current_username()
    environment_block = _environment_block(environment)
    creation_flags = _CREATE_SUSPENDED | _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT
    process, thread, _pid = api.create_process_with_logon(
        username,
        command_line,
        creation_flags,
        environment_block,
        working_directory,
        stdin_handle,
        stdout_handle,
        stderr_handle,
    )
    job: int | None = None
    try:
        job = api.create_kill_on_close_job()
        api.assign_process_to_job(job, process)
        api.resume_thread(thread)
    except BaseException:
        try:
            api.terminate_process(process)
        except Exception:
            pass
        api.close_handle(thread)
        api.close_handle(process)
        if job is not None:
            api.close_handle(job)
        raise
    wait_ms = _INFINITE
    if timeout_seconds is not None:
        wait_ms = max(0, min(int(timeout_seconds * 1000), _INFINITE - 1))
    try:
        try:
            api.wait(process, wait_ms)
        except BaseException:
            # Deadline or failed wait: never leave it running. Closing the Job
            # below then kills every descendant it started.
            try:
                api.terminate_process(process)
            except Exception:
                pass
            raise
        return api.exit_code(process)
    finally:
        api.close_handle(thread)
        api.close_handle(process)
        api.close_handle(job)


def _current_username() -> str:
    """The caller's own account name; LOGON_NETCREDENTIALS_ONLY runs as them."""

    try:
        import getpass

        name = getpass.getuser()
        return name or "aiworkhub"
    except Exception:
        return "aiworkhub"


# --------------------------------------------------------------------------- #
# Worker-supervisor hop (NF-2026-01154)
# --------------------------------------------------------------------------- #


# Set on the inner supervisor's environment by the worker-lane hop.
_PRIVATE_SESSION_HOP_ENV = "AIWORKHUB_PRIVATE_SESSION_HOP"


def _supervisor_script() -> str:
    try:
        from . import worker_supervisor
    except ImportError:  # direct-script entrypoint
        import worker_supervisor  # type: ignore[no-redef]
    return str(worker_supervisor.__file__)


def _read_spec_without_unlinking(spec_path: Any) -> dict[str, Any] | None:
    """Load the launch spec read-only; the private copy that reads stdin unlinks it."""

    try:
        from . import worker_supervisor
    except ImportError:  # direct-script entrypoint
        import worker_supervisor  # type: ignore[no-redef]
    try:
        spec = worker_supervisor._load_spec(spec_path)
    except Exception:
        return None
    return spec if isinstance(spec, dict) else None


def reexec_supervisor_if_owner_session(spec_path: Any) -> int | None:
    """Re-exec this supervisor through the private-session hop, or return ``None``.

    Returns the inner supervisor's exit code when the hop ran -- the caller
    must exit with it, as the launcher-tracked pid, having read neither stdin
    nor the spec.  Returns ``None`` (continue directly) when the host is not
    Windows, this process is already private, or the spec does not name the
    AppContainer backend.  The inherited stdin handle is handed straight to the
    inner process, so the prompt payload stays byte-exact.
    """

    windows_appcontainer = _appcontainer()

    if not windows_appcontainer.platform_supported():
        return None
    if in_private_logon_session():
        return None
    if os.environ.get(_PRIVATE_SESSION_HOP_ENV):
        # Already the far side of a hop, yet the probe still reads "owner": a
        # named launch failure, never a second hop (one seclogon session per level).
        raise PrivateLogonSessionError("private_session_hop_not_private")
    spec = _read_spec_without_unlinking(spec_path)
    if not spec or spec.get("execution_backend") != "windows_appcontainer":
        return None
    api = _CtypesSpawnApi()
    command_line = windows_appcontainer.build_command_line(
        [sys.executable, _supervisor_script(), "--spec", str(spec_path)]
    )
    return spawn_in_private_logon_session(
        command_line,
        environment=dict(os.environ, **{_PRIVATE_SESSION_HOP_ENV: "1"}),
        working_directory=os.getcwd(),
        stdin_handle=api.std_handle(_STD_INPUT_HANDLE),
        stdout_handle=api.std_handle(_STD_OUTPUT_HANDLE),
        stderr_handle=api.std_handle(_STD_ERROR_HANDLE),
        api=api,
    )


# --------------------------------------------------------------------------- #
# Validation-lane hop (NF-2026-01154)
# --------------------------------------------------------------------------- #
# The finalize/accept lane runs ``worker_workspace.run_validations`` inside the
# MCP server, in the owner's interactive session, and never inside a worker
# supervisor.  Each AppContainer validation is therefore relayed to a helper in
# a private session: the spec goes in on the helper's stdin and exactly one
# result record comes back on its stdout.  Pipes, not files: the request root
# must keep holding nothing but ``worktree`` and ``home``
# (``_appcontainer_request_root``), and a result file in a container-writable
# directory could be planted by the candidate code it is meant to judge.

_VALIDATION_RELAY_SCHEMA = "aiworkhub.appcontainer_validation_relay.v1"
# The helper enforces ``timeout_seconds`` on the AppContainer itself; this grace
# only covers its interpreter start-up and the result write.  Past it the hop
# Job is closed and the whole private tree dies with it.
_VALIDATION_RELAY_GRACE_SECONDS = 60
# Each relayed stream (spec in, result out) is bounded: a larger one is a
# failure, never a silent truncation.
_MAX_VALIDATION_RELAY_BYTES = 256 * 1024 * 1024
_MAX_VALIDATION_RELAY_DIAGNOSTIC_BYTES = 8192
_VALIDATION_RELAY_DRAIN_JOIN_SECONDS = 30
_VALIDATION_RELAY_ENTRY = (
    "from aiworkhub.windows_private_session import validation_relay_main as main;"
    " raise SystemExit(main())"
)


def _pipe_handle(fd: int) -> int:
    """The Win32 HANDLE behind one relay pipe end (the seam relay tests pin)."""

    return int(_appcontainer().native_handle(fd))


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _feed(fd: int, payload: bytes) -> None:
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view):]
    except OSError:
        pass  # the helper died unread; its missing result fails the hop closed
    finally:
        _close_quietly(fd)


def _drain(fd: int, sink: dict[str, Any], key: str, limit: int) -> None:
    chunks: list[bytes] = []
    size = 0
    oversized = False
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            if size + len(chunk) > limit:
                oversized = True  # keep draining so the writer never blocks
                continue
            chunks.append(chunk)
            size += len(chunk)
    except OSError:
        pass
    finally:
        sink[key] = (b"".join(chunks), oversized)


def _relay_environment() -> dict[str, str]:
    """The helper's own environment: the caller's, with this package importable."""

    environment = dict(os.environ)
    package_parent = str(Path(__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (package_parent, environment.get("PYTHONPATH")) if part
    )
    return environment


def _relay_through_private_session(
    payload: bytes, deadline_seconds: float
) -> tuple[int, tuple[bytes, bool] | None, bytes]:
    """Run the validation helper in a private session with ``payload`` on stdin.

    Returns the helper's exit code, its stdout as ``(bytes, oversized)`` --
    ``None`` when the stream never reached EOF -- and a bounded stderr head.
    """

    stdin_read, stdin_write = os.pipe()
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    captured: dict[str, Any] = {}
    threads = [
        threading.Thread(target=_feed, args=(stdin_write, payload), daemon=True),
        threading.Thread(
            target=_drain,
            args=(stdout_read, captured, "result", _MAX_VALIDATION_RELAY_BYTES),
            daemon=True,
        ),
        threading.Thread(
            target=_drain,
            args=(stderr_read, captured, "diagnostic", _MAX_VALIDATION_RELAY_DIAGNOSTIC_BYTES),
            daemon=True,
        ),
    ]
    try:
        for thread in threads:
            thread.start()
        # -P: ``-c`` would otherwise put the caller's cwd first on sys.path, so a
        # json.py or aiworkhub/ planted there would load in the helper -- which
        # runs with the owner's full token, before any confinement exists.
        exit_code = spawn_in_private_logon_session(
            _appcontainer().build_command_line(
                [sys.executable, "-P", "-c", _VALIDATION_RELAY_ENTRY]
            ),
            environment=_relay_environment(),
            working_directory=os.getcwd(),
            stdin_handle=_pipe_handle(stdin_read),
            stdout_handle=_pipe_handle(stdout_write),
            stderr_handle=_pipe_handle(stderr_write),
            timeout_seconds=deadline_seconds,
        )
    finally:
        # The helper and its Job are gone; releasing our copies of its pipe ends
        # lets each drain see EOF and unblocks a feeder it never read.
        for fd in (stdin_read, stdout_write, stderr_write):
            _close_quietly(fd)
        for thread in threads:
            thread.join(_VALIDATION_RELAY_DRAIN_JOIN_SECONDS)
        for fd in (stdout_read, stderr_read):
            _close_quietly(fd)
    diagnostic = captured.get("diagnostic", (b"", False))[0]
    return exit_code, captured.get("result"), diagnostic


def _relay_failure(reason: str, exit_code: int, diagnostic: bytes) -> OSError:
    tail = diagnostic.decode("utf-8", errors="replace").strip()
    return OSError(f"windows_private_session_validation_{reason}:exit={exit_code}:{tail}")


def _relayed_text(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("relayed stream is not text")
    return value


def _relayed_validation_result(
    exit_code: int, result: tuple[bytes, bool] | None, diagnostic: bytes
) -> subprocess.CompletedProcess[str]:
    """Map the helper's result record back onto ``subprocess.run``'s shapes.

    Fails closed: a helper that did not exit 0, or a missing, oversized,
    unparsable or unknown record, is an :class:`OSError`, never a pass.
    """

    if exit_code != 0:
        raise _relay_failure("relay_exit_nonzero", exit_code, diagnostic)
    if result is None or not result[0]:
        raise _relay_failure("result_missing", exit_code, diagnostic)
    raw, oversized = result
    if oversized:
        raise _relay_failure("result_oversized", exit_code, diagnostic)
    try:
        record = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise _relay_failure("result_unparsable", exit_code, diagnostic) from exc
    if not isinstance(record, dict) or record.get("schema_id") != _VALIDATION_RELAY_SCHEMA:
        raise _relay_failure("result_unparsable", exit_code, diagnostic)
    kind = record.get("kind")
    try:
        if kind == "completed":
            return subprocess.CompletedProcess(
                [str(part) for part in record["args"]],
                int(record["returncode"]),
                _relayed_text(record["stdout"]),
                _relayed_text(record["stderr"]),
            )
        if kind == "timeout":
            raise subprocess.TimeoutExpired(
                [str(part) for part in record["args"]],
                float(record["timeout"]),
                output=_relayed_text(record["stdout"]),
                stderr=_relayed_text(record["stderr"]),
            )
        if kind == "oserror":
            errno_value, strerror = record.get("errno"), record.get("strerror")
            if isinstance(errno_value, int) and isinstance(strerror, str):
                raise OSError(errno_value, strerror)
            raise OSError(_relayed_text(record["message"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise _relay_failure("result_unparsable", exit_code, diagnostic) from exc
    raise _relay_failure("result_kind_unknown", exit_code, diagnostic)


def run_appcontainer_validation_hopped(
    argv: list[str],
    *,
    workspace: Any,
    adapter_id: str,
    cwd: str | Path,
    env: Mapping[str, str],
    timeout_seconds: int,
    run_direct: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    """Run one AppContainer validation inside a private logon session.

    Off Windows, or when this process is already private (a hopped supervisor,
    the relay helper itself), ``run_direct`` is called with exactly these
    arguments -- no double hop.  Otherwise the call is relayed to
    :func:`validation_relay_main` in a new ``CreateProcessWithLogonW`` session
    and its answer comes back in ``run_direct``'s own shapes: a
    :class:`subprocess.CompletedProcess`, a :class:`subprocess.TimeoutExpired`
    or an :class:`OSError`.  A helper still alive ``timeout_seconds`` plus a
    fixed grace after its start is killed with its whole private tree.
    """

    if not _appcontainer().platform_supported() or in_private_logon_session():
        return run_direct(
            argv,
            workspace=workspace,
            adapter_id=adapter_id,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )
    payload = json.dumps(
        {
            "schema_id": _VALIDATION_RELAY_SCHEMA,
            "argv": [str(part) for part in argv],
            "workspace": workspace.as_metadata(),
            "adapter_id": str(adapter_id),
            "cwd": str(cwd),
            "env": {str(key): str(value) for key, value in env.items()},
            "timeout_seconds": timeout_seconds,
        },
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > _MAX_VALIDATION_RELAY_BYTES:
        raise OSError(
            "windows_private_session_validation_spec_oversized:"
            f"{len(payload)}>{_MAX_VALIDATION_RELAY_BYTES}"
        )
    try:
        exit_code, result, diagnostic = _relay_through_private_session(
            payload, float(timeout_seconds) + _VALIDATION_RELAY_GRACE_SECONDS
        )
    except PrivateLogonSessionTimeout as exc:
        raise subprocess.TimeoutExpired(
            [str(part) for part in argv], timeout_seconds, output="", stderr=""
        ) from exc
    except PrivateLogonSessionError as exc:
        raise OSError(f"windows_private_session_validation_hop_failed:{exc}") from exc
    return _relayed_validation_result(exit_code, result, diagnostic)


def _captured_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _relayed_args(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(part) for part in value]
    return [str(value)]


def _run_relayed_validation(raw: bytes) -> dict[str, Any]:
    if len(raw) > _MAX_VALIDATION_RELAY_BYTES:
        raise PrivateLogonSessionError("validation_relay_spec_oversized")
    spec = json.loads(raw.decode("utf-8"))
    if not isinstance(spec, dict) or spec.get("schema_id") != _VALIDATION_RELAY_SCHEMA:
        raise PrivateLogonSessionError("validation_relay_spec_schema_mismatch")
    if not in_private_logon_session():
        # Never run the lane where the hop came from: that is the owner session.
        raise PrivateLogonSessionError("validation_relay_not_in_private_logon_session")
    from .worker_workspace import WorkerWorkspace, _run_appcontainer_validation

    try:
        completed = _run_appcontainer_validation(
            [str(part) for part in spec["argv"]],
            workspace=WorkerWorkspace.from_metadata(spec["workspace"]),
            adapter_id=str(spec["adapter_id"]),
            cwd=str(spec["cwd"]),
            env={str(key): str(value) for key, value in dict(spec["env"]).items()},
            timeout_seconds=spec["timeout_seconds"],
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "kind": "timeout",
            "args": _relayed_args(exc.cmd),
            "timeout": exc.timeout,
            "stdout": _captured_text(exc.output),
            "stderr": _captured_text(exc.stderr),
        }
    except OSError as exc:
        return {
            "kind": "oserror",
            "errno": exc.errno,
            "strerror": exc.strerror,
            "message": str(exc),
        }
    return {
        "kind": "completed",
        "args": _relayed_args(completed.args),
        "returncode": completed.returncode,
        "stdout": _captured_text(completed.stdout),
        "stderr": _captured_text(completed.stderr),
    }


def validation_relay_main() -> int:
    """Private-session side of :func:`run_appcontainer_validation_hopped`.

    Reads the spec from stdin and writes exactly one result record to stdout;
    anything else this process prints goes to stderr, so it cannot corrupt the
    record.  Any helper fault becomes an ``oserror`` record, which the parent
    raises: a fault is a failed launch, never a pass.
    """

    result_stream = sys.stdout.buffer
    sys.stdout = sys.stderr
    try:
        record = _run_relayed_validation(
            sys.stdin.buffer.read(_MAX_VALIDATION_RELAY_BYTES + 1)
        )
    except Exception as exc:
        record = {
            "kind": "oserror",
            "errno": None,
            "strerror": None,
            "message": (
                "windows_private_session_validation_relay_failed:"
                f"{type(exc).__name__}:{exc}"
            ),
        }
    record["schema_id"] = _VALIDATION_RELAY_SCHEMA
    result_stream.write(json.dumps(record, sort_keys=True).encode("utf-8"))
    result_stream.flush()
    return 0
