"""NF-2026-01154: the sandbox drive letter only inside a private logon session.

OWNER DECISION: no AIWorkHub launch may add a DOS drive letter to the owner's
interactive logon session, even temporarily.  These tests pin the two halves of
the fix with no real Windows call:

* ``_CtypesWin32Api.sandbox_drive`` refuses -- with a typed error, before any
  ``DefineDosDeviceW`` -- when the logon-type probe reports the owner's
  interactive session (type 2), and proceeds exactly as before when it reports a
  private NewCredentials session (type 9);
* the ``CreateProcessWithLogonW`` spawn primitive launches suspended, jobs the
  child before ``ResumeThread``, terminates and closes on any failure, and
  relays the inner exit code.

The reproduction (``sandbox_drive`` under logon type 2) FAILS on today's code,
where ``sandbox_drive`` has no logon-session guard and would reach
``DefineDosDeviceW`` in the owner's session.
"""

from __future__ import annotations

import inspect
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import aiworkhub.windows_appcontainer as wac
import aiworkhub.worker_workspace as worker_workspace
from aiworkhub import windows_private_session as wps
from aiworkhub.windows_appcontainer import AppContainerError, AppContainerReason
from aiworkhub.windows_private_session import (
    _CREATE_NO_WINDOW,
    _CREATE_SUSPENDED,
    _CREATE_UNICODE_ENVIRONMENT,
    _CtypesSpawnApi,
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
    _LOGON_NETCREDENTIALS_ONLY,
    PrivateLogonSessionError,
    PrivateLogonSessionTimeout,
    SECURITY_LOGON_TYPE_NEW_CREDENTIALS,
    in_private_logon_session,
    spawn_in_private_logon_session,
)
from aiworkhub.worker_workspace import WorkerWorkspace

_ROOT = r"D:\Repo\.aiworkhub\runtime\worktrees"


class _FakeDosKernel:
    """kernel32's mutex and DOS-device calls, modelled as the real OS does.

    DefineDosDeviceW is a per-letter stack: a plain define pushes, a define with
    DDD_REMOVE_DEFINITION (0x2) pops. QueryDosDeviceW reports the whole stack,
    top first, as a multi-sz blob honouring the caller's buffer size.
    """

    def __init__(self, define_ok: bool = True) -> None:
        self.devices: dict[str, list[str]] = {}
        self.define_ok = define_ok
        self.last_error = 0
        self.calls: list[tuple] = []

    def CreateMutexW(self, _attributes, _owner, name):
        self.calls.append(("mutex", name))
        return 77

    def WaitForSingleObject(self, handle, wait_ms):
        self.calls.append(("wait", handle, wait_ms))
        return 0

    def ReleaseMutex(self, handle):
        self.calls.append(("release", handle))
        return 1

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return 1

    def GetLogicalDrives(self):
        return 0

    def QueryDosDeviceW(self, name, buffer, size):
        stack = self.devices.get(name) or []
        if not stack:
            return 0
        blob = "\0".join(reversed(stack)) + "\0\0"
        if len(blob) > size:
            self.last_error = 122
            return 0
        buffer[: len(blob)] = blob
        return len(blob)

    def DefineDosDeviceW(self, flags, name, target):
        self.calls.append(("define", flags, name, target))
        if not self.define_ok:
            return False
        wanted = "\\??\\" + target
        if flags & 0x2:
            stack = self.devices.get(name) or []
            if wanted in stack:
                stack.remove(wanted)
            self.devices[name] = stack
        else:
            self.devices.setdefault(name, []).append(wanted)
        return self.define_ok


def _ctypes_api(kernel: _FakeDosKernel) -> wac._CtypesWin32Api:
    api = wac._CtypesWin32Api.__new__(wac._CtypesWin32Api)
    api._kernel32 = kernel
    api._security_base = kernel
    api._security_base_library = "kernelbase"
    api._userenv = None
    api._advapi32 = None
    return api


