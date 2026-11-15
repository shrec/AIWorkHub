"""Progress-based stall watchdog shared by every validation backend (NF-2026-02269).

A validation command has a wall-clock cap (``worker_workspace.MAX_VALIDATION_
SECONDS``), but a command that sits on a lock with no output and no CPU use
would hold finalization for that whole cap.  :class:`StallWatchdog` observes
progress -- bytes the child wrote to stdout/stderr and, where the platform
exposes it without extra dependencies, the process-tree CPU time -- and fires
only when *neither* advanced for one stall window.  The window restarts on
every advance, so a slow command that keeps printing or burning CPU is never
killed; only a silent, idle one is.

On fire the backend kills the process tree and raises :class:`ValidationStalled`
(a :class:`subprocess.TimeoutExpired`, so every existing timeout handler still
catches it); ``run_validations`` finishes the row with
``validation_hung:<command>:<stall seconds>`` and the captured output tail.
"""

from __future__ import annotations

import contextlib
import functools
import importlib
import importlib.util
import locale
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO, Any


class _PlatformIO:
    """``aiworkhub.platform_io``, the sanctioned OS boundary, resolved on first use.

    worker_workspace imports this module before it loads its own platform
    support closure (also as a direct script, where this module is top-level),
    so an import-time ``from . import platform_io`` would bypass that loader.
    """

    def __init__(self) -> None:
        self._module: Any = None

    def _resolve(self) -> Any:
        if self._module is not None:
            return self._module
        if __package__:
            module = importlib.import_module(".platform_io", __package__)
        else:
            # Script mode: bind the SIBLING file, never whatever
            # ``aiworkhub.platform_io`` sys.path happens to resolve first.
            sibling = Path(__file__).resolve().with_name("platform_io.py")
            module = sys.modules.get("platform_io")
            loaded = getattr(module, "__file__", None) if module is not None else None
            if not loaded or Path(loaded).resolve() != sibling:
                spec = importlib.util.spec_from_file_location("platform_io", sibling)
                if spec is None or spec.loader is None:
                    raise ImportError(f"platform_io_unloadable:{sibling}")
                module = importlib.util.module_from_spec(spec)
                sys.modules["platform_io"] = module
                try:
                    spec.loader.exec_module(module)
                except BaseException:
                    sys.modules.pop("platform_io", None)
                    raise
        self._module = module
        return module

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name == "_module":
            raise AttributeError(name)
        return getattr(self._resolve(), name)


platform_io = _PlatformIO()

STALL_SECONDS = 600.0
STALL_SECONDS_ENV = "AIWORKHUB_VALIDATION_STALL_SECONDS"
HUNG_REASON_PREFIX = "validation_hung"
_MAX_POLL_SECONDS = 5.0
_MIN_POLL_SECONDS = 0.05
_CPU_EPSILON_SECONDS = 0.01
_DRAIN_GRACE_SECONDS = 5.0
_MAX_TREE_PIDS = 4_096
_MAX_SNAPSHOT_ENTRIES = 65_536
_READ_CHUNK = 65_536
_SUPPORTED_RUN_KWARGS = frozenset(
    {
        "cwd", "env", "stdin", "stdout", "stderr", "capture_output", "text",
        "encoding", "errors", "timeout", "check", "shell", "pass_fds",
    }
)


