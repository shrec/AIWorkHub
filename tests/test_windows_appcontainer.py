"""Mocked-Windows-API regression tests for the AppContainer launch foundation.

These tests never touch the real ctypes boundary; they drive
:func:`launch_appcontainer` through a :class:`FakeWin32Api` that records every
resource it allocates/frees and can fail at any single step.  That lets us
assert, for the happy path and for every partial-initialization failure, that
the child is owned by its kill-on-close job before success and that every
SID / attribute-list / job / process / thread handle is unwound on failure
without ever leaving a child running outside the job.
"""

from __future__ import annotations

import ctypes
import json
import os

import pytest

import aiworkhub.windows_appcontainer as wac
from aiworkhub import runtime_adapters
from aiworkhub.windows_appcontainer import (
    AppContainerError,
    AppContainerLifecycleState,
    AppContainerReason,
    AppContainerRequest,
    ContainerGrant,
    _AttributeList,
    _Identity,
    _PathGrant,
    _ProcessCreation,
    _SecurityCapabilities,
    _Win32Failure,
    build_command_line,
    launch_appcontainer,
    request_scoped_grants,
)


# ---------------------------------------------------------------------------
# Mocked Windows boundary
# ---------------------------------------------------------------------------


# NF-2026-00964: the seven tests carrying this marker need real host Win32
# privileges (the actual process token, a real named pipe, a real job
# object) that the AppContainer validation lane does not have when it runs
# this suite inside a container.  On a host -- and on any non-Windows CI
# runner, where the detector is always False -- they run and must pass.
requires_host_win32_privileges = pytest.mark.skipif(
    wac.current_process_is_appcontainer(),
    reason="requires host Win32 privileges; not available inside an AppContainer",
)


class FakeWin32Api:
    """A recording, fail-injectable stand-in for the real Win32 boundary.

    Set ``fail_at`` to a Win32 operation name to raise :class:`_Win32Failure`
    on that call.  Every allocation and release is tracked so tests can assert
    a complete unwind.
    """

    def __init__(self, fail_at: str | None = None, fail_error: int = 0) -> None:
        self.fail_at = fail_at
        self.fail_error = fail_error
        self.events: list[str] = []
        self._counter = 1000

        self.identity: _Identity | None = None
        self.identity_freed = False
        self.security: _SecurityCapabilities | None = None
        self.security_freed = False
        self.job: int | None = None
        self.job_configured = False
        self.job_closed = False
        self.job_terminated = False
        self.attrs: _AttributeList | None = None
        self.attr_deleted = False
        self.security_caps_set = False
        self.inherited_handles: list[int] | None = None
        self.spec = None
        self.creation: _ProcessCreation | None = None
        self.assigned = False
        self.resumed = False
        self.process_terminated = False
        self.process_handle_closed = False
        self.thread_handle_closed = False
        self.wait_results: list[bool] = [False]
        self.exit_code = 0
        self.lifecycle_creations: list[_ProcessCreation] = []
        self.dacl_queries: list[str] = []

    def _token(self) -> int:
        self._counter += 1
        return self._counter

    def _maybe_fail(self, operation: str) -> None:
        self.events.append(operation)
        if operation == self.fail_at:
            raise _Win32Failure(self.fail_error, operation, f"forced-{operation}")

    # -- identity -----------------------------------------------------------

    def derive_identity(self, name, display_name, description):
        self._maybe_fail("derive_appcontainer_sid")
        self.identity = _Identity(
            name, display_name, f"S-1-15-2-{self._token()}", self._token(), True
        )
        return self.identity

    def free_identity(self, identity):
        self.events.append("free_identity")
        self.identity_freed = True

    # -- filesystem grants --------------------------------------------------

    fail_grant_attempt: int | None = None
    grant_attempts = 0

    def grant_path_access(self, identity, path, access, *, persistent=False):
        self.grant_attempts += 1
        if self.grant_attempts == self.fail_grant_attempt:
            raise _Win32Failure(5, "grant_path_access", f"forced-grant {path}")
        assert identity is self.identity, "grant must target this launch's SID"
        self.events.append(f"grant:{access}:{path}")
        return _PathGrant(path, access, None if persistent else ("dacl", path))

    def revoke_path_access(self, grant):
        self.events.append(f"revoke:{grant.path}")
        grant.restore = None

    # Normcased directories whose DACL the fake reports as protected.
    protected_paths: frozenset[str] = frozenset()

    def dacl_protected(self, path):
        self.dacl_queries.append(path)
        return os.path.normcase(path) in self.protected_paths

    def build_security_capabilities(self, identity, capability_sids):
        self._maybe_fail("build_security_capabilities")
        self.security = _SecurityCapabilities(
            identity.sid_string, {"caps": list(capability_sids)}
        )
        return self.security

    def free_security_capabilities(self, sec_caps):
        self.events.append("free_security_capabilities")
        self.security_freed = True

    # -- job ----------------------------------------------------------------

    def create_job_object(self, name):
        self._maybe_fail("create_job_object")
        self.job = self._token()
        return self.job

    def configure_job_object(self, job):
        self._maybe_fail("configure_job_object")
        self.job_configured = True

    def terminate_job(self, job, exit_code=1):
        self._maybe_fail("terminate_job")
        self.job_terminated = True

    def close_job(self, job):
        self._maybe_fail("close_job")
        self.job_closed = True

    # -- attribute list -----------------------------------------------------

    def init_attribute_list(self, attribute_count):
        self._maybe_fail("init_attribute_list")
        self.attrs = _AttributeList(self._token(), [], attribute_count)
        return self.attrs

    def set_security_capabilities(self, attrs, sec_caps):
        self._maybe_fail("set_security_capabilities")
        self.security_caps_set = True

    def set_inherited_handles(self, attrs, handles):
        self._maybe_fail("set_inherited_handles")
        self.inherited_handles = list(handles)

    def delete_attribute_list(self, attrs):
        self.events.append("delete_attribute_list")
        self.attr_deleted = True

    # -- process ------------------------------------------------------------

    def create_process(self, spec):
        self._maybe_fail("create_process")
        self.spec = spec
        self.creation = _ProcessCreation(
            4321, 8765, self._token(), self._token()
        )
        return self.creation

    def assign_process_to_job(self, job, creation):
        self._maybe_fail("assign_process_to_job")
        self.assigned = True

    def resume_thread(self, creation):
        self._maybe_fail("resume_thread")
        self.resumed = True

    def terminate_process(self, creation):
        self.events.append("terminate_process")
        self.process_terminated = True
        self.process_handle_closed = True

    def close_thread_handle(self, creation):
        self.events.append("close_thread_handle")
        self.thread_handle_closed = True

    def close_process_handle(self, creation):
        self._maybe_fail("close_process_handle")
        self.process_handle_closed = True

    def wait_process(self, creation, timeout_ms):
        self._maybe_fail("wait_process")
        self.events.append(f"wait_timeout:{timeout_ms}")
        self.lifecycle_creations.append(creation)
        return self.wait_results.pop(0)

    def get_process_exit_code(self, creation):
        self._maybe_fail("get_process_exit_code")
        self.lifecycle_creations.append(creation)
        return self.exit_code


def make_request(**overrides) -> AppContainerRequest:
    params = dict(
        argv=["C:\\tools\\claude.exe", "--flag", "value with space"],
        repo_id="repo_57de971f",
        worker_kind="claude_cli",
        stdout_handle=101,
        stderr_handle=102,
    )
    params.update(overrides)
    return AppContainerRequest(**params)


def assert_no_leak(fake: FakeWin32Api) -> None:
    """Assert every allocated resource was released and no child escaped."""
    if fake.identity is not None:
        assert fake.identity_freed, "SID/profile memory not freed"
    if fake.security is not None:
        assert fake.security_freed, "capability SIDs not freed"
    if fake.job is not None:
        assert fake.job_closed, "job handle not closed"
    if fake.attrs is not None:
        assert fake.attr_deleted, "attribute list not deleted"
    if fake.creation is not None:
        assert fake.process_terminated, "child left outside the job"
        assert fake.process_handle_closed, "process handle not closed"
        assert fake.thread_handle_closed, "thread handle not closed"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_launch_success_owns_child_before_return():
    fake = FakeWin32Api()
    launch = launch_appcontainer(make_request(), api=fake)

    assert launch.pid == 4321
    assert launch.process_id == 4321
    assert launch.thread_id == 8765
    assert launch.container_name.startswith("aiworkhub.claude-cli.")
    assert launch.container_sid.startswith("S-1-15-2-")
    assert launch.creation_identity
    assert launch.command_line == build_command_line(make_request().argv)

    # Kill-on-close job owns the child before the thread is ever resumed.
    assert fake.job_configured
    assert fake.assigned and fake.resumed
    assert fake.events.index("assign_process_to_job") < fake.events.index(
        "resume_thread"
    )

    # Transient resources freed; owned job/process kept alive.
    assert fake.identity_freed
    assert fake.security_freed
    assert fake.attr_deleted
    assert fake.thread_handle_closed
    assert not fake.job_closed
    assert not fake.job_terminated
    assert not fake.process_terminated
    assert not fake.process_handle_closed


def test_creation_identity_is_deterministic_per_repo_and_kind():
    a = launch_appcontainer(make_request(), api=FakeWin32Api())
    b = launch_appcontainer(make_request(), api=FakeWin32Api())
    assert a.container_name == b.container_name
    assert a.creation_identity == b.creation_identity

    other = launch_appcontainer(
        make_request(worker_kind="grok_kilo_cli"), api=FakeWin32Api()
    )
    assert other.container_name != a.container_name


def test_handle_inheritance_and_creation_flags():
    fake = FakeWin32Api()
    launch_appcontainer(make_request(create_no_window=True), api=fake)

    assert fake.inherited_handles == [101, 102]
    spec = fake.spec
    assert spec is not None
    assert spec.inherit_handles is True
    assert spec.std_output == 101 and spec.std_error == 102
    assert spec.executable == "C:\\tools\\claude.exe"
    assert spec.creation_flags & wac.EXTENDED_STARTUPINFO_PRESENT
    assert spec.creation_flags & wac.CREATE_SUSPENDED
    assert spec.creation_flags & wac.CREATE_NO_WINDOW


def test_no_std_handles_skips_handle_list_and_inheritance():
    fake = FakeWin32Api()
    req = make_request(stdout_handle=None, stderr_handle=None)
    launch_appcontainer(req, api=fake)

    assert fake.inherited_handles is None
    assert fake.attrs is not None and fake.attrs.attribute_count == 1
    assert fake.spec.inherit_handles is False
    assert "set_inherited_handles" not in fake.events


def test_create_no_window_disabled_omits_flag():
    fake = FakeWin32Api()
    launch_appcontainer(make_request(create_no_window=False), api=fake)
    assert not (fake.spec.creation_flags & wac.CREATE_NO_WINDOW)


def test_environment_sets_unicode_environment_flag():
    fake = FakeWin32Api()
    launch_appcontainer(make_request(environment={"A": "B"}), api=fake)
    assert fake.spec.creation_flags & wac.CREATE_UNICODE_ENVIRONMENT


def test_request_local_opencode_config_and_home_reach_the_child_unchanged():
    """The supervisor hands its whole environment to the AppContainer child.

    For an OpenCode worker that environment carries the request HOME and the
    request-local ``awh`` config spelled with real Windows paths; both must
    reach CreateProcess exactly, never a trimmed or re-derived copy.
    """
    home = "C:\\aiworkhub\\worktrees\\R-1\\home"
    key = home + "\\task_mcp_worker_runtime\\audit_hmac.key"
    config = runtime_adapters.build_opencode_worker_mcp_config(
        ["C:\\Python312\\python.exe", "-m", "aiworkhub.worker_ai_tools_mcp"],
        environment={
            "AIWORKHUB_WORKER_MCP_REQUEST_ID": "R-1",
            "AIWORKHUB_WORKER_MCP_AUDIT_HMAC_KEY_PATH": key,
        },
    )
    environment = {
        "HOME": home,
        "USERPROFILE": home,
        runtime_adapters.OPENCODE_WORKER_CONFIG_ENV: (
            runtime_adapters.serialize_opencode_worker_config(config)
        ),
        runtime_adapters.OPENCODE_DISABLE_PROJECT_CONFIG_ENV: "1",
    }
    fake = FakeWin32Api()

    launch_appcontainer(make_request(environment=dict(environment)), api=fake)

    # Every key reaches the child exactly. The one permitted addition is
    # LOCALAPPDATA, which launch_appcontainer supplies when it is absent
    # because AppContainer process creation fails without it (203); it is
    # only added where it resolves, so a non-Windows host adds nothing.
    child_environment = dict(fake.spec.environment)
    assert {k: child_environment[k] for k in environment} == environment
    assert set(child_environment) - set(environment) <= {"LOCALAPPDATA"}
    delivered = json.loads(
        fake.spec.environment[runtime_adapters.OPENCODE_WORKER_CONFIG_ENV]
    )
    server = delivered["mcp"]["awh"]
    assert server["command"][0] == "C:\\Python312\\python.exe"
    assert server["environment"]["AIWORKHUB_WORKER_MCP_AUDIT_HMAC_KEY_PATH"] == key


def test_unicode_and_quoted_argv_preserved_in_command_line():
    fake = FakeWin32Api()
    argv = [
        "C:\\Program Files\\claude.exe",
        "--msg",
        'héllo "wörld"',
        "trailing\\",
    ]
    launch = launch_appcontainer(make_request(argv=argv), api=fake)
    assert launch.command_line == build_command_line(argv)
    assert fake.spec.command_line == build_command_line(argv)
    assert '"C:\\Program Files\\claude.exe"' in fake.spec.command_line


# ---------------------------------------------------------------------------
# Command-line quoting (argv-preserving, no shell)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["a"], "a"),
        (["a", "b"], "a b"),
        (["a b"], '"a b"'),
        (["a\tb"], '"a\tb"'),
        ([""], '""'),
        (['a"b'], '"a\\"b"'),
        (["a\\", "b"], "a\\ b"),
        (["a b\\"], '"a b\\\\"'),
        (["c:\\path with space\\x.exe"], '"c:\\path with space\\x.exe"'),
        (["ünïcödé"], "ünïcödé"),
        (["ünï cödé"], '"ünï cödé"'),
    ],
)
def test_build_command_line_quoting(argv, expected):
    assert build_command_line(argv) == expected


def test_build_command_line_rejects_empty_argv():
    with pytest.raises(ValueError):
        build_command_line([])


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def test_empty_argv_rejected():
    request = AppContainerRequest(
        argv=[], repo_id="repo", worker_kind="claude_cli"
    )
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(request, api=FakeWin32Api())
    assert excinfo.value.reason is AppContainerReason.INVALID_ARGV


def test_missing_repo_id_rejected():
    request = AppContainerRequest(
        argv=["x.exe"], repo_id="", worker_kind="claude_cli"
    )
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(request, api=FakeWin32Api())
    assert excinfo.value.reason is AppContainerReason.INVALID_REQUEST


# ---------------------------------------------------------------------------
# Partial-failure unwind (every step)
# ---------------------------------------------------------------------------


FAILURE_CASES = [
    ("derive_appcontainer_sid", 0, AppContainerReason.CAPABILITY_DERIVATION_FAILED),
    (
        "build_security_capabilities",
        0,
        AppContainerReason.SECURITY_CAPABILITIES_FAILED,
    ),
    ("create_job_object", 0, AppContainerReason.JOB_CREATE_FAILED),
    ("configure_job_object", 0, AppContainerReason.JOB_CONFIGURE_FAILED),
    ("init_attribute_list", 0, AppContainerReason.ATTRIBUTE_LIST_INIT_FAILED),
    (
        "set_security_capabilities",
        0,
        AppContainerReason.ATTRIBUTE_LIST_UPDATE_FAILED,
    ),
    ("set_inherited_handles", 0, AppContainerReason.ATTRIBUTE_LIST_UPDATE_FAILED),
    ("create_process", 0, AppContainerReason.PROCESS_LAUNCH_FAILED),
    ("create_process", 5, AppContainerReason.ACCESS_DENIED),
    ("assign_process_to_job", 0, AppContainerReason.JOB_ASSIGNMENT_FAILED),
    ("resume_thread", 0, AppContainerReason.PROCESS_LAUNCH_FAILED),
]