def _define_calls(kernel: _FakeDosKernel) -> list[tuple]:
    return [call for call in kernel.calls if call[0] == "define"]


# --------------------------------------------------------------------------- #
# sandbox_drive fails closed outside a private logon session
# --------------------------------------------------------------------------- #


def test_sandbox_drive_refuses_in_the_owner_interactive_session(monkeypatch):
    """Logon type 2 (interactive): a typed refusal before any DefineDosDeviceW."""
    monkeypatch.setattr(wps, "current_logon_type", lambda: 2)
    kernel = _FakeDosKernel()
    api = _ctypes_api(kernel)

    with pytest.raises(AppContainerError) as excinfo:
        api.sandbox_drive(_ROOT)

    assert excinfo.value.reason is AppContainerReason.PRIVATE_LOGON_SESSION_REQUIRED
    assert excinfo.value.operation == "sandbox_drive"
    assert excinfo.value.detail.startswith("sandbox_drive_requires_private_logon_session")
    # The whole point: not one DefineDosDeviceW, and not even the mutex, ran.
    assert _define_calls(kernel) == []
    assert kernel.calls == []


def test_sandbox_drive_proceeds_in_a_private_new_credentials_session(monkeypatch):
    """Logon type 9 (NewCredentials): the letter is defined exactly as today."""
    monkeypatch.setattr(wps, "current_logon_type", lambda: SECURITY_LOGON_TYPE_NEW_CREDENTIALS)
    kernel = _FakeDosKernel()
    api = _ctypes_api(kernel)

    assert api.sandbox_drive(_ROOT) == "Z:"
    assert _define_calls(kernel) == [("define", 0x8, "Z:", _ROOT)]
    assert kernel.devices["Z:"] == ["\\??\\" + _ROOT]


def test_in_private_logon_session_is_true_only_for_type_nine(monkeypatch):
    monkeypatch.setattr(wps, "current_logon_type", lambda: 2)
    assert in_private_logon_session() is False
    monkeypatch.setattr(wps, "current_logon_type", lambda: SECURITY_LOGON_TYPE_NEW_CREDENTIALS)
    assert in_private_logon_session() is True
    monkeypatch.setattr(wps, "current_logon_type", lambda: None)
    assert in_private_logon_session() is False


# --------------------------------------------------------------------------- #
# CreateProcessWithLogonW spawn primitive
# --------------------------------------------------------------------------- #


class _FakeSpawnApi:
    """Records the spawn orchestration without touching Windows."""

    def __init__(self, *, exit_code=7, fail_at=None):
        self.exit_code_value = exit_code
        self.fail_at = fail_at
        self.events: list[str] = []
        self.create_kwargs: dict = {}
        self.closed: list[int] = []
        self.terminated: list[int] = []

    def create_process_with_logon(
        self, username, command_line, creation_flags, environment_block,
        working_directory, stdin_handle, stdout_handle, stderr_handle,
    ):
        self.events.append("create")
        self.create_kwargs = dict(
            username=username, command_line=command_line,
            creation_flags=creation_flags, environment_block=environment_block,
            working_directory=working_directory, stdin_handle=stdin_handle,
            stdout_handle=stdout_handle, stderr_handle=stderr_handle,
        )
        return 100, 200, 4242

    def create_kill_on_close_job(self):
        self.events.append("job")
        return 300

    def assign_process_to_job(self, job, process):
        self.events.append("assign")
        if self.fail_at == "assign":
            raise PrivateLogonSessionError("assign_process_failed", win_error=5)

    def resume_thread(self, thread):
        self.events.append("resume")
        if self.fail_at == "resume":
            raise PrivateLogonSessionError("resume_thread_failed", win_error=5)

    def terminate_process(self, process, exit_code=1):
        self.terminated.append(process)

    def wait(self, process, timeout_ms):
        self.events.append("wait")

    def exit_code(self, process):
        return self.exit_code_value

    def close_handle(self, handle):
        if handle:
            self.closed.append(handle)


