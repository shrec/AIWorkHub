"""Durable timeout/cancel supervisor for one sandboxed model process."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
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
    from .windows_job_structures import JOBOBJECT_EXTENDED_LIMIT_INFORMATION
except ImportError:  # direct-script entrypoint
    import windows_appcontainer  # type: ignore[no-redef]
    from windows_job_structures import JOBOBJECT_EXTENDED_LIMIT_INFORMATION

try:
    from .platform_io import atomic_replace, chmod_fd, chmod_path, retrying_unlink
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
    from platform_io import atomic_replace, chmod_fd, chmod_path, retrying_unlink
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
# Attribute under which supervise() records the kill-on-close Job a spawned
# child was assigned to, so _terminate_child can escalate to it without a new
# parameter -- the callers wrap _terminate_child with a single-argument shim.
_KILL_JOB_ATTR = "_aiworkhub_kill_on_close_job"
MAX_USAGE_SCAN_BYTES = 32 * 1024 * 1024
MAX_PROGRESS_SCAN_BYTES = 128 * 1024
TRUSTED_PROGRESS_ADAPTERS = {"vscode_lm", "deepseek_vscode_lm", "glm_vscode_lm"}
MEANINGFUL_PROGRESS_PHASES = {
    "request_accepted",
    "tool_turn",
    "final_edit",
    "terminal_error",
}


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
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
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

    def terminate(self, exit_code: int = 1) -> None:
        """Kill every process in this Job, keeping the handle open to wait on.

        NF-2026-01166: this is the tree kill the supervisor owns outright. It
        needs no pid lookup and no helper executable, so it still works where
        `taskkill` is refused.
        """
        if self._handle is None or not self._kernel32.TerminateJobObject(
            self._handle, ctypes.c_uint32(exit_code)
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
        retrying_unlink(temp, missing_ok=True)


def _open_0600(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    chmod_path(path.parent, 0o700)
    flags = os.O_CREAT | os.O_APPEND | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    chmod_fd(fd, 0o600)
    return os.fdopen(fd, "a+b", buffering=0)


# How long a bridge waits for its worker's MCP client; claude connects during
# its own startup, seconds after launch.
_BRIDGE_ACCEPT_SECONDS = 120.0
# The server's actual import path, printed by the same interpreter, flags,
# environment and working directory the server itself gets.
_SYS_PATH_PROBE = "import json,sys;sys.stdout.write(json.dumps(sys.path))"


def _within_any(path: str, roots: Any) -> bool:
    key = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    return any(
        (key + os.sep).startswith(os.path.normcase(os.path.normpath(root)).rstrip(os.sep) + os.sep)
        for root in roots
    )


class _WorkerMcpBridge:
    """The host side of an AppContainer worker's MCP connection (NF-2026-00034).

    The worker MCP server holds the request's audit key, reads canonical
    repository state and applies edits under its own authority checks, so it
    runs here -- the supervisor's own child, outside the container -- with the
    command, arguments and binding environment its generated config names.
    The contained worker reaches it only through ``pipe``: one connection,
    from a process of this launch's ``job``, relayed byte for byte.  The
    server gets its own kill-on-close job, so neither close() nor a killed
    supervisor can leave it behind.

    Nothing the container can write may decide what this host process runs,
    because the model writes there first and connects second.  So the server
    runs from a directory the container cannot write (``bridge["cwd"]``), as
    ``python -P -s`` with PYTHONSAFEPATH and PYTHONNOUSERSITE, no inherited
    PYTHON* variable, and no inherited variable naming a container-writable
    path; HOME and the temp variables point at that private directory.  Its
    real ``sys.path`` is probed before it starts, and a server that would
    import from anywhere the container can write is never started.
    """

    def __init__(
        self,
        pipe: Any,
        bridge: dict[str, Any],
        writable_roots: Any,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        server_job: Callable[[], Any] = _WindowsKillOnCloseJob,
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        withheld = [str(p) for p in bridge.get("withheld_directories") or ()]
        writable = [str(p) for p in writable_roots]

        def private(path: str) -> bool:
            return not _within_any(path, writable) or _within_any(path, withheld)

        self._private = private
        command = str(bridge["command"])
        self._cwd = str(bridge["cwd"])
        config_env = {str(k): str(v) for k, v in (bridge.get("env") or {}).items()}
        pythonpath = [p for p in config_env.get("PYTHONPATH", "").split(os.pathsep) if p]
        for path in (command, self._cwd, *pythonpath):
            if not os.path.isabs(path) or not private(path):
                raise ValueError(f"worker_mcp_bridge_input_container_writable:{path}")
        env: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.upper().startswith("PYTHON") or key.upper() in ("HOMEDRIVE", "HOMEPATH"):
                continue
            kept = [
                part for part in value.split(os.pathsep)
                if not (os.path.isabs(part) and not private(part))
            ]
            if kept:
                env[key] = os.pathsep.join(kept)
        for key in ("HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR"):
            env[key] = self._cwd
        env.update(config_env)
        env.update(PYTHONSAFEPATH="1", PYTHONNOUSERSITE="1", PYTHONUNBUFFERED="1")
        self._env = env
        self._command = command
        self._argv = [command, "-P", "-s", *(str(a) for a in bridge.get("args") or ())]
        self._pipe = pipe
        self._job: Any = None
        self._stderr_path = Path(str(bridge["stderr_path"]))
        self._popen = popen
        self._run = run
        self._server_job_factory = server_job
        self._lock = threading.Lock()
        self._closed = False
        self._threads: list[threading.Thread] = []
        self.server: Any = None
        self._server_job: Any = None
        self.sys_path: list[str] = []
        self.error = ""

    def host_sys_path(self) -> list[str]:
        """The import path the server would run with; raises if any entry is
        a directory the container can write."""
        probe = self._run(
            [self._command, "-P", "-s", "-c", _SYS_PATH_PROBE],
            cwd=self._cwd, env=self._env, shell=False, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=60, check=True,
        )
        entries = [str(entry) for entry in json.loads(probe.stdout)]
        exposed = [entry for entry in entries if entry and not self._private(entry)]
        if exposed or "" in entries:
            raise ValueError(f"worker_mcp_server_sys_path_container_writable:{exposed or ['']}")
        return entries

    def start(self, job: Any) -> None:
        """Serve the first client of ``job``, the worker's own job object."""
        self._job = job
        self._thread(self._serve)

    def _thread(self, target: Callable[..., None], *args: Any) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _serve(self) -> None:
        server = None
        try:
            if not self._pipe.accept(self._job, timeout=_BRIDGE_ACCEPT_SECONDS):
                return
            with self._lock:
                if self._closed:
                    return
                self.sys_path = self.host_sys_path()
                with _open_0600(self._stderr_path) as stderr:
                    stderr.write(
                        ("aiworkhub_worker_mcp_bridge host_sys_path="
                         + json.dumps(self.sys_path) + "\n").encode("utf-8")
                    )
                    server = self._popen(
                        self._argv, cwd=self._cwd, env=self._env, shell=False,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                    )
                self.server = server
                self._server_job = self._server_job_factory()
                self._server_job.assign(server)
            self._thread(self._server_to_pipe, server.stdout)
            while chunk := self._pipe.read():
                server.stdin.write(chunk)
                server.stdin.flush()
        except Exception as exc:
            self.error = self.error or f"{type(exc).__name__}:{exc}"[:500]
        finally:
            if server is None:
                self._pipe.close()  # nothing to serve: let a waiting client go
            else:
                try:
                    server.stdin.close()  # client gone: the server sees EOF
                except OSError:
                    pass

    def _server_to_pipe(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read1(65536):  # type: ignore[attr-defined]
                self._pipe.write(chunk)
        except Exception as exc:
            self.error = self.error or f"{type(exc).__name__}:{exc}"[:500]
        finally:
            self._pipe.close()  # server gone: the client sees the pipe close

    def close(self, grace: float = KILL_GRACE_SECONDS) -> None:
        """Stop serving, let the server exit on EOF, kill whatever is left
        with its job, and remove the pipe.  Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._pipe.shutdown()
        server = self.server
        if server is not None:
            try:
                server.stdin.close()
            except OSError:
                pass
            try:
                server.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        if self._server_job is not None:
            self._server_job.close()
        if server is not None and server.poll() is None:
            server.kill()
            try:
                server.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.error = self.error or "worker_mcp_server_survived_kill"
        for thread in self._threads:
            thread.join(grace)
        if not self._pipe.close():
            self.error = self.error or "worker_pipe_still_in_use_at_close"


class _AppContainerProcess:
    """Popen-shaped owner for one authenticated AppContainer launch."""

    def __init__(
        self,
        launch: windows_appcontainer.AppContainerLaunch,
        stdout: BinaryIO,
        stderr: BinaryIO,
        owned_fds: tuple[int, ...] = (),
        bridge: _WorkerMcpBridge | None = None,
    ) -> None:
        self._launch = launch
        self.stdout = stdout
        self.stderr = stderr
        self.pid = launch.pid
        self.returncode: int | None = None
        self._owned_fds = owned_fds
        self._bridge = bridge
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
        if self._bridge is not None:
            try:
                self._bridge.close()
            except Exception as exc:
                first_error = exc
        try:
            self._launch.close()
        except Exception as exc:
            first_error = first_error or exc
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


# NF-2026-00033: a worker reaches its provider API and nothing else --
# outbound internet only.  Never internetClientServer (inbound listening) or
# privateNetworkClientServer (LAN).  Validation launches request no capability
# at all (worker_workspace._run_appcontainer_validation).
WORKER_NETWORK_CAPABILITIES = ("internetClient",)
_NPM_SHIM_TARGET = re.compile(r'"%dp0%\\node_modules\\([^"]+)"')


def _strictly_beneath(child: str, parent: str) -> bool:
    child, parent = (os.path.normcase(os.path.normpath(p)) for p in (child, parent))
    try:
        return child != parent and os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives
        return False


def _resolve_npm_shim(executable: str) -> tuple[Path, Path] | None:
    """``(target, package_root)`` of an npm cmd-shim; None if it is not one.

    The shim runs ``"%dp0%\\node_modules\\<package>\\...\\<file>"``, and that
    target decides both what runs and what the container may read, so a shim
    naming anything but a path strictly inside its own ``node_modules`` is
    REFUSED (ValueError) -- never followed, never fallen back from.  Both
    separators split segments, as Win32 does; ``:`` (drive-relative paths,
    alternate data streams), empty/dot segments and trailing dots or spaces
    (which Win32 silently strips) are refused outright; and containment is
    then asserted on the Win32-canonical path, not inferred from segments.
    """
    path = Path(executable)
    if path.suffix.lower() not in {".cmd", ".bat"}:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as shim:
            match = _NPM_SHIM_TARGET.search(shim.read(65536))
    except OSError:
        return None
    if not match:
        return None
    relative = match.group(1)
    parts = re.split(r"[\\/]", relative)
    depth = 2 if parts[0].startswith("@") else 1
    refusal = ValueError(f"appcontainer_npm_shim_target_outside_node_modules:{relative!r}")
    if (
        ":" in relative
        or "\x00" in relative
        or len(parts) <= depth
        or any(not part or part != part.rstrip(". ") for part in parts)
    ):
        raise refusal
    root = os.path.abspath(path.parent / "node_modules")
    target = os.path.abspath(os.path.join(root, *parts))
    package = os.path.abspath(os.path.join(root, *parts[:depth]))
    if not (_strictly_beneath(target, root) and _strictly_beneath(package, root)):
        raise refusal
    return Path(target), Path(package)


def _native_worker_argv(argv: list[str]) -> list[str]:
    """Run an npm shim's native ``.exe`` directly instead of through cmd.exe.

    Measured on Windows 11 26200: cmd.exe inside the container fails EVERY
    batch file -- even one in its fully granted cwd -- with "Access is
    denied.", because it canonicalizes the script path through each ancestor
    and an AppContainer cannot list ``C:\\`` or ``C:\\Users`` (which the user
    cannot re-permission either).  The shim does nothing but run
    ``<target>.exe %*``, so this is the same program with the same arguments,
    minus cmd.exe re-parsing the worker's arguments for metacharacters.
    """
    resolved = _resolve_npm_shim(argv[0])
    if resolved is None or resolved[0].suffix.lower() != ".exe":
        return argv
    return [str(resolved[0]), *argv[1:]]


def _provider_install_grants(
    executable: str,
) -> list[windows_appcontainer.ContainerGrant]:
    """Read/execute on exactly what the provider CLI needs to start.

    For an npm shim: the shim and the one package it runs -- not the whole
    npm directory the other CLIs live in.  Any other executable: that file
    alone, never its directory, which may be a shared root.  Persistent: see
    the rationale in ``launch_appcontainer``.  Nothing in a system tree: the
    container already reads Program Files and %SystemRoot%
    (``windows_appcontainer.outside_system_trees``).
    """
    path = Path(executable)
    resolved = _resolve_npm_shim(executable)
    roots = [path, resolved[1]] if resolved else [path]
    return windows_appcontainer.outside_system_trees([
        windows_appcontainer.ContainerGrant(str(root), "read_execute", persistent=True)
        for root in roots
    ])


def _worker_filesystem_grants(
    argv: list[str], cwd: str, env: dict[str, str]
) -> list[windows_appcontainer.ContainerGrant]:
    """NF-2026-00025: the container SID starts with no access to anything the
    worker needs.  Grant the provider install (read) plus the per-request
    worktree, isolated HOME and request temp (modify, revoked on close)."""
    return [
        *_provider_install_grants(argv[0]),
        *windows_appcontainer.request_scoped_grants(env, cwd),
    ]


def _feed_and_close_stdin(stream: BinaryIO, text: str) -> None:
    """Write text to stream and close it from a thread, so a non-reading child cannot block the caller."""

    def _write() -> None:
        try:
            stream.write(text.encode("utf-8"))
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    threading.Thread(target=_write, daemon=True).start()


def _launch_appcontainer_process(
    argv: list[str], cwd: str, spec: dict[str, Any], *, stdin_text: str | None = None
) -> _AppContainerProcess:
    # Identity is checked before a single handle is opened.  A blank or absent
    # repo_id/worker_kind cannot name the repo-scoped AppContainer profile this
    # worker must be confined by, and the only safe answer is a stable
    # mechanical refusal: never a guessed moniker, and never a fall-through to
    # the plain-subprocess branch the caller has already ruled out.
    repo_id = str(spec.get("repo_id") or "").strip()
    worker_kind = str(
        spec.get("worker_kind") or spec.get("adapter_id") or ""
    ).strip()
    if not repo_id:
        raise ValueError("appcontainer_spec_missing_repo_id")
    if not worker_kind:
        raise ValueError("appcontainer_spec_missing_worker_kind")
    bridge_spec = spec.get("worker_mcp_bridge")
    if bridge_spec is not None and not isinstance(bridge_spec, dict):
        raise ValueError("invalid_worker_mcp_bridge")
    launch: windows_appcontainer.AppContainerLaunch | None = None
    pipe: Any = None
    bridge: _WorkerMcpBridge | None = None
    stdin_write_fd: int | None = None
    if stdin_text is None:
        stdin_fd = os.open(os.devnull, os.O_RDONLY)
    else:
        stdin_fd, stdin_write_fd = os.pipe()
        os.set_inheritable(stdin_fd, True)
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    fds = (stdin_fd, stdout_read, stdout_write, stderr_read, stderr_write) + (
        (stdin_write_fd,) if stdin_write_fd is not None else ()
    )
    try:
        os.set_inheritable(stdout_write, True)
        os.set_inheritable(stderr_write, True)
        environment = os.environ.copy()
        grants = _worker_filesystem_grants(argv, cwd, environment)
        if bridge_spec is not None:
            # The pipe exists before the worker does, so its MCP shim finds it.
            pipe = windows_appcontainer.WorkerPipe(
                str(bridge_spec["pipe"]),
                windows_appcontainer.container_sid(repo_id, worker_kind),
            )
            # Checked before the worker exists: every input the host server
            # starts from must lie outside what the container can write.
            bridge = _WorkerMcpBridge(
                pipe, bridge_spec, [g.path for g in grants if g.access == "modify"]
            )
        request = windows_appcontainer.AppContainerRequest(
            argv=_native_worker_argv(argv),
            repo_id=repo_id,
            worker_kind=worker_kind,
            working_directory=cwd,
            environment=environment,
            stdin_handle=_native_handle(stdin_fd),
            stdout_handle=_native_handle(stdout_write),
            stderr_handle=_native_handle(stderr_write),
            capability_sids=WORKER_NETWORK_CAPABILITIES,
            filesystem_grants=grants,
            withheld_directories=tuple(
                str(path) for path in (bridge_spec or {}).get("withheld_directories") or ()
            ),
        )
        launch = windows_appcontainer.launch_appcontainer(request)
        if bridge is not None:
            bridge.start(launch.job)
        # CreateProcess has duplicated every inherited handle into the child.
        # Close the parent's copies immediately. In particular, retaining the
        # stdin reader prevents a fast-exiting child from breaking the prompt
        # writer's pipe and can starve this supervisor before it records
        # child_pid/running state.
        os.close(stdin_fd)
        fds = tuple(fd for fd in fds if fd != stdin_fd)
        os.close(stdout_write)
        fds = tuple(fd for fd in fds if fd != stdout_write)
        os.close(stderr_write)
        fds = tuple(fd for fd in fds if fd != stderr_write)
        if stdin_write_fd is not None:
            stdin_stream = os.fdopen(stdin_write_fd, "wb")
            fds = tuple(fd for fd in fds if fd != stdin_write_fd)
            _feed_and_close_stdin(stdin_stream, stdin_text)
        return _AppContainerProcess(
            launch,
            os.fdopen(stdout_read, "rb", buffering=0),
            os.fdopen(stderr_read, "rb", buffering=0),
            (),
            bridge,
        )
    except Exception:
        try:
            if bridge is not None:
                bridge.close()
            elif pipe is not None:
                pipe.close()
        except Exception:
            pass
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


class ChildTerminationError(RuntimeError):
    """A child outlived every termination primitive the supervisor owns."""


def _wait_for_child_exit(child: Any, grace: float) -> int | None:
    """Return the child's exit code, or None if it is still alive after grace."""
    try:
        return int(child.wait(timeout=grace))
    except subprocess.TimeoutExpired:
        return None


def _windows_tree_kill(pid: int, grace: float) -> str:
    """Attempt a `taskkill /T` tree kill; return "" on success, else why it failed.

    NF-2026-01166: inside the Windows AppContainer worker sandbox this exits 1
    with "ERROR: The user name or password is incorrect." and leaves the child
    running, so the outcome has to be read rather than discarded. A taskkill
    that hangs must not block the rest of the ladder either, so it is bounded
    by the caller's own grace period.
    """
    try:
        completed = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=grace,
        )
    except subprocess.TimeoutExpired:
        return f"timeout_after_{grace}s"
    except OSError as exc:
        return f"{type(exc).__name__}:{exc}"[:200]
    if completed.returncode == 0:
        return ""
    detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
    return f"exit_{completed.returncode}:{detail}"[:200]