def stall_seconds() -> float:
    """The stall window: ``STALL_SECONDS`` unless the env names a positive number."""

    raw = os.environ.get(STALL_SECONDS_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return STALL_SECONDS
        if value > 0:
            return value
    return STALL_SECONDS


def format_stall_seconds(value: float) -> str:
    return f"{value:g}"


def hung_reason(command: str, window: float) -> str:
    return f"{HUNG_REASON_PREFIX}:{command}:{format_stall_seconds(window)}"


class ValidationStalled(subprocess.TimeoutExpired):
    """A validation command made no output or CPU progress for ``stall_seconds``."""

    def __init__(
        self,
        cmd: Any,
        stall_seconds: float,
        output: str | bytes | None = None,
        stderr: str | bytes | None = None,
    ) -> None:
        super().__init__(cmd, stall_seconds, output=output, stderr=stderr)
        self.stall_seconds = float(stall_seconds)

    def __str__(self) -> str:
        return (
            f"{HUNG_REASON_PREFIX}: command {self.cmd!r} made no output or CPU "
            f"progress for {format_stall_seconds(self.stall_seconds)} seconds"
        )


def _linux_tree(pid: int) -> Iterator[tuple[int, list[str]]]:
    """``(pid, stat fields after comm)`` for ``pid`` and its live descendants."""

    seen: set[int] = set()
    stack = [pid]
    while stack and len(seen) < _MAX_TREE_PIDS:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        proc = Path("/proc") / str(current)
        try:
            raw = (proc / "stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        yield current, raw[raw.rfind(")") + 2:].split()
        try:
            tasks = os.listdir(proc / "task")
        except OSError:
            continue
        for task in tasks:
            try:
                children = (proc / "task" / task / "children").read_text(encoding="ascii")
            except OSError:
                continue
            stack.extend(int(child) for child in children.split() if child.isdigit())


def _linux_tree_cpu_seconds(pid: int) -> float | None:
    try:
        ticks = float(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, OSError, ValueError):
        return None
    total = 0
    found = False
    for _pid, fields in _linux_tree(pid):
        try:
            # utime, stime, cutime, cstime: stat fields 14-17.
            total += sum(int(value) for value in fields[11:15])
        except ValueError:
            continue
        found = True
    return total / ticks if found and ticks > 0 else None


_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002


@functools.lru_cache(maxsize=1)
def _kernel32() -> Any:
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

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    filetime_p = ctypes.POINTER(wintypes.FILETIME)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE, filetime_p, filetime_p, filetime_p, filetime_p]
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    entry_p = ctypes.POINTER(PROCESSENTRY32W)
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, entry_p]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, entry_p]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.aiworkhub_entry_type = PROCESSENTRY32W
    return k32


def _windows_times(pid: int) -> tuple[int, int] | None:
    """``(creation, kernel+user)`` in 100 ns units, or ``None`` when unreadable."""

    import ctypes
    from ctypes import wintypes

    k32 = _kernel32()
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            return None
        creation, _exit, kernel, user = (
            (item.dwHighDateTime << 32) | item.dwLowDateTime for item in times
        )
        return creation, kernel + user
    finally:
        k32.CloseHandle(handle)


def _windows_tree(pid: int) -> list[tuple[int, int, int]]:
    """``(pid, cpu_100ns, creation_100ns)`` for ``pid`` and its live descendants, root first.

    A parent id can be stale after the parent exited and its id was reused, so
    a process counts as a child only when it was created after its parent.
    """

    import ctypes

    k32 = _kernel32()
    children: dict[int, list[int]] = {}
    snapshot = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot and snapshot != ctypes.c_void_p(-1).value:
        try:
            entry = k32.aiworkhub_entry_type()
            entry.dwSize = ctypes.sizeof(entry)
            ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
            listed = 0
            while ok and listed < _MAX_SNAPSHOT_ENTRIES:
                listed += 1
                children.setdefault(int(entry.th32ParentProcessID), []).append(
                    int(entry.th32ProcessID)
                )
                ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snapshot)
    root = _windows_times(pid)
    if root is None:
        return []
    tree = [(pid, root[1], root[0])]
    stack = [(pid, root[0])]
    seen = {pid}
    while stack and len(seen) < _MAX_TREE_PIDS:
        parent, parent_created = stack.pop()
        for child in children.get(parent, ()):
            if child in seen or child == 0:
                continue
            times = _windows_times(child)
            if times is None or times[0] < parent_created:
                continue
            seen.add(child)
            tree.append((child, times[1], times[0]))
            stack.append((child, times[0]))
    return tree


def process_tree_cpu_seconds(pid: int | None) -> float | None:
    """CPU seconds of ``pid`` and its live descendants, or ``None``.

    ``None`` means the platform exposes no CPU probe without extra
    dependencies; the watchdog then judges progress by output bytes alone.
    """

    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        if platform_io.is_linux():
            return _linux_tree_cpu_seconds(pid)
        if platform_io.is_windows():
            tree = _windows_tree(pid)
            return sum(cpu for _pid, cpu, _created in tree) / 10_000_000 if tree else None
    except (OSError, ValueError, AttributeError):
        return None
    return None


