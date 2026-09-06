from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import toolchain_authority, worker_workspace


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


def _runtime_executable(runtime_root: Path, name: str) -> Path:
    relative = (
        Path("Scripts") / f"{name}.exe"
        if os.name == "nt"
        else Path("bin") / name
    )
    return _executable(runtime_root / relative)


def test_bare_approved_ruff_resolves_to_arbitrary_repo_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "D Dev Project"
    runtime_root = repo / ".venv"
    ruff = _runtime_executable(runtime_root, "ruff")
    home = tmp_path / "home"
    home_runtime = _runtime_executable(home / "AIWorkHub" / ".venv", "ruff")
    monkeypatch.setattr(worker_workspace.Path, "home", lambda: home)

    assert home_runtime.resolve() != ruff.resolve()
    assert (
        worker_workspace.resolve_trusted_validation_executable("ruff", repo)
        == ruff.resolve()
    )
    assert worker_workspace._normalize_trusted_validation_executable_argv(
        ["ruff", "check", "src"], repo
    ) == [str(ruff.resolve()), "check", "src"]


def test_python_module_ruff_preserves_safe_module_form_without_runtime_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "project"
    runtime_root = repo / ".venv"
    ruff = _runtime_executable(runtime_root, "ruff")
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

    assert ruff.is_file()
    assert argv == [sys.executable, "-P", "-m", "ruff", "check", "src"]
    assert roots == ()


def test_bare_python_validation_uses_portable_python3_alias() -> None:
    argv, roots = (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            ["python", "-m", "compileall", "-q", "src"]
        )
    )

    assert argv == [sys.executable, "-m", "compileall", "-q", "src"]
    assert roots == ()


def test_unrelated_home_aiworkhub_runtime_is_not_selected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "customer-repository"
    repo.mkdir()
    home = tmp_path / "home"
    _runtime_executable(home / "AIWorkHub" / ".venv", "ruff")
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
        ["pylint", "src"]
    ) == ["pylint", "src"]
    with pytest.raises(
        worker_workspace.WorkspaceError, match="validation_executable_not_approved"
    ):
        worker_workspace.resolve_trusted_validation_executable("pylint")


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
    link = (
        runtime_root / "Scripts" / "ruff.exe"
        if os.name == "nt"
        else runtime_root / "bin" / "ruff"
    )
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
    monkeypatch.setattr(
        worker_workspace,
        "posix_path_modes_supported",
        lambda _platform=None: True,
    )
    runtime_root = tmp_path / "project" / ".venv"
    ruff = _runtime_executable(runtime_root, "ruff")
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
    ruff = _runtime_executable(runtime_root, "ruff")

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
    sandbox_relative = "Scripts/ruff.exe" if os.name == "nt" else "bin/ruff"
    assert argv[-3:] == [f"{alias}/{sandbox_relative}", "check", "src"]


def test_run_validations_resolves_ruff_before_landlock_exec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    runtime_root = tmp_path / "project" / ".venv"
    ruff = _runtime_executable(runtime_root, "ruff")
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
    assert result[0]["declared_command"] == "ruff check src"
    assert result[0]["declared_argv"] == ["ruff", "check", "src"]
    assert result[0]["executed_argv"] == [str(ruff.resolve()), "check", "src"]
    assert result[0]["argv_rewritten"] is True
    assert captured["argv"] == [str(ruff.resolve()), "check", "src"]
    assert captured["shell"] is False
    assert captured["env"]["RUFF_CACHE_DIR"] == str(tmp_path / "scratch")


def test_toolchain_receipt_detects_executable_swap(tmp_path: Path) -> None:
    first = _executable(tmp_path / "first" / "node")
    second = _executable(tmp_path / "second" / "node")
    status = first.stat()
    receipt = {
        "schema_id": "aiworkhub.toolchain_authority.receipt.v1",
        "executables": [
            {
                "canonical_path": str(second.resolve()),
                "device": status.st_dev,
                "inode": status.st_ino,
                "size": status.st_size,
                "mode": status.st_mode & 0o777,
                "mtime_ns": status.st_mtime_ns,
            }
        ],
    }

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_toolchain_authority_executable_identity_drift",
    ):
        worker_workspace._verify_authority_receipt_executable(receipt, str(second))