def test_spawn_suspends_jobs_then_resumes_and_relays_the_inner_exit_code():
    api = _FakeSpawnApi(exit_code=42)
    code = spawn_in_private_logon_session(
        r"C:\Python\python.exe worker.py",
        environment={"A": "B"},
        working_directory=r"C:\work",
        stdin_handle=11, stdout_handle=12, stderr_handle=13,
        api=api,
    )
    assert code == 42
    # Suspended create, then job, then assign, then resume -- job ownership is
    # established before the child can run a single instruction.
    assert api.events == ["create", "job", "assign", "resume", "wait"]
    assert api.create_kwargs["creation_flags"] == (
        _CREATE_SUSPENDED | _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT
    )
    assert api.create_kwargs["environment_block"] == "A=B\x00\x00"
    assert api.create_kwargs["working_directory"] == r"C:\work"
    assert api.create_kwargs["stdin_handle"] == 11
    # The thread, process and job handles are all released.
    assert set(api.closed) == {100, 200, 300}


@pytest.mark.parametrize("fail_at", ["assign", "resume"])
def test_spawn_terminates_the_suspended_child_and_raises_on_failure(fail_at):
    api = _FakeSpawnApi(fail_at=fail_at)
    with pytest.raises(PrivateLogonSessionError):
        spawn_in_private_logon_session(
            r"C:\Python\python.exe worker.py",
            environment={"A": "B"},
            api=api,
        )
    # The suspended process is terminated and every handle closed, so nothing is
    # left running outside the job.
    assert api.terminated == [100]
    assert set(api.closed) == {100, 200, 300}
    assert "wait" not in api.events


class _FakeAdvapi:
    def __init__(self):
        self.calls: list[tuple] = []

    def CreateProcessWithLogonW(self, *args):
        self.calls.append(args)
        return 1


class _FakeKernelHandles:
    def __init__(self):
        self.set_inherit: list[int] = []
        self.jobs: list[tuple] = []
        self.job_handle = 500

    def SetHandleInformation(self, handle, mask, flags):
        self.set_inherit.append(int(getattr(handle, "value", handle) or 0))
        return 1

    def CreateJobObjectW(self, attributes, name):
        return self.job_handle

    def SetInformationJobObject(self, handle, info_class, info, size):
        self.jobs.append((int(getattr(handle, "value", handle) or 0), info_class, size))
        return 1

    def CloseHandle(self, handle):
        return 1


def _real_spawn_api(kernel, advapi) -> _CtypesSpawnApi:
    api = _CtypesSpawnApi.__new__(_CtypesSpawnApi)
    api._kernel32 = kernel
    api._advapi32 = advapi
    return api


def test_create_process_with_logon_requests_netcredentials_only_and_the_right_flags():
    advapi = _FakeAdvapi()
    kernel = _FakeKernelHandles()
    api = _real_spawn_api(kernel, advapi)

    flags = _CREATE_SUSPENDED | _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT
    api.create_process_with_logon(
        "owner", r"C:\python.exe x.py", flags, "A=B\x00\x00", r"C:\cwd",
        11, 12, 13,
    )

    (args,) = advapi.calls
    username, domain, password, logon_flags = args[0], args[1], args[2], args[3]
    application, _command, creation_flags, environment = args[4], args[5], args[6], args[7]
    current_directory = args[8]
    assert username == "owner"
    assert domain == "."
    assert password == "-"
    assert logon_flags == _LOGON_NETCREDENTIALS_ONLY
    assert application is None
    assert creation_flags == flags
    assert environment is not None  # an explicit env block, never NULL
    assert current_directory == r"C:\cwd"
    # Every std handle was marked inheritable before the launch.
    assert kernel.set_inherit == [11, 12, 13]