@pytest.mark.parametrize("operation, win_error, reason", FAILURE_CASES)
def test_failure_unwinds_every_resource(operation, win_error, reason):
    fake = FakeWin32Api(fail_at=operation, fail_error=win_error)
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(make_request(), api=fake)

    error = excinfo.value
    assert error.reason is reason
    assert error.operation == operation
    assert error.win_error == win_error
    assert_no_leak(fake)


def test_access_denied_maps_only_for_create_process():
    # ERROR_ACCESS_DENIED on a non-create step keeps that step's own reason.
    fake = FakeWin32Api(fail_at="create_job_object", fail_error=5)
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(make_request(), api=fake)
    assert excinfo.value.reason is AppContainerReason.JOB_CREATE_FAILED


def test_assignment_failure_terminates_child_and_never_resumes():
    fake = FakeWin32Api(fail_at="assign_process_to_job")
    with pytest.raises(AppContainerError):
        launch_appcontainer(make_request(), api=fake)
    assert fake.process_terminated
    assert not fake.resumed
    assert not fake.assigned


def test_failure_cleanup_runs_each_action_exactly_once():
    fake = FakeWin32Api(fail_at="resume_thread")
    with pytest.raises(AppContainerError):
        launch_appcontainer(make_request(), api=fake)
    assert fake.events.count("terminate_process") == 1
    assert fake.events.count("close_thread_handle") == 1
    assert fake.events.count("free_identity") == 1
    assert fake.events.count("delete_attribute_list") == 1
    assert fake.events.count("close_job") == 1
    assert fake.events.count("free_security_capabilities") == 1
    # The child was assigned to the job, then unwound; never left running.
    assert fake.assigned
    assert fake.process_terminated


# ---------------------------------------------------------------------------
# Cancellation / tree kill / idempotent cleanup
# ---------------------------------------------------------------------------


def test_poll_running_is_nonblocking_and_uses_owned_creation():
    fake = FakeWin32Api()
    launch = launch_appcontainer(make_request(), api=fake)

    result = launch.poll()

    assert result.state is AppContainerLifecycleState.RUNNING
    assert fake.events[-2:] == ["wait_process", "wait_timeout:0"]
    assert fake.lifecycle_creations == [launch.creation]
    assert not fake.job_terminated and not launch.closed


def test_wait_exited_observes_exit_code_on_same_handle_without_cleanup():
    fake = FakeWin32Api()
    fake.wait_results = [True]
    fake.exit_code = 23
    launch = launch_appcontainer(make_request(), api=fake)

    result = launch.wait(250)

    assert result.state is AppContainerLifecycleState.EXITED
    assert result.exit_code == 23
    assert fake.events[-3:] == [
        "wait_process",
        "wait_timeout:250",
        "get_process_exit_code",
    ]
    assert fake.lifecycle_creations == [launch.creation, launch.creation]
    assert not fake.job_terminated and not fake.job_closed


def test_bounded_wait_timeout_can_leave_process_owned_and_running():
    fake = FakeWin32Api()
    launch = launch_appcontainer(make_request(), api=fake)

    result = launch.wait(10)

    assert result.state is AppContainerLifecycleState.TIMEOUT
    assert result.terminated is False
    assert not launch.closed and not fake.job_terminated


def test_wait_timeout_can_kill_job_and_close_every_handle_exactly_once():
    fake = FakeWin32Api()
    fake.wait_results = [False, True]
    fake.exit_code = 9
    launch = launch_appcontainer(make_request(), api=fake)

    result = launch.wait(10, terminate_on_timeout=True, terminate_exit_code=9)

    assert result.state is AppContainerLifecycleState.TIMEOUT
    assert result.terminated is True
    lifecycle = fake.events[-8:]
    assert lifecycle == [
        "wait_process",
        "wait_timeout:10",
        "terminate_job",
        "wait_process",
        f"wait_timeout:{wac._TERMINATION_WAIT_MS}",
        "get_process_exit_code",
        "close_process_handle",
        "close_job",
    ]
    launch.close()
    assert fake.events.count("terminate_job") == 1
    assert fake.events.count("close_process_handle") == 1
    assert fake.events.count("close_job") == 1


def test_zero_timeout_termination_reports_timeout_and_closes_exactly_once():
    fake = FakeWin32Api()
    fake.wait_results = [False, True]
    fake.exit_code = 9
    launch = launch_appcontainer(make_request(), api=fake)

    result = launch.wait(0, terminate_on_timeout=True, terminate_exit_code=9)

    assert result.state is AppContainerLifecycleState.TIMEOUT
    assert result.terminated is True
    assert fake.events[-8:] == [
        "wait_process",
        "wait_timeout:0",
        "terminate_job",
        "wait_process",
        f"wait_timeout:{wac._TERMINATION_WAIT_MS}",
        "get_process_exit_code",
        "close_process_handle",
        "close_job",
    ]
    assert launch.closed is True
    launch.close()
    assert fake.events.count("terminate_job") == 1
    assert fake.events.count("close_process_handle") == 1
    assert fake.events.count("close_job") == 1


def test_wait_and_exit_code_failures_are_structured_and_do_not_orphan():
    wait_fake = FakeWin32Api(fail_at="wait_process", fail_error=6)
    wait_launch = launch_appcontainer(make_request(), api=wait_fake)
    wait_result = wait_launch.poll()
    assert wait_result.state is AppContainerLifecycleState.ERROR
    assert (wait_result.operation, wait_result.win_error) == ("wait_process", 6)
    assert not wait_launch.closed and not wait_fake.job_terminated

    exit_fake = FakeWin32Api(fail_at="get_process_exit_code", fail_error=5)
    exit_fake.wait_results = [True, True]
    exit_launch = launch_appcontainer(make_request(), api=exit_fake)
    exit_result = exit_launch.exit_status()
    assert exit_result.state is AppContainerLifecycleState.ERROR
    assert (exit_result.operation, exit_result.win_error) == (
        "get_process_exit_code",
        5,
    )
    exit_launch.cancel()
    assert exit_fake.job_terminated and exit_fake.job_closed


def test_closed_launch_returns_explicit_closed_state_without_win32_query():
    fake = FakeWin32Api()
    launch = launch_appcontainer(make_request(), api=fake)
    launch.close()
    fake.events.clear()

    assert launch.poll().state is AppContainerLifecycleState.CLOSED
    assert launch.exit_status().state is AppContainerLifecycleState.CLOSED
    assert fake.events == []


@pytest.mark.parametrize("timeout", [-1, 0xFFFFFFFF])
def test_wait_rejects_unbounded_or_negative_timeout(timeout):
    launch = launch_appcontainer(make_request(), api=FakeWin32Api())
    with pytest.raises(ValueError):
        launch.wait(timeout)


def test_terminate_kills_tree_then_releases_and_is_idempotent():
    fake = FakeWin32Api()
    fake.wait_results = [True]
    fake.exit_code = 1
    launch = launch_appcontainer(make_request(), api=fake)

    launch.terminate()
    assert fake.job_terminated
    assert fake.job_closed
    assert fake.process_handle_closed
    assert launch.closed
    assert launch.wait(0).exit_code == 1

    fake.events.clear()
    launch.terminate()
    launch.close()
    assert fake.events == []


def test_terminate_then_wait_uses_authenticated_native_exit_and_no_handles():
    fake = FakeWin32Api()
    fake.wait_results = [True]
    fake.exit_code = 73
    launch = launch_appcontainer(make_request(), api=fake)

    result = launch.terminate(9)

    assert result.state is AppContainerLifecycleState.EXITED
    assert result.exit_code == 73
    assert launch.wait(0) is result
    assert launch.closed is True
    assert fake.events.count("wait_process") == 1
    assert fake.events.count("get_process_exit_code") == 1
    assert fake.events.count("close_process_handle") == 1
    assert fake.events.count("close_job") == 1


def test_close_releases_without_terminating_and_is_idempotent():
    fake = FakeWin32Api()
    launch = launch_appcontainer(make_request(), api=fake)

    launch.close()
    assert fake.job_closed
    assert fake.process_handle_closed
    assert not fake.job_terminated

    fake.events.clear()
    launch.close()
    assert fake.events == []


def test_cleanup_evidence_is_serializable():
    launch = launch_appcontainer(make_request(), api=FakeWin32Api())
    evidence = launch.cleanup_evidence()
    assert evidence["pid"] == 4321
    assert evidence["container_name"].startswith("aiworkhub.claude-cli.")
    assert evidence["closed"] is False


# ---------------------------------------------------------------------------
# Platform gating and probing (side-effect free on non-Windows)
# ---------------------------------------------------------------------------


def test_platform_supported_false_off_windows(monkeypatch):
    monkeypatch.setattr(wac.os, "name", "posix")
    assert wac.platform_supported() is False


def test_probe_reports_platform_unsupported_off_windows(monkeypatch):
    monkeypatch.setattr(wac.os, "name", "posix")
    result = wac.probe()
    assert result.available is False
    assert result.reason is AppContainerReason.PLATFORM_UNSUPPORTED


def test_launch_without_api_off_windows_raises_platform_unsupported(monkeypatch):
    monkeypatch.setattr(wac.os, "name", "posix")
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(make_request(), api=None)
    assert excinfo.value.reason is AppContainerReason.PLATFORM_UNSUPPORTED


def test_probe_maps_loader_failure_to_capability_reason(monkeypatch):
    monkeypatch.setattr(wac.os, "name", "nt")

    def boom():
        raise _Win32Failure(1, "derive_appcontainer_sid", "unavailable")

    result = wac.probe(api_loader=boom)
    assert result.available is False
    assert result.reason is AppContainerReason.CAPABILITY_DERIVATION_FAILED


def test_probe_reports_available_when_loader_succeeds(monkeypatch):
    monkeypatch.setattr(wac.os, "name", "nt")
    result = wac.probe(api_loader=lambda: FakeWin32Api())
    assert result.available is True
    assert result.reason is None


# ---------------------------------------------------------------------------
# Capability-SID ownership: the real ctypes freeing logic (mocked kernel32)
#
# These drive :class:`_CtypesWin32Api` directly (constructed without loading
# any real DLL) so we can prove the DeriveCapabilitySidsFromName allocations
# are freed exactly once on success, partial failure and idempotent close,
# with no leak and no double-free.
# ---------------------------------------------------------------------------

CT = wac.ctypes
WT = wac.wintypes


class FakeKernel32:
    """kernel32 stand-in that models DeriveCapabilitySidsFromName allocations.

    It writes real ctypes ``LPVOID`` arrays (kept alive here) into the caller's
    out-pointers, filling them with distinct fake SID addresses, so every
    ``LocalFree`` can be balanced against the allocation that produced it.
    """

    def __init__(self, group_n=2, cap_n=3, ok=True):
        self.group_n = group_n
        self.cap_n = cap_n
        self.ok = ok
        self._next = 0x1000
        self.allocated: list[int] = []
        self.array_addrs: list[int] = []
        self.freed: list[int] = []
        self._keepalive: list = []

    def _alloc_sid(self) -> int:
        addr = self._next
        self._next += 0x100
        self.allocated.append(addr)
        return addr

    def _make_array(self, count):
        arr = (WT.LPVOID * count)(*[self._alloc_sid() for _ in range(count)])
        self._keepalive.append(arr)
        self.array_addrs.append(CT.addressof(arr))
        return arr

    @staticmethod
    def _write_out_ptr(ref, array):
        dst = ref._obj
        src = CT.cast(array, CT.POINTER(WT.LPVOID))
        CT.memmove(CT.byref(dst), CT.byref(src), CT.sizeof(CT.c_void_p))

    def DeriveCapabilitySidsFromName(
        self, name, group_ref, group_count_ref, cap_ref, cap_count_ref
    ):
        if not self.ok:
            return 0
        grp = self._make_array(self.group_n)
        cap = self._make_array(self.cap_n)
        self._write_out_ptr(group_ref, grp)
        self._write_out_ptr(cap_ref, cap)
        group_count_ref._obj.value = self.group_n
        cap_count_ref._obj.value = self.cap_n
        return 1

    def LocalFree(self, ptr):
        if isinstance(ptr, CT.c_void_p):
            addr = ptr.value or 0
        elif ptr:
            addr = int(ptr)
        else:
            addr = 0
        self.freed.append(addr)
        return None


def make_ctypes_api(kernel32):
    api = wac._CtypesWin32Api.__new__(wac._CtypesWin32Api)
    api._kernel32 = kernel32
    # DeriveCapabilitySidsFromName is a security-base export that kernel32 does
    # not forward, so production resolves it separately. The fake publishes the
    # function itself, so both names bind to the same recording library and
    # these SID-lifetime assertions keep measuring exactly what they did.
    api._security_base = kernel32
    api._security_base_library = "kernelbase"
    api._userenv = None
    api._advapi32 = None
    return api


def test_derive_capability_sid_frees_group_and_nonretained_sids():
    k = FakeKernel32(group_n=2, cap_n=3)
    api = make_ctypes_api(k)

    retained = api._derive_capability_sid("aiworkhub.cap")

    # Retained SID is the first capability slot (right after the group SIDs).
    assert retained == k.allocated[k.group_n]
    # Every SID except the retained one is LocalFree'd exactly once; the
    # retained one is kept alive for free_security_capabilities.
    for sid in k.allocated:
        assert k.freed.count(sid) == (0 if sid == retained else 1)
    # Both OS-allocated arrays are freed exactly once.
    for addr in k.array_addrs:
        assert k.freed.count(addr) == 1


def test_build_and_free_security_capabilities_balances_every_sid():
    k = FakeKernel32(group_n=1, cap_n=2)
    api = make_ctypes_api(k)
    identity = _Identity("name", "disp", "S-1-15-2-1", 0xABCD, True)

    sec = api.build_security_capabilities(identity, ["capA", "capB"])
    retained = list(sec.native.capability_sids)
    assert len(retained) == 2
    for sid in retained:
        assert k.freed.count(sid) == 0

    api.free_security_capabilities(sec)
    for sid in retained:
        assert k.freed.count(sid) == 1

    # Idempotent: a second free neither raises nor double-frees.
    before = list(k.freed)
    api.free_security_capabilities(sec)
    assert k.freed == before
    assert sec.native.capability_sids == []


def test_free_security_capabilities_without_caps_is_a_noop():
    k = FakeKernel32()
    api = make_ctypes_api(k)
    identity = _Identity("name", "disp", "S-1-15-2-1", 0xABCD, True)

    sec = api.build_security_capabilities(identity, [])
    api.free_security_capabilities(sec)

    assert k.freed == []
    assert sec.native.capability_sids == []


def test_build_security_capabilities_partial_failure_frees_retained(monkeypatch):
    monkeypatch.setattr(wac.ctypes, "get_last_error", lambda: 1337, raising=False)
    k = FakeKernel32(group_n=1, cap_n=2)
    real_derive = k.DeriveCapabilitySidsFromName
    calls = {"n": 0}

    def flaky(*args):
        calls["n"] += 1
        if calls["n"] == 2:
            return 0  # the second capability derivation fails
        return real_derive(*args)

    k.DeriveCapabilitySidsFromName = flaky
    api = make_ctypes_api(k)
    identity = _Identity("name", "disp", "S-1-15-2-1", 0xABCD, True)

    with pytest.raises(_Win32Failure):
        api.build_security_capabilities(identity, ["capA", "capB"])

    # capA succeeded and retained its SID; the partial failure must free it
    # exactly once so a partially-built SECURITY_CAPABILITIES never leaks.
    retained_first = k.allocated[k.group_n]
    assert k.freed.count(retained_first) == 1


def test_derive_capability_sid_failure_frees_partial_group_array(monkeypatch):
    monkeypatch.setattr(wac.ctypes, "get_last_error", lambda: 5, raising=False)
    # ok=True but zero capability SIDs -> cap_count < 1 failure after the group
    # array has already been allocated; that array and its SIDs must be freed.
    k = FakeKernel32(group_n=2, cap_n=0)
    api = make_ctypes_api(k)

    with pytest.raises(_Win32Failure):
        api._derive_capability_sid("aiworkhub.cap")

    for sid in k.allocated:  # the two group SIDs
        assert k.freed.count(sid) == 1
    for addr in k.array_addrs:  # both arrays
        assert k.freed.count(addr) == 1


# ---------------------------------------------------------------------------
# Rework regression: std-handle inheritance goes through the signature-
# configured SetHandleInformation, its BOOL result is checked, and a false
# return fails closed *before* CreateProcessW (no child, no leaked handle).
# A > 32-bit handle must be passed intact (HANDLE is pointer-width).
#
# These drive the real :class:`_CtypesWin32Api` create_process / _mark_inheritable
# with a mocked kernel32 (no DLL is ever loaded).
# ---------------------------------------------------------------------------