def _authority_receipt_for(
    repo: Path, monkeypatch: pytest.MonkeyPatch, executable: Path
) -> tuple[dict[str, object], dict[str, object]]:
    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        lambda argv, _repo: ([str(executable.resolve()), *argv[1:]], ()),
    )
    monkeypatch.setattr(
        worker_workspace,
        "trusted_validation_executable_version",
        lambda _resolved: "tool 1.2.3",
    )
    card = {
        "task_id": "TASK_RECEIPT",
        "request_id": "request-a",
        "validation": ["tool --check"],
    }
    snapshot = toolchain_authority.ToolchainAuthority(
        repo, capability_probe=lambda _repo, _card: ()
    ).evaluate(card)
    return toolchain_authority.authority_receipt(snapshot, card), card


def test_toolchain_receipt_rejects_tampered_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _executable(tmp_path / "bin" / "tool")
    receipt, card = _authority_receipt_for(tmp_path, monkeypatch, executable)
    receipt["snapshot_digest"] = "0" * 64

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_toolchain_authority_receipt_mac_mismatch",
    ):
        worker_workspace._verify_authority_receipt(receipt, tmp_path, card)


def test_toolchain_receipt_rejects_cross_repo_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo-a"
    other = tmp_path / "repo-b"
    repo.mkdir()
    other.mkdir()
    executable = _executable(tmp_path / "bin" / "tool")
    receipt, card = _authority_receipt_for(repo, monkeypatch, executable)

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_toolchain_authority_receipt_repository_mismatch",
    ):
        worker_workspace._verify_authority_receipt(receipt, other, card)


def test_toolchain_receipt_rejects_altered_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _executable(tmp_path / "bin" / "tool")
    receipt, card = _authority_receipt_for(tmp_path, monkeypatch, executable)
    facts = receipt["executables"]
    assert isinstance(facts, list)
    facts[0]["version_fact"] = "tool 9.9.9"

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_toolchain_authority_receipt_mac_mismatch",
    ):
        worker_workspace._verify_authority_receipt(receipt, tmp_path, card)


def test_toolchain_receipt_rejects_altered_fact_with_recomputed_public_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _executable(tmp_path / "bin" / "tool")
    receipt, card = _authority_receipt_for(tmp_path, monkeypatch, executable)
    facts = receipt["executables"]
    assert isinstance(facts, list)
    facts[0]["version_fact"] = "tool 9.9.9"
    raw_snapshot = dict(receipt)
    raw_snapshot["schema_id"] = toolchain_authority.SCHEMA_ID
    raw_snapshot["digest"] = receipt["snapshot_digest"]
    snapshot = toolchain_authority.ToolchainAuthority._snapshot_from_dict(
        raw_snapshot
    )
    assert snapshot is not None
    receipt["snapshot_digest"] = toolchain_authority.ToolchainAuthority._payload_digest(
        snapshot
    )

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_toolchain_authority_receipt_mac_mismatch",
    ):
        worker_workspace._verify_authority_receipt(receipt, tmp_path, card)


def test_toolchain_receipt_rejects_wrong_hmac_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _executable(tmp_path / "bin" / "tool")
    receipt, card = _authority_receipt_for(tmp_path, monkeypatch, executable)
    monkeypatch.setenv(
        "AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY",
        "hex:" + ("42" * 32),
    )

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_toolchain_authority_receipt_mac_mismatch",
    ):
        worker_workspace._verify_authority_receipt(receipt, tmp_path, card)


def test_toolchain_receipt_rejects_cross_request_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _executable(tmp_path / "bin" / "tool")
    receipt, card = _authority_receipt_for(tmp_path, monkeypatch, executable)
    replay_card = dict(card)
    replay_card["request_id"] = "request-b"

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_toolchain_authority_receipt_request_id_mismatch",
    ):
        worker_workspace._verify_authority_receipt(receipt, tmp_path, replay_card)