def test_create_kill_on_close_job_uses_the_extended_limit_class():
    kernel = _FakeKernelHandles()
    api = _real_spawn_api(kernel, _FakeAdvapi())
    assert api.create_kill_on_close_job() == 500
    (job_call,) = kernel.jobs
    assert job_call[1] == _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS


class _TimeoutSpawnApi(_FakeSpawnApi):
    def wait(self, process, timeout_ms):
        self.events.append(("wait", timeout_ms))
        raise PrivateLogonSessionTimeout("wait_process_timeout")


def test_spawn_deadline_terminates_the_child_closes_its_job_and_raises():
    api = _TimeoutSpawnApi()
    with pytest.raises(PrivateLogonSessionTimeout):
        spawn_in_private_logon_session(
            r"C:\Python\python.exe helper.py",
            environment={"A": "B"},
            api=api,
            timeout_seconds=2.5,
        )
    assert ("wait", 2500) in api.events
    # Terminated, and the kill-on-close Job (300) closed: the whole tree dies.
    assert api.terminated == [100]
    assert set(api.closed) == {100, 200, 300}


def test_spawn_without_a_deadline_waits_infinitely():
    api = _FakeSpawnApi()
    seen: list[int] = []
    api.wait = lambda process, timeout_ms: seen.append(timeout_ms)
    spawn_in_private_logon_session("x", environment={"A": "B"}, api=api)
    assert seen == [wps._INFINITE]


# --------------------------------------------------------------------------- #
# Validation-lane hop: run_appcontainer_validation_hopped / validation_relay_main
# --------------------------------------------------------------------------- #


def _workspace(tmp_path: Path) -> WorkerWorkspace:
    request_root = tmp_path / ("r" * 32)
    (request_root / "worktree").mkdir(parents=True)
    (request_root / "home").mkdir()
    return WorkerWorkspace(
        request_id="r" * 32,
        repo=tmp_path,
        path=request_root / "worktree",
        home=request_root / "home",
        allowed_writes=("src/a.py",),
        parent_baseline={},
        workspace_baseline={},
    )


def _never_direct(*_args, **_kwargs):
    raise AssertionError("run_direct must not run in the owner-session parent")


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class _PrivateHelperSpawn:
    """Stands in for CreateProcessWithLogonW: runs the real relay helper body.

    The parent probe answers "owner session" until this fake starts, then
    "private" -- the helper is, by construction, the type-9 side of the hop.
    """

    def __init__(self, monkeypatch, *, exit_code=0, raw_result=None, error=None):
        self.monkeypatch = monkeypatch
        self.exit_code = exit_code
        self.raw_result = raw_result
        self.error = error
        self.calls: list[dict] = []
        self.spec: dict | None = None
        self.private = False

    def probe(self) -> bool:
        return self.private

    def __call__(self, command_line, **kwargs):
        self.calls.append({"command_line": command_line, **kwargs})
        if self.error is not None:
            raise self.error
        raw = _read_all(kwargs["stdin_handle"])
        self.spec = json.loads(raw)
        if self.raw_result is None:
            self.private = True
            out = io.BytesIO()
            self.monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
            self.monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=out))
            assert wps.validation_relay_main() == 0
            result = out.getvalue()
        else:
            result = self.raw_result
        _write_all(kwargs["stdout_handle"], result)
        _write_all(kwargs["stderr_handle"], b"helper diagnostics")
        return self.exit_code


def _install_hop(monkeypatch, spawn: _PrivateHelperSpawn, inner=None) -> None:
    monkeypatch.setattr(wac, "platform_supported", lambda: True)
    monkeypatch.setattr(wps, "in_private_logon_session", spawn.probe)
    monkeypatch.setattr(wps, "_pipe_handle", lambda fd: fd)
    monkeypatch.setattr(wps, "spawn_in_private_logon_session", spawn)
    if inner is not None:
        monkeypatch.setattr(worker_workspace, "_run_appcontainer_validation", inner)