class _RecordingFn:
    """Function-pointer stand-in that records ``restype`` / ``argtypes``."""

    def __init__(self) -> None:
        self.restype = None
        self.argtypes = None


class _RecordingLib:
    """DLL stand-in that hands out and remembers recording function pointers."""

    def __getattr__(self, name):
        fn = self.__dict__.get(name)
        if fn is None:
            fn = _RecordingFn()
            self.__dict__[name] = fn
        return fn


def test_set_handle_information_signature_is_handle_width_safe():
    api = wac._CtypesWin32Api.__new__(wac._CtypesWin32Api)
    api._kernel32 = _RecordingLib()
    api._security_base = _RecordingLib()
    api._userenv = _RecordingLib()
    api._advapi32 = _RecordingLib()

    api._configure_signatures()

    fn = api._kernel32.SetHandleInformation
    assert fn.restype is WT.BOOL
    # HANDLE (== c_void_p) is pointer-width, so a > 32-bit handle is passed
    # intact rather than truncated to a 32-bit DWORD.
    assert fn.argtypes == [WT.HANDLE, WT.DWORD, WT.DWORD]
    assert WT.HANDLE is CT.c_void_p


class FakeCreateKernel32:
    """kernel32 stand-in for create_process / _mark_inheritable regressions."""

    def __init__(self, set_handle_result=1, create_process_result=1):
        self.set_handle_result = set_handle_result
        self.create_process_result = create_process_result
        self.set_handle_calls: list = []
        self.create_process_called = False

    def SetHandleInformation(self, handle, mask, flags):
        self.set_handle_calls.append((handle, mask, flags))
        return self.set_handle_result

    def CreateProcessW(self, *args):
        self.create_process_called = True
        return self.create_process_result


def _make_process_spec(**overrides):
    params = dict(
        executable="C:\\tools\\claude.exe",
        command_line='"C:\\tools\\claude.exe" --flag',
        working_directory=None,
        environment=None,
        attribute_list=_AttributeList(0, [], 1),
        std_input=None,
        std_output=101,
        std_error=102,
        creation_flags=0,
        inherit_handles=True,
    )
    params.update(overrides)
    return wac._ProcessSpec(**params)


def test_mark_inheritable_false_return_fails_closed_before_create_process(
    monkeypatch,
):
    monkeypatch.setattr(wac.ctypes, "get_last_error", lambda: 5, raising=False)
    k = FakeCreateKernel32(set_handle_result=0)
    api = make_ctypes_api(k)

    with pytest.raises(_Win32Failure) as excinfo:
        api.create_process(_make_process_spec())

    # A false SetHandleInformation return is raised through the existing
    # taxonomy (operation "create_process" -> PROCESS_LAUNCH_FAILED) carrying
    # the real GetLastError value.
    assert excinfo.value.operation == "create_process"
    assert excinfo.value.win_error == 5
    # Fail closed: CreateProcessW is never reached, so no child is launched and
    # no process/thread handle can leak.
    assert k.create_process_called is False
    assert k.set_handle_calls  # the failing handle was actually attempted


def test_mark_inheritable_passes_large_handle_intact(monkeypatch):
    monkeypatch.setattr(wac.ctypes, "get_last_error", lambda: 0, raising=False)
    big_handle = 0x1_0000_0001  # > 32 bits: must survive intact, untruncated
    k = FakeCreateKernel32(set_handle_result=1, create_process_result=0)
    api = make_ctypes_api(k)

    with pytest.raises(_Win32Failure) as excinfo:
        api.create_process(
            _make_process_spec(std_output=big_handle, std_error=None)
        )

    # SetHandleInformation received the full 64-bit handle, not a value
    # truncated to its low 32 bits.
    assert k.set_handle_calls == [
        (big_handle, wac._HANDLE_FLAG_INHERIT, wac._HANDLE_FLAG_INHERIT)
    ]
    assert k.set_handle_calls[0][0] != (big_handle & 0xFFFFFFFF)
    # Marking succeeded, so CreateProcessW ran; its failure maps to the launch
    # reason, proving marking preceded (did not replace) the launch call.
    assert k.create_process_called is True
    assert excinfo.value.operation == "create_process"


def test_mark_inheritable_marks_every_std_handle_with_inherit_flag():
    k = FakeCreateKernel32(set_handle_result=1, create_process_result=0)
    api = make_ctypes_api(k)

    with pytest.raises(_Win32Failure):
        api.create_process(
            _make_process_spec(std_input=100, std_output=101, std_error=102)
        )

    assert [call[0] for call in k.set_handle_calls] == [100, 101, 102]
    for call in k.set_handle_calls:
        assert call[1] == wac._HANDLE_FLAG_INHERIT
        assert call[2] == wac._HANDLE_FLAG_INHERIT


# ---------------------------------------------------------------------------
# Rework regression: environment blocks must fail closed on embedded NUL
#
# An embedded NUL in a key or value would either truncate the child's
# environment block or splice an attacker-chosen NAME=VALUE pair into it.  The
# request must be rejected *before* any ctypes call, so nothing malicious can
# reach CreateProcessW.
# ---------------------------------------------------------------------------


def test_environment_embedded_nul_in_value_fails_closed_before_launch():
    fake = FakeWin32Api()
    req = make_request(environment={"A": "B\x00INJECTED=evil"})
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(req, api=fake)

    assert excinfo.value.reason is AppContainerReason.INVALID_ENVIRONMENT
    # Nothing reached the Win32 boundary: no CreateProcessW, no allocations.
    assert fake.spec is None
    assert fake.creation is None
    assert fake.events == []


def test_environment_embedded_nul_in_key_fails_closed_before_launch():
    fake = FakeWin32Api()
    req = make_request(environment={"A\x00B": "value"})
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(req, api=fake)
    assert excinfo.value.reason is AppContainerReason.INVALID_ENVIRONMENT
    assert fake.events == []


def test_environment_equals_in_key_fails_closed_before_launch():
    fake = FakeWin32Api()
    req = make_request(environment={"A=B": "value"})
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(req, api=fake)
    assert excinfo.value.reason is AppContainerReason.INVALID_ENVIRONMENT
    assert fake.events == []


def test_environment_empty_key_fails_closed_before_launch():
    fake = FakeWin32Api()
    req = make_request(environment={"": "value"})
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(req, api=fake)
    assert excinfo.value.reason is AppContainerReason.INVALID_ENVIRONMENT
    assert fake.events == []


def test_environment_block_rejects_embedded_nul_in_value():
    with pytest.raises(ValueError):
        wac._environment_block({"A": "B\x00C"})


def test_environment_block_rejects_embedded_nul_in_key():
    with pytest.raises(ValueError):
        wac._environment_block({"A\x00B": "C"})


def test_environment_block_builds_full_block_without_truncation():
    # A valid multi-variable environment must produce the whole double-NUL
    # terminated block with no truncation.
    env = {"ALPHA": "one", "BETA": "two"}
    buffer = wac._environment_block(env)
    expected = "ALPHA=one\x00BETA=two\x00\x00"
    # create_unicode_buffer is one wchar longer than the source string.
    assert len(buffer) == len(expected) + 1
    assert buffer[: len(expected)] == expected


def test_environment_block_none_is_none():
    assert wac._environment_block(None) is None


# ---------------------------------------------------------------------------
# Rework regression: argv elements must fail closed on embedded NUL.
#
# create_unicode_buffer stops at the first NUL, so an embedded NUL in any argv
# element would silently truncate the child's command line and drop every
# following argument.  The request must be rejected *before* any ctypes call,
# so nothing truncated can reach CreateProcessW.  These cover argv[0], a middle
# argument and the final argument, and prove no Win32 boundary call is made.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["C:\\tools\\claude\x00.exe", "--flag", "value"],
        ["C:\\tools\\claude.exe", "--fl\x00ag", "value"],
        ["C:\\tools\\claude.exe", "--flag", "val\x00ue"],
        ["C:\\tools\\claude.exe", "--flag", "trailing\x00"],
    ],
)
def test_argv_embedded_nul_fails_closed_before_launch(argv):
    fake = FakeWin32Api()
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(make_request(argv=argv), api=fake)

    assert excinfo.value.reason is AppContainerReason.INVALID_ARGV
    # Nothing reached the Win32 boundary: no CreateProcessW, no allocations.
    assert fake.spec is None
    assert fake.creation is None
    assert fake.events == []


def test_argv_nul_in_first_element_reports_invalid_argv():
    fake = FakeWin32Api()
    req = make_request(argv=["\x00", "--flag"])
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(req, api=fake)
    assert excinfo.value.reason is AppContainerReason.INVALID_ARGV
    assert fake.events == []


def test_build_command_line_rejects_embedded_nul_in_argv():
    with pytest.raises(ValueError):
        build_command_line(["a.exe", "b\x00c"])
    with pytest.raises(ValueError):
        build_command_line(["a\x00.exe"])


def test_build_command_line_preserves_quoting_without_nul():
    # The NUL guard must not disturb existing argv-preserving quoting.  A bare
    # trailing backslash has no whitespace/quote, so it stays unquoted; the
    # space- and quote-bearing arguments keep their MSVCRT quoting.
    argv = ["C:\\Program Files\\x.exe", "a b", 'q"q', "trailing\\"]
    assert build_command_line(argv) == (
        '"C:\\Program Files\\x.exe" "a b" "q\\"q" trailing\\'
    )


# ---------------------------------------------------------------------------
# Rework regression: probe() maps a missing Win32 export (AttributeError) into
# the structured taxonomy instead of letting the raw AttributeError escape.
# ---------------------------------------------------------------------------


def test_probe_maps_missing_export_attributeerror_to_taxonomy(monkeypatch):
    monkeypatch.setattr(wac.os, "name", "nt")

    def missing_export():
        # ctypes raises AttributeError when resolving an absent function pointer.
        raise AttributeError(
            "function 'CreateAppContainerProfile' not found"
        )

    result = wac.probe(api_loader=missing_export)
    assert result.available is False
    assert result.reason is AppContainerReason.CAPABILITY_DERIVATION_FAILED
    assert "CreateAppContainerProfile" in result.detail


# ---------------------------------------------------------------------------
# Rework regression: the AppContainer moniker stays <= 64 chars for an
# arbitrarily long worker_kind while remaining deterministic and unique.
# ---------------------------------------------------------------------------


def test_derive_container_identity_bounds_long_worker_kind_to_64():
    long_kind = "grok_kilo_" + "x" * 400
    name, display_name, description = wac.derive_container_identity(
        "repo_57de971f", long_kind
    )
    assert len(name) <= 64
    assert name.startswith("aiworkhub.")
    assert not name.endswith("-")
    # display_name / description are unbounded and keep the full kind.
    assert "worker" in display_name and "repo_57de971f" in description


def test_long_worker_kinds_sharing_prefix_stay_unique_and_stable():
    prefix = "claude_cli_" + "a" * 200
    kind_one = prefix + "_one"
    kind_two = prefix + "_two"

    name_one, _, _ = wac.derive_container_identity("repo_x", kind_one)
    name_two, _, _ = wac.derive_container_identity("repo_x", kind_two)

    # Labels collide after truncation, but the full-identity digest keeps the
    # monikers distinct...
    assert name_one != name_two
    assert len(name_one) <= 64 and len(name_two) <= 64
    # ...and each derivation is deterministic.
    assert wac.derive_container_identity("repo_x", kind_one)[0] == name_one


def test_derive_container_identity_is_deterministic_and_repo_scoped():
    a = wac.derive_container_identity("repo_a", "claude_cli")
    b = wac.derive_container_identity("repo_a", "claude_cli")
    c = wac.derive_container_identity("repo_b", "claude_cli")
    assert a == b
    assert a[0] != c[0]


def test_launch_with_long_worker_kind_bounds_container_name():
    fake = FakeWin32Api()
    launch = launch_appcontainer(
        make_request(worker_kind="grok_kilo_" + "z" * 300), api=fake
    )
    assert len(launch.container_name) <= 64
    assert launch.container_name.startswith("aiworkhub.")


# ---------------------------------------------------------------------------
# Rework regression: lifecycle ownership survives partial cleanup failures.
# ---------------------------------------------------------------------------


def test_close_process_failure_still_closes_job_and_retry_closes_only_process():
    fake = FakeWin32Api(fail_at="close_process_handle", fail_error=6)
    launch = launch_appcontainer(make_request(), api=fake)

    with pytest.raises(_Win32Failure) as excinfo:
        launch.close()

    assert excinfo.value.operation == "close_process_handle"
    assert fake.events[-2:] == ["close_process_handle", "close_job"]
    assert fake.job_closed and not fake.process_handle_closed
    assert launch.closed is False

    fake.fail_at = None
    launch.close()
    assert fake.events[-1] == "close_process_handle"
    assert fake.events.count("close_job") == 1
    assert fake.events.count("close_process_handle") == 2
    assert launch.closed is True


def test_close_job_failure_retries_only_job_and_process_observation_is_closed():
    fake = FakeWin32Api(fail_at="close_job", fail_error=6)
    launch = launch_appcontainer(make_request(), api=fake)

    with pytest.raises(_Win32Failure) as excinfo:
        launch.close()

    assert excinfo.value.operation == "close_job"
    assert fake.process_handle_closed and not fake.job_closed
    assert launch.closed is False
    assert launch.poll().state is AppContainerLifecycleState.CLOSED
    assert fake.events.count("wait_process") == 0

    fake.fail_at = None
    launch.close()
    assert fake.events[-1] == "close_job"
    assert fake.events.count("close_process_handle") == 1
    assert fake.events.count("close_job") == 2
    assert launch.closed is True


def test_close_rethrows_first_failure_after_attempting_every_owned_handle():
    class FailBothCloseApi(FakeWin32Api):
        def close_process_handle(self, creation):
            self.events.append("close_process_handle")
            raise _Win32Failure(6, "close_process_handle")

        def close_job(self, job):
            self.events.append("close_job")
            raise _Win32Failure(5, "close_job")

    fake = FailBothCloseApi()
    launch = launch_appcontainer(make_request(), api=fake)

    with pytest.raises(_Win32Failure) as excinfo:
        launch.close()

    assert excinfo.value.operation == "close_process_handle"
    assert fake.events[-2:] == ["close_process_handle", "close_job"]
    assert launch.closed is False


def test_terminate_failure_still_cleans_handles_without_orphan():
    fake = FakeWin32Api(fail_at="terminate_job", fail_error=5)
    launch = launch_appcontainer(make_request(), api=fake)

    with pytest.raises(_Win32Failure) as excinfo:
        launch.terminate(9)

    assert excinfo.value.operation == "terminate_job"
    assert fake.events[-3:] == [
        "terminate_job",
        "close_process_handle",
        "close_job",
    ]
    assert fake.process_handle_closed and fake.job_closed
    assert launch.closed is True
    launch.close()
    assert fake.events.count("close_process_handle") == 1
    assert fake.events.count("close_job") == 1


def test_terminate_and_job_close_failures_retry_only_live_job():
    class FailTerminateAndFirstJobCloseApi(FakeWin32Api):
        def __init__(self):
            super().__init__()
            self.job_close_attempts = 0

        def terminate_job(self, job, exit_code=1):
            self.events.append("terminate_job")
            raise _Win32Failure(5, "terminate_job")

        def close_job(self, job):
            self.events.append("close_job")
            self.job_close_attempts += 1
            if self.job_close_attempts == 1:
                raise _Win32Failure(6, "close_job")
            self.job_closed = True

    fake = FailTerminateAndFirstJobCloseApi()
    launch = launch_appcontainer(make_request(), api=fake)

    with pytest.raises(_Win32Failure) as excinfo:
        launch.terminate()
    assert excinfo.value.operation == "terminate_job"
    assert fake.events[-3:] == [
        "terminate_job",
        "close_process_handle",
        "close_job",
    ]
    assert launch.closed is False

    with pytest.raises(_Win32Failure) as retry_exc:
        launch.terminate()
    assert retry_exc.value.operation == "terminate_job"
    assert fake.events[-2:] == ["terminate_job", "close_job"]
    assert fake.events.count("close_process_handle") == 1
    assert launch.closed is True