def test_toolchain_receipt_rejects_executable_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _executable(tmp_path / "bin" / "tool")
    receipt, card = _authority_receipt_for(tmp_path, monkeypatch, executable)
    executable.write_text("#!/bin/sh\nexit 0\n# changed\n", encoding="utf-8")

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_toolchain_authority_executable_identity_drift",
    ):
        worker_workspace._verify_authority_receipt(receipt, tmp_path, card)


def test_version_probe_uses_sandbox_boundary_not_direct_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _executable(tmp_path / "bin" / "tool")
    captured: dict[str, object] = {}
    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", lambda: "bubblewrap")
    monkeypatch.setattr(
        worker_workspace,
        "sandbox_argv",
        lambda ws, adapter_id, argv, **kw: captured.update(
            {
                "workspace": ws,
                "argv": list(argv),
                "roots": kw.get("validation_executable_roots"),
            }
        )
        or ["sandboxed-tool", "--version"],
    )
    monkeypatch.setattr(
        worker_workspace,
        "sanitized_env",
        lambda *args, **kwargs: {"PATH": "/usr/bin:/bin"},
    )

    def fake_run(argv, **kwargs):
        assert argv != [str(executable.resolve()), "--version"]
        captured["run_argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, "tool 1.2.3\n", "")

    monkeypatch.setattr(worker_workspace.subprocess, "run", fake_run)

    assert worker_workspace.trusted_validation_executable_version(str(executable)) == "tool 1.2.3"
    assert captured["argv"] == [str(executable.resolve()), "--version"]
    assert captured["run_argv"] == ["sandboxed-tool", "--version"]
    assert captured["roots"] == (executable.resolve().parent,)


def test_version_probe_publishes_hosted_python_base_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "hosted-python"
    executable = _executable(runtime / "bin" / "python3.12")
    captured: dict[str, object] = {}
    monkeypatch.setattr(worker_workspace.sys, "base_prefix", str(runtime))
    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", lambda: "bubblewrap")
    monkeypatch.setattr(
        worker_workspace,
        "sandbox_argv",
        lambda ws, adapter_id, argv, **kw: captured.update(
            {
                "roots": kw.get("validation_executable_roots"),
                "identity_root": kw.get("validation_python_runtime_identity_root"),
            }
        )
        or ["sandboxed-python", "--version"],
    )
    monkeypatch.setattr(
        worker_workspace,
        "sanitized_env",
        lambda *args, **kwargs: {"PATH": "/usr/bin:/bin"},
    )
    monkeypatch.setattr(
        worker_workspace.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, "Python 3.12.0\n", ""
        ),
    )

    assert (
        worker_workspace.trusted_validation_executable_version(str(executable))
        == "Python 3.12.0"
    )
    assert captured["roots"] == ()
    assert captured["identity_root"] == runtime.resolve()


def test_bubblewrap_identity_root_keeps_runtime_at_original_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    (workspace.path / ".git").write_text(
        "gitdir: /nonexistent/b1460\n", encoding="utf-8"
    )
    runtime = tmp_path / "hosted-python"
    executable = _executable(runtime / "bin" / "python3.12")
    monkeypatch.setattr(worker_workspace.sys, "base_prefix", str(runtime))

    argv = worker_workspace.sandbox_argv(
        workspace,
        "validation",
        [str(executable), "--version"],
        backend="bubblewrap",
        validation_python_runtime_identity_root=runtime,
    )

    bind_index = argv.index(str(runtime.resolve()))
    assert argv[bind_index - 1] == "--ro-bind"
    assert argv[bind_index + 1] == str(runtime.resolve())
    assert argv[-2:] == [str(executable.resolve()), "--version"]
    if not Path("/usr/bin/bwrap").is_file():
        pytest.skip("bubblewrap unavailable")
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0 and "setting up uid map: Permission denied" in result.stderr:
        pytest.skip("bubblewrap user namespaces unavailable")
    assert result.returncode == 0, result.stderr


