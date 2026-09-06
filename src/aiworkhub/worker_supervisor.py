"""Durable timeout/cancel supervisor for one sandboxed model process."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable, cast

try:
    from . import windows_appcontainer
except ImportError:  # direct-script entrypoint
    import windows_appcontainer  # type: ignore[no-redef]

try:
    from .platform_io import atomic_replace, chmod_fd, chmod_path
    from .provider_usage import live_total_tokens, read_provider_usage
    from .token_budget import (
        SampleKind,
        TelemetryAuthority,
        TokenBudgetDecision,
        TokenBudgetState,
        TokenSample,
        consume_sample,
        supervisor_evidence,
    )
except ImportError:  # direct-script entrypoint
    from platform_io import atomic_replace, chmod_fd, chmod_path
    from provider_usage import live_total_tokens, read_provider_usage
    from token_budget import (  # type: ignore[no-redef]
        SampleKind,
        TelemetryAuthority,
        TokenBudgetDecision,
        TokenBudgetState,
        TokenSample,
        consume_sample,
        supervisor_evidence,
    )

try:
    from .runtime_temp import process_start_ticks
except ImportError:  # direct-script entrypoint
    from runtime_temp import process_start_ticks  # type: ignore[no-redef]


POLL_SECONDS = 0.1
KILL_GRACE_SECONDS = 5.0
_PR_SET_PDEATHSIG = 1
# Token-free liveness contract (B412): the supervisor -- never the model --
# atomically refreshes this owner-only status artifact on a fixed cadence
# while the child runs. Heartbeat cadence is deliberately independent of any
# model turn, dashboard read, or MCP request.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0
# Keep each live provider stream within a small, predictable disk envelope.
# The writer retains the newest bytes and marks every compaction explicitly, so
# lowering this ceiling does not hide the terminal diagnostic tail.
DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_TOTAL_OUTPUT_BYTES = 8 * 1024 * 1024
MIN_MAX_OUTPUT_BYTES = 1024
MAX_TOTAL_OUTPUT_BYTES = 1024 * 1024 * 1024
_TRUNCATION_MARKER = b"\n[AIWorkHub: earlier worker output truncated; latest bytes retained]\n"
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
MAX_USAGE_SCAN_BYTES = 32 * 1024 * 1024
MAX_PROGRESS_SCAN_BYTES = 128 * 1024
TRUSTED_PROGRESS_ADAPTERS = {"vscode_lm", "deepseek_vscode_lm", "glm_vscode_lm"}
MEANINGFUL_PROGRESS_PHASES = {
    "request_accepted",
    "tool_turn",
    "final_edit",
    "terminal_error",
}


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operations", ctypes.c_uint64),
        ("write_operations", ctypes.c_uint64),
        ("other_operations", ctypes.c_uint64),
        ("read_bytes", ctypes.c_uint64),
        ("write_bytes", ctypes.c_uint64),
        ("other_bytes", ctypes.c_uint64),
    ]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_int64),
        ("per_job_user_time_limit", ctypes.c_int64),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _WindowsKillOnCloseJob:
    """Own a Windows Job Object that kills the worker tree on supervisor loss."""

    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _JobExtendedLimitInformation()
        info.basic_limit_information.limit_flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        self._kernel32 = kernel32
        self._handle: int | None = int(handle)

    def assign(self, child: subprocess.Popen[bytes]) -> None:
        if self._handle is None or not self._kernel32.AssignProcessToJobObject(
            self._handle, int(child._handle)  # type: ignore[attr-defined]
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _write_json_0600(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    chmod_path(path.parent, 0o700)
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        chmod_fd(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(fd)
        fd = -1
        atomic_replace(temp, path)
        chmod_path(path, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


def _open_0600(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    chmod_path(path.parent, 0o700)
    flags = os.O_CREAT | os.O_APPEND | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    chmod_fd(fd, 0o600)
    return os.fdopen(fd, "a+b", buffering=0)


class _AppContainerProcess:
    """Popen-shaped owner for one authenticated AppContainer launch."""

    def __init__(
        self,
        launch: windows_appcontainer.AppContainerLaunch,
        stdout: BinaryIO,
        stderr: BinaryIO,
        owned_fds: tuple[int, ...] = (),
    ) -> None:
        self._launch = launch
        self.stdout = stdout
        self.stderr = stderr
        self.pid = launch.pid
        self.returncode: int | None = None
        self._owned_fds = owned_fds
        self._closed = False

    def _observe(self, timeout_ms: int) -> int | None:
        result = self._launch.wait(timeout_ms)
        if result.state is windows_appcontainer.AppContainerLifecycleState.EXITED:
            if result.exit_code is None:
                raise OSError("appcontainer_wait_missing_exit_code")
            self.returncode = int(result.exit_code)
            self.close()
            return self.returncode
        if result.state is windows_appcontainer.AppContainerLifecycleState.ERROR:
            raise OSError(
                f"appcontainer_{result.operation or 'wait'}_failed:"
                f"{result.win_error if result.win_error is not None else 'unknown'}"
            )
        if result.state is windows_appcontainer.AppContainerLifecycleState.CLOSED:
            raise OSError("appcontainer_process_closed_before_terminal_outcome")
        return None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        return self._observe(0)

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        timeout_ms = 0xFFFFFFFE if timeout is None else max(0, int(timeout * 1000))
        result = self._observe(timeout_ms)
        if result is None:
            if timeout is None:
                raise OSError("appcontainer_unbounded_wait_returned_timeout")
            raise subprocess.TimeoutExpired(
                self._launch.command_line, float(timeout)
            )
        return result

    def terminate(self) -> None:
        # Never populate returncode here.  wait() must authenticate the native
        # terminal transition after the Job-owned tree has been terminated.
        result = self._launch.terminate(1)
        if result.state is windows_appcontainer.AppContainerLifecycleState.ERROR:
            raise OSError(
                f"appcontainer_{result.operation or 'terminate_wait'}_failed:"
                f"{result.win_error if result.win_error is not None else 'unknown'}"
            )

    def kill(self) -> None:
        self.terminate()

    def close(self) -> None:
        if self._closed:
            return
        first_error: Exception | None = None
        try:
            self._launch.close()
        except Exception as exc:
            first_error = exc
        for fd in self._owned_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._owned_fds = ()
        self._closed = True
        if first_error is not None:
            raise first_error


def _native_handle(fd: int) -> int:
    try:
        import msvcrt
    except ImportError:
        return fd
    get_osfhandle = cast(
        Callable[[int], int], getattr(msvcrt, "get_osfhandle")
    )
    return int(get_osfhandle(fd))


def _launch_appcontainer_process(
    argv: list[str], cwd: str, spec: dict[str, Any]
) -> _AppContainerProcess:
    launch: windows_appcontainer.AppContainerLaunch | None = None
    stdin_fd = os.open(os.devnull, os.O_RDONLY)
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    fds = (stdin_fd, stdout_read, stdout_write, stderr_read, stderr_write)
    try:
        os.set_inheritable(stdout_write, True)
        os.set_inheritable(stderr_write, True)
        request = windows_appcontainer.AppContainerRequest(
            argv=argv,
            repo_id=str(spec["repo_id"]),
            worker_kind=str(spec.get("worker_kind") or spec.get("adapter_id") or "worker"),
            working_directory=cwd,
            environment=os.environ.copy(),
            stdin_handle=_native_handle(stdin_fd),
            stdout_handle=_native_handle(stdout_write),
            stderr_handle=_native_handle(stderr_write),
        )
        launch = windows_appcontainer.launch_appcontainer(request)
        os.close(stdout_write)
        os.close(stderr_write)
        return _AppContainerProcess(
            launch,
            os.fdopen(stdout_read, "rb", buffering=0),
            os.fdopen(stderr_read, "rb", buffering=0),
            (stdin_fd,),
        )
    except Exception:
        if launch is not None:
            try:
                launch.close()
            except Exception:
                pass
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _terminate_child(child: Any, grace: float = KILL_GRACE_SECONDS) -> int:
    if isinstance(child, _AppContainerProcess):
        child.terminate()
        return int(child.wait(timeout=grace))
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/PID", str(child.pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
        )
        return int(child.wait(timeout=grace))
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return int(child.wait())
    try:
        return int(child.wait(timeout=grace))
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return int(child.wait(timeout=grace))


def _unlink_if_regular(path: Path) -> None:
    """Remove ``path`` only if it exists and is not a symlink.

    Defense-in-depth for the spec-file cleanup below: ``unlink(2)`` never
    dereferences a symlink for removal, but an auditor-flagged short-lived
    attacker-writable-looking filename should still refuse to act on one if
    the expected regular file was ever replaced by a symlink.
    """
    try:
        if path.is_symlink():
            return
        path.unlink(missing_ok=True)
    except OSError:
        return


def _load_spec(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if os.name != "nt" and mode & 0o077:
            raise ValueError(f"insecure_spec_mode:{mode:o}")
        with os.fdopen(fd, "r", closefd=False, encoding="utf-8") as fh:
            payload = json.loads(fh.read())
    finally:
        os.close(fd)
    if not isinstance(payload, dict):
        raise ValueError("invalid_spec_object")
    return payload


def _validated_argv(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("invalid_worker_argv")
    if any(not isinstance(item, str) or not item or "\x00" in item for item in value):
        raise ValueError("invalid_worker_argv_item")
    return list(value)


def _die_with_supervisor() -> None:
    """Ensure an abruptly killed Linux supervisor cannot orphan its worker."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        os._exit(126)
    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGKILL)