class FakeTerminateFailureKernel32:
    """Recording ctypes boundary where native job termination fails."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def WaitForSingleObject(self, handle, timeout_ms):
        self.calls.append(("wait", handle, timeout_ms))
        return wac._WAIT_TIMEOUT

    def TerminateJobObject(self, job, exit_code):
        self.calls.append(("terminate", job, exit_code))
        return 0

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return 1


def _ctypes_launch_with_failed_termination(kernel):
    creation = _ProcessCreation(
        process_id=41,
        thread_id=42,
        process_handle=0xCAFE,
        thread_handle=None,
    )
    return wac.AppContainerLaunch(
        pid=41,
        process_id=41,
        thread_id=42,
        container_name="container",
        container_sid="sid",
        creation_identity="41:51966",
        command_line="worker.exe",
        api=make_ctypes_api(kernel),
        job=0xBEEF,
        creation=creation,
    )


@pytest.mark.parametrize("operation", ["terminate", "cancel", "timeout"])
def test_ctypes_terminate_job_false_result_never_reports_termination(
    monkeypatch, operation
):
    monkeypatch.setattr(wac.ctypes, "get_last_error", lambda: 5, raising=False)
    kernel = FakeTerminateFailureKernel32()
    launch = _ctypes_launch_with_failed_termination(kernel)

    with pytest.raises(_Win32Failure) as excinfo:
        if operation == "terminate":
            launch.terminate(9)
        elif operation == "cancel":
            launch.cancel(9)
        else:
            result = launch.wait(25, terminate_on_timeout=True, terminate_exit_code=9)
            pytest.fail(f"timeout falsely returned termination result: {result}")

    assert excinfo.value.operation == "terminate_job"
    assert excinfo.value.win_error == 5
    expected = []
    if operation == "timeout":
        expected.append(("wait", 0xCAFE, 25))
    expected.extend(
        [
            ("terminate", 0xBEEF, 9),
            ("close", 0xCAFE),
            ("close", 0xBEEF),
        ]
    )
    assert kernel.calls == expected
    assert launch.closed is True
    assert launch._termination_completed is False

    launch.close()
    assert kernel.calls == expected


def test_zero_timeout_native_termination_failure_propagates_after_cleanup(
    monkeypatch,
):
    monkeypatch.setattr(wac.ctypes, "get_last_error", lambda: 5, raising=False)
    kernel = FakeTerminateFailureKernel32()
    launch = _ctypes_launch_with_failed_termination(kernel)

    with pytest.raises(_Win32Failure) as excinfo:
        launch.wait(0, terminate_on_timeout=True, terminate_exit_code=9)

    assert (excinfo.value.operation, excinfo.value.win_error) == (
        "terminate_job",
        5,
    )
    assert kernel.calls == [
        ("wait", 0xCAFE, 0),
        ("terminate", 0xBEEF, 9),
        ("close", 0xCAFE),
        ("close", 0xBEEF),
    ]
    assert launch.closed is True
    assert launch._termination_completed is False
    launch.close()
    assert len(kernel.calls) == 4


# ---------------------------------------------------------------------------
# Read-only native filesystem ACL snapshots
# ---------------------------------------------------------------------------


class FakeAclSnapshotApi:
    def __init__(self, *, present=True, dacl=0x1_0000_2000, fail=None):
        self.descriptor = 0x1_0000_1000
        self.present = present
        self.dacl = dacl
        self.fail = fail
        self.freed = []
        self.live = False
        self.aces = []
        self.revision = 2
        self.sbz1 = 0
        self.sbz2 = 0
        self.raw_override = None

    def get_named_security_info(self, path):
        if self.fail == "get_named_security_info":
            raise wac.AclSnapshotError("get_named_security_info", 5)
        self.live = True
        return self.descriptor

    def get_security_descriptor_dacl(self, descriptor):
        assert descriptor == self.descriptor and self.live
        if self.fail == "get_security_descriptor_dacl":
            raise wac.AclSnapshotError("get_security_descriptor_dacl", 87)
        return self.present, self.dacl, False

    def acl_information(self, dacl):
        assert dacl == self.dacl and self.live
        if self.fail == "acl_information":
            raise wac.AclSnapshotError("acl_information", 87)
        return 8 + sum(len(data) for _, data in self.aces), len(self.aces)

    def acl_bytes(self, dacl, size):
        assert dacl == self.dacl and self.live
        raw = self.raw_override
        if raw is None:
            body = b"".join(data for _, data in self.aces)
            total = 8 + len(body)
            raw = bytes((self.revision, self.sbz1)) + total.to_bytes(2, "little") + len(self.aces).to_bytes(2, "little") + self.sbz2.to_bytes(2, "little") + body
        return raw[:size]

    def get_ace(self, dacl, index, acl_end):
        assert dacl == self.dacl and self.live
        assert acl_end == dacl + 8 + sum(len(data) for _, data in self.aces)
        if self.fail == "get_ace":
            raise wac.AclSnapshotError("get_ace", 87)
        address, data = self.aces[index]
        return address, data

    def sid_bytes(self, address, ace_end):
        assert self.live
        if self.fail == "sid":
            raise wac.AclSnapshotError("invalid_sid")
        for ace_address, data in self.aces:
            if ace_address <= address < ace_address + len(data):
                sid = data[address - ace_address :]
                if address + len(sid) > ace_end:
                    raise wac.AclSnapshotError("sid_out_of_range")
                return sid
        raise wac.AclSnapshotError("invalid_sid")

    def local_free(self, descriptor):
        assert descriptor == self.descriptor
        self.freed.append(descriptor)
        self.live = False
        if self.fail == "local_free":
            raise wac.AclSnapshotError("local_free", 6)


def _simple_ace(ace_type=0, flags=3, mask=0x120089, sid=b"\x01\x01\0\0\0\0\0\x05\x20\0\0\0"):
    size = 8 + len(sid)
    return bytes((ace_type, flags)) + size.to_bytes(2, "little") + mask.to_bytes(4, "little") + sid


def test_acl_snapshot_distinguishes_absent_null_and_empty_dacl():
    absent = wac.snapshot_filesystem_acl("C:\\safe", api=FakeAclSnapshotApi(present=False, dacl=0))
    null = wac.snapshot_filesystem_acl("C:\\safe", api=FakeAclSnapshotApi(present=True, dacl=0))
    empty = wac.snapshot_filesystem_acl("C:\\safe", api=FakeAclSnapshotApi())
    assert (absent.dacl_state, null.dacl_state, empty.dacl_state) == (
        wac.DaclState.ABSENT, wac.DaclState.NULL, wac.DaclState.PRESENT
    )
    assert absent.aces == null.aces == empty.aces == ()
    assert absent.raw_acl is null.raw_acl is None
    assert empty.raw_acl == b"\x02\0\x08\0\0\0\0\0"


def test_acl_snapshot_copies_exact_simple_ace_sid_and_large_pointers():
    api = FakeAclSnapshotApi()
    raw = _simple_ace()
    api.aces = [(api.dacl + 8, raw)]
    result = wac.snapshot_filesystem_acl("C:\\safe", api=api)
    assert result.aces[0].raw == raw
    assert result.aces[0].sid == raw[8:]
    assert result.aces[0].mask == 0x120089
    assert result.raw_acl == b"\x02\0" + (8 + len(raw)).to_bytes(2, "little") + b"\x01\0\0\0" + raw
    assert api.freed == [0x1_0000_1000]
    assert not api.live


def test_acl_snapshot_object_ace_preserves_guid_metadata():
    sid = _simple_ace()[8:]
    guid = bytes(range(16))
    size = 12 + len(guid) + len(sid)
    raw = bytes((5, 1)) + size.to_bytes(2, "little") + (1).to_bytes(4, "little") + (1).to_bytes(4, "little") + guid + sid
    api = FakeAclSnapshotApi()
    api.revision, api.sbz1, api.sbz2 = 4, 0xA5, 0xBEEF
    api.aces = [(api.dacl + 8, raw)]
    snapshot = wac.snapshot_filesystem_acl("C:\\safe", api=api)
    ace = snapshot.aces[0]
    assert ace.object_flags == 1 and ace.object_type == guid and ace.inherited_object_type is None
    assert ace.sid == sid and ace.raw == raw
    assert snapshot.raw_acl is not None
    assert snapshot.raw_acl[:2] == b"\x04\xa5"


@pytest.mark.parametrize("raw", [b"", b"\x00\0\x03\0", b"\x00\0\xff\xff" + b"x" * 4, bytes((99, 0, 8, 0)) + b"x" * 4])
def test_acl_snapshot_malformed_or_unsupported_ace_fails_closed(raw):
    api = FakeAclSnapshotApi()
    api.aces = [(api.dacl + 8, raw)]
    with pytest.raises(wac.AclSnapshotError):
        wac.snapshot_filesystem_acl("C:\\safe", api=api)
    assert api.freed == [api.descriptor]


@pytest.mark.parametrize("failure", ["get_security_descriptor_dacl", "acl_information", "get_ace", "sid"])
def test_acl_snapshot_every_borrowed_stage_failure_frees_descriptor_once(failure):
    api = FakeAclSnapshotApi(fail=failure)
    api.aces = [(api.dacl + 8, _simple_ace())]
    with pytest.raises(wac.AclSnapshotError):
        wac.snapshot_filesystem_acl("C:\\safe", api=api)
    assert api.freed == [api.descriptor] and not api.live


def test_acl_snapshot_cleanup_failure_is_observable_with_primary_failure():
    api = FakeAclSnapshotApi(fail="local_free")
    api.aces = [(api.dacl + 8, bytes((99, 0, 8, 0)) + b"xxxx")]
    with pytest.raises(wac.AclSnapshotError) as exc:
        wac.snapshot_filesystem_acl("C:\\safe", api=api)
    assert exc.value.operation == "unsupported_ace_type"
    assert exc.value.cleanup_error is not None
    assert api.freed == [api.descriptor]


@pytest.mark.parametrize("revision", [0, 1, 3, 5, 255])
def test_acl_snapshot_rejects_unsupported_acl_revision_and_frees(revision):
    api = FakeAclSnapshotApi()
    api.revision = revision
    with pytest.raises(wac.AclSnapshotError, match="unsupported_acl_revision"):
        wac.snapshot_filesystem_acl("C:\\safe", api=api)
    assert api.freed == [api.descriptor]


def test_acl_snapshot_preserves_revision_header_and_exact_partition():
    api = FakeAclSnapshotApi()
    first, second = _simple_ace(0), _simple_ace(1, flags=0x80)
    api.revision, api.sbz1, api.sbz2 = 4, 0x7A, 0xCAFE
    api.aces = [(api.dacl + 8, first), (api.dacl + 8 + len(first), second)]
    snapshot = wac.snapshot_filesystem_acl("C:\\safe", api=api)
    assert snapshot.raw_acl is not None
    assert snapshot.raw_acl[:2] == b"\x04\x7a"
    assert snapshot.raw_acl[6:8] == b"\xfe\xca"
    assert b"".join(ace.raw for ace in snapshot.aces) == snapshot.raw_acl[8:]
    snapshot.verify_integrity()


@pytest.mark.parametrize("kind", ["size", "count", "truncated", "gap"])
def test_acl_snapshot_rejects_malformed_raw_acl_and_always_frees(kind):
    api = FakeAclSnapshotApi()
    raw = _simple_ace()
    api.aces = [(api.dacl + 8, raw)]
    total = 8 + len(raw)
    valid = b"\x02\0" + total.to_bytes(2, "little") + b"\x01\0\0\0" + raw
    if kind == "size":
        api.raw_override = valid[:2] + (len(valid) + 1).to_bytes(2, "little") + valid[4:]
    elif kind == "count":
        api.raw_override = valid[:4] + b"\x02\0" + valid[6:]
    elif kind == "truncated":
        api.raw_override = valid[:-1]
    else:
        api.aces = [(api.dacl + 9, raw)]
    with pytest.raises(wac.AclSnapshotError):
        wac.snapshot_filesystem_acl("C:\\safe", api=api)
    assert api.freed == [api.descriptor]


def test_acl_snapshot_authentication_detects_field_and_raw_drift():
    api = FakeAclSnapshotApi()
    raw = _simple_ace()
    api.aces = [(api.dacl + 8, raw)]
    snapshot = wac.snapshot_filesystem_acl("C:\\safe", api=api)
    object.__setattr__(snapshot, "defaulted", not snapshot.defaulted)
    with pytest.raises(wac.AclSnapshotError, match="snapshot_authentication"):
        snapshot.verify_integrity()


def test_nonpresent_snapshot_cannot_fabricate_raw_acl():
    with pytest.raises(ValueError, match="cannot have raw"):
        wac.AclSnapshot("C:\\safe", wac.DaclState.NULL, False, (), b"acl")


@pytest.mark.parametrize("path", ["", "\x00", "C:\\x\x00y", 1, None])
def test_acl_snapshot_path_validation_is_deterministic(path):
    with pytest.raises((TypeError, ValueError)):
        wac.snapshot_filesystem_acl(path, api=FakeAclSnapshotApi())


def test_acl_snapshot_native_abi_is_pointer_width_safe():
    class Fn:
        def __call__(self, *args): return 0
    class Dll:
        GetNamedSecurityInfoW = Fn(); GetSecurityDescriptorDacl = Fn()
        GetAclInformation = Fn(); GetAce = Fn(); IsValidSid = Fn(); GetLengthSid = Fn()
        LocalFree = Fn()
    adv, kernel = Dll(), Dll()
    wac._NativeAclSnapshotApi._configure_signatures(adv, kernel)
    assert adv.GetNamedSecurityInfoW.argtypes[-1]._type_ is wac.wintypes.LPVOID
    assert adv.GetAce.argtypes[-1]._type_ is wac.wintypes.LPVOID
    assert kernel.LocalFree.argtypes == [wac.wintypes.HLOCAL]
    assert kernel.LocalFree.restype is wac.wintypes.HLOCAL


@pytest.mark.parametrize(
    ("address", "header", "operation"),
    [
        (0x1_0000_1FFF, b"", "ace_out_of_range"),
        (0x1_0000_20FE, b"", "ace_out_of_range"),
        (0x1_0000_20F0, b"\x00\x00\x20\x00", "ace_out_of_range"),
    ],
)
def test_acl_snapshot_native_rejects_ace_ranges_before_unsafe_read(
    monkeypatch, address, header, operation
):
    class GetAce:
        def __call__(self, _dacl, _index, output):
            wac.ctypes.cast(output, wac.ctypes.POINTER(wac.wintypes.LPVOID))[0] = address
            return 1

    api = wac._NativeAclSnapshotApi.__new__(wac._NativeAclSnapshotApi)
    api._advapi32 = type("Advapi", (), {"GetAce": GetAce()})()
    reads = []

    def guarded_read(pointer, size):
        reads.append((pointer, size))
        return header

    monkeypatch.setattr(wac.ctypes, "string_at", guarded_read)
    with pytest.raises(wac.AclSnapshotError) as exc:
        api.get_ace(0x1_0000_2000, 0, 0x1_0000_2100)
    assert exc.value.operation == operation
    if address == 0x1_0000_20F0:
        assert reads == [(address, 4)]
    else:
        assert reads == []


def test_acl_snapshot_native_accepts_valid_ace_above_32_bits(monkeypatch):
    address = 0x1_0000_2020
    raw = _simple_ace()

    class GetAce:
        def __call__(self, dacl, _index, output):
            assert dacl.value == 0x1_0000_2000
            wac.ctypes.cast(output, wac.ctypes.POINTER(wac.wintypes.LPVOID))[0] = address
            return 1

    api = wac._NativeAclSnapshotApi.__new__(wac._NativeAclSnapshotApi)
    api._advapi32 = type("Advapi", (), {"GetAce": GetAce()})()
    reads = []

    def bounded_read(pointer, size):
        reads.append((pointer, size))
        return raw[:size]

    monkeypatch.setattr(wac.ctypes, "string_at", bounded_read)
    assert api.get_ace(0x1_0000_2000, 0, 0x1_0000_2100) == (address, raw)
    assert reads == [(address, 4), (address, len(raw))]


@pytest.mark.parametrize(
    ("address", "ace_end", "header"),
    [
        (0x1_0000_20FF, 0x1_0000_2100, b""),
        (0x1_0000_20F9, 0x1_0000_2100, b""),
        (0x1_0000_20F0, 0x1_0000_2100, b"\x01\xff" + b"\0" * 6),
    ],
)
def test_acl_snapshot_native_rejects_sid_range_before_native_validation(
    monkeypatch, address, ace_end, header
):
    calls = []

    class Advapi:
        def IsValidSid(self, _pointer):
            calls.append("valid")
            return 1

        def GetLengthSid(self, _pointer):
            calls.append("length")
            return 8

    api = wac._NativeAclSnapshotApi.__new__(wac._NativeAclSnapshotApi)
    api._advapi32 = Advapi()
    reads = []

    def bounded_read(pointer, size):
        reads.append((pointer, size))
        return header

    monkeypatch.setattr(wac.ctypes, "string_at", bounded_read)
    with pytest.raises(wac.AclSnapshotError) as exc:
        api.sid_bytes(address, ace_end)
    assert exc.value.operation == "sid_out_of_range"
    assert calls == []
    assert reads == ([] if ace_end - address < 8 else [(address, 8)])


def test_acl_snapshot_native_sid_checks_bounded_header_before_native_calls(monkeypatch):
    address = 0x1_0000_20E0
    sid = b"\x01\x02" + b"\0" * 6 + b"\x20\0\0\0\x21\0\0\0"
    calls = []

    class Advapi:
        def IsValidSid(self, pointer):
            calls.append(("valid", pointer.value))
            return 1

        def GetLengthSid(self, pointer):
            calls.append(("length", pointer.value))
            return len(sid)

    api = wac._NativeAclSnapshotApi.__new__(wac._NativeAclSnapshotApi)
    api._advapi32 = Advapi()

    def bounded_read(pointer, size):
        calls.append(("read", pointer, size))
        return sid[:size]

    monkeypatch.setattr(wac.ctypes, "string_at", bounded_read)
    assert api.sid_bytes(address, address + len(sid)) == sid
    assert calls == [
        ("read", address, 8),
        ("valid", address),
        ("length", address),
        ("read", address, len(sid)),
    ]


@pytest.mark.skipif(wac.os.name != "nt", reason="Windows native canary")
def test_acl_snapshot_windows_native_canary_is_structurally_valid():
    path = wac.os.getcwd()
    snapshot = wac.snapshot_filesystem_acl(path)
    reacquired = wac.snapshot_filesystem_acl(path)
    assert isinstance(snapshot, wac.AclSnapshot)
    assert isinstance(snapshot.aces, tuple)
    assert (reacquired.dacl_state, reacquired.defaulted) == (
        snapshot.dacl_state,
        snapshot.defaulted,
    )
    assert reacquired.raw_acl == snapshot.raw_acl
    if snapshot.dacl_state is wac.DaclState.PRESENT:
        assert snapshot.raw_acl is not None
        assert reacquired.raw_acl is not None
        assert reacquired.raw_acl[:8] == snapshot.raw_acl[:8]
        assert b"".join(ace.raw for ace in snapshot.aces) == snapshot.raw_acl[8:]
        assert b"".join(ace.raw for ace in reacquired.aces) == reacquired.raw_acl[8:]
        assert reacquired.aces == snapshot.aces
    else:
        assert snapshot.raw_acl is reacquired.raw_acl is None
        assert snapshot.aces == reacquired.aces == ()


# ---------------------------------------------------------------------------
# LOCALAPPDATA is load-bearing for AppContainer process creation
#
# Measured on Windows 11: CreateProcessW for an AppContainer token answers
# ERROR_ENVVAR_NOT_FOUND (203) when the CHILD's environment block lacks
# LOCALAPPDATA.  The sanitized worker and validation environments drop it, so
# every launch from them failed at create_process -- including a bare
# ``cmd.exe /c echo`` -- while the identical request succeeded from a full
# interactive environment.  The caller's own environment was proven not to
# matter; only the block handed to the child does.
# ---------------------------------------------------------------------------


def test_child_environment_none_still_inherits_the_caller():
    assert wac.appcontainer_child_environment(None, local_appdata=r"C:\L") is None


@pytest.mark.parametrize("key", ["LOCALAPPDATA", "LocalAppData", "localappdata"])
def test_child_environment_keeps_an_existing_value_in_any_case(key):
    env = {key: r"C:\Original", "A": "B"}
    assert wac.appcontainer_child_environment(env, local_appdata=r"C:\Other") is env


def test_child_environment_adds_the_value_without_mutating_the_input():
    env = {"A": "B"}
    merged = wac.appcontainer_child_environment(
        env, local_appdata=r"C:\Users\u\AppData\Local"
    )
    assert merged == {"A": "B", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
    assert env == {"A": "B"}


def test_child_environment_is_left_alone_when_nothing_resolves():
    env = {"A": "B"}
    assert wac.appcontainer_child_environment(env, local_appdata="") is env


def test_resolve_local_appdata_prefers_the_process_environment(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\FromEnv")
    monkeypatch.setattr(
        wac,
        "_known_folder_local_appdata",
        lambda: pytest.fail("known-folder lookup must not run when env has it"),
    )
    assert wac.resolve_local_appdata() == r"C:\FromEnv"


def test_resolve_local_appdata_falls_back_when_the_env_was_sanitized(monkeypatch):
    """The worker supervisor runs under an allowlist env without LOCALAPPDATA."""
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(wac.os, "name", "nt")
    monkeypatch.setattr(
        wac, "_known_folder_local_appdata", lambda: r"C:\FromKnownFolder"
    )
    assert wac.resolve_local_appdata() == r"C:\FromKnownFolder"


def test_resolve_local_appdata_fails_soft_when_the_lookup_breaks(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(wac.os, "name", "nt")

    def _broken():
        raise OSError("shell32 unavailable")

    monkeypatch.setattr(wac, "_known_folder_local_appdata", _broken)
    assert wac.resolve_local_appdata() == ""


def test_launch_hands_a_sanitized_child_environment_localappdata(monkeypatch):
    monkeypatch.setattr(
        wac, "resolve_local_appdata", lambda: r"C:\Users\u\AppData\Local"
    )
    fake = FakeWin32Api()
    launch_appcontainer(make_request(environment={"A": "B"}), api=fake)
    assert fake.spec.environment["LOCALAPPDATA"] == r"C:\Users\u\AppData\Local"
    assert fake.spec.environment["A"] == "B"


def test_launch_leaves_an_inherited_environment_inherited(monkeypatch):
    monkeypatch.setattr(wac, "resolve_local_appdata", lambda: r"C:\L")
    fake = FakeWin32Api()
    launch_appcontainer(make_request(environment=None), api=fake)
    assert fake.spec.environment is None


def test_launch_refuses_hostile_keys_before_supplying_localappdata(monkeypatch):
    monkeypatch.setattr(wac, "resolve_local_appdata", lambda: r"C:\L")
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(
            make_request(environment={"A=B": "value"}), api=FakeWin32Api()
        )
    assert excinfo.value.reason is AppContainerReason.INVALID_ENVIRONMENT


# ---------------------------------------------------------------------------
# Filesystem grants (NF-2026-00025)
# ---------------------------------------------------------------------------


def _grant_dirs(tmp_path):
    paths = []
    for name in ("worktree", "home", "provider"):
        path = tmp_path / name
        path.mkdir()
        paths.append(str(path))
    worktree, home, provider = paths
    return [
        ContainerGrant(worktree, "modify"),
        ContainerGrant(home, "modify"),
        ContainerGrant(provider, "read_execute", persistent=True),
    ]


def _grant_events(fake):
    return [e for e in fake.events if e.startswith(("grant:", "revoke:"))]


def test_grants_apply_in_order_for_this_sid_before_create_process(tmp_path):
    grants = _grant_dirs(tmp_path)
    fake = FakeWin32Api()
    launch = launch_appcontainer(make_request(filesystem_grants=grants), api=fake)

    expected = [f"grant:{g.access}:{g.path}" for g in grants]
    assert _grant_events(fake) == expected
    first_grant = fake.events.index(expected[0])
    assert fake.events.index("derive_appcontainer_sid") < first_grant
    assert fake.events.index(expected[-1]) < fake.events.index("create_process")
    # Only the revocable grants are owned by the launch; nothing revoked yet.
    assert [g.path for g in launch.grants] == [grants[0].path, grants[1].path]


def test_create_process_failure_revokes_every_applied_grant_lifo(tmp_path):
    grants = _grant_dirs(tmp_path)
    fake = FakeWin32Api(fail_at="create_process")
    with pytest.raises(AppContainerError):
        launch_appcontainer(make_request(filesystem_grants=grants), api=fake)

    revokes = [e for e in _grant_events(fake) if e.startswith("revoke:")]
    # Reverse order, and the persistent provider grant is never revoked.
    assert revokes == [f"revoke:{grants[1].path}", f"revoke:{grants[0].path}"]
    assert_no_leak(fake)


def test_second_grant_failure_revokes_the_first_and_never_launches(tmp_path):
    grants = _grant_dirs(tmp_path)
    fake = FakeWin32Api()
    fake.fail_grant_attempt = 2
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(make_request(filesystem_grants=grants), api=fake)

    assert excinfo.value.reason is AppContainerReason.FILESYSTEM_GRANT_FAILED
    assert excinfo.value.operation == "grant_path_access"
    assert _grant_events(fake) == [
        f"grant:modify:{grants[0].path}",
        f"revoke:{grants[0].path}",
    ]
    assert "create_process" not in fake.events
    assert fake.identity_freed


def test_close_revokes_grants_once_and_second_close_is_a_noop(tmp_path):
    grants = _grant_dirs(tmp_path)
    fake = FakeWin32Api()
    launch = launch_appcontainer(make_request(filesystem_grants=grants), api=fake)
    fake.events.clear()

    launch.close()
    assert _grant_events(fake) == [
        f"revoke:{grants[1].path}",
        f"revoke:{grants[0].path}",
    ]
    assert launch.grants == []
    assert launch.cleanup_evidence()["outstanding_grants"] == []

    fake.events.clear()
    launch.close()
    assert fake.events == []


def test_terminate_also_revokes_grants(tmp_path):
    grants = _grant_dirs(tmp_path)
    fake = FakeWin32Api()
    fake.wait_results = [True]
    launch = launch_appcontainer(make_request(filesystem_grants=grants), api=fake)

    launch.terminate()
    assert [e for e in fake.events if e.startswith("revoke:")] == [
        f"revoke:{grants[1].path}",
        f"revoke:{grants[0].path}",
    ]


def test_request_without_grants_touches_no_grant_api():
    fake = FakeWin32Api()
    launch = launch_appcontainer(make_request(), api=fake)
    launch.close()
    assert fake.grant_attempts == 0
    assert _grant_events(fake) == []
    assert launch.grants == []


def _make_link(target, link):
    """A symlink where permitted, else (Windows) a junction: both reparse."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


