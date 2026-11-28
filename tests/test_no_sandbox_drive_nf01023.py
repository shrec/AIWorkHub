"""NF-2026-01023: AIWorkHub never creates a virtual drive letter.

Owner decision 2026-11-28: no DefineDosDeviceW drive, subst, junction or
mount drive.  AppContainer children run on real worktree paths; the only
DefineDosDeviceW call left is the one-time pop of what the removed sandbox
drive recorded in ``<repo>\\.aiworkhub\\runtime\\sandbox-drive-leases``.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import aiworkhub.windows_appcontainer as wac
from aiworkhub.windows_appcontainer import (
    AppContainerRequest,
    _AttributeList,
    _Identity,
    _PathGrant,
    _ProcessCreation,
    _SecurityCapabilities,
    launch_appcontainer,
    request_scoped_grants,
)

_PACKAGE = Path(wac.__file__).resolve().parent
_CLEANUP_FUNCTIONS = {"reclaim_sandbox_drive_leases", "_cleanup_kernel32"}


def _define_dos_device_uses(path: Path) -> list[tuple[str, int, str, ast.AST]]:
    """Every ``*.DefineDosDeviceW`` attribute in ``path``: (file, line,
    enclosing function, the node's parent call or the attribute itself)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    uses: list[tuple[str, int, str, ast.AST]] = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = function
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "DefineDosDeviceW"
            ):
                uses.append((path.name, child.lineno, name, child))
            elif isinstance(child, ast.Attribute) and child.attr == "DefineDosDeviceW":
                uses.append((path.name, child.lineno, name, child))
            visit(child, name)

    visit(tree, "<module>")
    return uses


def test_no_production_module_defines_a_dos_device_except_the_cleanup_pop():
    calls: list[tuple[str, int, str, ast.AST]] = []
    others: list[tuple[str, int, str]] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        for file_name, line, function, node in _define_dos_device_uses(path):
            if isinstance(node, ast.Call):
                calls.append((file_name, line, function, node))
            elif not (
                file_name == "windows_appcontainer.py" and function in _CLEANUP_FUNCTIONS
            ):
                others.append((file_name, line, function))

    # Signature setup outside the cleanup, a defining call, or any caller in
    # another module would be a drive mechanism; there must be none.
    assert others == []
    assert [(f, fn) for f, _l, fn, _n in calls] == [
        ("windows_appcontainer.py", "reclaim_sandbox_drive_leases")
    ]
    (_file, _line, _function, call) = calls[0]
    flags = call.args[0]
    assert isinstance(flags, ast.Name) and flags.id == "SANDBOX_DRIVE_CLEANUP_POP_FLAGS"
    # DDD_REMOVE_DEFINITION | DDD_RAW_TARGET_PATH | DDD_EXACT_MATCH_ON_REMOVE
    # | DDD_NO_BROADCAST_SYSTEM: a pop of one exact target, never a define.
    assert wac.SANDBOX_DRIVE_CLEANUP_POP_FLAGS == 0x2 | 0x1 | 0x4 | 0x8
    # The drive mechanism and its private-session hop are gone.
    for name in (
        "_choose_sandbox_drive",
        "_SandboxDriveLeases",
        "_on_sandbox_drive",
        "_SANDBOX_DRIVE_MUTEX",
        "_SANDBOX_DRIVE_LETTERS",
        "in_private_logon_session",
    ):
        assert not hasattr(wac, name), name
    assert not hasattr(wac._CtypesWin32Api, "sandbox_drive")
    assert not hasattr(wac._CtypesWin32Api, "release_sandbox_drive")
    assert "PRIVATE_LOGON_SESSION_REQUIRED" not in wac.AppContainerReason.__members__
    session_source = (_PACKAGE / "windows_private_session.py").read_text(encoding="utf-8")
    assert "CreateProcessWithLogonW" not in session_source
    supervisor_source = (_PACKAGE / "worker_supervisor.py").read_text(encoding="utf-8")
    assert "reexec_supervisor_if_owner_session" not in supervisor_source
    workspace_source = (_PACKAGE / "worker_workspace.py").read_text(encoding="utf-8")
    assert "run_appcontainer_validation_hopped" not in workspace_source