def _hop(tmp_path, workspace=None, timeout_seconds=90):
    workspace = workspace or _workspace(tmp_path)
    return wps.run_appcontainer_validation_hopped(
        ["python", "-m", "pytest", "-q"],
        workspace=workspace,
        adapter_id="claude_cli",
        cwd=workspace.path,
        env={"PATH": r"C:\Windows", "HOME": str(workspace.home)},
        timeout_seconds=timeout_seconds,
        run_direct=_never_direct,
    )


def test_owner_session_relays_through_one_private_spawn_and_never_runs_direct(
    tmp_path, monkeypatch
):
    seen: list[dict] = []

    def inner(argv, **kwargs):
        seen.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    spawn = _PrivateHelperSpawn(monkeypatch)
    _install_hop(monkeypatch, spawn, inner)
    workspace = _workspace(tmp_path)

    result = _hop(tmp_path, workspace, timeout_seconds=90)

    assert result.returncode == 0 and result.stdout == "ok\n"
    (call,) = spawn.calls
    # The spec rides on the helper's stdin: argv, cwd, env, timeout, adapter and
    # the workspace in its existing as_metadata() form -- no second schema.
    assert spawn.spec["schema_id"] == wps._VALIDATION_RELAY_SCHEMA
    assert spawn.spec["argv"] == ["python", "-m", "pytest", "-q"]
    assert spawn.spec["workspace"] == workspace.as_metadata()
    assert spawn.spec["timeout_seconds"] == 90
    assert spawn.spec["adapter_id"] == "claude_cli"
    # The helper is this interpreter running the relay entry point, under a
    # deadline of the lane's own timeout plus the fixed grace.
    assert sys.executable in call["command_line"]
    assert "validation_relay_main" in call["command_line"]
    # -P: the caller's cwd never becomes the helper's first import root.
    assert " -P -c " in call["command_line"]
    assert call["timeout_seconds"] == 90 + wps._VALIDATION_RELAY_GRACE_SECONDS
    package_parent = str(Path(wps.__file__).resolve().parent.parent)
    assert call["environment"]["PYTHONPATH"].split(os.pathsep)[0] == package_parent
    # Inside the helper the real lane ran once, with the rebuilt workspace.
    (inner_call,) = seen
    assert inner_call["argv"] == ["python", "-m", "pytest", "-q"]
    assert inner_call["workspace"].request_id == workspace.request_id
    assert inner_call["adapter_id"] == "claude_cli"
    assert inner_call["timeout_seconds"] == 90


def test_already_private_runs_direct_and_never_spawns(tmp_path, monkeypatch):
    monkeypatch.setattr(wac, "platform_supported", lambda: True)
    monkeypatch.setattr(wps, "in_private_logon_session", lambda: True)

    def no_spawn(*_args, **_kwargs):
        raise AssertionError("an already-private caller must not hop again")

    monkeypatch.setattr(wps, "spawn_in_private_logon_session", no_spawn)
    workspace = _workspace(tmp_path)
    direct: list[tuple] = []

    def run_direct(argv, **kwargs):
        direct.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 3, "o", "e")

    result = wps.run_appcontainer_validation_hopped(
        ["node", "--version"], workspace=workspace, adapter_id="claude_cli",
        cwd=workspace.path, env={"A": "B"}, timeout_seconds=5, run_direct=run_direct,
    )

    assert result.returncode == 3
    assert direct == [(
        ["node", "--version"],
        {"workspace": workspace, "adapter_id": "claude_cli", "cwd": workspace.path,
         "env": {"A": "B"}, "timeout_seconds": 5},
    )]