def _terminate_child_windows(child: Any, grace: float) -> int:
    """End a live Windows child, escalating to the handles the supervisor owns.

    The tree kill stays the first attempt because it is the only one that
    reaches grandchildren by pid. When it cannot do its job the supervisor
    falls back to what it holds directly -- the kill-on-close Job this child
    was assigned to, then the child handle itself -- and raises rather than
    returning an exit code while the child is still alive.
    """
    tree_kill_error = _windows_tree_kill(child.pid, grace)
    # A tree kill that ran is given the full grace period. One that failed is
    # given none: waiting on it is exactly the TimeoutExpired that used to
    # escape this function with the child still running.
    if tree_kill_error:
        returncode = child.poll()
    else:
        returncode = _wait_for_child_exit(child, grace)
    if returncode is not None:
        return int(returncode)
    attempts = [f"taskkill:{tree_kill_error or 'child_survived'}"]
    job = getattr(child, _KILL_JOB_ATTR, None)
    if job is not None:
        try:
            job.terminate()
        except OSError as exc:
            attempts.append(f"kill_on_close_job:{type(exc).__name__}:{exc}")
        else:
            attempts.append("kill_on_close_job:terminated")
            returncode = _wait_for_child_exit(child, grace)
            if returncode is not None:
                return returncode
    try:
        child.kill()
    except OSError as exc:
        attempts.append(f"child_handle:{type(exc).__name__}:{exc}")
    else:
        attempts.append("child_handle:killed")
    returncode = _wait_for_child_exit(child, grace)
    if returncode is not None:
        return returncode
    raise ChildTerminationError(
        f"child_pid={child.pid} still alive after " + ", ".join(attempts)
    )