@pytest.mark.parametrize(
    "case",
    [
        "relative",
        "missing",
        "leaf_link",
        "ancestor_link",
        "access",
        "nul",
        "persistent_modify",
    ],
)
def test_invalid_grant_is_refused_before_any_win32_call(tmp_path, case):
    real = tmp_path / "real"
    (real / "child").mkdir(parents=True)
    path, access, persistent = str(real), "modify", case == "persistent_modify"
    if case == "relative":
        path = os.path.join("relative", "dir")
    elif case == "missing":
        path = str(tmp_path / "absent")
    elif case in {"leaf_link", "ancestor_link"}:
        _make_link(real, tmp_path / "link")
        path = str(tmp_path / "link")
        if case == "ancestor_link":
            path = str(tmp_path / "link" / "child")
    elif case == "access":
        access = "full_control"
    elif case == "nul":
        path = str(real) + "\x00"
    fake = FakeWin32Api()
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(
            make_request(
                filesystem_grants=[ContainerGrant(path, access, persistent)]
            ),
            api=fake,
        )
    assert excinfo.value.reason is AppContainerReason.INVALID_REQUEST
    assert fake.events == []


@pytest.mark.parametrize(
    ("access", "persistent"),
    [("read_execute", True), ("read_execute", False), ("modify", False)],
)
def test_no_grant_may_equal_or_contain_a_protected_tree(
    tmp_path, monkeypatch, access, persistent
):
    profile = tmp_path / "profile"
    temp = profile / "AppData" / "Local" / "Temp"
    temp.mkdir(parents=True)
    protected = [os.path.normcase(str(p)) for p in (profile, temp)]
    monkeypatch.setattr(wac, "_sensitive_roots", lambda: (protected, []))
    fake = FakeWin32Api()
    # Equal to a protected tree, or an ancestor of one -- at any access level.
    for path in (profile, temp, profile / "AppData", tmp_path):
        with pytest.raises(AppContainerError) as excinfo:
            launch_appcontainer(
                make_request(
                    filesystem_grants=[ContainerGrant(str(path), access, persistent)]
                ),
                api=fake,
            )
        assert excinfo.value.reason is AppContainerReason.INVALID_REQUEST
        assert "protected tree" in excinfo.value.detail
    assert fake.events == []
    # A request directory *inside* one is what grants are for.
    inside = temp / "request"
    inside.mkdir()
    launch_appcontainer(
        make_request(filesystem_grants=[ContainerGrant(str(inside), access, persistent)]),
        api=fake,
    )


def _refused(grants, fake):
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(make_request(filesystem_grants=grants), api=fake)
    assert excinfo.value.reason is AppContainerReason.INVALID_REQUEST
    assert fake.events == []
    return excinfo.value.detail


ALIASES = {
    "long_path": lambda p: "\\\\?\\" + p,
    "device": lambda p: "\\\\.\\" + p,
    "admin_share": lambda p: "\\\\localhost\\" + p.replace(":", "$", 1),
    "admin_share_slashes": lambda p: "//localhost/"
    + p.replace(":", "$", 1).replace("\\", "/"),
}


@pytest.mark.parametrize("alias", sorted(ALIASES))
@pytest.mark.parametrize(("access", "persistent"), [("read_execute", True), ("modify", False)])
def test_unc_device_and_long_path_aliases_are_refused(tmp_path, alias, access, persistent):
    # \\?\C:\Users\x and \\localhost\C$\Users\x alias local trees past every
    # string comparison against the protected roots.
    path = ALIASES[alias](str(tmp_path))
    detail = _refused([ContainerGrant(path, access, persistent)], FakeWin32Api())
    if os.path.isabs(path):  # off Windows, "\\?\..." is refused as relative
        assert "UNC, device" in detail


@pytest.mark.parametrize(
    ("relative", "access", "persistent"),
    [
        (("System32",), "read_execute", True),
        (("System32", "drivers", "etc"), "modify", False),
        ((), "read_execute", False),
    ],
)
def test_nothing_inside_a_system_tree_is_granted(
    tmp_path, monkeypatch, relative, access, persistent
):
    windows = tmp_path / "Windows"
    target = windows.joinpath(*relative)
    target.mkdir(parents=True, exist_ok=True)
    profile = tmp_path / "profile"
    (profile / "npm").mkdir(parents=True)
    monkeypatch.setattr(
        wac,
        "_sensitive_roots",
        lambda: ([os.path.normcase(str(profile))], [os.path.normcase(str(windows))]),
    )
    fake = FakeWin32Api()
    detail = _refused([ContainerGrant(str(target), access, persistent)], fake)
    assert "Windows or Program Files" in detail
    # The descendant rule is for system trees only: the npm install and the
    # per-request directories legitimately live inside the profile.
    launch_appcontainer(
        make_request(
            filesystem_grants=[
                ContainerGrant(str(profile / "npm"), "read_execute", persistent=True)
            ]
        ),
        api=fake,
    )


@pytest.mark.skipif(os.name != "nt", reason="resolves real Windows locations")
def test_real_system32_and_its_subtrees_are_refused():
    system32 = os.path.join(os.environ["SYSTEMROOT"], "System32")
    etc = os.path.join(system32, "drivers", "etc")
    _refused([ContainerGrant(system32, "read_execute", persistent=True)], FakeWin32Api())
    _refused([ContainerGrant(etc, "modify")], FakeWin32Api())


@pytest.mark.parametrize(
    ("revocable", "persistent"),
    [
        (("npm",), ("npm",)),  # equal
        (("npm", "pkg"), ("npm",)),  # revocable inside persistent
        ((), ("npm",)),  # revocable contains persistent
    ],
)
def test_revocable_and_persistent_grants_must_not_overlap(tmp_path, revocable, persistent):
    base = tmp_path / "root"
    (base / "npm" / "pkg").mkdir(parents=True)
    grants = [
        ContainerGrant(str(base.joinpath(*persistent)), "read_execute", persistent=True),
        ContainerGrant(str(base.joinpath(*revocable)), "modify"),
    ]
    detail = _refused(grants, FakeWin32Api())
    assert "overlaps a persistent grant" in detail


def test_disjoint_revocable_and_persistent_grants_are_fine(tmp_path):
    grants = _grant_dirs(tmp_path)  # sibling worktree, home and provider dirs
    launch_appcontainer(make_request(filesystem_grants=grants), api=FakeWin32Api())