class _RecordingApi:
    """The Win32Api boundary, recorded; it has no drive operation at all."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.spec = None
        self._n = 100

    def _token(self) -> int:
        self._n += 1
        return self._n

    def derive_identity(self, name, display_name, description):
        self.identity = _Identity(name, display_name, "S-1-15-2-9", self._token(), True)
        return self.identity

    def free_identity(self, identity):
        self.events.append("free_identity")

    def grant_path_access(self, identity, path, access, *, persistent=False):
        self.events.append(f"grant:{access}:{path}")
        return _PathGrant(path, access, None if persistent else ("dacl", path))

    def revoke_path_access(self, grant):
        self.events.append(f"revoke:{grant.path}")
        grant.restore = None

    def dacl_protected(self, path):
        return False

    def build_security_capabilities(self, identity, capability_sids):
        return _SecurityCapabilities(identity.sid_string, None)

    def free_security_capabilities(self, sec_caps):
        pass

    def create_job_object(self, name):
        return self._token()

    def configure_job_object(self, job):
        pass

    def init_attribute_list(self, attribute_count):
        return _AttributeList(self._token(), [], attribute_count)

    def set_security_capabilities(self, attrs, sec_caps):
        pass

    def set_inherited_handles(self, attrs, handles):
        pass

    def delete_attribute_list(self, attrs):
        pass

    def create_process(self, spec):
        self.spec = spec
        return _ProcessCreation(4321, 8765, self._token(), self._token())

    def assign_process_to_job(self, job, creation):
        pass

    def resume_thread(self, creation):
        pass

    def terminate_process(self, creation):
        pass

    def close_thread_handle(self, creation):
        pass

    def close_process_handle(self, creation):
        pass

    def wait_process(self, creation, timeout_ms):
        return False

    def get_process_exit_code(self, creation):
        return 0

    def terminate_job(self, job, exit_code=1):
        pass

    def close_job(self, job):
        pass


class _FakeKernel32:
    """QueryDosDeviceW / DefineDosDeviceW over per-letter stacks, recorded.

    A removal (DDD_REMOVE_DEFINITION) with DDD_RAW_TARGET_PATH and
    DDD_EXACT_MATCH_ON_REMOVE pops only a definition whose raw target is the
    exact string passed; anything else would be a define, which is recorded
    and refused.
    """

    def __init__(self, devices: dict[str, list[str]]) -> None:
        self.devices = {drive: list(stack) for drive, stack in devices.items()}
        self.calls: list[tuple[int, str, str]] = []

    def QueryDosDeviceW(self, name, buffer, size):
        stack = self.devices.get(name) or []
        if not stack:
            return 0
        blob = "\0".join(reversed(stack)) + "\0\0"
        if len(blob) > size:
            return 0
        buffer[: len(blob)] = blob
        return len(blob)

    def DefineDosDeviceW(self, flags, name, target):
        self.calls.append((flags, name, target))
        if not flags & 0x2:
            return False  # a define: never expected
        stack = self.devices.get(name) or []
        if flags & 0x4 and target not in stack:
            return False
        stack.remove(target)
        return True


def test_a_production_launch_defines_no_drive_and_passes_the_real_worktree(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    request_dir = repo / ".aiworkhub" / "runtime" / "worktrees" / "req1"
    worktree = request_dir / "worktree"
    home = request_dir / "home"
    worktree.mkdir(parents=True)
    home.mkdir()
    kernel = _FakeKernel32({})
    # Anything in the launch path that reached for kernel32's DOS devices
    # would land here.
    monkeypatch.setattr(wac, "_cleanup_kernel32", lambda: kernel)
    env = {"HOME": str(home), "PLAIN": "value"}
    argv = [str(worktree / "tool.exe"), f"--cwd={worktree}"]
    api = _RecordingApi()

    launch = launch_appcontainer(
        AppContainerRequest(
            argv=argv,
            repo_id="repo_1023",
            worker_kind="claude_cli",
            working_directory=str(worktree),
            environment=env,
            filesystem_grants=request_scoped_grants(env, str(worktree)),
        ),
        api=api,
    )
    launch.close()

    assert kernel.calls == []
    assert not any("drive" in event for event in api.events)
    assert api.spec.working_directory == str(worktree)
    assert api.spec.executable == str(worktree / "tool.exe")
    assert api.spec.command_line == wac.build_command_line(argv)
    assert api.spec.environment["HOME"] == str(home)
    assert "sandbox_drive" not in json.dumps(launch.cleanup_evidence())

    # NF-2026-01023 manager feedback: the grant plan never names the
    # repository root or any ancestor of it -- only AIWorkHub-owned paths
    # under <repo>\.aiworkhub\runtime are written.
    runtime = os.path.normcase(str(repo / ".aiworkhub" / "runtime"))
    granted = [
        os.path.normcase(event.split(":", 2)[2])
        for event in api.events
        if event.startswith("grant:")
    ]
    assert granted
    forbidden = {os.path.normcase(str(p)) for p in (repo, *repo.parents)}
    forbidden |= {os.path.normcase(str(repo / ".aiworkhub"))}
    assert not set(granted) & forbidden
    assert all(path.startswith(runtime + os.sep) for path in granted)
    plan = request_scoped_grants({"HOME": str(home), "TMP": str(home)}, str(worktree))
    for grant in plan:
        key = os.path.normcase(os.path.normpath(grant.path))
        assert key not in forbidden
        assert key.startswith(runtime + os.sep)


def test_lease_cleanup_pops_only_exact_targets_under_the_worktrees_root(tmp_path):
    repo = tmp_path / "repo"
    runtime = repo / ".aiworkhub" / "runtime"
    worktrees = runtime / "worktrees"
    worktrees.mkdir(parents=True)
    leases = runtime / wac.SANDBOX_DRIVE_LEASE_DIRECTORY
    leases.mkdir()
    ours = "\\??\\" + str(worktrees)
    other_repo = "\\??\\" + str(tmp_path / "other" / ".aiworkhub" / "runtime" / "worktrees")
    kernel = _FakeKernel32(
        {
            # Two stacked pushes of this repo's root on Z:, one foreign one under it.
            "Z:": [r"\??\C:\keep-me", ours, ours],
            "Y:": [other_repo],
            "X:": [r"\??\C:\Windows"],
        }
    )

    def lease(name: str, drive: str, root: str) -> None:
        (leases / f"{name * 32}.lease").write_text(
            json.dumps(
                {
                    "schema": "aiworkhub.sandbox_drive_lease.v1",
                    "drive": drive,
                    "real_root": root,
                    "pid": 1234,
                    "start_ticks": 5,
                }
            ),
            encoding="utf-8",
        )

    lease("a", "Z:", str(worktrees))
    lease("b", "Z:", str(worktrees))  # same push recorded twice
    lease("c", "Y:", str(tmp_path / "other" / ".aiworkhub" / "runtime" / "worktrees"))
    lease("d", "X:", r"C:\Windows")
    (leases / "not-a-lease.txt").write_text("{}", encoding="utf-8")

    result = wac.reclaim_sandbox_drive_leases(repo, kernel32=kernel)

    flags = wac.SANDBOX_DRIVE_CLEANUP_POP_FLAGS
    assert kernel.calls == [(flags, "Z:", ours), (flags, "Z:", ours)]
    assert result.popped == (("Z:", ours), ("Z:", ours))
    assert result.failures == ()
    assert kernel.devices["Z:"] == [r"\??\C:\keep-me"]
    assert kernel.devices["Y:"] == [other_repo]
    assert kernel.devices["X:"] == [r"\??\C:\Windows"]
    assert sorted(result.skipped) == sorted(
        [f"{'c' * 32}.lease", f"{'d' * 32}.lease", "not-a-lease.txt"]
    )
    assert result.directory_removed
    assert not leases.exists()
    assert worktrees.is_dir()

    # Idempotent: nothing left to do, nothing touched.
    again = wac.reclaim_sandbox_drive_leases(repo, kernel32=kernel)
    assert again == wac.SandboxDriveCleanup()
    assert len(kernel.calls) == 2


def test_lease_cleanup_keeps_the_directory_when_a_pop_fails(tmp_path):
    repo = tmp_path / "repo"
    runtime = repo / ".aiworkhub" / "runtime"
    worktrees = runtime / "worktrees"
    worktrees.mkdir(parents=True)
    leases = runtime / wac.SANDBOX_DRIVE_LEASE_DIRECTORY
    leases.mkdir()
    (leases / f"{'e' * 32}.lease").write_text(
        json.dumps({"drive": "Z:", "real_root": str(worktrees)}), encoding="utf-8"
    )

    class _Refusing(_FakeKernel32):
        def DefineDosDeviceW(self, flags, name, target):
            self.calls.append((flags, name, target))
            return False

    kernel = _Refusing({"Z:": ["\\??\\" + str(worktrees)]})
    result = wac.reclaim_sandbox_drive_leases(repo, kernel32=kernel)

    assert result.popped == ()
    assert len(result.failures) == 1
    assert not result.directory_removed
    assert leases.is_dir()