def _install_supervisor_hop(monkeypatch, calls: list[dict]) -> None:
    monkeypatch.setattr(wac, "platform_supported", lambda: True)
    monkeypatch.setattr(wps, "in_private_logon_session", lambda: False)
    monkeypatch.setattr(
        wps, "_read_spec_without_unlinking",
        lambda _path: {"execution_backend": "windows_appcontainer"},
    )
    monkeypatch.setattr(wps, "_CtypesSpawnApi", lambda: SimpleNamespace(std_handle=lambda n: n))

    def spawn(command_line, **kwargs):
        calls.append({"command_line": command_line, **kwargs})
        return 7

    monkeypatch.setattr(wps, "spawn_in_private_logon_session", spawn)


def test_supervisor_hop_marks_the_inner_copy(tmp_path, monkeypatch):
    monkeypatch.delenv(wps._PRIVATE_SESSION_HOP_ENV, raising=False)
    calls: list[dict] = []
    _install_supervisor_hop(monkeypatch, calls)

    assert wps.reexec_supervisor_if_owner_session(tmp_path / "spec.json") == 7

    (call,) = calls
    assert call["environment"][wps._PRIVATE_SESSION_HOP_ENV] == "1"
    assert "--spec" in call["command_line"]


def test_hopped_supervisor_that_is_still_not_private_fails_instead_of_hopping_again(
    tmp_path, monkeypatch
):
    # A logon-type probe that fails inside the private copy reads as "owner";
    # without the marker that copy would hop again, one seclogon session per level.
    monkeypatch.setenv(wps._PRIVATE_SESSION_HOP_ENV, "1")
    calls: list[dict] = []
    _install_supervisor_hop(monkeypatch, calls)

    with pytest.raises(PrivateLogonSessionError, match="private_session_hop_not_private"):
        wps.reexec_supervisor_if_owner_session(tmp_path / "spec.json")
    assert calls == []


def test_completed_result_round_trips_byte_exact(tmp_path, monkeypatch):
    stdout = "héllo\r\nline ✓\x00tail\n" * 5000
    stderr = "warn: \u2603\r\n"

    def inner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout, stderr)

    _install_hop(monkeypatch, _PrivateHelperSpawn(monkeypatch), inner)
    result = _hop(tmp_path)

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.args == ["python", "-m", "pytest", "-q"]
    assert result.returncode == 1
    assert result.stdout == stdout
    assert result.stderr == stderr


def test_timeout_result_is_timeout_expired_with_the_captured_output(tmp_path, monkeypatch):
    def inner(argv, **_kwargs):
        raise subprocess.TimeoutExpired(list(argv), 7, output="partial out", stderr="partial err")

    _install_hop(monkeypatch, _PrivateHelperSpawn(monkeypatch), inner)
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        _hop(tmp_path)

    assert excinfo.value.timeout == 7
    assert excinfo.value.cmd == ["python", "-m", "pytest", "-q"]
    assert excinfo.value.output == "partial out"
    assert excinfo.value.stderr == "partial err"


def test_launch_failure_result_is_oserror(tmp_path, monkeypatch):
    def inner(_argv, **_kwargs):
        raise OSError("windows_appcontainer_validation_launch_failed:boom")

    _install_hop(monkeypatch, _PrivateHelperSpawn(monkeypatch), inner)
    with pytest.raises(OSError) as excinfo:
        _hop(tmp_path)

    assert str(excinfo.value) == "windows_appcontainer_validation_launch_failed:boom"


def test_a_helper_fault_is_relayed_as_oserror_never_a_pass(tmp_path, monkeypatch):
    def inner(_argv, **_kwargs):
        raise RuntimeError("unexpected")

    _install_hop(monkeypatch, _PrivateHelperSpawn(monkeypatch), inner)
    with pytest.raises(OSError) as excinfo:
        _hop(tmp_path)

    assert "validation_relay_failed:RuntimeError:unexpected" in str(excinfo.value)