def test_a_filesystem_root_is_never_granted(tmp_path):
    fake = FakeWin32Api()
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(
            make_request(
                filesystem_grants=[ContainerGrant(tmp_path.anchor, "read_execute")]
            ),
            api=fake,
        )
    assert "protected tree" in excinfo.value.detail
    assert fake.events == []


@requires_host_win32_privileges
@pytest.mark.skipif(os.name != "nt", reason="resolves real Windows locations")
def test_protected_trees_come_from_the_token_not_the_request_env(
    tmp_path, monkeypatch
):
    real_profile_path = os.environ["USERPROFILE"]
    real_profile = os.path.normcase(real_profile_path)
    # A launcher points USERPROFILE/TEMP at the request's own directories,
    # and then the env-expanding known-folder lookups fail (measured).  Never
    # call the real one with a bogus USERPROFILE here: shell32 caches the
    # failure for the rest of the process.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(wac, "_known_folder_path", lambda _folder: "")
    protected, system = wac._sensitive_roots()
    assert real_profile in protected
    assert os.path.join(real_profile, "appdata", "local", "temp") in protected
    assert os.path.join(real_profile, "appdata", "roaming") in protected
    assert os.path.normcase(os.environ["SYSTEMROOT"]) in system
    assert os.path.normcase(str(tmp_path)) not in protected + system
    # The reviewer's repro: persistent read access to the whole profile.
    fake = FakeWin32Api()
    grant = ContainerGrant(real_profile_path, "read_execute", persistent=True)
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(make_request(filesystem_grants=[grant]), api=fake)
    assert "protected tree" in excinfo.value.detail
    assert fake.events == []


def test_request_scoped_grants_dedupe_and_skip_unset(tmp_path):
    cwd, home, temp = (str(tmp_path / n) for n in ("wt", "home", "tmp"))
    env = {"HOME": home, "USERPROFILE": home, "TMP": temp, "TEMP": temp, "PATH": "x"}
    assert request_scoped_grants(env, cwd) == [
        ContainerGrant(cwd, "modify"),
        ContainerGrant(home, "modify"),
        ContainerGrant(temp, "modify"),
    ]
    assert request_scoped_grants({}) == []


# -- real ctypes boundary against recording advapi32/kernel32 doubles --------


# A real, readable SID (S-1-15-2) so the boundary's string_at is safe.
_SID = b"\x01\x01\x00\x00\x00\x00\x00\x0f\x02\x00\x00\x00"
_SID_BUFFER = ctypes.create_string_buffer(_SID, len(_SID))
_GRANT, _REVOKE = 1, 4


def _identity():
    return _Identity("n", "d", "S-1-15-2", ctypes.addressof(_SID_BUFFER), False)


class FakeSecurityLib:
    """Stands in for both advapi32 and kernel32 in the grant/revoke path."""

    def __init__(self, *, dacl=222, protected=False, set_status=0):
        self.dacl = dacl
        self.protected = protected
        self.set_status = set_status
        self.entries = []
        self.set_calls = []
        self.freed = []

    def GetLengthSid(self, sid):
        return len(_SID)

    def GetNamedSecurityInfoW(self, path, obj, info, owner, group, dacl, sacl, sd):
        sd._obj.value = 111
        dacl._obj.value = self.dacl
        return 0

    def GetSecurityDescriptorControl(self, descriptor, control, revision):
        control._obj.value = 0x1000 if self.protected else 0
        return 1

    def SetEntriesInAclW(self, count, entry, old_acl, new_acl):
        e = entry._obj
        trustee = ctypes.string_at(e.Trustee.ptstrName, len(_SID))
        self.entries.append(
            (count, e.grfAccessPermissions, e.grfAccessMode, e.grfInheritance,
             e.Trustee.TrusteeForm, trustee, old_acl.value)
        )
        new_acl._obj.value = 333
        return 0

    def SetNamedSecurityInfoW(self, path, obj, info, owner, group, dacl, sacl):
        self.set_calls.append((path, info, getattr(dacl, "value", dacl)))
        return self.set_status

    def LocalFree(self, ptr):
        self.freed.append(getattr(ptr, "value", ptr))


def _security_api(lib, monkeypatch, *, present=None):
    api = make_ctypes_api(lib)
    api._advapi32 = lib

    def _present(*_args):
        if present is None:
            pytest.fail("a revocable grant must never take the already-present path")
        return "container_sid" if present else ""

    monkeypatch.setattr(api, "_grant_already_satisfied", _present)
    return api


@pytest.mark.parametrize("protected", [False, True])
def test_ctypes_revocable_grant_writes_and_revoke_removes_only_this_sid(
    tmp_path, monkeypatch, protected
):
    lib = FakeSecurityLib(protected=protected)
    api = _security_api(lib, monkeypatch)  # present=None: must not be consulted

    grant = api.grant_path_access(_identity(), str(tmp_path), "modify")

    # One GRANT_ACCESS entry whose trustee is the container SID itself,
    # inheritable to files and subdirectories, merged into the DACL just read.
    assert lib.entries == [(1, 0x1301BF, _GRANT, 0x3, 0, _SID, 222)]
    info = 0x4 | (0x80000000 if protected else 0x20000000)
    assert lib.set_calls == [(str(tmp_path), info, 333)]
    assert lib.freed == [333, 111]  # merged ACL, then the descriptor
    assert grant.restore == _SID  # what a revoke needs, even after free_identity

    api.revoke_path_access(grant)
    # Revoke re-reads the CURRENT DACL and drops only this SID's ACEs, so a
    # concurrent edit by anyone else survives, and so does nothing of ours.
    assert lib.entries[-1] == (1, 0, _REVOKE, 0, 0, _SID, 222)
    assert lib.set_calls[-1] == (str(tmp_path), info, 333)
    assert lib.freed == [333, 111, 333, 111]
    assert grant.revoke_error is None
    api.revoke_path_access(grant)
    assert len(lib.set_calls) == 2


def test_ctypes_persistent_grant_already_present_rewrites_nothing(
    tmp_path, monkeypatch
):
    lib = FakeSecurityLib()
    api = _security_api(lib, monkeypatch, present=True)
    grant = api.grant_path_access(
        _identity(), str(tmp_path), "read_execute", persistent=True
    )
    assert lib.entries == [] and lib.set_calls == [] and lib.freed == []
    assert grant.restore is None


def test_ctypes_persistent_file_grant_is_not_inheritable_and_never_revoked(
    tmp_path, monkeypatch
):
    target = tmp_path / "claude.cmd"
    target.write_text("@echo off\n", encoding="utf-8")
    lib = FakeSecurityLib()
    api = _security_api(lib, monkeypatch, present=False)

    grant = api.grant_path_access(
        _identity(), str(target), "read_execute", persistent=True
    )

    assert lib.entries[0][1:4] == (0x1200A9, _GRANT, 0)
    assert grant.restore is None
    assert lib.freed == [333, 111]
    api.revoke_path_access(grant)
    assert len(lib.set_calls) == 1


def test_ctypes_grant_leaves_a_null_dacl_alone(tmp_path, monkeypatch):
    lib = FakeSecurityLib(dacl=None)
    api = _security_api(lib, monkeypatch)
    grant = api.grant_path_access(_identity(), str(tmp_path), "modify")
    assert lib.entries == [] and lib.set_calls == []
    assert grant.restore is None
    assert lib.freed == [111]


def test_ctypes_failed_revoke_is_recorded_not_raised(tmp_path, monkeypatch):
    lib = FakeSecurityLib(set_status=5)
    api = _security_api(lib, monkeypatch)
    grant = _PathGrant(str(tmp_path), "modify", _SID)
    api.revoke_path_access(grant)
    assert grant.revoke_error == 5
    assert lib.freed == [333, 111]


def test_launch_close_surfaces_a_failed_revoke(tmp_path):
    class RevokeFails(FakeWin32Api):
        def revoke_path_access(self, grant):
            super().revoke_path_access(grant)
            grant.revoke_error = 1307

    fake = RevokeFails()
    grant = ContainerGrant(str(tmp_path), "modify")
    launch = launch_appcontainer(make_request(filesystem_grants=[grant]), api=fake)
    launch.close()
    assert launch.cleanup_evidence()["grant_revoke_failures"] == [
        {"path": str(tmp_path), "win_error": 1307}
    ]


# -- protected descendants (NF-2026-00034) -----------------------------------


def _request_tree(tmp_path):
    """Two per-request directories laid out the way AIWorkHub makes them."""
    worktrees = tmp_path / ".aiworkhub" / "runtime" / "worktrees"
    home = worktrees / "req_a" / "home"
    for relative in ("task_mcp_worker_runtime", ".config/kilo", "plain/deep"):
        (home / relative).mkdir(parents=True)
    other = worktrees / "req_b" / "home" / "task_mcp_worker_runtime"
    other.mkdir(parents=True)
    return home, other


def _protect(fake, *paths):
    fake.protected_paths = frozenset(os.path.normcase(str(p)) for p in paths)


def test_protected_descendants_are_granted_and_revoked_with_their_directory(tmp_path):
    home, _ = _request_tree(tmp_path)
    runtime, config, kilo = (
        home / "task_mcp_worker_runtime", home / ".config", home / ".config" / "kilo"
    )
    fake = FakeWin32Api()
    _protect(fake, runtime, config, kilo)
    launch = launch_appcontainer(
        make_request(filesystem_grants=[ContainerGrant(str(home), "modify")]), api=fake
    )

    granted = _grant_events(fake)
    # HOME first, then each protected directory with HOME's access -- a
    # protected parent before its protected child; plain/ and plain/deep
    # inherit HOME's ACE and get none of their own.
    assert granted[0] == f"grant:modify:{home}"
    assert sorted(granted[1:]) == sorted(
        f"grant:modify:{path}" for path in (runtime, config, kilo)
    )
    assert granted.index(f"grant:modify:{config}") < granted.index(f"grant:modify:{kilo}")
    assert len(launch.grants) == 4

    fake.events.clear()
    launch.close()
    assert _grant_events(fake) == [
        event.replace("grant:modify:", "revoke:") for event in reversed(granted)
    ]


def test_failure_unwind_revokes_the_protected_descendants_too(tmp_path):
    home, _ = _request_tree(tmp_path)
    runtime = home / "task_mcp_worker_runtime"
    fake = FakeWin32Api(fail_at="create_process")
    _protect(fake, runtime)
    with pytest.raises(AppContainerError):
        launch_appcontainer(
            make_request(filesystem_grants=[ContainerGrant(str(home), "modify")]),
            api=fake,
        )
    assert _grant_events(fake) == [
        f"grant:modify:{home}",
        f"grant:modify:{runtime}",
        f"revoke:{runtime}",
        f"revoke:{home}",
    ]
    assert_no_leak(fake)


def test_a_reparse_point_is_never_followed_out_of_the_request_directory(tmp_path):
    home, other = _request_tree(tmp_path)
    # A link inside req_a's HOME into req_b's HOME: were it followed, req_b's
    # protected runtime directory -- another request's files -- would be
    # granted to this container.
    _make_link(other.parent, home / "link")
    fake = FakeWin32Api()
    _protect(fake, home / "link", home / "link" / "task_mcp_worker_runtime", other)
    launch_appcontainer(
        make_request(filesystem_grants=[ContainerGrant(str(home), "modify")]), api=fake
    )

    assert _grant_events(fake) == [f"grant:modify:{home}"]
    assert not any("link" in path or "req_b" in path for path in fake.dacl_queries)
    request_dir = os.path.normcase(str(home.parent))
    for event in _grant_events(fake):
        path = os.path.normcase(event.split(":", 2)[2])
        assert wac._within(path, request_dir), event


def test_a_descendant_swapped_for_a_link_after_the_walk_is_refused(tmp_path):
    home, other = _request_tree(tmp_path)
    runtime = home / "task_mcp_worker_runtime"

    class SwapsAfterWalk(FakeWin32Api):
        def dacl_protected(self, path):
            protected = super().dacl_protected(path)
            if protected:  # swap it for a link between the walk and the grant
                runtime.rmdir()
                _make_link(other, runtime)
            return protected

    fake = SwapsAfterWalk()
    _protect(fake, runtime)
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(
            make_request(filesystem_grants=[ContainerGrant(str(home), "modify")]),
            api=fake,
        )
    # _validate_grants re-runs over the expanded plan before any Win32 call.
    assert excinfo.value.reason is AppContainerReason.INVALID_REQUEST
    assert "reparse point" in excinfo.value.detail
    assert fake.events == []


def test_persistent_grants_are_never_walked(tmp_path):
    provider = tmp_path / "npm"
    (provider / "pkg").mkdir(parents=True)
    fake = FakeWin32Api()
    _protect(fake, provider / "pkg")
    launch_appcontainer(
        make_request(
            filesystem_grants=[ContainerGrant(str(provider), "read_execute", persistent=True)]
        ),
        api=fake,
    )
    assert fake.dacl_queries == []
    assert _grant_events(fake) == [f"grant:read_execute:{provider}"]


def test_the_walk_is_bounded_and_fails_closed_before_any_grant(tmp_path, monkeypatch):
    home, _ = _request_tree(tmp_path)  # five directories beneath HOME
    monkeypatch.setattr(wac, "_DESCENDANT_WALK_LIMIT", 4)
    fake = FakeWin32Api()
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(
            make_request(filesystem_grants=[ContainerGrant(str(home), "modify")]),
            api=fake,
        )
    assert excinfo.value.reason is AppContainerReason.FILESYSTEM_GRANT_FAILED
    assert "more than 4 directories" in excinfo.value.detail
    assert fake.events == []


def test_a_descendant_already_requested_is_granted_once(tmp_path):
    home, _ = _request_tree(tmp_path)
    runtime = home / "task_mcp_worker_runtime"
    fake = FakeWin32Api()
    _protect(fake, runtime)
    launch_appcontainer(
        make_request(
            filesystem_grants=[
                ContainerGrant(str(home), "modify"),
                ContainerGrant(str(runtime), "modify"),
            ]
        ),
        api=fake,
    )
    assert _grant_events(fake) == [f"grant:modify:{home}", f"grant:modify:{runtime}"]


def test_a_withheld_protected_directory_is_never_granted_or_walked(tmp_path):
    home, _ = _request_tree(tmp_path)
    runtime = home / "task_mcp_worker_runtime"
    (runtime / "validation_runs").mkdir()
    fake = FakeWin32Api()
    _protect(fake, runtime, runtime / "validation_runs", home / ".config")
    launch_appcontainer(
        make_request(
            filesystem_grants=[ContainerGrant(str(home), "modify")],
            withheld_directories=[str(runtime)],
        ),
        api=fake,
    )
    granted = _grant_events(fake)
    assert f"grant:modify:{home / '.config'}" in granted
    assert not any("task_mcp_worker_runtime" in event for event in granted)
    assert not any(str(runtime) + os.sep in query for query in fake.dacl_queries)


@pytest.mark.parametrize("case", ["unprotected", "missing", "also_granted"])
def test_a_withheld_directory_that_would_not_stay_closed_fails_closed(tmp_path, case):
    home, _ = _request_tree(tmp_path)
    runtime = home / "task_mcp_worker_runtime"
    fake = FakeWin32Api()
    if case != "unprotected":
        _protect(fake, runtime)
    withheld = str(home / "absent") if case == "missing" else str(runtime)
    grants = [ContainerGrant(str(home), "modify")]
    if case == "also_granted":
        grants.append(ContainerGrant(str(runtime), "modify"))
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(
            make_request(filesystem_grants=grants, withheld_directories=[withheld]), api=fake
        )
    assert excinfo.value.reason is AppContainerReason.INVALID_REQUEST
    assert "withheld" in excinfo.value.detail
    assert _grant_events(fake) == []


# -- the validation interpreter's read set (NF-2026-00034) ---------------------


def _venv(tmp_path):
    base = tmp_path / "Python312"
    (base / "Lib").mkdir(parents=True)
    (base / "python.exe").write_bytes(b"MZ")
    venv = tmp_path / "repo" / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Lib" / "site-packages").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"MZ")
    (venv / "pyvenv.cfg").write_text(
        f"home = {base}\ninclude-system-site-packages = false\n", encoding="utf-8"
    )
    return base, venv