def _posix_worker_spawn_kwargs(platform: str | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"start_new_session": True}
    if (platform or sys.platform) == "linux":
        kwargs["preexec_fn"] = _die_with_supervisor
    return kwargs


def _pid_start_ticks(pid: int) -> int | None:
    """Stable process creation stamp guarding against PID reuse.

    Delegates to the single cross-platform primitive
    :func:`runtime_temp.process_start_ticks` so the supervisor and the launcher
    can never disagree about process identity.  Returns None on a platform that
    cannot supply a creation time; the status writers below persist that None
    verbatim (``child_pid_start_ticks``/``supervisor_pid_start_ticks`` become
    JSON null) and never do arithmetic on it, so an absent identity degrades to
    bare-liveness reporting rather than raising.
    """
    return process_start_ticks(pid)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _usage_total_from_output(path: Path, adapter_id: str) -> int | None:
    """Return provider-reported cumulative tokens from bounded JSON output.

    Only structured ``usage`` objects are authority. Free text and token-like
    prose are ignored. The maximum observed field values are used because
    supported provider streams report cumulative snapshots; summing repeated
    snapshots would double count. Claude cache fields are disjoint from its
    input count, while OpenAI-shaped adapters report cache hits as an input
    subset.

    Provider stdout is untrusted: malformed or pathologically deep JSON must
    fail soft here rather than propagate and kill an otherwise healthy child.
    """
    try:
        summary = read_provider_usage(
            path,
            max_bytes=MAX_USAGE_SCAN_BYTES,
            include_samples=False,
        )
        return live_total_tokens(summary, adapter_id)
    except (RecursionError, MemoryError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _latest_progress_events(
    path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the newest observed and meaningful events from one bounded tail."""
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            handle.seek(max(0, size - MAX_PROGRESS_SCAN_BYTES))
            raw = handle.read(MAX_PROGRESS_SCAN_BYTES)
    except OSError:
        return {}, {}
    latest: dict[str, Any] = {}
    meaningful: dict[str, Any] = {}
    for raw_line in reversed(raw.splitlines()):
        try:
            payload = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, MemoryError):
            continue
        if not isinstance(payload, dict) or payload.get("type") != "aiworkhub_progress":
            continue
        sequence = payload.get("sequence")
        phase = payload.get("phase")
        if (
            isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and sequence > 0
            and isinstance(phase, str)
            and phase
        ):
            event = {"sequence": sequence, "phase": phase[:80]}
            for key, maximum in (
                ("tool_name", 200), ("tool_state", 16),
                ("error_code", 256), ("timeout_phase", 80),
            ):
                value = payload.get(key)
                if isinstance(value, str):
                    event[key] = value[:maximum]
            for key in ("elapsed_ms", "timeout_ms"):
                value = payload.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    event[key] = max(0, min(value, 86_400_000))
            if not latest:
                latest = event
            if phase in MEANINGFUL_PROGRESS_PHASES:
                meaningful = event
                break
    return latest, meaningful


def _latest_progress_event(path: Path) -> dict[str, Any]:
    """Backward-compatible newest-event view used by focused callers/tests."""

    latest, _meaningful = _latest_progress_events(path)
    return latest


def _token_budget_config(spec: dict[str, Any]) -> tuple[int | None, str]:
    # Legacy token_budget metadata is parsed for backward-compatible diagnostics
    # only. The returned cap never enforces: worker runs are always uncapped.
    raw = spec.get("token_budget")
    if raw in (None, {}):
        return None, str(spec.get("adapter_id") or "")
    if not isinstance(raw, dict):
        raise ValueError("invalid_token_budget")
    cap = raw.get("cap_tokens")
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 100_000_000:
        raise ValueError("token_budget_cap_out_of_range")
    return cap, str(spec.get("adapter_id") or "")


class _BoundedTailWriter:
    """Continuously drain a pipe while bounding its on-disk tail file."""

    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max(MIN_MAX_OUTPUT_BYTES, int(max_bytes))
        self.keep_bytes = max(512, self.max_bytes // 2)
        self.dropped_bytes = 0
        self.received_bytes = 0
        self.error = ""

    def _compact(self, handle: BinaryIO) -> None:
        size = os.fstat(handle.fileno()).st_size
        if size <= self.max_bytes:
            return
        max_tail = max(0, self.max_bytes - len(_TRUNCATION_MARKER))
        keep = min(self.keep_bytes, max_tail, size)
        os.lseek(handle.fileno(), max(0, size - keep), os.SEEK_SET)
        tail = os.read(handle.fileno(), keep)
        self.dropped_bytes += max(0, size - len(tail))
        os.ftruncate(handle.fileno(), 0)
        os.lseek(handle.fileno(), 0, os.SEEK_SET)
        os.write(handle.fileno(), _TRUNCATION_MARKER)
        if tail:
            os.write(handle.fileno(), tail)

    def drain(self, stream: BinaryIO) -> None:
        try:
            with _open_0600(self.path) as handle:
                while True:
                    # ``BufferedReader.read(n)`` may wait for all ``n`` bytes
                    # or EOF. Provider streams are normally line-sized, so
                    # that behavior hid live output (and structured usage)
                    # until the worker exited. ``read1`` returns the bytes
                    # currently available while retaining the same bounded
                    # capture and no-shell guarantees.
                    read1 = getattr(stream, "read1", None)
                    chunk = (
                        read1(64 * 1024)
                        if callable(read1)
                        else stream.read(64 * 1024)
                    )
                    if not chunk:
                        break
                    self.received_bytes += len(chunk)
                    handle.write(chunk)
                    self._compact(handle)
        except Exception as exc:  # drain to EOF even when persistence fails
            self.error = f"{type(exc).__name__}:{exc}"[:500]
            try:
                while stream.read(64 * 1024):
                    pass
            except Exception:
                pass
        finally:
            try:
                stream.close()
            except OSError:
                pass


def supervise(spec: dict[str, Any]) -> int:
    argv = _validated_argv(spec.get("argv"))
    cwd = str(spec["cwd"])
    timeout = int(spec["timeout_seconds"])
    if timeout < 1 or timeout > 86_400:
        raise ValueError("timeout_out_of_range")
    status_path = Path(str(spec["status_path"]))
    cancel_path = Path(str(spec["cancel_path"]))
    stdout_path = Path(str(spec["stdout_path"]))
    stderr_path = Path(str(spec["stderr_path"]))
    try:
        max_output_bytes = int(spec.get("max_output_bytes") or DEFAULT_MAX_OUTPUT_BYTES)
    except (TypeError, ValueError):
        max_output_bytes = DEFAULT_MAX_OUTPUT_BYTES
    max_output_bytes = max(MIN_MAX_OUTPUT_BYTES, max_output_bytes)
    try:
        max_total_output_bytes = int(
            spec.get("max_total_output_bytes") or DEFAULT_MAX_TOTAL_OUTPUT_BYTES
        )
    except (TypeError, ValueError):
        max_total_output_bytes = DEFAULT_MAX_TOTAL_OUTPUT_BYTES
    # This byte threshold bounds capture/telemetry history only. It must not
    # terminate useful work or masquerade as token-budget authority.
    max_total_output_bytes = max(
        MIN_MAX_OUTPUT_BYTES,
        min(max_total_output_bytes, MAX_TOTAL_OUTPUT_BYTES),
    )
    token_cap, adapter_id = _token_budget_config(spec)
    token_state = TokenBudgetState(cap_tokens=token_cap)
    token_decisions: list[TokenBudgetDecision] = []
    last_observed_tokens: int | None = None
    try:
        heartbeat_interval = float(spec.get("heartbeat_interval_seconds") or DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        heartbeat_interval = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    heartbeat_interval = max(0.05, min(heartbeat_interval, 3600.0))
    supervisor_pid = os.getpid()
    supervisor_pid_start_ticks = _pid_start_ticks(supervisor_pid)
    started_epoch = time.time()
    execution_backend = spec.get("execution_backend")
    appcontainer_selected = execution_backend == "windows_appcontainer"
    deadline_epoch = started_epoch + timeout if appcontainer_selected else None
    deadline_monotonic = time.monotonic() + timeout if appcontainer_selected else None
    cancel_requested = False
    child: subprocess.Popen[bytes] | _AppContainerProcess | None = None
    windows_job: _WindowsKillOnCloseJob | None = None

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal cancel_requested
        cancel_requested = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, stop)
    _write_json_0600(status_path, {
        "state": "starting",
        "supervisor_pid": supervisor_pid,
        "supervisor_pid_start_ticks": supervisor_pid_start_ticks,
        "started_at_epoch": started_epoch,
        "deadline_epoch": deadline_epoch,
        "timeout_seconds": timeout,
        "timeout_enforced": appcontainer_selected,
    })

    try:
        spawn_phase = "child_spawn"
        try:
            if execution_backend == "windows_appcontainer":
                child = _launch_appcontainer_process(argv, cwd, spec)
            else:
                popen_kwargs: dict[str, Any] = {
                    "cwd": cwd,
                    "env": os.environ.copy(),
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "shell": False,
                }
                if os.name == "nt":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                    windows_job = _WindowsKillOnCloseJob()
                else:
                    popen_kwargs.update(_posix_worker_spawn_kwargs())
                child = subprocess.Popen(argv, **popen_kwargs)
                if windows_job is not None:
                    spawn_phase = "job_assignment"
                    windows_job.assign(child)
        except Exception as exc:
            if child is not None and child.poll() is None:
                _terminate_child(child)
            if windows_job is not None:
                windows_job.close()
                windows_job = None
            _write_json_0600(status_path, {
                "state": "spawn_failed",
                "supervisor_pid": supervisor_pid,
                "exit_code": 126,
                "spawn_phase": spawn_phase,
                "error": f"{type(exc).__name__}:{exc}"[:500],
                "started_at_epoch": started_epoch,
                "finished_at_epoch": time.time(),
                "deadline_epoch": None,
                "timeout_seconds": timeout,
                "timeout_enforced": False,
            })
            return 126

        assert child.stdout is not None and child.stderr is not None
        stdout_capture = _BoundedTailWriter(stdout_path, max_output_bytes)
        stderr_capture = _BoundedTailWriter(stderr_path, max_output_bytes)
        capture_threads = [
            threading.Thread(target=stdout_capture.drain, args=(child.stdout,), daemon=True),
            threading.Thread(target=stderr_capture.drain, args=(child.stderr,), daemon=True),
        ]
        for capture_thread in capture_threads:
            capture_thread.start()

        child_start_ticks = _pid_start_ticks(child.pid)
        heartbeat_seq = 0
        last_stdout_bytes = _file_size(stdout_path)
        last_stderr_bytes = _file_size(stderr_path)
        last_output_change_epoch = started_epoch
        last_meaningful_progress_epoch = started_epoch
        last_meaningful_phase = "worker_started"
        last_progress_sequence = 0
        last_meaningful_progress_sequence = 0
        last_progress_event: dict[str, Any] = {}
        last_meaningful_progress_event: dict[str, Any] = {}
        next_heartbeat_monotonic = time.monotonic()
        _write_json_0600(status_path, {
            "state": "running",
            "supervisor_pid": supervisor_pid,
            "supervisor_pid_start_ticks": supervisor_pid_start_ticks,
            "child_pid": child.pid,
            "child_pid_start_ticks": child_start_ticks,
            "started_at_epoch": started_epoch,
            "deadline_epoch": None,
            "timeout_seconds": timeout,
            "timeout_enforced": False,
            "heartbeat_seq": heartbeat_seq,
            "heartbeat_at_epoch": time.time(),
            "stdout_bytes": last_stdout_bytes,
            "stderr_bytes": last_stderr_bytes,
            "last_output_change_epoch": last_output_change_epoch,
            "last_meaningful_progress_epoch": last_meaningful_progress_epoch,
            "last_meaningful_phase": last_meaningful_phase,
            "last_progress_sequence": last_progress_sequence,
            "last_meaningful_progress_sequence": last_meaningful_progress_sequence,
            "last_progress_event": last_progress_event,
            "last_meaningful_progress_event": last_meaningful_progress_event,
        })
        final_state = "exited"
        while True:
            returncode = child.poll()
            if returncode is not None:
                break
            if cancel_requested or cancel_path.exists():
                final_state = "cancelled"
                returncode = _terminate_child(child)
                break
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                final_state = "timed_out"
                returncode = _terminate_child(child)
                break
            now_monotonic = time.monotonic()
            if now_monotonic >= next_heartbeat_monotonic:
                # Heartbeat is a supervisor-owned liveness signal only --
                # it never touches task lifecycle/updated_at and is
                # orthogonal to "state" (semantic progress), which is
                # still set exclusively by the transitions above/below.
                stdout_bytes = _file_size(stdout_path)
                stderr_bytes = _file_size(stderr_path)
                if stdout_bytes != last_stdout_bytes or stderr_bytes != last_stderr_bytes:
                    last_output_change_epoch = time.time()
                    if adapter_id not in TRUSTED_PROGRESS_ADAPTERS:
                        last_meaningful_progress_epoch = last_output_change_epoch
                        last_meaningful_phase = "provider_output"
                    last_stdout_bytes = stdout_bytes
                    last_stderr_bytes = stderr_bytes
                progress, meaningful_progress = (
                    _latest_progress_events(stdout_path)
                    if adapter_id in TRUSTED_PROGRESS_ADAPTERS
                    else ({}, {})
                )
                progress_sequence = int(progress.get("sequence") or 0)
                if progress_sequence > last_progress_sequence:
                    last_progress_sequence = progress_sequence
                    last_progress_event = progress
                meaningful_sequence = int(meaningful_progress.get("sequence") or 0)
                if meaningful_sequence > last_meaningful_progress_sequence:
                    last_meaningful_progress_epoch = time.time()
                    last_meaningful_phase = str(meaningful_progress["phase"])
                    last_meaningful_progress_sequence = meaningful_sequence
                    last_meaningful_progress_event = meaningful_progress
                observed_tokens = _usage_total_from_output(stdout_path, adapter_id)
                if observed_tokens is not None and observed_tokens != last_observed_tokens:
                    last_meaningful_progress_epoch = time.time()
                    last_meaningful_phase = "provider_usage"
                    decision = consume_sample(
                        token_state,
                        TokenSample(
                            report_id=f"live:{heartbeat_seq}:{observed_tokens}",
                            authority=TelemetryAuthority.ENFORCED_LIVE,
                            kind=SampleKind.CUMULATIVE,
                            total_tokens=observed_tokens,
                            source=f"{adapter_id or 'provider'}:stdout",
                        ),
                    )
                    token_state = decision.state
                    token_decisions.append(decision)
                    last_observed_tokens = observed_tokens
                    # Provider usage is non-enforcing telemetry only: a crossed
                    # legacy cap never signals, terminates, or reaps the child.
                heartbeat_seq += 1
                _write_json_0600(status_path, {
                    "state": "running",
                    "supervisor_pid": supervisor_pid,
                    "supervisor_pid_start_ticks": supervisor_pid_start_ticks,
                    "child_pid": child.pid,
                    "child_pid_start_ticks": child_start_ticks,
                    "started_at_epoch": started_epoch,
                    "deadline_epoch": None,
                    "timeout_seconds": timeout,
                    "timeout_enforced": False,
                    "heartbeat_seq": heartbeat_seq,
                    "heartbeat_at_epoch": time.time(),
                    "stdout_bytes": last_stdout_bytes,
                    "stderr_bytes": last_stderr_bytes,
                    "last_output_change_epoch": last_output_change_epoch,
                    "last_meaningful_progress_epoch": last_meaningful_progress_epoch,
                    "last_meaningful_phase": last_meaningful_phase,
                    "last_progress_sequence": last_progress_sequence,
                    "last_meaningful_progress_sequence": (
                        last_meaningful_progress_sequence
                    ),
                    "last_progress_event": last_progress_event,
                    "last_meaningful_progress_event": last_meaningful_progress_event,
                    "token_budget": supervisor_evidence(
                        token_state,
                        token_decisions,
                        subject="worker-request",
                    ),
                    "output_budget": {
                        "cap_bytes": max_total_output_bytes,
                        "observed_bytes": (
                            stdout_capture.received_bytes
                            + stderr_capture.received_bytes
                        ),
                        "byte_labels_are_token_truth": False,
                    },
                })
                next_heartbeat_monotonic = now_monotonic + heartbeat_interval
            time.sleep(POLL_SECONDS)

        for capture_thread in capture_threads:
            capture_thread.join(timeout=KILL_GRACE_SECONDS)
        capture_errors = [
            value for value in (stdout_capture.error, stderr_capture.error) if value
        ]
        final_stdout_bytes = _file_size(stdout_path)
        final_stderr_bytes = _file_size(stderr_path)
        final_output_received_bytes = (
            stdout_capture.received_bytes + stderr_capture.received_bytes
        )
        # A large captured byte count never converts a successful exit into a
        # failure; persisted tails remain bounded independently.
        if (
            final_stdout_bytes != last_stdout_bytes
            or final_stderr_bytes != last_stderr_bytes
        ):
            last_output_change_epoch = time.time()
            if adapter_id not in TRUSTED_PROGRESS_ADAPTERS:
                last_meaningful_progress_epoch = last_output_change_epoch
                last_meaningful_phase = "provider_output"
        final_progress, final_meaningful_progress = (
            _latest_progress_events(stdout_path)
            if adapter_id in TRUSTED_PROGRESS_ADAPTERS
            else ({}, {})
        )
        final_progress_sequence = int(final_progress.get("sequence") or 0)
        if final_progress_sequence > last_progress_sequence:
            last_progress_sequence = final_progress_sequence
            last_progress_event = final_progress
        final_meaningful_sequence = int(
            final_meaningful_progress.get("sequence") or 0
        )
        if final_meaningful_sequence > last_meaningful_progress_sequence:
            last_meaningful_progress_epoch = time.time()
            # The meaningful event was parsed from the captured stdout, so its
            # observation cannot truthfully post-date the output-change clock.
            # Keep the two clocks monotonic even when the final capture and
            # parse happen inside the same sub-millisecond scheduling slice.
            last_output_change_epoch = max(
                last_output_change_epoch, last_meaningful_progress_epoch
            )
            last_meaningful_phase = str(final_meaningful_progress["phase"])
            last_meaningful_progress_sequence = final_meaningful_sequence
            last_meaningful_progress_event = final_meaningful_progress
        final_observed_tokens = _usage_total_from_output(stdout_path, adapter_id)
        if (
            final_observed_tokens is not None
            and final_observed_tokens != last_observed_tokens
        ):
            final_decision = consume_sample(
                token_state,
                TokenSample(
                    report_id=f"posthoc:{heartbeat_seq}:{final_observed_tokens}",
                    authority=TelemetryAuthority.POSTHOC_ONLY,
                    kind=SampleKind.CUMULATIVE,
                    total_tokens=final_observed_tokens,
                    source=f"{adapter_id or 'provider'}:stdout",
                ),
            )
            token_state = final_decision.state
            token_decisions.append(final_decision)
        _write_json_0600(status_path, {
            "state": final_state,
            "supervisor_pid": supervisor_pid,
            "supervisor_pid_start_ticks": supervisor_pid_start_ticks,
            "child_pid": child.pid,
            "child_pid_start_ticks": child_start_ticks,
            "exit_code": returncode,
            "started_at_epoch": started_epoch,
            "finished_at_epoch": time.time(),
            "deadline_epoch": deadline_epoch,
            "timeout_seconds": timeout,
            "timeout_enforced": appcontainer_selected,
            "heartbeat_seq": heartbeat_seq,
            "heartbeat_at_epoch": time.time(),
            "stdout_bytes": final_stdout_bytes,
            "stderr_bytes": final_stderr_bytes,
            "stdout_dropped_bytes": stdout_capture.dropped_bytes,
            "stderr_dropped_bytes": stderr_capture.dropped_bytes,
            "capture_errors": capture_errors,
            "last_output_change_epoch": last_output_change_epoch,
            "last_meaningful_progress_epoch": last_meaningful_progress_epoch,
            "last_meaningful_phase": last_meaningful_phase,
            "last_progress_sequence": last_progress_sequence,
            "last_meaningful_progress_sequence": last_meaningful_progress_sequence,
            "last_progress_event": last_progress_event,
            "last_meaningful_progress_event": last_meaningful_progress_event,
            "token_budget": supervisor_evidence(
                token_state,
                token_decisions,
                subject="worker-request",
            ),
            "output_budget": {
                "cap_bytes": max_total_output_bytes,
                "observed_bytes": final_output_received_bytes,
                "stdout_received_bytes": stdout_capture.received_bytes,
                "stderr_received_bytes": stderr_capture.received_bytes,
                "byte_labels_are_token_truth": False,
            },
            "error": "",
        })
    except Exception as exc:
        cleanup_error = ""
        if child is not None:
            try:
                if child.poll() is None:
                    _terminate_child(child)
            except Exception as cleanup_exc:
                cleanup_error = (
                    f";cleanup={type(cleanup_exc).__name__}:{cleanup_exc}"
                )[:250]
        # NF-2026-00082: preserve bounded child stdout/stderr/return-code
        # diagnostics so a missing/partial status artifact never hides what
        # the nested child actually produced. The supervisor owns
        # stdout_path/stderr_path independently of the sandboxed child, so
        # re-reading them here is a fail-closed review of already-captured
        # evidence (never a new privileged op against the sandbox).
        _salvage_cap = max(
            MIN_MAX_OUTPUT_BYTES, min(max_output_bytes, DEFAULT_MAX_OUTPUT_BYTES)
        )

        def _salvage_tail(source: Path) -> str:
            try:
                total = source.stat().st_size
                with source.open("rb") as handle:
                    if total > _salvage_cap:
                        handle.seek(-_salvage_cap, os.SEEK_END)
                    return handle.read().decode("utf-8", errors="replace")
            except OSError:
                return ""

        _child_rc = child.poll() if child is not None else None
        _stdout_tail = _salvage_tail(stdout_path)
        _stderr_tail = _salvage_tail(stderr_path)
        try:
            _write_json_0600(status_path, {
                "state": "supervisor_error",
                "supervisor_pid": supervisor_pid,
                "supervisor_pid_start_ticks": supervisor_pid_start_ticks,
                "child_pid": child.pid if child is not None else None,
                "exit_code": 126,
                "child_returncode": _child_rc,
                "stdout_tail": _stdout_tail,
                "stderr_tail": _stderr_tail,
                "error": (f"{type(exc).__name__}:{exc}" + cleanup_error)[:500],
                "started_at_epoch": started_epoch,
                "finished_at_epoch": time.time(),
                "deadline_epoch": None,
                "timeout_seconds": timeout,
                "timeout_enforced": False,
            })
        except Exception:
            # Status artifact itself is unwritable (e.g. a nested read-only
            # validation exec scratch). Stay fail-closed on the artifact but
            # still surface bounded child diagnostics on the supervisor's own
            # stderr so the orchestrator/run_validations can report them
            # instead of an opaque missing-status failure.
            sys.stderr.write(
                "aiworkhub_supervisor_error:"
                + json.dumps(
                    {
                        "state": "supervisor_error",
                        "exit_code": 126,
                        "child_returncode": _child_rc,
                        "stdout_tail": _stdout_tail[-1000:],
                        "stderr_tail": _stderr_tail[-1000:],
                        "error": (
                            f"{type(exc).__name__}:{exc}" + cleanup_error
                        )[:500],
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        return 126
    finally:
        if isinstance(child, _AppContainerProcess):
            try:
                child.close()
            except Exception:
                # A lifecycle close error encountered on the normal path is
                # raised by _observe and recorded above.  Cleanup retries must
                # never replace that bounded primary diagnostic.
                pass
        if windows_job is not None:
            windows_job.close()
        cancel_path.unlink(missing_ok=True)

    if final_state == "cancelled":
        return 125
    if final_state == "timed_out":
        return 124
    return int(returncode)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    spec_path = Path(args.spec)
    try:
        spec = _load_spec(spec_path)
    except Exception as exc:
        print(f"invalid supervisor spec: {exc}", file=sys.stderr)
        raise SystemExit(126)
    finally:
        _unlink_if_regular(spec_path)
    try:
        code = supervise(spec)
    except Exception as exc:
        status_raw = spec.get("status_path")
        if status_raw:
            try:
                _write_json_0600(Path(str(status_raw)), {
                    "state": "supervisor_error",
                    "supervisor_pid": os.getpid(),
                    "exit_code": 126,
                    "error": f"{type(exc).__name__}:{exc}"[:500],
                    "finished_at_epoch": time.time(),
                })
            except OSError:
                pass
        raise SystemExit(126)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
