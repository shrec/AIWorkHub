from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from aiworkhub import worker_workspace


def _workspace(tmp_path: Path) -> worker_workspace.WorkerWorkspace:
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    worktree.mkdir()
    home.mkdir()
    return worker_workspace.WorkerWorkspace(
        request_id="b1460",
        repo=tmp_path,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    return path


def test_bare_approved_ruff_resolves_to_arbitrary_repo_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "D Dev Project"
    runtime_root = repo / ".venv"
    ruff = _executable(runtime_root / "bin" / "ruff")
    home = tmp_path / "home"
    home_runtime = _executable(home / "AIWorkHub" / ".venv" / "bin" / "ruff")
    monkeypatch.setattr(worker_workspace.Path, "home", lambda: home)

    assert home_runtime.resolve() != ruff.resolve()
    assert (
        worker_workspace.resolve_trusted_validation_executable("ruff", repo)
        == ruff.resolve()
    )
    assert worker_workspace._normalize_trusted_validation_executable_argv(
        ["ruff", "check", "src"], repo
    ) == [str(ruff.resolve()), "check", "src"]


def test_python_module_ruff_resolves_to_trusted_console_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "project"
    runtime_root = repo / ".venv"
    ruff = _executable(runtime_root / "bin" / "ruff")
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_validation_runtime_roots",
        lambda repo=None: (runtime_root,),
    )

    argv, roots = (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            ["python3", "-m", "ruff", "check", "src"], repo
        )
    )

    assert argv == [str(ruff.resolve()), "check", "src"]
    assert roots == (runtime_root.resolve(strict=False),)


def test_bare_python_validation_uses_portable_python3_alias() -> None:
    argv, roots = (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            ["python", "-m", "compileall", "-q", "src"]
        )
    )

    expected = "python" if os.name == "nt" else "python3"
    assert argv == [expected, "-m", "compileall", "-q", "src"]
    assert roots == ()


def test_unrelated_home_aiworkhub_runtime_is_not_selected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "customer-repository"
    repo.mkdir()
    home = tmp_path / "home"
    _executable(home / "AIWorkHub" / ".venv" / "bin" / "ruff")
    monkeypatch.setattr(worker_workspace.Path, "home", lambda: home)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(
        worker_workspace.sys, "prefix", worker_workspace.sys.base_prefix
    )

    with pytest.raises(
        worker_workspace.WorkspaceError, match="validation_executable_unavailable"
    ):
        worker_workspace.resolve_trusted_validation_executable("ruff", repo)


def test_windows_runtime_path_uses_venv_scripts_exe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_root = tmp_path / "project" / ".venv"
    ruff = _executable(runtime_root / "Scripts" / "ruff.exe")
    monkeypatch.setattr(worker_workspace.os, "name", "nt")
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_validation_runtime_roots",
        lambda repo=None: (runtime_root,),
    )

    assert worker_workspace.resolve_trusted_validation_executable("ruff") == ruff.resolve()


def test_unapproved_bare_executable_is_not_rewritten() -> None:
    assert worker_workspace._normalize_trusted_validation_executable_argv(
        ["mypy", "src"]
    ) == ["mypy", "src"]
    with pytest.raises(
        worker_workspace.WorkspaceError, match="validation_executable_not_approved"
    ):
        worker_workspace.resolve_trusted_validation_executable("mypy")


def test_missing_approved_executable_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_validation_runtime_roots",
        lambda repo=None: (tmp_path / ".venv",),
    )
    with pytest.raises(
        worker_workspace.WorkspaceError, match="validation_executable_unavailable"
    ):
        worker_workspace._normalize_trusted_validation_executable_argv(["ruff", "check"])


def test_runtime_symlink_escape_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_root = tmp_path / "project" / ".venv"
    escaped = _executable(tmp_path / "elsewhere" / "ruff")
    link = runtime_root / "bin" / "ruff"
    link.parent.mkdir(parents=True)
    link.symlink_to(escaped)
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_validation_runtime_roots",
        lambda repo=None: (runtime_root,),
    )

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_executable_untrusted_runtime_root",
    ):
        worker_workspace.resolve_trusted_validation_executable("ruff")


def test_world_writable_runtime_root_and_executable_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_root = tmp_path / "project" / ".venv"
    ruff = _executable(runtime_root / "bin" / "ruff")
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_validation_runtime_roots",
        lambda repo=None: (runtime_root,),
    )

    monkeypatch.setattr(worker_workspace.stat, "S_IMODE", lambda mode: 0o002)
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_executable_runtime_root_world_writable",
    ):
        worker_workspace.resolve_trusted_validation_executable("ruff")

    seen_root = False

    def fake_imode(mode: int) -> int:
        nonlocal seen_root
        if not seen_root:
            seen_root = True
            return 0o755
        return 0o002

    monkeypatch.setattr(worker_workspace.stat, "S_IMODE", fake_imode)
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_executable_world_writable",
    ):
        worker_workspace.resolve_trusted_validation_executable("ruff")