def test_a_venv_interpreter_needs_its_launcher_config_site_packages_and_base(tmp_path):
    base, venv = _venv(tmp_path)
    worktree, runtime = tmp_path / "wt", tmp_path / "runtime"
    (worktree / "src").mkdir(parents=True)
    runtime.mkdir()
    grants = wac.python_read_grants(
        str(venv / "Scripts" / "python.exe"),
        os.pathsep.join([str(runtime), str(worktree / "src"), "src", str(tmp_path / "gone")]),
        covered=[str(worktree)],
    )
    assert grants == [
        # Its Scripts directory, not just the launcher: ``python -m ruff`` execs
        # Scripts/ruff.exe (NF-40).
        ContainerGrant(str(venv / "Scripts"), "read_execute", persistent=True),
        ContainerGrant(str(venv / "pyvenv.cfg"), "read_execute", persistent=True),
        ContainerGrant(str(venv / "Lib" / "site-packages"), "read_execute", persistent=True),
        ContainerGrant(str(base), "read_execute", persistent=True),
        # An absolute import root outside the request; the worktree's own
        # (already granted), relative and missing entries add nothing.
        ContainerGrant(str(runtime), "read_execute", persistent=True),
    ]


def test_a_plain_interpreter_needs_its_install_root_and_anything_else_nothing(tmp_path):
    base, _venv_dir = _venv(tmp_path)
    assert wac.python_read_grants(str(base / "python.exe")) == [
        ContainerGrant(str(base), "read_execute", persistent=True)
    ]
    assert wac.python_read_grants("pytest") == []
    assert wac.python_read_grants(str(tmp_path / "ruff.exe")) == []


@pytest.mark.parametrize("planted", ["venv", "home", "plain"])
def test_an_interpreter_the_container_can_write_is_never_granted(tmp_path, planted):
    """The review's steering attack: a worker plants ``.venv`` in its own
    worktree with ``home =`` naming a directory it wants a PERSISTENT grant
    on (another repo, a sibling request's worktree)."""
    worktree, target = tmp_path / "wt", tmp_path / "sibling_request_worktree"
    worktree.mkdir()
    target.mkdir()
    base, venv = _venv(tmp_path)
    if planted == "venv":
        venv = worktree / ".venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "python.exe").write_bytes(b"MZ")
        (venv / "pyvenv.cfg").write_text(f"home = {target}\n", encoding="utf-8")
        executable = venv / "Scripts" / "python.exe"
    elif planted == "home":
        (venv / "pyvenv.cfg").write_text(f"home = {worktree}\n", encoding="utf-8")
        executable = venv / "Scripts" / "python.exe"
    else:
        (worktree / "python.exe").write_bytes(b"MZ")
        executable = worktree / "python.exe"
    with pytest.raises(AppContainerError) as excinfo:
        wac.python_read_grants(str(executable), covered=[str(worktree)])
    assert excinfo.value.reason is AppContainerReason.INVALID_REQUEST


def test_the_python_read_set_passes_grant_validation(tmp_path):
    base, venv = _venv(tmp_path)
    fake = FakeWin32Api()
    launch_appcontainer(
        make_request(
            filesystem_grants=wac.python_read_grants(str(venv / "Scripts" / "python.exe"))
        ),
        api=fake,
    )
    assert _grant_events(fake) == [
        f"grant:read_execute:{path}"
        for path in (
            venv / "Scripts", venv / "pyvenv.cfg",
            venv / "Lib" / "site-packages", base,
        )
    ]


def _mock_system_tree(tmp_path, monkeypatch):
    """``tmp_path/Program Files`` as the system tree, ``tmp_path/profile`` as
    the protected profile."""
    program_files, profile = tmp_path / "Program Files", tmp_path / "profile"
    program_files.mkdir()
    profile.mkdir()
    monkeypatch.setattr(
        wac,
        "_sensitive_roots",
        lambda: ([os.path.normcase(str(profile))], [os.path.normcase(str(program_files))]),
    )
    return program_files, profile


def _interpreter(home):
    home.mkdir(parents=True)
    (home / "python.exe").write_bytes(b"MZ")
    return home / "python.exe"


def test_a_venv_on_a_program_files_python_grants_all_but_the_base(tmp_path, monkeypatch):
    """Python.org's "Install for all users" home is C:\\Program Files\\Python3xx:
    ALL APPLICATION PACKAGES already reads it, so it gets no grant at all."""
    program_files, _profile = _mock_system_tree(tmp_path, monkeypatch)
    _base, venv = _venv(tmp_path)
    home = _interpreter(program_files / "Python312").parent
    (venv / "pyvenv.cfg").write_text(f"home = {home}\n", encoding="utf-8")

    grants = wac.python_read_grants(str(venv / "Scripts" / "python.exe"))
    expected = [venv / "Scripts", venv / "pyvenv.cfg", venv / "Lib" / "site-packages"]
    assert grants == [
        ContainerGrant(str(path), "read_execute", persistent=True) for path in expected
    ]
    fake = FakeWin32Api()
    launch_appcontainer(make_request(filesystem_grants=grants), api=fake)
    assert _grant_events(fake) == [f"grant:read_execute:{path}" for path in expected]
    assert fake.resumed
    # Omitted, never allowed: a request naming it is still refused.
    detail = _refused([ContainerGrant(str(home), "read_execute", persistent=True)], FakeWin32Api())
    assert "Windows or Program Files" in detail


def test_a_plain_program_files_interpreter_gets_no_grant(tmp_path, monkeypatch):
    program_files, _profile = _mock_system_tree(tmp_path, monkeypatch)
    executable = _interpreter(program_files / "Python312")
    assert wac.python_read_grants(str(executable)) == []


def test_a_per_user_interpreter_is_still_granted(tmp_path, monkeypatch):
    _program_files, profile = _mock_system_tree(tmp_path, monkeypatch)
    executable = _interpreter(profile / "AppData" / "Local" / "Programs" / "Python312")
    grants = wac.python_read_grants(str(executable))
    assert grants == [ContainerGrant(str(executable.parent), "read_execute", persistent=True)]
    fake = FakeWin32Api()
    launch_appcontainer(make_request(filesystem_grants=grants), api=fake)
    assert _grant_events(fake) == [f"grant:read_execute:{executable.parent}"]


@requires_host_win32_privileges
@pytest.mark.parametrize("kind", ["directory", "file"])
def test_ctypes_denied_persistent_grant_names_the_one_time_command(
    tmp_path, monkeypatch, kind
):
    target = tmp_path / "Admin Owned Tools"
    target.mkdir()
    requested = str(target) + os.sep  # a trailing \ would escape the closing quote
    command = f'icacls "{target}" /grant "*S-1-15-2-1:(OI)(CI)(RX)" /T'
    if kind == "file":
        target = target / "tool.exe"
        target.write_bytes(b"MZ")
        requested = str(target)
        # Measured: icacls drops an (OI)(CI) ACE on a file yet reports success.
        command = f'icacls "{target}" /grant "*S-1-15-2-1:(RX)"'
    api = _security_api(FakeSecurityLib(set_status=5), monkeypatch, present=False)
    with pytest.raises(_Win32Failure) as excinfo:
        api.grant_path_access(_identity(), requested, "read_execute", persistent=True)

    failure = excinfo.value
    assert (failure.win_error, failure.operation) == (5, "grant_path_access")

    def _raise():
        raise failure

    with pytest.raises(AppContainerError) as surfaced:
        wac._step("grant_path_access", _raise)
    assert surfaced.value.reason is AppContainerReason.FILESYSTEM_GRANT_FAILED
    assert str(surfaced.value) == (
        f"filesystem_grant_failed: cannot add AppContainer read access to {target}: "
        "this user lacks WRITE_DAC there (win_error 5). Run once from an elevated "
        f"shell: {command}"
    )


@pytest.mark.parametrize(
    ("status", "access", "persistent"),
    [(1307, "read_execute", True), (5, "modify", False)],
)
def test_ctypes_other_grant_failures_keep_the_plain_detail(
    tmp_path, monkeypatch, status, access, persistent
):
    api = _security_api(
        FakeSecurityLib(set_status=status), monkeypatch, present=False if persistent else None
    )
    with pytest.raises(_Win32Failure) as excinfo:
        api.grant_path_access(_identity(), str(tmp_path), access, persistent=persistent)
    assert excinfo.value.detail == f"write DACL {tmp_path}"


def test_the_grant_hint_is_bounded_and_keeps_the_command_whole():
    path = "C:\\" + "d" * 200
    hint = wac._all_packages_grant_hint(path)
    assert hint == f'icacls "{path}" /grant "*S-1-15-2-1:(RX)"'
    assert len(hint) <= wac._GRANT_HINT_MAX_CHARS


# -- worker MCP bridge pipe (NF-2026-00034) ------------------------------------


_OWNER = "S-1-5-21-389243392-615521012-1854199069-1001"
_CONTAINER = "S-1-15-2-2390138238-917833039-2980490063-148298555-1665516221-1143707983-1249757738"


def test_worker_pipe_sddl_names_the_owner_and_one_container_only():
    sddl = wac.worker_pipe_sddl(_OWNER, _CONTAINER)
    assert sddl == f"D:P(A;;GA;;;{_OWNER})(A;;GRGW;;;{_CONTAINER})"
    # Protected, two ACEs, and no ALL APPLICATION PACKAGES / Everyone / label.
    assert "S-1-15-2-1)" not in sddl and ";WD)" not in sddl and "S:" not in sddl
    for bad in ("WD", "S-1-15-2-1)(A;;GA;;;WD", "", "S-1-15"):
        with pytest.raises(ValueError):
            wac.worker_pipe_sddl(_OWNER, bad)


def test_worker_pipe_names_are_per_request_unguessable_and_quote_safe():
    first = wac.new_worker_pipe_name("3a2b" * 8)
    second = wac.new_worker_pipe_name("3a2b" * 8)
    assert first != second
    assert first.startswith("\\\\.\\pipe\\aiworkhub-worker-" + "3a2b" * 8 + "-")
    for hostile in ("a'b", "a b", "..\\x", "", "a" * 200):
        with pytest.raises(ValueError):
            wac.new_worker_pipe_name(hostile)


@pytest.mark.skipif(os.name != "nt", reason="resolves the real Windows directory")
def test_worker_pipe_shim_is_system32_powershell_with_the_name_encoded():
    import base64

    name = wac.new_worker_pipe_name("req")
    argv = wac.worker_pipe_shim_argv(name)
    assert os.path.normcase(argv[0]) == os.path.normcase(
        os.path.join(os.environ["SYSTEMROOT"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    )
    assert argv[1:5] == ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    script = base64.b64decode(argv[5]).decode("utf-16-le")
    assert f"NamedPipeClientStream]::new('.','{name[9:]}'," in script
    with pytest.raises(ValueError):
        wac.worker_pipe_shim_argv("\\\\.\\pipe\\someone-else")


_PIPE_CLIENT = (
    "import sys\n"
    "sys.stdin.readline()\n"
    "try:\n"
    "    f=open(sys.argv[1],'r+b',buffering=0)\n"
    "    f.write(b'ping')\n"
    "    print('got',f.read(64),flush=True)\n"
    "except OSError as e:\n"
    "    print('refused',type(e).__name__,flush=True)\n"
)


def _pipe_sddl(pipe):
    from ctypes import wintypes

    a = ctypes.WinDLL("advapi32", use_last_error=True)
    a.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD] + [
        ctypes.POINTER(wintypes.LPVOID)
    ] * 5
    a.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR), wintypes.LPVOID,
    ]
    descriptor, text = wintypes.LPVOID(), wintypes.LPWSTR()
    assert a.GetSecurityInfo(pipe._handle, 6, 4, None, None, None, None, ctypes.byref(descriptor)) == 0
    assert a.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, ctypes.byref(text), None)
    return text.value


