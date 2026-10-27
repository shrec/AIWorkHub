"""NF-2026-01740: the landlock VALIDATION lane grants /dev/shm for SemLock.

A validation whose tests construct a ``multiprocessing`` SemLock (a Lock, a
Queue, a Pool) failed on Linux with
``validation_unsupported_in_sandbox:multiprocessing.SemLock:posix_shm`` because
the landlock ruleset never allowed glibc ``sem_open`` to create its file beneath
/dev/shm. The fix grants exactly that, only to a SemLock-needing VALIDATION
ruleset, and the SemLock preflight grants exactly when that rule is present.

The ruleset composition and the preflight decision are pure and run on every
platform; only the real Landlock ``multiprocessing.Pool(2)`` run needs Linux.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import validation_runner, worker_workspace
from aiworkhub.worker_workspace import WorkspaceError

_LOCK_SOURCE = "import multiprocessing as mp\nmp.get_context().Lock()\n"
_PLAIN_SOURCE = "import multiprocessing as mp\n"
_PYTEST_COMMAND = "python -m pytest tests/test_x.py"
_POOL_COMMAND = 'python -c "import multiprocessing as mp; mp.Pool(2)"'
_SEMLOCK_RIGHTS = (
    worker_workspace._LL_WRITE_FILE
    | worker_workspace._LL_MAKE_REG
    | worker_workspace._LL_REMOVE_FILE
)


def _probe(*, supported: bool, backend: str) -> dict[str, object]:
    return {
        "schema": validation_runner.SEMLOCK_PROBE_SCHEMA,
        "supported": supported,
        "primitive": validation_runner.SEMLOCK_PRIMITIVE,
        "backend": backend,
    }


def _workspace(tmp_path: Path) -> worker_workspace.WorkerWorkspace:
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    worktree.mkdir()
    home.mkdir(mode=0o700)
    (home / "tmp").mkdir(mode=0o700)
    return worker_workspace.WorkerWorkspace(
        request_id=f"nf01740-{os.getpid()}-{tmp_path.name}",
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
                # A real descriptor: the production ``finally`` closes it.
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


class TestPreflightDecision:
    def test_landlock_semlock_need_with_dev_shm_rule_is_grant(self) -> None:
        decision = validation_runner.preflight_semlock_capability(
            [_PYTEST_COMMAND],
            backend="landlock",
            probe=_probe(supported=True, backend="posix_shm"),
            source_text=lambda rel: _LOCK_SOURCE,
            sandbox_measured=True,
            dev_shm_rule=True,
        )
        assert decision.action == "grant"
        assert decision.backend == "posix_shm"
        assert decision.primitive == validation_runner.SEMLOCK_PRIMITIVE

    def test_denial_measured_with_the_rule_stays_unsupported(self) -> None:
        # The grant is measured, never inferred: a posix_shm denial measured
        # under the /dev/shm-ruled validation ruleset is not "repaired".
        decision = validation_runner.preflight_semlock_capability(
            [_PYTEST_COMMAND],
            backend="landlock",
            probe=_probe(supported=False, backend="posix_shm"),
            source_text=lambda rel: _LOCK_SOURCE,
            sandbox_measured=True,
            dev_shm_rule=True,
        )
        assert decision.action == "unsupported"
        assert decision.evidence == (
            f"{validation_runner.VALIDATION_UNSUPPORTED_IN_SANDBOX}:"
            f"{validation_runner.SEMLOCK_PRIMITIVE}:posix_shm"
        )

    def test_default_rule_follows_the_dev_shm_root_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe = _probe(supported=True, backend="posix_shm")
        monkeypatch.setattr(
            validation_runner, "landlock_semlock_dev_shm_root", lambda *a: "/dev/shm"
        )
        granted = validation_runner.preflight_semlock_capability(
            [_PYTEST_COMMAND],
            backend="landlock",
            probe=probe,
            source_text=lambda rel: _LOCK_SOURCE,
        )
        assert granted.action == "grant"
        monkeypatch.setattr(
            validation_runner, "landlock_semlock_dev_shm_root", lambda *a: None
        )
        refused = validation_runner.preflight_semlock_capability(
            [_PYTEST_COMMAND],
            backend="landlock",
            probe=probe,
            source_text=lambda rel: _LOCK_SOURCE,
        )
        assert refused.action == "unsupported"
        assert refused.evidence == (
            f"{validation_runner.VALIDATION_UNSUPPORTED_IN_SANDBOX}:"
            f"{validation_runner.SEMLOCK_PRIMITIVE}:posix_shm"
        )

    def test_without_rule_landlock_posix_shm_stays_unsupported(self) -> None:
        for supported in (True, False):
            decision = validation_runner.preflight_semlock_capability(
                [_PYTEST_COMMAND],
                backend="landlock",
                probe=_probe(supported=supported, backend="posix_shm"),
                source_text=lambda rel: _LOCK_SOURCE,
                dev_shm_rule=False,
            )
            assert decision.action == "unsupported"

    def test_rule_repairs_only_the_landlock_posix_shm_denial(self) -> None:
        for backend, probe_backend in (
            ("landlock", "named_semaphore"),
            ("bubblewrap", "posix_shm"),
            ("windows_appcontainer", "named_semaphore"),
        ):
            decision = validation_runner.preflight_semlock_capability(
                [_PYTEST_COMMAND],
                backend=backend,
                probe=_probe(supported=False, backend=probe_backend),
                source_text=lambda rel: _LOCK_SOURCE,
                dev_shm_rule=True,
            )
            assert decision.action == "unsupported", (backend, probe_backend)

    def test_non_landlock_grants_are_unchanged(self) -> None:
        for backend, probe_backend in (
            ("windows_appcontainer", "named_semaphore"),
            ("bubblewrap", "posix_shm"),
        ):
            decision = validation_runner.preflight_semlock_capability(
                [_PYTEST_COMMAND],
                backend=backend,
                probe=_probe(supported=True, backend=probe_backend),
                source_text=lambda rel: _LOCK_SOURCE,
                dev_shm_rule=False,
            )
            assert decision.action == "grant"

    def test_validation_without_semlock_need_is_skip(self) -> None:
        decision = validation_runner.preflight_semlock_capability(
            [_PYTEST_COMMAND],
            backend="landlock",
            probe=_probe(supported=False, backend="posix_shm"),
            source_text=lambda rel: _PLAIN_SOURCE,
            dev_shm_rule=True,
        )
        assert decision.action == "skip"

    def test_sandbox_can_grant_semlock_is_the_rule(self) -> None:
        grant = validation_runner.sandbox_can_grant_semlock
        assert grant("landlock", "posix_shm") is False
        assert grant("landlock", "posix_shm", dev_shm_rule=True) is True
        assert grant("landlock", "named_semaphore") is True
        assert grant("bubblewrap", "posix_shm") is True
        assert grant("windows_appcontainer", "named_semaphore") is True

    def test_pool_is_a_semlock_need(self) -> None:
        assert validation_runner.command_needs_multiprocessing_semlock(_POOL_COMMAND)
        assert validation_runner.command_needs_multiprocessing_semlock(
            _PYTEST_COMMAND,
            source_text=lambda rel: "from multiprocessing import Pool\nPool(2)\n",
        )


class TestDevShmRoot:
    def test_only_a_real_directory_is_a_root(self, tmp_path: Path) -> None:
        root = validation_runner.landlock_semlock_dev_shm_root
        directory = tmp_path / "shm"
        directory.mkdir()
        assert root(str(directory)) == str(directory)
        assert root(str(tmp_path / "missing")) is None
        regular = tmp_path / "file"
        regular.write_text("x", encoding="utf-8")
        assert root(str(regular)) is None
        link = tmp_path / "link"
        try:
            link.symlink_to(directory, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable on this host")
        assert root(str(link)) is None

    def test_default_root_is_dev_shm(self) -> None:
        assert validation_runner.LANDLOCK_SEMLOCK_DEV_SHM == "/dev/shm"
        expected = (
            "/dev/shm"
            if os.path.isdir("/dev/shm") and not os.path.islink("/dev/shm")
            else None
        )
        assert validation_runner.landlock_semlock_dev_shm_root() == expected


class TestRulesetComposition:
    def _dirs(self, tmp_path: Path) -> tuple[worker_workspace.WorkerWorkspace, Path, Path]:
        workspace = _workspace(tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        shm = tmp_path / "shm"
        shm.mkdir()
        return workspace, scratch, shm

    def test_validation_ruleset_contains_dev_shm_beneath_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace, scratch, shm = self._dirs(tmp_path)
        # The rule is pinned to LANDLOCK_SEMLOCK_DEV_SHM; point it at the fixture.
        monkeypatch.setattr(validation_runner, "LANDLOCK_SEMLOCK_DEV_SHM", str(shm))
        granted = _stub_landlock_syscalls(monkeypatch)
        worker_workspace._apply_landlock(
            workspace.path, workspace.home, [], scratch, semlock_dev_shm=shm
        )
        assert (shm, _SEMLOCK_RIGHTS) in granted
        assert worker_workspace._LL_SEMLOCK_DEV_SHM == _SEMLOCK_RIGHTS
        shm_rights = [rights for path, rights in granted if path == shm]
        assert shm_rights == [_SEMLOCK_RIGHTS]
        for wider in (
            worker_workspace._LL_MAKE_DIR,
            worker_workspace._LL_MAKE_SYM,
            worker_workspace._LL_REMOVE_DIR,
            worker_workspace._LL_REFER,
        ):
            assert not shm_rights[0] & wider

    def test_validation_without_semlock_gets_no_dev_shm_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace, scratch, shm = self._dirs(tmp_path)
        granted = _stub_landlock_syscalls(monkeypatch)
        worker_workspace._apply_landlock(workspace.path, workspace.home, [], scratch)
        paths = {path for path, _rights in granted}
        assert shm not in paths
        assert Path("/dev/shm") not in paths

    def test_worker_run_ruleset_never_gains_dev_shm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace, _scratch, shm = self._dirs(tmp_path)
        granted = _stub_landlock_syscalls(monkeypatch)
        with pytest.raises(WorkspaceError, match="requires_validation"):
            worker_workspace._apply_landlock(
                workspace.path, workspace.home, [], None, semlock_dev_shm=shm
            )
        assert shm not in {path for path, _rights in granted}

    def test_rule_refuses_a_missing_or_symlinked_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _workspace_, scratch, shm = self._dirs(tmp_path)
        handled = worker_workspace._landlock_supported_mutations(3)
        rule = worker_workspace._landlock_semlock_dev_shm_rule
        assert rule(None, scratch, handled) is None
        monkeypatch.setattr(validation_runner, "LANDLOCK_SEMLOCK_DEV_SHM", str(shm))
        assert rule(shm, scratch, handled) == (shm, _SEMLOCK_RIGHTS)
        missing = tmp_path / "missing"
        monkeypatch.setattr(validation_runner, "LANDLOCK_SEMLOCK_DEV_SHM", str(missing))
        with pytest.raises(WorkspaceError, match="not_directory"):
            rule(missing, scratch, handled)
        link = tmp_path / "shm-link"
        try:
            link.symlink_to(shm, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable on this host")
        monkeypatch.setattr(validation_runner, "LANDLOCK_SEMLOCK_DEV_SHM", str(link))
        with pytest.raises(WorkspaceError, match="not_directory"):
            rule(link, scratch, handled)

    def test_rule_is_pinned_to_the_dev_shm_root(self, tmp_path: Path) -> None:
        _workspace_, scratch, shm = self._dirs(tmp_path)
        handled = worker_workspace._landlock_supported_mutations(3)
        rule = worker_workspace._landlock_semlock_dev_shm_rule
        for other in (Path("/tmp"), shm):
            with pytest.raises(WorkspaceError, match="not_directory"):
                rule(other, scratch, handled)


class TestArgvComposition:
    def _argv(
        self, workspace: worker_workspace.WorkerWorkspace, **kwargs: object
    ) -> list[str]:
        return worker_workspace.sandbox_argv(
            workspace,
            str(kwargs.pop("adapter_id", "validation")),
            [sys.executable, "-c", "pass"],
            backend="landlock",
            **kwargs,  # type: ignore[arg-type]
        )

    def test_validation_argv_carries_flag_only_when_needed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace(tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        monkeypatch.setattr(
            validation_runner, "landlock_semlock_dev_shm_root", lambda *a: "/dev/shm"
        )
        argv = self._argv(
            workspace, validation_exec_scratch=scratch, validation_semlock_dev_shm=True
        )
        index = argv.index("--semlock-dev-shm")
        assert argv[index + 1] == "/dev/shm"
        assert index < argv.index("--")
        plain = self._argv(workspace, validation_exec_scratch=scratch)
        assert "--semlock-dev-shm" not in plain
        monkeypatch.setattr(
            validation_runner, "landlock_semlock_dev_shm_root", lambda *a: None
        )
        absent = self._argv(
            workspace, validation_exec_scratch=scratch, validation_semlock_dev_shm=True
        )
        assert "--semlock-dev-shm" not in absent

    def test_provider_argv_never_carries_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace(tmp_path)
        monkeypatch.setattr(
            validation_runner, "landlock_semlock_dev_shm_root", lambda *a: "/dev/shm"
        )
        assert "--semlock-dev-shm" not in self._argv(workspace, adapter_id="claude_cli")
        with pytest.raises(WorkspaceError, match="requires_validation"):
            self._argv(
                workspace, adapter_id="claude_cli", validation_semlock_dev_shm=True
            )

    def test_landlock_exec_forwards_flag_to_ruleset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace(tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        shm = tmp_path / "shm"
        shm.mkdir()
        captured: list[object] = []

        class _Stop(Exception):
            pass

        def fake_apply(*args: object, **kwargs: object) -> None:
            captured.append(kwargs.get("semlock_dev_shm"))
            raise _Stop

        monkeypatch.delenv(worker_workspace._METADATA_BROKER_EVIDENCE_ENV, raising=False)
        monkeypatch.setattr(worker_workspace, "_apply_landlock", fake_apply)
        base = [
            "--landlock-exec",
            "--workspace", str(workspace.path),
            "--home", str(workspace.home),
            "--exec-scratch", str(scratch),
        ]
        with pytest.raises(_Stop):
            worker_workspace._landlock_exec(
                [*base, "--semlock-dev-shm", str(shm), "--", "true"]
            )
        with pytest.raises(_Stop):
            worker_workspace._landlock_exec([*base, "--", "true"])
        assert captured == [shm, None]

    def test_run_validations_need_follows_the_preflight_predicate(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        tests_dir = workspace.path / "tests"
        tests_dir.mkdir()
        target = tests_dir / "test_x.py"
        needs = worker_workspace._validation_needs_semlock_dev_shm
        target.write_text(_LOCK_SOURCE, encoding="utf-8")
        assert needs(workspace, _PYTEST_COMMAND, "landlock") is True
        assert needs(workspace, _PYTEST_COMMAND, "bubblewrap") is False
        assert needs(workspace, _POOL_COMMAND, "landlock") is True
        target.write_text(_PLAIN_SOURCE, encoding="utf-8")
        assert needs(workspace, _PYTEST_COMMAND, "landlock") is False
        assert needs(workspace, "python -m pytest tests/missing.py", "landlock") is False


class TestCandidateSourceReader:
    def _root(self, tmp_path: Path) -> Path:
        root = tmp_path / "root"
        (root / "tests").mkdir(parents=True)
        return root

    def test_normal_file_is_its_text(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        (root / "tests" / "test_x.py").write_bytes(_LOCK_SOURCE.encode("utf-8"))
        read = validation_runner.read_candidate_source
        assert read(root, "tests/test_x.py") == _LOCK_SOURCE

    def test_escape_outside_root_is_none(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        outside = tmp_path / "outside.py"
        outside.write_bytes(_LOCK_SOURCE.encode("utf-8"))
        read = validation_runner.read_candidate_source
        assert read(root, "../outside.py") is None
        assert read(root, "tests/../../outside.py") is None
        assert read(root, str(outside)) is None
        assert read(root, "") is None

    def test_symlink_outside_root_is_none(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        outside = tmp_path / "outside.py"
        outside.write_bytes(_LOCK_SOURCE.encode("utf-8"))
        link = root / "tests" / "test_link.py"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable on this host")
        assert validation_runner.read_candidate_source(root, "tests/test_link.py") is None

    def test_oversized_file_is_none(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        cap = validation_runner.CANDIDATE_SOURCE_MAX_BYTES
        assert cap == 1024 * 1024
        (root / "tests" / "test_big.py").write_bytes(b"#" * (cap + 1))
        (root / "tests" / "test_cap.py").write_bytes(b"#" * cap)
        read = validation_runner.read_candidate_source
        assert read(root, "tests/test_big.py") is None
        assert read(root, "tests/test_cap.py") == "#" * cap

    def test_invalid_utf8_directory_and_missing_are_none(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        (root / "tests" / "test_bin.py").write_bytes(b"\xff\xfe\x80import multiprocessing\n")
        read = validation_runner.read_candidate_source
        assert read(root, "tests/test_bin.py") is None
        assert read(root, "tests") is None
        assert read(root, "tests/missing.py") is None


class TestProbeRuleset:
    """The SemLock probe is measured under the validation's own ruleset."""

    def _probe_kwargs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        root: str | None,
    ) -> tuple[dict[str, object], list[str], list[object], Path]:
        from aiworkhub import process_launcher

        workspace = _workspace(tmp_path)
        (workspace.path / "tests").mkdir()
        (workspace.path / "tests" / "test_x.py").write_bytes(_LOCK_SOURCE.encode("utf-8"))
        scratch = tmp_path / "probe-scratch"
        provisioned: list[str] = []
        released: list[object] = []
        captured: list[dict[str, object]] = []

        class _Stop(Exception):
            pass

        def fake_provision(target: object, *, backend: str) -> Path:
            provisioned.append(backend)
            scratch.mkdir()
            return scratch

        def fake_sandbox_argv(
            target: object, adapter_id: str, argv: list[str], **kwargs: object
        ) -> list[str]:
            captured.append(kwargs)
            raise _Stop

        monkeypatch.setattr(
            validation_runner, "landlock_semlock_dev_shm_root", lambda *a: root
        )
        monkeypatch.setattr(
            worker_workspace, "provision_validation_exec_scratch", fake_provision
        )
        monkeypatch.setattr(
            worker_workspace, "cleanup_validation_exec_scratch", released.append
        )
        monkeypatch.setattr(process_launcher, "sandbox_argv", fake_sandbox_argv)
        with pytest.raises(_Stop):
            process_launcher._run_validations_with_toolchain_receipt(
                workspace,
                [_PYTEST_COMMAND],
                {},
                backend="landlock",
                adapter_id="validation",
            )
        assert len(captured) == 1
        return captured[0], provisioned, released, scratch

    def test_landlock_probe_carries_the_validation_dev_shm_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kwargs, provisioned, released, scratch = self._probe_kwargs(
            tmp_path, monkeypatch, "/dev/shm"
        )
        assert kwargs["backend"] == "landlock"
        assert kwargs["validation_semlock_dev_shm"] is True
        assert kwargs["validation_exec_scratch"] == scratch
        assert provisioned == ["landlock"]
        assert released == [scratch]

    def test_probe_without_dev_shm_root_carries_no_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kwargs, provisioned, released, _scratch = self._probe_kwargs(
            tmp_path, monkeypatch, None
        )
        assert kwargs["backend"] == "landlock"
        assert "validation_semlock_dev_shm" not in kwargs
        assert "validation_exec_scratch" not in kwargs
        assert provisioned == []
        assert released == [None]