def _terminate_child(child: Any, grace: float = KILL_GRACE_SECONDS) -> int:
    if isinstance(child, _AppContainerProcess):
        child.terminate()
        return int(child.wait(timeout=grace))
    if os.name == "nt":
        return _terminate_child_windows(child, grace)
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


# NF-2026-01159.  The launcher records how many prompt bytes it is about to
# put on this supervisor's stdin under this spec key -- a count only, never the
# prompt text, so the spec, the logs and the receipts stay free of it.  The
# producer is ``runtime_adapters.WORKER_PROMPT_BYTES_SPEC_KEY``; it is spelled
# out here rather than imported because the supervisor also runs as a direct
# script next to its sibling modules, with no ``runtime_adapters`` on the path.
WORKER_PROMPT_BYTES_SPEC_KEY = "stdin_text_bytes"
WORKER_PROMPT_NOT_DELIVERED = "worker_prompt_not_delivered"
# NF-2026-01354.  ``runtime_adapters.PROMPT_ON_STDIN_ADAPTERS``, spelled out for
# the same standalone reason.  These CLIs read their prompt from stdin, so a
# spec for one of them that declares no prompt bytes was written by a launcher
# that lost the prompt -- the auth relaunch did exactly that -- and is refused.
PROMPT_ON_STDIN_ADAPTERS = frozenset({"claude_cli", "codex_cli"})