@requires_host_win32_privileges
@pytest.mark.skipif(os.name != "nt", reason="real named pipe and job object")
def test_worker_pipe_serves_only_a_client_of_the_job_and_leaves_nothing(tmp_path):
    import subprocess
    import sys
    import threading

    from aiworkhub.worker_supervisor import _WindowsKillOnCloseJob

    name = wac.new_worker_pipe_name("pytest")
    pipe = wac.WorkerPipe(name, _CONTAINER)
    owner = wac._token_user_sid(*wac._pipe_libraries())
    # FA/0x12019f are GA and GRGW mapped onto a pipe: owner + one container.
    assert _pipe_sddl(pipe) == f"D:P(A;;FA;;;{owner})(A;;0x12019f;;;{_CONTAINER})"
    with pytest.raises(wac._Win32Failure):
        wac.WorkerPipe(name, _CONTAINER)  # first instance only: no squatting

    job = _WindowsKillOnCloseJob()
    client = [sys.executable, "-c", _PIPE_CLIENT, name]
    stranger = subprocess.Popen(client, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    ours = subprocess.Popen(client, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    job.assign(ours)
    accepted = {}
    server = threading.Thread(target=lambda: accepted.setdefault("ok", pipe.accept(job._handle)))
    server.start()
    try:
        stranger.communicate(b"go\n", timeout=20)  # connected, then disconnected unserved
        ours.stdin.write(b"go\n")
        ours.stdin.flush()
        server.join(20)
        assert accepted == {"ok": True}
        assert pipe.read() == b"ping"
        pipe.write(b"pong")
        assert b"got b'pong'" in ours.communicate(timeout=20)[0]
        assert pipe.read() == b""  # client gone
    finally:
        assert pipe.close()
        job.close()
    with pytest.raises(FileNotFoundError):
        open(name, "r+b", buffering=0)  # no leftover pipe


@requires_host_win32_privileges
@pytest.mark.skipif(os.name != "nt", reason="real named pipe")
def test_worker_pipe_accept_gives_up_at_its_timeout():
    import time

    pipe = wac.WorkerPipe(wac.new_worker_pipe_name("pytest"), _CONTAINER)
    started = time.monotonic()
    assert pipe.accept(0, timeout=0.3) is False
    assert 0.2 < time.monotonic() - started < 5
    assert pipe.close()


@requires_host_win32_privileges
@pytest.mark.skipif(os.name != "nt", reason="real named pipe")
def test_worker_pipe_shutdown_releases_a_waiting_accept():
    import threading

    pipe = wac.WorkerPipe(wac.new_worker_pipe_name("pytest"), _CONTAINER)
    result = {}
    waiter = threading.Thread(target=lambda: result.setdefault("ok", pipe.accept(0)))
    waiter.start()
    pipe.shutdown()
    waiter.join(10)
    assert result == {"ok": False}
    assert pipe.read() == b""
    with pytest.raises(BrokenPipeError):
        pipe.write(b"x")
    assert pipe.close() and pipe.close()


@pytest.mark.parametrize("protected", [False, True])
def test_ctypes_dacl_protected_only_reads(tmp_path, monkeypatch, protected):
    lib = FakeSecurityLib(protected=protected)
    api = _security_api(lib, monkeypatch)
    assert api.dacl_protected(str(tmp_path)) is protected
    assert lib.entries == [] and lib.set_calls == []
    assert lib.freed == [111]


# -- ALL APPLICATION PACKAGES satisfies a persistent read grant (NF-2026-00034) --


_AAP = wac._ALL_APPLICATION_PACKAGES_SID
_RX = 0x1200A9


def _ace(sid, flags, mask=_RX, ace_type=0):
    return wac.AclAce(ace_type, flags, mask, sid, b"")


@pytest.mark.parametrize(
    ("aces", "inherit", "expected"),
    [
        # icacls C:\Python312 /grant *S-1-15-2-1:(OI)(CI)(RX): the owner's command.
        ([_ace(_AAP, 0x03)], 0x3, "all_application_packages"),
        ([_ace(_AAP, 0x13)], 0x3, "all_application_packages"),  # inherited
        ([_ace(_AAP, 0x10)], 0x0, "all_application_packages"),  # file, inherited
        ([_ace(_SID, 0x03)], 0x3, "container_sid"),
        ([_ace(_AAP, 0x0B)], 0x3, ""),  # inherit-only: not this directory
        ([_ace(_AAP, 0x07)], 0x3, ""),  # no-propagate: stops below children
        ([_ace(_AAP, 0x00)], 0x3, ""),  # this directory only
        ([_ace(_AAP, 0x03, mask=0x120089)], 0x3, ""),  # read without execute
        ([_ace(_AAP, 0x03, ace_type=1)], 0x3, ""),  # a deny ACE grants nothing
        ([_ace(_SID, 0x13)], 0x3, ""),  # this SID's ACE, but only inherited
        ([], 0x3, ""),
        # Stored order decides, as in the kernel: a deny first wins ...
        ([_ace(_SID, 0x03, mask=0x1, ace_type=1), _ace(_AAP, 0x03)], 0x3, ""),
        ([_ace(_AAP, 0x13, mask=0x20, ace_type=1), _ace(_AAP, 0x03)], 0x3, ""),
        # ... an allow before it has already granted the bits ...
        ([_ace(_AAP, 0x03), _ace(_SID, 0x03, ace_type=1)], 0x3, "all_application_packages"),
        # ... and a deny for someone else, or for other bits, is not relevant.
        ([_ace(b"\x01\x01" + b"\x00" * 10, 0x03, ace_type=1), _ace(_AAP, 0x03)], 0x3,
         "all_application_packages"),
        ([_ace(_SID, 0x03, mask=0x40000, ace_type=1), _ace(_AAP, 0x03)], 0x3,
         "all_application_packages"),
    ],
)
def test_satisfying_trustee(aces, inherit, expected):
    assert wac._satisfying_trustee(aces, _SID, _RX, inherit) == expected


def _aap_snapshot(monkeypatch, *aces):
    snapshot = wac.AclSnapshot("x", wac.DaclState.PRESENT, False, tuple(aces), b"\x02" * 8)
    monkeypatch.setattr(wac, "snapshot_filesystem_acl", lambda _path: snapshot)


def _real_check_api(lib):
    api = make_ctypes_api(lib)
    api._advapi32 = lib
    return api


def test_ctypes_persistent_grant_satisfied_by_all_packages_writes_nothing(
    tmp_path, monkeypatch
):
    _aap_snapshot(monkeypatch, _ace(_AAP, 0x03))
    lib = FakeSecurityLib()
    grant = _real_check_api(lib).grant_path_access(
        _identity(), str(tmp_path), "read_execute", persistent=True
    )
    assert grant.satisfied_by == "all_application_packages"
    assert grant.restore is None
    assert lib.entries == [] and lib.set_calls == []


def test_ctypes_unsatisfied_persistent_grant_writes_this_sid_never_all_packages(
    tmp_path, monkeypatch
):
    _aap_snapshot(monkeypatch, _ace(_AAP, 0x0B))  # inherit-only: not enough
    lib = FakeSecurityLib()
    grant = _real_check_api(lib).grant_path_access(
        _identity(), str(tmp_path), "read_execute", persistent=True
    )
    assert grant.satisfied_by == ""
    assert [entry[5] for entry in lib.entries] == [_SID]
    assert len(lib.set_calls) == 1


def test_launch_records_how_each_persistent_grant_was_satisfied(tmp_path):
    root, written = tmp_path / "python", tmp_path / "npm"
    root.mkdir()
    written.mkdir()

    class SatisfiedApi(FakeWin32Api):
        def grant_path_access(self, identity, path, access, *, persistent=False):
            applied = super().grant_path_access(identity, path, access, persistent=persistent)
            if path == str(root):
                applied.satisfied_by = "all_application_packages"
            return applied

    launch = launch_appcontainer(
        make_request(
            filesystem_grants=[
                ContainerGrant(str(root), "read_execute", persistent=True),
                ContainerGrant(str(written), "read_execute", persistent=True),
            ]
        ),
        api=SatisfiedApi(),
    )
    assert launch.cleanup_evidence()["persistent_grants"] == [
        {"path": str(root), "satisfied_by": "all_application_packages"},
        {"path": str(written), "satisfied_by": "granted"},
    ]
    assert launch.grants == []


# ---------------------------------------------------------------------------
# NF-40: the sitecustomize every Python in the validation container loads
# ---------------------------------------------------------------------------


def _appcontainer_site(name="aiworkhub_appcontainer_site_under_test"):
    """The shim, loaded under a name other than ``sitecustomize``: its
    functions, without it patching this test process."""
    import importlib.util

    path = os.path.join(wac.APPCONTAINER_PYTHON_SITE, "sitecustomize.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_site_directory_holds_only_the_shim():
    """Everything in it is imported by every validation Python: keep it one file."""
    assert sorted(
        entry for entry in os.listdir(wac.APPCONTAINER_PYTHON_SITE) if entry != "__pycache__"
    ) == ["sitecustomize.py"]


def test_is_python_executable():
    assert wac.is_python_executable(r"C:\venv\Scripts\python.exe")
    assert wac.is_python_executable(r"C:\Python312\python3.12.EXE")
    assert not wac.is_python_executable(r"C:\venv\Scripts\ruff.exe")
    assert not wac.is_python_executable("python")
    assert not wac.is_python_executable(r"C:\Program Files\Git\mingw64\bin\git.exe")


windows_only = pytest.mark.skipif(os.name != "nt", reason="exercises the real Win32 path APIs")


@windows_only
def test_the_shim_patches_nothing_unless_it_is_sitecustomize():
    import ntpath

    mkdir, final = os.mkdir, ntpath._getfinalpathname
    _appcontainer_site()
    assert os.mkdir is mkdir
    assert ntpath._getfinalpathname is final


@requires_host_win32_privileges
@windows_only
def test_the_shim_mkdir_0o700_inherits_the_parent_dacl(tmp_path):
    """CPython >= 3.12.4 makes a 0o700 directory with a protected DACL that no
    container SID is on; the shim's directory inherits its parent's instead."""
    site = _appcontainer_site()
    inherited = 0x10  # INHERITED_ACE

    os.mkdir(tmp_path / "host", 0o700)
    host = wac.snapshot_filesystem_acl(str(tmp_path / "host"))
    site._mkdir_inheriting(tmp_path / "shim", 0o700)
    shim = wac.snapshot_filesystem_acl(str(tmp_path / "shim"))

    assert host.aces and not any(ace.flags & inherited for ace in host.aces)
    assert shim.aces and all(ace.flags & inherited for ace in shim.aces)


@windows_only
def test_the_shim_final_path_matches_the_host_when_the_volume_lookup_is_denied(
    tmp_path, monkeypatch
):
    """In the container GetFinalPathNameByHandleW(VOLUME_NAME_DOS) is denied for
    every path; the rebuilt name must equal what the host call returns."""
    import ntpath

    site = _appcontainer_site()
    (tmp_path / "Dir").mkdir()
    (tmp_path / "Dir" / "File.txt").write_text("x", encoding="utf-8")
    paths = [str(tmp_path / "Dir"), str(tmp_path / "dir" / "file.TXT")]
    expected = [ntpath._getfinalpathname(path) for path in paths]

    def _denied(path):
        raise PermissionError(5, "Access is denied", path)

    monkeypatch.setattr(site, "_getfinalpathname_host", _denied)
    assert [site._getfinalpathname(path) for path in paths] == expected


@windows_only
def test_the_shim_final_path_never_names_a_different_file(tmp_path, monkeypatch):
    """The rebuilt path is returned only if it is the very same file; otherwise
    the container's original error stands."""
    site = _appcontainer_site()
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    other = site._volume_relative_name(str(tmp_path / "b.txt"))

    def _denied(path):
        raise PermissionError(5, "Access is denied", path)

    monkeypatch.setattr(site, "_getfinalpathname_host", _denied)
    monkeypatch.setattr(site, "_volume_relative_name", lambda _path: other)
    with pytest.raises(PermissionError):
        site._getfinalpathname(str(tmp_path / "a.txt"))
    # Nothing to rebuild from: a missing path, and a path without a drive letter.
    monkeypatch.undo()
    monkeypatch.setattr(site, "_getfinalpathname_host", _denied)
    with pytest.raises(PermissionError):
        site._getfinalpathname(str(tmp_path / "missing.txt"))
    with pytest.raises(PermissionError):
        site._getfinalpathname("\\?\\" + str(tmp_path / "a.txt"))


def test_ancestor_stat_facts_are_the_hosts_lstat_of_each_parent(tmp_path):
    target = tmp_path / "worktrees" / "request"
    target.mkdir(parents=True)
    facts = json.loads(wac.ancestor_stat_facts(str(target)))
    assert list(facts) == [os.path.normcase(str(parent)) for parent in target.parents]
    fields = facts[os.path.normcase(str(tmp_path))]
    real = os.lstat(tmp_path)
    assert fields[:3] == [real.st_mode, real.st_ino, real.st_dev]
    assert os.path.normcase(str(target)) not in facts  # never the request itself


@windows_only
def test_the_shim_reads_the_facts_the_lane_writes():
    assert _appcontainer_site()._ANCESTORS_ENV == wac.APPCONTAINER_ANCESTORS_ENV


@windows_only
def test_the_shim_answers_a_denied_stat_only_for_a_brokered_directory(tmp_path):
    import stat

    site = _appcontainer_site()
    target = tmp_path / "worktrees" / "request"
    target.mkdir(parents=True)
    facts = site._ancestor_facts(wac.ancestor_stat_facts(str(target)))
    rebuilt = facts[os.path.normcase(str(tmp_path))]
    assert os.path.samestat(rebuilt, os.lstat(tmp_path))
    assert stat.S_ISDIR(rebuilt.st_mode) and not stat.S_ISLNK(rebuilt.st_mode)
    assert rebuilt.st_file_attributes == os.lstat(tmp_path).st_file_attributes

    calls = []

    def _denied(path, *args, **kwargs):
        calls.append(path)
        raise PermissionError(5, "Access is denied", str(path))

    brokered = site._brokered(_denied, facts)
    assert brokered(tmp_path) is rebuilt
    assert brokered(str(tmp_path).upper(), follow_symlinks=False) is rebuilt
    for denied in (target, tmp_path / "other", os.fsencode(str(tmp_path)), 3):
        with pytest.raises(PermissionError):
            brokered(denied)
    with pytest.raises(PermissionError):
        brokered(tmp_path, dir_fd=3)

    def _missing(path, *args, **kwargs):
        raise FileNotFoundError(2, "missing", str(path))

    with pytest.raises(FileNotFoundError):  # only a denial is ever answered
        site._brokered(_missing, facts)(tmp_path)
    real = site._brokered(os.lstat, facts)(target)
    assert os.path.samestat(real, os.lstat(target))  # a granted path asks the OS


# ---------------------------------------------------------------------------
# NF-2026-00042: legible launch failures / NF-2026-00964: truthful lane skips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("win_error", "name"),
    [
        (87, "ERROR_INVALID_PARAMETER"),
        (203, "ERROR_ENVVAR_NOT_FOUND"),
        (206, "ERROR_FILENAME_EXCED_RANGE"),
    ],
)
def test_launch_error_names_the_win32_code_and_its_symbol(win_error, name):
    fake = FakeWin32Api(fail_at="create_process", fail_error=win_error)
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(make_request(), api=fake)

    error = excinfo.value
    assert error.win_error == win_error
    assert error.win_error_name == name
    text = str(error)
    assert f"win_error={win_error}" in text
    assert name in text


def test_launch_error_carries_sizes_and_paths_as_text_and_attributes():
    request = make_request(
        argv=["C:\\tools\\claude.exe", "--flag", "value with space"],
        working_directory="C:\\work\\dir",
        environment={"FOO": "bar"},
    )
    fake = FakeWin32Api(fail_at="create_process", fail_error=87)
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(request, api=fake)

    error = excinfo.value
    assert error.executable == "C:\\tools\\claude.exe"
    assert error.command_line_length == len(build_command_line(request.argv))
    assert error.argument_count == len(request.argv)
    # The launch measures the block it actually hands CreateProcessW, which is
    # the request environment after the LOCALAPPDATA chokepoint adds to it.
    launched_environment = wac.appcontainer_child_environment(request.environment)
    assert error.environment_length == len(wac._environment_block_text(launched_environment))
    assert error.working_directory == "C:\\work\\dir"

    text = str(error)
    assert repr(error.executable) in text
    assert f"command_line_length={error.command_line_length}" in text
    assert f"argument_count={error.argument_count}" in text
    assert f"environment_length={error.environment_length}" in text
    assert repr(error.working_directory) in text


def test_launch_error_never_leaks_an_argument_command_line_or_environment_value():
    sentinel_arg = "SENTINEL-ARG-VALUE-zzz"
    sentinel_env_value = "SENTINEL-ENV-VALUE-zzz"
    request = make_request(
        argv=["C:\\tools\\claude.exe", sentinel_arg],
        environment={"SENTINEL_ENV_KEY": sentinel_env_value},
    )
    command_line = build_command_line(request.argv)
    fake = FakeWin32Api(fail_at="create_process", fail_error=87)
    with pytest.raises(AppContainerError) as excinfo:
        launch_appcontainer(request, api=fake)

    text = str(excinfo.value)
    assert sentinel_arg not in text
    assert sentinel_env_value not in text
    assert command_line not in text
    values = repr(vars(excinfo.value))
    assert sentinel_arg not in values
    assert sentinel_env_value not in values
    assert command_line not in values


def test_build_command_line_accepts_32766_and_refuses_32767_characters():
    accepted = build_command_line(["x" * 32766])
    assert len(accepted) == 32766

    with pytest.raises(ValueError) as excinfo:
        build_command_line(["x" * 32767])
    assert str(excinfo.value) == "command_line_too_long:32767"


class _FakeTokenDll:
    """kernel32/advapi32 stand-in for a faked TokenIsAppContainer query.

    Methods are plain closures assigned per-instance (never ``def`` class
    methods): a bound method cannot take the ``.argtypes``/``.restype``
    attribute assignments the production code performs on every DLL function
    it calls, real or faked.
    """

    def __init__(self, *, is_appcontainer=False, open_ok=True, query_ok=True):
        def _open_process_token(process, access, token_ref):
            if not open_ok:
                return False
            token_ref._obj.value = 99
            return True

        def _get_token_information(token, info_class, value_ref, size, size_ref):
            if not query_ok:
                return False
            value_ref._obj.value = int(is_appcontainer)
            size_ref._obj.value = size
            return True

        self.GetCurrentProcess = lambda: 7
        self.CloseHandle = lambda token: True
        self.OpenProcessToken = _open_process_token
        self.GetTokenInformation = _get_token_information


def test_current_process_is_appcontainer_reflects_a_faked_token_query(monkeypatch):
    monkeypatch.setattr(wac.os, "name", "nt")

    monkeypatch.setattr(
        wac, "_load_windows_dll", lambda name: _FakeTokenDll(is_appcontainer=True)
    )
    assert wac.current_process_is_appcontainer() is True

    monkeypatch.setattr(
        wac, "_load_windows_dll", lambda name: _FakeTokenDll(is_appcontainer=False)
    )
    assert wac.current_process_is_appcontainer() is False


def test_current_process_is_appcontainer_is_false_when_the_token_cannot_be_read(
    monkeypatch,
):
    monkeypatch.setattr(wac.os, "name", "nt")

    monkeypatch.setattr(wac, "_load_windows_dll", lambda name: _FakeTokenDll(open_ok=False))
    assert wac.current_process_is_appcontainer() is False

    monkeypatch.setattr(wac, "_load_windows_dll", lambda name: _FakeTokenDll(query_ok=False))
    assert wac.current_process_is_appcontainer() is False


def test_current_process_is_appcontainer_is_false_off_windows(monkeypatch):
    monkeypatch.setattr(wac.os, "name", "posix")
    assert wac.current_process_is_appcontainer() is False


_PRIVILEGED_LANE_SKIP_TEST_NAMES = {
    "test_protected_trees_come_from_the_token_not_the_request_env",
    "test_ctypes_denied_persistent_grant_names_the_one_time_command",
    "test_worker_pipe_serves_only_a_client_of_the_job_and_leaves_nothing",
    "test_worker_pipe_accept_gives_up_at_its_timeout",
    "test_worker_pipe_shutdown_releases_a_waiting_accept",
    "test_the_shim_mkdir_0o700_inherits_the_parent_dacl",
}


def test_the_appcontainer_lane_skip_marks_exactly_the_seven_privileged_tests():
    import sys

    module = sys.modules[__name__]
    marked = {
        name
        for name, obj in vars(module).items()
        if name.startswith("test_")
        and callable(obj)
        and requires_host_win32_privileges.mark in getattr(obj, "pytestmark", [])
    }
    assert marked == _PRIVILEGED_LANE_SKIP_TEST_NAMES

    # One of the six is parametrized with two cases, making seven collected
    # items in total -- the exact seven the objective names.
    parametrized = module.test_ctypes_denied_persistent_grant_names_the_one_time_command
    parametrize_marks = [m for m in parametrized.pytestmark if m.name == "parametrize"]
    assert len(parametrize_marks) == 1
    assert list(parametrize_marks[0].args[1]) == ["directory", "file"]