@pytest.mark.parametrize(
    ("raw_result", "exit_code", "reason"),
    [
        (b"", 0, "result_missing"),
        (b"{not json", 0, "result_unparsable"),
        (b"\xff\xfe", 0, "result_unparsable"),
        (b'{"kind": "completed"}', 0, "result_unparsable"),
        (json.dumps({"schema_id": wps._VALIDATION_RELAY_SCHEMA, "kind": "completed"}).encode(),
         0, "result_unparsable"),
        (json.dumps({"schema_id": wps._VALIDATION_RELAY_SCHEMA, "kind": "pass"}).encode(),
         0, "result_kind_unknown"),
        (json.dumps({
            "schema_id": wps._VALIDATION_RELAY_SCHEMA, "kind": "completed", "args": [],
            "returncode": 0, "stdout": "", "stderr": "",
        }).encode(), 1, "relay_exit_nonzero"),
    ],
)
def test_missing_garbled_or_unclean_results_fail_closed_as_oserror(
    tmp_path, monkeypatch, raw_result, exit_code, reason
):
    spawn = _PrivateHelperSpawn(monkeypatch, exit_code=exit_code, raw_result=raw_result)
    _install_hop(monkeypatch, spawn)
    with pytest.raises(OSError) as excinfo:
        _hop(tmp_path)

    message = str(excinfo.value)
    assert message.startswith(f"windows_private_session_validation_{reason}:exit={exit_code}")
    assert "helper diagnostics" in message


def test_an_oversized_result_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(wps, "_MAX_VALIDATION_RELAY_BYTES", 64 * 1024)
    spawn = _PrivateHelperSpawn(monkeypatch, raw_result=b"x" * (200 * 1024))
    _install_hop(monkeypatch, spawn)
    with pytest.raises(OSError) as excinfo:
        _hop(tmp_path)

    assert str(excinfo.value).startswith("windows_private_session_validation_result_oversized")


def test_hop_deadline_is_timeout_expired_and_seclogon_failure_names_win_error(
    tmp_path, monkeypatch
):
    timeout_spawn = _PrivateHelperSpawn(
        monkeypatch, error=PrivateLogonSessionTimeout("wait_process_timeout")
    )
    _install_hop(monkeypatch, timeout_spawn)
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        _hop(tmp_path, timeout_seconds=11)
    assert excinfo.value.timeout == 11

    failing_spawn = _PrivateHelperSpawn(
        monkeypatch,
        error=PrivateLogonSessionError("create_process_with_logon_failed", win_error=1058),
    )
    _install_hop(monkeypatch, failing_spawn)
    with pytest.raises(OSError) as oserror:
        _hop(tmp_path, _workspace(tmp_path / "second"))
    assert str(oserror.value) == (
        "windows_private_session_validation_hop_failed:"
        "create_process_with_logon_failed win_error=1058"
    )


def test_the_relay_helper_refuses_to_run_outside_a_private_session(monkeypatch):
    monkeypatch.setattr(wps, "in_private_logon_session", lambda: False)

    def inner(*_args, **_kwargs):
        raise AssertionError("the lane must not run in the owner session")

    monkeypatch.setattr(worker_workspace, "_run_appcontainer_validation", inner)
    spec = json.dumps({"schema_id": wps._VALIDATION_RELAY_SCHEMA}).encode()
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(spec)))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=out))

    assert wps.validation_relay_main() == 0
    record = json.loads(out.getvalue())
    assert record["kind"] == "oserror"
    assert "validation_relay_not_in_private_logon_session" in record["message"]


def test_run_validations_routes_its_appcontainer_branch_through_the_hop():
    """The one production call site hands run_direct=_run_appcontainer_validation
    to the hop instead of calling the lane itself (worker_workspace stays at the
    module ratchet, so this is a source-shape check of that single call)."""
    source = inspect.getsource(worker_workspace.run_validations)
    assert "run_appcontainer_validation_hopped(" in source
    assert "run_direct=_run_appcontainer_validation" in source
    assert "result = _run_appcontainer_validation(" not in source
