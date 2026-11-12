"""NF-2026-02239: worker /dev/shm SemLock rule and the exported CPU budget.

Ruleset/argv/env construction is unit-tested on every OS with the landlock
syscalls stubbed; the one test that really opens a SemLock under landlock is
skipped off Linux (and where the host lacks landlock or a real /dev/shm).
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import (
    platform_io,
    process_launcher,
    runtime_adapters,
    sandbox_cpu_budget,
    validation_runner,
    worker_workspace,
)
from aiworkhub.worker_workspace import WorkspaceError

_SEMLOCK_RIGHTS = (
    worker_workspace._LL_WRITE_FILE
    | worker_workspace._LL_MAKE_REG
    | worker_workspace._LL_REMOVE_FILE
)
_WORKER_FLAG = sandbox_cpu_budget.WORKER_SEMLOCK_DEV_SHM_FLAG
_VALIDATION_FLAG = sandbox_cpu_budget.VALIDATION_SEMLOCK_DEV_SHM_FLAG
_BUDGET = sandbox_cpu_budget.CPU_BUDGET_ENV
_POOL_SOURCE = (
    "import multiprocessing as mp\n"
    "lock = mp.Lock()\n"
    "lock.acquire()\n"
    "lock.release()\n"
    "with mp.get_context('fork').Pool(2) as pool:\n"
    "    assert pool.map(abs, [-1, -2, -3]) == [1, 2, 3]\n"
    "print('semlock-ok')\n"
)


def _workspace(tmp_path: Path) -> worker_workspace.WorkerWorkspace:
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    worktree.mkdir()
    home.mkdir(mode=0o700)
    (home / "tmp").mkdir(mode=0o700)
    return worker_workspace.WorkerWorkspace(
        request_id=f"nf02239-{os.getpid()}-{tmp_path.name}",
        repo=tmp_path,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )


def _stub_landlock_syscalls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, int]]:
    """Record ``_apply_landlock``'s rules without confining the test runner."""
    import ctypes

    granted: list[tuple[Path, int]] = []

    class _FakeLibc:
        def syscall(self, number: int, *args: object) -> int:
            if number == worker_workspace._LANDLOCK_CREATE_RULESET:
                return os.open(os.devnull, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            return 0

        def prctl(self, *args: object) -> int:
            return 0

    monkeypatch.setattr(worker_workspace, "landlock_abi_version", lambda: 3)
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: _FakeLibc())
    monkeypatch.setattr(
        worker_workspace,
        "_landlock_add_path_rule",
        lambda libc, fd, path, rights: granted.append((Path(path), rights)),
    )
    return granted


def _gate(monkeypatch: pytest.MonkeyPatch, root: str | None) -> None:
    monkeypatch.setattr(validation_runner, "landlock_semlock_dev_shm_root", lambda *a: root)


def _argv(
    workspace: worker_workspace.WorkerWorkspace, adapter_id: str, **kwargs: object
) -> list[str]:
    return worker_workspace.sandbox_argv(
        workspace,
        adapter_id,
        [sys.executable, "-c", "pass"],
        backend="landlock",
        **kwargs,  # type: ignore[arg-type]
    )