def _expected_prompt_bytes(spec: dict[str, Any]) -> int:
    """Prompt bytes the launcher declared it would deliver, else ``0``.

    ``0`` is the whole no-prompt fact: an absent key (every adapter that keeps
    its prompt in argv, and every spec written before this key existed), a
    malformed value, or a negative one.  Only a positive declared count can
    make :func:`supervise` refuse, so no existing launch shape is affected.
    """
    try:
        declared = int(spec.get(WORKER_PROMPT_BYTES_SPEC_KEY) or 0)
    except (TypeError, ValueError):
        return 0
    return declared if declared > 0 else 0

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


def supervise(spec: dict[str, Any], *, stdin_text: str | None = None) -> int:
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
    # NF-2026-00517: timeout_seconds is a monotonic hard wall deadline for
    # every spawned backend (POSIX/Landlock subprocess, editor bridge, native
    # CLI route, OpenCode, Windows AppContainer). Heartbeats, provider output,
    # trusted progress and usage telemetry never extend it.
    deadline_epoch = started_epoch + timeout
    deadline_monotonic = time.monotonic() + timeout
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
    # NF-2026-01159: the number of prompt bytes this supervisor actually
    # received on its own stdin.  A count is not the prompt, so it is safe in
    # every receipt -- and it is the only thing that can tell "this plan
    # carries no prompt" apart from "this plan's prompt was lost on the way".
    prompt_bytes_delivered = len(stdin_text.encode("utf-8")) if stdin_text else 0
    expected_prompt_bytes = _expected_prompt_bytes(spec)
    _write_json_0600(status_path, {
        "state": "starting",
        "supervisor_pid": supervisor_pid,
        "supervisor_pid_start_ticks": supervisor_pid_start_ticks,
        "started_at_epoch": started_epoch,
        "deadline_epoch": deadline_epoch,
        "timeout_seconds": timeout,
        "timeout_enforced": True,
        "prompt_bytes_delivered": prompt_bytes_delivered,
    })

    try:
        # The launch plan declared a prompt of exactly this many bytes.  Refuse
        # here rather than hand the child DEVNULL and let it die on an empty
        # stdin with a provider-worded error and zero changed files.  This runs
        # before any spawn and ahead of the backend branch below, so the
        # plain-subprocess and AppContainer paths are covered by one check.
        # NF-2026-01354: for a prompt-on-stdin CLI an undeclared count is not
        # "no prompt", it is a launcher that forgot one.
        if (
            expected_prompt_bytes and prompt_bytes_delivered != expected_prompt_bytes
        ) or (not expected_prompt_bytes and adapter_id in PROMPT_ON_STDIN_ADAPTERS):
            _write_json_0600(status_path, {
                "state": "spawn_failed",
                "supervisor_pid": supervisor_pid,
                "exit_code": 126,
                "spawn_phase": "worker_prompt_delivery",
                "error": (
                    f"{WORKER_PROMPT_NOT_DELIVERED}:"
                    f"expected_bytes={expected_prompt_bytes or 'undeclared'}:"
                    f"received_bytes={prompt_bytes_delivered}"
                ),
                "prompt_bytes_expected": expected_prompt_bytes,
                "prompt_bytes_delivered": prompt_bytes_delivered,
                "started_at_epoch": started_epoch,
                "finished_at_epoch": time.time(),
                "deadline_epoch": deadline_epoch,
                "timeout_seconds": timeout,
                "timeout_enforced": True,
            })
            return 126
        spawn_phase = "child_spawn"
        try:
            if execution_backend == "windows_appcontainer":
                child = _launch_appcontainer_process(argv, cwd, spec, stdin_text=stdin_text)
            elif execution_backend not in (None, ""):
                # A spec that names a backend gets that backend or nothing.
                # Silently falling through to the plain-subprocess branch on a
                # near-miss spelling is exactly how a worker ends up reading as
                # confined while being held by a Job Object and nothing else.
                spawn_phase = "execution_backend_unsupported"
                raise ValueError(
                    f"unsupported_execution_backend:{execution_backend}"
                )
            else:
                popen_kwargs: dict[str, Any] = {
                    "cwd": cwd,
                    "env": os.environ.copy(),
                    "stdin": subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
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
                if stdin_text is not None:
                    _feed_and_close_stdin(child.stdin, stdin_text)
                if windows_job is not None:
                    spawn_phase = "job_assignment"
                    windows_job.assign(child)
                    # NF-2026-01166: remember the Job this child just joined so
                    # _terminate_child can escalate to a supervisor-owned tree
                    # kill when `taskkill` cannot do its job.
                    setattr(child, _KILL_JOB_ATTR, windows_job)
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
                "deadline_epoch": deadline_epoch,
                "timeout_seconds": timeout,
                "timeout_enforced": True,
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
            "deadline_epoch": deadline_epoch,
            "timeout_seconds": timeout,
            "timeout_enforced": True,
            "prompt_bytes_delivered": prompt_bytes_delivered,
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
                    last_output_change_epoch = max(
                        last_output_change_epoch, last_meaningful_progress_epoch
                    )
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
                    "deadline_epoch": deadline_epoch,
                    "timeout_seconds": timeout,
                    "timeout_enforced": True,
                    "prompt_bytes_delivered": prompt_bytes_delivered,
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
            "timeout_enforced": True,
            "prompt_bytes_delivered": prompt_bytes_delivered,
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
                "deadline_epoch": deadline_epoch,
                "timeout_seconds": timeout,
                "timeout_enforced": True,
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
        # A manager poller reading the cancel file must not crash this
        # cleanup with a transient Windows sharing denial (NF-2026-01348).
        retrying_unlink(cancel_path, missing_ok=True)

    if final_state == "cancelled":
        return 125
    if final_state == "timed_out":
        return 124
    return int(returncode)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    # The prompt (NF-2026-00042) arrives on our own stdin, never in the spec
    # file: a plain-subprocess DEVNULL launch reads back empty bytes here.
    #
    # NF-2026-01159: an empty read is NOT the same fact as "this launch plan
    # carries no prompt", and collapsing both into ``None`` here is what let a
    # prompt lost anywhere upstream reach ``claude -p`` as ``stdin=DEVNULL``.
    # The value still collapses to ``None`` so the genuine no-prompt branch is
    # unchanged; ``supervise`` is what refuses a short delivery now, by
    # comparing the bytes that arrived against the count the launcher recorded
    # in the spec.  Undecodable bytes are a lost prompt too, never a prompt.
    raw_stdin = sys.stdin.buffer.read() if sys.stdin is not None else b""
    try:
        decoded = raw_stdin.decode("utf-8")
    except UnicodeDecodeError:
        decoded = ""
    stdin_text = decoded or None
    spec_path = Path(args.spec)
    try:
        spec = _load_spec(spec_path)
    except Exception as exc:
        print(f"invalid supervisor spec: {exc}", file=sys.stderr)
        raise SystemExit(126)
    finally:
        _unlink_if_regular(spec_path)
    try:
        code = supervise(spec, stdin_text=stdin_text)
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