def test_bubblewrap_identity_root_rejects_untrusted_or_unrelated_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    trusted_runtime = tmp_path / "trusted-python"
    untrusted_runtime = tmp_path / "untrusted-python"
    executable = _executable(untrusted_runtime / "bin" / "python3.12")
    trusted_runtime.mkdir()
    monkeypatch.setattr(worker_workspace.sys, "base_prefix", str(trusted_runtime))

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_python_runtime_identity_root_untrusted",
    ):
        worker_workspace.sandbox_argv(
            workspace,
            "validation",
            [str(executable), "--version"],
            backend="bubblewrap",
            validation_python_runtime_identity_root=untrusted_runtime,
        )

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_python_runtime_executable_outside_root",
    ):
        worker_workspace.sandbox_argv(
            workspace,
            "validation",
            [str(executable), "--version"],
            backend="bubblewrap",
            validation_python_runtime_identity_root=trusted_runtime,
        )


def test_run_validations_passes_runtime_root_to_bubblewrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    runtime_root = tmp_path / "project" / ".venv"
    ruff = _runtime_executable(runtime_root, "ruff")
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


def test_pytest_uses_the_trusted_module_validator_authority() -> None:
    assert "pytest" in worker_workspace._TRUSTED_VALIDATION_BARE_EXECUTABLES
    assert worker_workspace._is_module_validator_invocation(
        ["python", "-m", "pytest", "tests/test_x.py"]
    )


def test_git_uses_the_trusted_system_tool_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_git = tmp_path.parent / "system" / "git"
    system_git.parent.mkdir()
    system_git.write_text("#!/bin/sh\n", encoding="utf-8")
    system_git.chmod(0o755)
    monkeypatch.setattr(worker_workspace.shutil, "which", lambda name: str(system_git))

    normalized, roots = (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            ["git", "diff", "--check"], tmp_path
        )
    )

    assert normalized == [str(system_git.resolve()), "diff", "--check"]
    assert roots == ()


def test_node_uses_system_authority_and_publishes_nvm_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime_root = tmp_path / ".nvm" / "versions" / "node" / "v24.15.0"
    system_node = _executable(runtime_root / "bin" / "node")
    monkeypatch.setattr(
        worker_workspace.shutil,
        "which",
        lambda name: str(system_node) if name == "node" else None,
    )

    normalized, roots = (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            ["node", "test/check.js"], repo
        )
    )

    assert normalized == [str(system_node.resolve()), "test/check.js"]
    assert roots == (runtime_root.resolve(),)


def test_node_capability_preflight_uses_the_same_system_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_node = _executable(tmp_path.parent / "system" / "node")
    monkeypatch.setattr(
        worker_workspace.shutil,
        "which",
        lambda name: str(system_node) if name == "node" else None,
    )
    monkeypatch.setattr(
        worker_workspace,
        "_declared_workspace_seed_closure",
        lambda *args: ((), (), ()),
    )

    assert worker_workspace.preflight_validation_capabilities(
        tmp_path,
        {"allowed_writes": [], "validation": ["node test/check.js"]},
    ) == ()


def test_git_system_tool_authority_rejects_repository_owned_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_git = tmp_path / "bin" / "git"
    repo_git.parent.mkdir()
    repo_git.write_text("#!/bin/sh\n", encoding="utf-8")
    repo_git.chmod(0o755)
    monkeypatch.setattr(worker_workspace.shutil, "which", lambda name: str(repo_git))

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_executable_repository_owned",
    ):
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            ["git", "diff", "--check"], tmp_path
        )