class TestFlagDecision:
    def test_worker_launch_gains_worker_flag_under_the_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gate(monkeypatch, "/dev/shm")
        flags = sandbox_cpu_budget.landlock_semlock_dev_shm_flags(False, None)
        assert flags == [_WORKER_FLAG, "/dev/shm"]

    def test_gate_absent_means_no_rule_for_any_lane(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gate(monkeypatch, None)
        decide = sandbox_cpu_budget.landlock_semlock_dev_shm_flags
        assert decide(False, None) == []
        assert decide(True, tmp_path) == []

    def test_validation_lane_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gate(monkeypatch, "/dev/shm")
        decide = sandbox_cpu_budget.landlock_semlock_dev_shm_flags
        assert decide(True, tmp_path) == [_VALIDATION_FLAG, "/dev/shm"]
        assert decide(False, tmp_path) == []
        with pytest.raises(WorkspaceError, match="requires_validation"):
            decide(True, None)

    def test_gate_is_the_single_existing_judgement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def gate(*_args: object) -> str:
            calls.append(1)
            return "/dev/shm"

        monkeypatch.setattr(validation_runner, "landlock_semlock_dev_shm_root", gate)
        sandbox_cpu_budget.landlock_semlock_dev_shm_flags(False, None)
        assert calls == [1]


class TestArgvComposition:
    def test_provider_argv_carries_worker_flag_before_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gate(monkeypatch, "/dev/shm")
        argv = _argv(_workspace(tmp_path), "claude_cli")
        index = argv.index(_WORKER_FLAG)
        assert argv[index + 1] == "/dev/shm"
        assert index < argv.index("--")
        assert _VALIDATION_FLAG not in argv

    def test_provider_argv_without_gate_carries_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gate(monkeypatch, None)
        argv = _argv(_workspace(tmp_path), "claude_cli")
        assert _WORKER_FLAG not in argv
        assert _VALIDATION_FLAG not in argv

    def test_validation_argv_never_carries_worker_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gate(monkeypatch, "/dev/shm")
        workspace = _workspace(tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        plain = _argv(workspace, "validation", validation_exec_scratch=scratch)
        assert _WORKER_FLAG not in plain and _VALIDATION_FLAG not in plain
        needed = _argv(
            workspace,
            "validation",
            validation_exec_scratch=scratch,
            validation_semlock_dev_shm=True,
        )
        assert _WORKER_FLAG not in needed
        assert needed[needed.index(_VALIDATION_FLAG) + 1] == "/dev/shm"

    def test_appcontainer_argv_is_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def gate(*_args: object) -> str:
            raise AssertionError("AppContainer must not consult the landlock gate")

        monkeypatch.setattr(validation_runner, "landlock_semlock_dev_shm_root", gate)
        monkeypatch.setattr(
            worker_workspace,
            "windows_confinement_report",
            lambda *a, **k: {"available": True, "reason": ""},
        )
        adapter_argv = ["C:\\tools\\claude.exe", "--print", "x"]
        argv = worker_workspace.sandbox_argv(
            _workspace(tmp_path),
            "claude_cli",
            adapter_argv,
            backend=worker_workspace.WINDOWS_APPCONTAINER_BACKEND,
        )
        assert argv == adapter_argv
        assert argv is not adapter_argv


class TestRulesetComposition:
    def _dirs(self, tmp_path: Path) -> tuple[worker_workspace.WorkerWorkspace, Path, Path]:
        workspace = _workspace(tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        shm = tmp_path / "shm"
        shm.mkdir()
        return workspace, scratch, shm

    def test_worker_ruleset_contains_the_same_dev_shm_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace, _scratch, shm = self._dirs(tmp_path)
        monkeypatch.setattr(worker_workspace, "_LANDLOCK_SEMLOCK_DEV_SHM", str(shm))
        granted = _stub_landlock_syscalls(monkeypatch)
        worker_workspace._apply_landlock(
            workspace.path, workspace.home, [], None, worker_semlock_dev_shm=shm
        )
        shm_rights = [rights for path, rights in granted if path == shm]
        assert shm_rights == [_SEMLOCK_RIGHTS]
        assert worker_workspace._LL_SEMLOCK_DEV_SHM == _SEMLOCK_RIGHTS
        for wider in (
            worker_workspace._LL_MAKE_DIR,
            worker_workspace._LL_MAKE_SYM,
            worker_workspace._LL_REMOVE_DIR,
            worker_workspace._LL_REFER,
        ):
            assert not shm_rights[0] & wider

    def test_worker_without_flag_gets_no_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace, _scratch, shm = self._dirs(tmp_path)
        granted = _stub_landlock_syscalls(monkeypatch)
        worker_workspace._apply_landlock(workspace.path, workspace.home, [], None)
        paths = {path for path, _rights in granted}
        assert shm not in paths
        assert Path("/dev/shm") not in paths

    def test_worker_flag_is_refused_on_a_validation_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace, scratch, shm = self._dirs(tmp_path)
        monkeypatch.setattr(worker_workspace, "_LANDLOCK_SEMLOCK_DEV_SHM", str(shm))
        granted = _stub_landlock_syscalls(monkeypatch)
        with pytest.raises(WorkspaceError, match="requires_worker"):
            worker_workspace._apply_landlock(
                workspace.path, workspace.home, [], scratch, worker_semlock_dev_shm=shm
            )
        assert shm not in {path for path, _rights in granted}

    def test_worker_rule_refuses_symlink_or_other_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _workspace_, _scratch, shm = self._dirs(tmp_path)
        handled = worker_workspace._landlock_supported_mutations(3)
        rule = worker_workspace._landlock_semlock_dev_shm_rule
        assert rule(None, None, handled, True) is None
        monkeypatch.setattr(worker_workspace, "_LANDLOCK_SEMLOCK_DEV_SHM", str(shm))
        assert rule(shm, None, handled, True) == (shm, _SEMLOCK_RIGHTS)
        with pytest.raises(WorkspaceError, match="not_directory"):
            rule(tmp_path / "elsewhere", None, handled, True)
        link = tmp_path / "shm-link"
        try:
            link.symlink_to(shm, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable on this host")
        monkeypatch.setattr(worker_workspace, "_LANDLOCK_SEMLOCK_DEV_SHM", str(link))
        with pytest.raises(WorkspaceError, match="not_directory"):
            rule(link, None, handled, True)

    def test_landlock_exec_forwards_worker_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace, _scratch, shm = self._dirs(tmp_path)
        captured: list[tuple[object, object]] = []

        class _Stop(Exception):
            pass

        def fake_apply(*args: object, **kwargs: object) -> None:
            captured.append(
                (kwargs.get("semlock_dev_shm"), kwargs.get("worker_semlock_dev_shm"))
            )
            raise _Stop

        monkeypatch.delenv(worker_workspace._METADATA_BROKER_EVIDENCE_ENV, raising=False)
        monkeypatch.setattr(worker_workspace, "_apply_landlock", fake_apply)
        base = [
            "--landlock-exec",
            "--workspace", str(workspace.path),
            "--home", str(workspace.home),
        ]
        with pytest.raises(_Stop):
            worker_workspace._landlock_exec([*base, _WORKER_FLAG, str(shm), "--", "true"])
        with pytest.raises(_Stop):
            worker_workspace._landlock_exec([*base, "--", "true"])
        assert captured == [(None, shm), (None, None)]


class TestCpuBudget:
    def test_budget_is_derived_from_observed_cores_with_repo_headroom(self) -> None:
        derive = sandbox_cpu_budget.derived_cpu_budget
        assert [derive(n) for n in (1, 2, 4, 8, 64)] == [1, 1, 3, 7, 63]
        for cores in (1, 2, 3, 16):
            assert derive(cores) == platform_io.sandbox_slot_count(cores)
        assert derive(0) == 1

    def test_budget_follows_os_cpu_count_never_a_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = []
        for cores in (3, 12):
            monkeypatch.setattr(os, "cpu_count", lambda c=cores: c)
            seen.append(sandbox_cpu_budget.resolve_cpu_budget({}))
        assert seen == [2, 11]
        monkeypatch.setattr(os, "cpu_count", lambda: None)
        assert sandbox_cpu_budget.resolve_cpu_budget({}) == 1

    def test_valid_operator_override_is_kept(self) -> None:
        env = sandbox_cpu_budget.cpu_budget_env(
            "landlock", environ={_BUDGET: " 3 "}, cpu_count=8
        )
        assert env == {_BUDGET: "3"}
        full = sandbox_cpu_budget.cpu_budget_env(
            "bubblewrap", environ={_BUDGET: "8"}, cpu_count=8
        )
        assert full == {_BUDGET: "8"}

    @pytest.mark.parametrize(
        "raw", ["", "0", "-2", "+3", "2.5", "abc", "9", "1" * 40, chr(0x663)]
    )
    def test_invalid_operator_override_falls_back_to_derived(self, raw: str) -> None:
        env = sandbox_cpu_budget.cpu_budget_env(
            "landlock", environ={_BUDGET: raw}, cpu_count=8
        )
        assert env == {_BUDGET: "7"}

    def test_appcontainer_receives_no_budget(self) -> None:
        assert sandbox_cpu_budget.cpu_budget_env(
            "windows_appcontainer", environ={_BUDGET: "2"}, cpu_count=8
        ) == {}
        assert _BUDGET in sandbox_cpu_budget.cpu_budget_env(None, environ={}, cpu_count=4)

    def _stub_worker_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(process_launcher, "sanitized_env", lambda *a, **k: {"PATH": "x"})
        monkeypatch.setattr(
            process_launcher,
            "worker_temp_environment",
            lambda repo, request_id: dict.fromkeys(
                runtime_adapters.WORKER_TEMP_ENV_VARS, str(tmp_path)
            ),
        )
        monkeypatch.setattr(
            process_launcher, "worker_validation_affordance_env", lambda *a, **k: {}
        )
        monkeypatch.setattr(process_launcher, "build_tool_env", lambda *a, **k: {})

    def test_worker_launch_env_carries_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_worker_env(tmp_path, monkeypatch)
        monkeypatch.setattr(os, "cpu_count", lambda: 6)
        monkeypatch.delenv(_BUDGET, raising=False)
        env = process_launcher.worker_launch_env(
            "claude_cli", repo=tmp_path, request_id="r", sandbox_backend="landlock"
        )
        assert env[_BUDGET] == "5"
        monkeypatch.setenv(_BUDGET, "2")
        env = process_launcher.worker_launch_env(
            "claude_cli", repo=tmp_path, request_id="r", sandbox_backend="landlock"
        )
        assert env[_BUDGET] == "2"
        monkeypatch.setenv(_BUDGET, "not-a-number")
        env = process_launcher.worker_launch_env(
            "claude_cli", repo=tmp_path, request_id="r", sandbox_backend="bubblewrap"
        )
        assert env[_BUDGET] == "5"

    def test_appcontainer_worker_env_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_worker_env(tmp_path, monkeypatch)
        env = process_launcher.worker_launch_env(
            "claude_cli",
            repo=tmp_path,
            request_id="r",
            sandbox_backend="windows_appcontainer",
        )
        assert _BUDGET not in env

    def test_validation_env_is_composed_with_the_budget(self) -> None:
        source = inspect.getsource(worker_workspace.run_validations)
        assert "| cpu_budget_env(selected_backend)" in source


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="landlock is Linux-only")
def test_real_landlock_worker_runs_multiprocessing_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if worker_workspace.landlock_abi_version() < 1:
        pytest.skip("host kernel lacks landlock")
    if validation_runner.landlock_semlock_dev_shm_root() is None:
        pytest.skip("host has no real /dev/shm directory")
    if validation_runner.probe_multiprocessing_semlock()["supported"] is not True:
        # A nested landlock domain can only narrow its parent's.
        pytest.skip("outer sandbox denies multiprocessing.SemLock to this runner")
    workspace = _workspace(tmp_path)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    def run() -> tuple[list[str], subprocess.CompletedProcess[str]]:
        argv = worker_workspace.sandbox_argv(
            workspace, "claude_cli", [sys.executable, "-c", _POOL_SOURCE], backend="landlock"
        )
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=120, check=False, env=env
        )
        return argv, completed

    argv, granted = run()
    assert _WORKER_FLAG in argv
    assert granted.returncode == 0, granted.stderr[-2000:]
    assert "semlock-ok" in granted.stdout
    _gate(monkeypatch, None)
    argv, denied = run()
    assert _WORKER_FLAG not in argv
    assert denied.returncode != 0
    assert "PermissionError" in denied.stderr, denied.stderr[-2000:]