_POOL_SOURCE = (
    "import multiprocessing as mp\n"
    "with mp.Pool(2) as pool:\n"
    "    assert pool.map(abs, [-1, -2]) == [1, 2]\n"
)


@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-only")
def test_real_landlock_validation_runs_multiprocessing_pool(tmp_path: Path) -> None:
    if worker_workspace.landlock_abi_version() < 1:
        pytest.skip("host kernel lacks landlock")
    if validation_runner.landlock_semlock_dev_shm_root() is None:
        pytest.skip("host has no real /dev/shm directory")
    if validation_runner.probe_multiprocessing_semlock()["supported"] is not True:
        # A nested Landlock domain can only narrow its parent's: when an outer
        # sandbox already denies SemLock to this runner, no inner rule can
        # restore it, so the host genuinely lacks the capability under test.
        pytest.skip("outer sandbox denies multiprocessing.SemLock to this runner")
    workspace = _workspace(tmp_path)
    scratch = worker_workspace.provision_validation_exec_scratch(
        workspace, backend="landlock"
    )
    try:
        env = os.environ.copy()
        for name in ("TMPDIR", "TMP", "TEMP"):
            env[name] = str(scratch)

        def run(grant: bool) -> subprocess.CompletedProcess[str]:
            argv = worker_workspace.sandbox_argv(
                workspace,
                "validation",
                [sys.executable, "-c", _POOL_SOURCE],
                backend="landlock",
                validation_exec_scratch=scratch,
                validation_semlock_dev_shm=grant,
            )
            assert ("--semlock-dev-shm" in argv) is grant
            return subprocess.run(
                argv, capture_output=True, text=True, timeout=120, check=False, env=env
            )

        granted = run(True)
        assert granted.returncode == 0, granted.stderr[-2000:]
        denied = run(False)
        assert denied.returncode != 0
        assert "PermissionError" in denied.stderr, denied.stderr[-2000:]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
