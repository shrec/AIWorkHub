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

import pytest

import aiworkhub.windows_appcontainer as wac
from aiworkhub.windows_appcontainer import (
    AppContainerError,
    AppContainerLifecycleState,
    AppContainerReason,
    AppContainerRequest,
    _AttributeList,
    _Identity,
    _ProcessCreation,
    _SecurityCapabilities,
    _Win32Failure,
    build_command_line,
    launch_appcontainer,
)


# ---------------------------------------------------------------------------
# Mocked Windows boundary
# ---------------------------------------------------------------------------


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