def test_repo_venv_mypy_preferred_over_system(tmp_path, monkeypatch) -> None:
    repo_mypy = _runtime_executable(tmp_path / ".venv", "mypy")

    sys_prefix = tmp_path / "sys"
    sys_mypy = _runtime_executable(sys_prefix, "mypy")

    fake_module = tmp_path / "src" / "aiworkhub" / "worker_workspace.py"
    fake_module.parent.mkdir(parents=True, exist_ok=True)
    fake_module.touch()
    monkeypatch.setattr(worker_workspace, "__file__", str(fake_module))

    approved = set(worker_workspace._TRUSTED_VALIDATION_BARE_EXECUTABLES)
    with monkeypatch.context() as m:
        m.setattr(
            worker_workspace,
            "_TRUSTED_VALIDATION_BARE_EXECUTABLES",
            approved | {"mypy"},
        )
        m.setattr(worker_workspace.sys, "prefix", str(sys_prefix))
        m.setattr(worker_workspace.sys, "base_prefix", "/something/else")
        m.delenv("VIRTUAL_ENV", raising=False)

        result = worker_workspace._resolve_trusted_validation_executable(
            "mypy", repo=None
        )

    assert result.path == repo_mypy.resolve()
    assert result.root == (tmp_path / ".venv").resolve(strict=False)


def test_run_validations_mypy_declared_argv_bare_executed_is_repo_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    mypy = _runtime_executable(tmp_path / ".venv", "mypy")
    captured: dict[str, object] = {}
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(
        worker_workspace.sys, "prefix", worker_workspace.sys.base_prefix
    )
    monkeypatch.setattr(
        worker_workspace, "select_sandbox_backend", lambda: "landlock"
    )
    monkeypatch.setattr(
        worker_workspace,
        "provision_validation_exec_scratch",
        lambda ws: tmp_path / "scratch",
    )
    monkeypatch.setattr(
        worker_workspace, "cleanup_validation_exec_scratch", lambda path: None
    )
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
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(worker_workspace.subprocess, "run", fake_run)

    result = worker_workspace.run_validations(workspace, ["mypy check src"])

    assert result[0]["declared_command"] == "mypy check src"
    assert result[0]["declared_argv"] == ["mypy", "check", "src"]
    assert result[0]["argv"] == [str(mypy.resolve()), "check", "src"]
    assert result[0]["executed_argv"] == [str(mypy.resolve()), "check", "src"]
    assert result[0]["argv_rewritten"] is True
    assert captured["argv"] == [str(mypy.resolve()), "check", "src"]
    assert captured["shell"] is False


def test_run_validations_bare_pytest_executes_trusted_repo_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NF-2026-00271: a bare ``pytest`` command must execute through the trusted
    repository interpreter (``sys.executable -m pytest``) when a trusted pytest
    runtime root is available -- never through an unverifiable PATH ``pytest``."""
    workspace = _workspace(tmp_path)
    runtime_root = tmp_path / "project" / ".venv"
    runtime_root.mkdir(parents=True)
    interpreter = runtime_root / worker_workspace._python_interpreter_relative_paths()[0]
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    interpreter.chmod(0o700)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        worker_workspace,
        "_resolve_trusted_validation_executable",
        lambda name, repo=None: worker_workspace.TrustedValidationExecutable(
            path=runtime_root / "bin" / name,
            root=runtime_root.resolve(strict=False),
        ),
    )
    monkeypatch.setattr(
        worker_workspace,
        "resolve_trusted_pytest_runtime_root",
        lambda: runtime_root.resolve(strict=False),
    )
    monkeypatch.setattr(
        worker_workspace,
        "_validation_pythonpath_readonly_dirs",
        lambda components: tuple(Path(c) for c in components),
    )
    monkeypatch.setattr(
        worker_workspace,
        "resolve_validation_pythonpath",
        lambda workspace, backend, components: str(runtime_root.resolve(strict=False)),
    )
    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", lambda: "landlock")
    monkeypatch.setattr(
        worker_workspace,
        "provision_validation_exec_scratch",
        lambda ws: tmp_path / "scratch",
    )
    monkeypatch.setattr(
        worker_workspace, "cleanup_validation_exec_scratch", lambda path: None
    )
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
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(worker_workspace.subprocess, "run", fake_run)

    result = worker_workspace.run_validations(workspace, ["pytest -q"])

    expected = [str(interpreter), "-P", "-m", "pytest", "-q"]
    assert result[0]["executed_argv"] == expected
    assert result[0]["argv"] == expected
    assert result[0]["argv_rewritten"] is True
    assert captured["argv"] == expected
    assert captured["shell"] is False