class StallWatchdog:
    """Fires when neither output bytes nor CPU time advanced for ``window`` seconds."""

    def __init__(
        self,
        window: float | None = None,
        *,
        cpu_probe: Callable[[], float | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window = float(window if window is not None else stall_seconds())
        if self.window <= 0:
            raise ValueError(f"stall_window_not_positive:{self.window}")
        self._cpu_probe = cpu_probe
        self._clock = clock
        self._lock = threading.Lock()
        self._bytes = 0
        self._seen_bytes = 0
        self._seen_cpu: float | None = None
        self._last_progress = clock()
        self.fired = False

    @property
    def poll_seconds(self) -> float:
        return max(_MIN_POLL_SECONDS, min(_MAX_POLL_SECONDS, self.window / 10))

    @property
    def output_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def note_output(self, count: int) -> None:
        if count > 0:
            with self._lock:
                self._bytes += count

    def track_pid(self, pid: int | None) -> None:
        """Judge CPU progress by ``pid``'s process tree (set before :meth:`monitor`)."""

        self._cpu_probe = lambda: process_tree_cpu_seconds(pid)

    def restart(self) -> None:
        """Restart the stall window now: time before this call is not no-progress.

        A watchdog built before its command launched (the AppContainer path
        constructs it ahead of setup) restarts here, so launch/setup time never
        counts toward the window.  Output already counted is the new baseline.
        """

        with self._lock:
            self._seen_bytes = self._bytes
            self._seen_cpu = None
            self._last_progress = self._clock()
            self.fired = False

    def check(self) -> bool:
        """Sample progress once; ``True`` when the stall window has elapsed."""

        now = self._clock()
        with self._lock:
            observed = self._bytes
        if observed != self._seen_bytes:
            self._seen_bytes = observed
            self._last_progress = now
        cpu = self._cpu_probe() if self._cpu_probe is not None else None
        if cpu is not None:
            # Compare with the PREVIOUS sample, never a peak: when a CPU-heavy
            # child exits the tree total drops, and a new busy child must be
            # credited from that lower baseline.  A drop itself is not progress.
            if self._seen_cpu is None or cpu > self._seen_cpu + _CPU_EPSILON_SECONDS:
                self._last_progress = now
            self._seen_cpu = cpu
        return now - self._last_progress >= self.window

    @contextlib.contextmanager
    def monitor(self, on_stall: Callable[[], None]) -> Iterator[StallWatchdog]:
        """Run :meth:`check` on a thread; call ``on_stall`` once when it fires."""

        stop = threading.Event()

        def _loop() -> None:
            while not stop.wait(self.poll_seconds):
                if self.check():
                    self.fired = True
                    with contextlib.suppress(Exception):
                        on_stall()
                    return

        thread = threading.Thread(target=_loop, name="validation-stall-watchdog", daemon=True)
        thread.start()
        try:
            yield self
        finally:
            stop.set()
            thread.join(self.poll_seconds + _DRAIN_GRACE_SECONDS)


_LINUX_STARTTIME_INDEX = 19  # stat field 22 (starttime), counted from field 3


def _linux_start_ticks(fields: Sequence[str]) -> int | None:
    try:
        return int(fields[_LINUX_STARTTIME_INDEX])
    except (IndexError, ValueError):
        return None


def _tree_identities(pid: int) -> list[tuple[int, int]]:
    """``(pid, creation stamp)`` for ``pid`` and its live descendants, root first."""

    if platform_io.is_windows():
        return [(child, created) for child, _cpu, created in _windows_tree(pid)]
    if platform_io.is_linux():
        listed = ((child, _linux_start_ticks(fields)) for child, fields in _linux_tree(pid))
        return [(child, stamp) for child, stamp in listed if stamp is not None]
    return []


def _creation_stamp(pid: int) -> int | None:
    """``pid``'s creation stamp from the same source :func:`_tree_identities` uses."""

    if platform_io.is_windows():
        times = _windows_times(pid)
        return times[0] if times is not None else None
    if platform_io.is_linux():
        try:
            raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
        return _linux_start_ticks(raw[raw.rfind(")") + 2:].split())
    return None


def _terminate_pid(pid: int, stamp: int) -> None:
    """Kill ``pid`` only while it is still the process created at ``stamp``."""

    if not platform_io.is_windows():
        pidfd_open = getattr(os, "pidfd_open", None)
        pidfd_send = getattr(signal, "pidfd_send_signal", None)
        if platform_io.is_linux() and pidfd_open is not None and pidfd_send is not None:
            try:
                fd = pidfd_open(int(pid))
            except ProcessLookupError:
                return
            except OSError:
                fd = -1  # ENOSYS/seccomp: no pidfd support, fall back to os.kill
            if fd >= 0:
                try:
                    # The held pidfd pins the identity: re-check the stamp on it.
                    if _creation_stamp(pid) == stamp:
                        pidfd_send(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
                return
        os.kill(pid, signal.SIGKILL)
        return
    import ctypes
    from ctypes import wintypes

    k32 = _kernel32()
    handle = k32.OpenProcess(
        _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
    )
    if not handle:
        return
    try:
        # Re-verify on the very handle that terminates: no reuse window remains.
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            return
        if ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) == stamp:
            k32.TerminateProcess(handle, 1)
    finally:
        k32.CloseHandle(handle)


def kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill ``proc`` and every live descendant; never raises.

    The tree is listed before anything dies (a dead parent orphans its
    children), with each descendant's creation stamp.  Each one is re-checked
    immediately before it is killed: a vanished process or a reused pid (a
    different creation stamp) is skipped.  The root is killed last through its
    owned :class:`subprocess.Popen` handle.
    """

    with contextlib.suppress(Exception):
        windows = platform_io.is_windows()
        # A reaped POSIX root's pid may be reused, so its tree is listed only
        # while unreaped; on Windows the owned Popen handle pins the pid.
        listable = windows or proc.returncode is None
        descendants = [
            (pid, stamp) for pid, stamp in _tree_identities(proc.pid) if pid != proc.pid
        ] if listable else []
        if not windows:
            # start_new_session made proc.pid the group id; a surviving member
            # (the hung grandchild) keeps that id from being reused.
            with contextlib.suppress(OSError):
                platform_io.signal_process_group(proc.pid, graceful=False)
        for pid, stamp in reversed(descendants):
            if _creation_stamp(pid) != stamp:
                continue
            with contextlib.suppress(OSError):
                _terminate_pid(pid, stamp)
    with contextlib.suppress(OSError):
        proc.kill()


def _drain(stream: IO[bytes], sink: list[bytes], watchdog: StallWatchdog) -> None:
    fd = stream.fileno()
    with contextlib.suppress(OSError, ValueError):
        while True:
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                return
            sink.append(chunk)
            watchdog.note_output(len(chunk))


def _decode(chunks: list[bytes], encoding: str, errors: str) -> str:
    text = b"".join(chunks).decode(encoding, errors=errors)
    # ``text=True`` reads through universal newlines; keep that exact shape.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _is_stdlib_run() -> bool:
    run = subprocess.run
    return getattr(run, "__module__", None) == "subprocess" and getattr(
        run, "__qualname__", None
    ) == "run"


def run_watched(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` for one validation command, plus the stall watchdog.

    Accepts the subset of ``subprocess.run`` keywords validation uses and
    answers in its exact shapes: a text :class:`subprocess.CompletedProcess`,
    :class:`subprocess.TimeoutExpired` at the wall deadline, or
    :class:`ValidationStalled` when the command made no progress for the stall
    window.  When ``subprocess.run`` has been substituted (a test double), the
    substitute owns execution and receives the identical arguments.
    """

    unsupported = sorted(set(kwargs) - _SUPPORTED_RUN_KWARGS)
    if unsupported:
        raise TypeError(f"run_watched_unsupported_arguments:{','.join(unsupported)}")
    if not _is_stdlib_run():
        return subprocess.run(list(argv), **kwargs)
    if kwargs.get("shell") or kwargs.get("check"):
        raise ValueError("run_watched_requires_shell_false_and_check_false")
    timeout = kwargs.get("timeout")
    if timeout is None or float(timeout) <= 0:
        raise ValueError("run_watched_requires_a_positive_timeout")
    encoding = str(kwargs.get("encoding") or locale.getpreferredencoding(False))
    errors = str(kwargs.get("errors") or "replace")
    pass_fds = tuple(kwargs.get("pass_fds") or ())
    popen_kwargs: dict[str, Any] = {
        "cwd": kwargs.get("cwd"),
        "env": kwargs.get("env"),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
    }
    if pass_fds:
        popen_kwargs["pass_fds"] = pass_fds
    if platform_io.is_windows():
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        popen_kwargs["start_new_session"] = True
    return _run_popen(list(argv), popen_kwargs, float(timeout), encoding, errors)


def _run_popen(
    argv: list[str],
    popen_kwargs: Mapping[str, Any],
    timeout: float,
    encoding: str,
    errors: str,
) -> subprocess.CompletedProcess[str]:
    out: list[bytes] = []
    err: list[bytes] = []
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, **popen_kwargs) as proc:
        assert proc.stdout is not None and proc.stderr is not None
        watchdog = StallWatchdog(cpu_probe=lambda: process_tree_cpu_seconds(proc.pid))
        drains = [
            threading.Thread(target=_drain, args=(proc.stdout, out, watchdog), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, err, watchdog), daemon=True),
        ]
        for thread in drains:
            thread.start()
        timed_out = False
        with watchdog.monitor(lambda: kill_process_tree(proc)):
            try:
                proc.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
            # A grandchild may still hold the pipes after the child exits; the
            # watchdog stays armed while they drain, up to the same deadline.
            for thread in drains:
                thread.join(max(0.0, deadline - time.monotonic()))
            if any(thread.is_alive() for thread in drains):
                timed_out = True
        if timed_out or watchdog.fired:
            kill_process_tree(proc)
            for thread in drains:
                thread.join(_DRAIN_GRACE_SECONDS)
        stdout = _decode(out, encoding, errors)
        stderr = _decode(err, encoding, errors)
        if watchdog.fired:
            raise ValidationStalled(argv, watchdog.window, output=stdout, stderr=stderr)
        if timed_out:
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
        returncode = proc.wait()
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def wait_appcontainer_watched(
    launch: Any,
    timeout_ms: int,
    drains: Sequence[threading.Thread],
    captured: Mapping[str, bytes],
    argv: Sequence[str],
    *,
    watchdog: StallWatchdog,
    error_type: type[BaseException],
    join_seconds: float,
) -> Any:
    """``launch.wait(timeout_ms)`` for an AppContainer validation, under ``watchdog``.

    NF-2026-02269: the drains feed ``watchdog.note_output``; the launch's pid
    (where exposed) feeds the CPU probe.  On a stall the job is terminated,
    the drains get ``join_seconds`` to deliver the tail, and
    :class:`ValidationStalled` is raised.  Any other ``error_type`` from the
    wait is re-raised as ``OSError("windows_appcontainer_validation_wait_failed:...")``.
    """

    watchdog.track_pid(getattr(launch, "pid", None))
    # The caller built the watchdog before setup/launch; the window starts now.
    watchdog.restart()

    def _terminate() -> None:
        with contextlib.suppress(error_type):
            launch.terminate()

    outcome = None
    with watchdog.monitor(_terminate):
        try:
            outcome = launch.wait(timeout_ms)
        except error_type as exc:
            if not watchdog.fired:
                raise OSError(f"windows_appcontainer_validation_wait_failed:{exc}") from exc
    if watchdog.fired:
        for thread in drains:
            thread.join(join_seconds)
        raise ValidationStalled(
            list(argv),
            watchdog.window,
            output=captured["stdout"].decode("utf-8", errors="replace"),
            stderr=captured["stderr"].decode("utf-8", errors="replace"),
        )
    return outcome


def annotate_stalled(record: dict[str, Any], exc: BaseException, command: str) -> None:
    """Mark a timeout ``record`` as hung when ``exc`` is :class:`ValidationStalled` (NF-2026-02269)."""

    if isinstance(exc, ValidationStalled):
        record["stalled"] = True
        record["stall_seconds"] = exc.stall_seconds
        record["failure_reason"] = hung_reason(command, exc.stall_seconds)


def _escaped_tail(value: Any) -> str:
    return str(value or "")[-1_000:].replace("\n", "\\n")


def hung_reason_detail(row: Mapping[str, Any]) -> str:
    """``validation_hung:<command>:<stall seconds>`` plus the hung row's last output."""

    return (
        f"{row.get('failure_reason')}:"
        f"stdout={_escaped_tail(row.get('stdout_tail'))}:stderr={_escaped_tail(row.get('stderr_tail'))}"
    )