def test_bubblewrap_binds_runtime_root_and_rewrites_executable_path(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    runtime_root = tmp_path / "runtime" / ".venv"
    ruff = _executable(runtime_root / "bin" / "ruff")

    argv = worker_workspace.sandbox_argv(
        workspace,
        "validation",
        [str(ruff.resolve()), "check", "src"],
        backend="bubblewrap",
        validation_executable_roots=(runtime_root,),
    )

    alias = f"{worker_workspace.SANDBOX_VALIDATION_EXECUTABLE_ROOT}/0"
    validation_dir_index = argv.index(worker_workspace.SANDBOX_VALIDATION_EXECUTABLE_ROOT)
    assert argv[validation_dir_index - 1 : validation_dir_index + 1] == [
        "--dir",
        worker_workspace.SANDBOX_VALIDATION_EXECUTABLE_ROOT,
    ]
    assert ["--ro-bind", str(runtime_root.resolve()), alias] == argv[
        argv.index(str(runtime_root.resolve())) - 1 : argv.index(str(runtime_root.resolve())) + 2
    ]
    assert argv[-3:] == [f"{alias}/bin/ruff", "check", "src"]


def test_run_validations_resolves_ruff_before_landlock_exec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    runtime_root = tmp_path / "project" / ".venv"
    ruff = _executable(runtime_root / "bin" / "ruff")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_validation_runtime_roots",
        lambda repo=None: (runtime_root,),
    )
    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", lambda: "landlock")
    monkeypatch.setattr(
        worker_workspace,
        "provision_validation_exec_scratch",
        lambda ws: tmp_path / "scratch",
    )
    monkeypatch.setattr(worker_workspace, "cleanup_validation_exec_scratch", lambda path: None)
    monkeypatch.setattr(
        worker_workspace,
        "sandbox_argv",
        lambda ws, adapter_id, argv, **kw: list(argv),
    )
    monkeypatch.setattr(
        worker_workspace, "sanitized_env", lambda *a, **kw: {"PATH": "/bin"}
    )

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["shell"] = kwargs.get("shell")
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(worker_workspace.subprocess, "run", fake_run)

    result = worker_workspace.run_validations(workspace, ["ruff check src"])

    assert result[0]["argv"] == [str(ruff.resolve()), "check", "src"]
    assert captured["argv"] == [str(ruff.resolve()), "check", "src"]
    assert captured["shell"] is False
    assert captured["env"]["RUFF_CACHE_DIR"] == str(tmp_path / "scratch")


def test_run_validations_passes_runtime_root_to_bubblewrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    runtime_root = tmp_path / "project" / ".venv"
    ruff = _executable(runtime_root / "bin" / "ruff")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_validation_runtime_roots",
        lambda repo=None: (runtime_root,),
    )
    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", lambda: "bubblewrap")
    monkeypatch.setattr(
        worker_workspace,
        "provision_validation_exec_scratch",
        lambda ws: tmp_path / "scratch",
    )
    monkeypatch.setattr(worker_workspace, "cleanup_validation_exec_scratch", lambda path: None)

    def fake_sandbox_argv(ws, adapter_id, argv, **kwargs):
        captured["argv"] = argv
        captured["validation_executable_roots"] = kwargs.get("validation_executable_roots")
        return list(argv)

    monkeypatch.setattr(worker_workspace, "sandbox_argv", fake_sandbox_argv)
    monkeypatch.setattr(worker_workspace, "sanitized_env", lambda *a, **kw: {"PATH": "/bin"})
    monkeypatch.setattr(
        worker_workspace.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "ok", ""),
    )

    worker_workspace.run_validations(workspace, ["ruff check src"])

    assert captured["argv"] == [str(ruff.resolve()), "check", "src"]
    assert captured["validation_executable_roots"] == (runtime_root.resolve(strict=False),)


def test_pytest_console_script_normalization_is_preserved() -> None:
    assert worker_workspace._normalize_pytest_validation_argv(["pytest", "-q"]) == [
        worker_workspace.sys.executable,
        "-m",
        "pytest",
        "-q",
    ]
    explicit = ["python3", "-m", "pytest", "--version"]
    assert worker_workspace._normalize_pytest_validation_argv(explicit) == explicit
