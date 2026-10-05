"""The dotnet lane's own decisions, provable off Windows and without dotnet.

NF-2026-01366: ``_appcontainer_dotnet_validation_argv``,
``_appcontainer_dotnet_env``, ``_appcontainer_dotnet_prerestore`` and the
``_appcontainer_dotnet_validation_command`` wrapper the lane calls are the whole
adaptation a dotnet command gets, so pinning them here keeps the container
contract in one file.  Every host call is injected -- the pre-restore's
``subprocess.run``, the dotnet resolution -- so no case needs dotnet, MSBuild,
NuGet or an AppContainer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import worker_workspace_appcontainer_dotnet as dotnet_lane

_ARTIFACTS = r"C:\req\home\dotnet-artifacts"
_SWITCHES = {
    "DOTNET_NOLOGO": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "MSBUILDDISABLENODEREUSE": "1",
    "DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER": "1",
}


def _workspace(tmp_path: Path):
    repo, worktree, home = tmp_path / "repo", tmp_path / "wt", tmp_path / "home"
    for directory in (repo, worktree, home):
        directory.mkdir(exist_ok=True)
    return SimpleNamespace(repo=repo, path=worktree, home=home)


def _install_fake_run(monkeypatch, *, exit_code=0, error=None, sink=None):
    """Replace the pre-restore's subprocess *and* the dotnet resolution."""
    real = subprocess

    class _FakeSubprocess:
        TimeoutExpired = real.TimeoutExpired

        @staticmethod
        def run(command, **kwargs):
            if sink is not None:
                sink.append((list(command), dict(kwargs)))
            if error is not None:
                raise error
            return real.CompletedProcess(command, exit_code, "", "")

    monkeypatch.setattr(dotnet_lane, "subprocess", _FakeSubprocess)
    monkeypatch.setattr(
        dotnet_lane,
        "shutil",
        SimpleNamespace(which=lambda _name: r"C:\tools\dotnet.exe"),
    )


def _lane_argv(argv):
    return dotnet_lane._appcontainer_dotnet_validation_argv(argv, _ARTIFACTS)


def test_dotnet_host_is_matched_by_basename_case_insensitively():
    for executable in (
        "dotnet",
        "dotnet.exe",
        "DOTNET.EXE",
        r"C:\Program Files\dotnet\dotnet.exe",
        "C:/tools/dotnet",
    ):
        assert dotnet_lane._is_appcontainer_dotnet_executable(executable)
    for executable in ("node", "node.exe", "dotnetx", "dotnet.cmd", "", "C:/x/notdotnet"):
        assert not dotnet_lane._is_appcontainer_dotnet_executable(executable)


def test_rewrite_inserts_every_lane_flag_and_is_idempotent():
    for verb in ("build", "test", "restore", "publish"):
        declared = ["dotnet", verb, "app.csproj"]
        once = _lane_argv(declared)
        assert once == [
            "dotnet",
            verb,
            "--artifacts-path",
            _ARTIFACTS,
            "-m:1",
            "-nodeReuse:false",
            "-p:UseSharedCompilation=false",
            "-p:NuGetAudit=false",
            "app.csproj",
        ]
        assert _lane_argv(once) == once
        assert declared == ["dotnet", verb, "app.csproj"]


def test_rewrite_lands_before_a_bare_separator_and_keeps_the_tail_whole():
    rewritten = _lane_argv(
        ["dotnet", "test", "--logger", "trx", "--", "--filter", "Name~X"]
    )
    marker = rewritten.index("--")
    assert rewritten[marker:] == ["--", "--filter", "Name~X"]
    assert all(
        flag in rewritten[:marker]
        for flag in (
            "--artifacts-path",
            "-m:1",
            "-nodeReuse:false",
            "-p:UseSharedCompilation=false",
            "-p:NuGetAudit=false",
        )
    )


def test_a_caller_declared_flag_always_wins_and_is_never_duplicated():
    declared = [
        "dotnet",
        "build",
        "-m:4",
        "-nodeReuse:true",
        "-p:UseSharedCompilation=true",
        "/p:NuGetAudit=true",
        "--artifacts-path",
        r"D:\out",
        "app.csproj",
    ]
    assert _lane_argv(declared) == declared


def test_rewrite_inserts_only_what_the_caller_did_not_declare():
    rewritten = _lane_argv(
        ["dotnet", "test", "-maxcpucount:4", "-property:NuGetAudit=true"]
    )
    assert "-m:1" not in rewritten
    assert "-p:NuGetAudit=false" not in rewritten
    assert "-nodeReuse:false" in rewritten
    assert "-p:UseSharedCompilation=false" in rewritten
    assert "--artifacts-path" in rewritten


def test_rewrite_leaves_non_adapted_verbs_and_foreign_argv_alone():
    for argv in (
        ["dotnet", "--info"],
        ["dotnet", "run"],
        ["dotnet", "pack"],
        ["dotnet"],
        [],
        ["cargo", "test"],
        ["python", "-m", "pytest"],
    ):
        assert _lane_argv(argv) == [str(part) for part in argv]


def test_env_is_request_scoped_under_home_and_never_mutates_the_input(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    home = tmp_path / "home"
    declared = {
        "PATH": "x",
        "DOTNET_CLI_HOME": r"C:\Users\host",
        "APPDATA": r"C:\Users\host\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\host\AppData\Local",
        "NUGET_PACKAGES": r"C:\Users\host\.nuget\packages",
    }
    before = dict(declared)

    scoped = dotnet_lane._appcontainer_dotnet_env(declared, home)

    assert declared == before
    assert scoped is not declared
    assert scoped["PATH"] == "x"
    assert scoped["DOTNET_CLI_HOME"] == str(home)
    assert scoped["APPDATA"] == str(home / "AppData" / "Roaming")
    assert scoped["LOCALAPPDATA"] == str(home / "AppData" / "Local")
    assert scoped["NUGET_PACKAGES"] == str(home / ".nuget" / "packages")
    assert {key: scoped[key] for key in _SWITCHES} == _SWITCHES
    assert (home / "AppData" / "Roaming").is_dir()
    assert (home / "AppData" / "Local").is_dir()
    assert (home / ".nuget" / "packages").is_dir()


def test_env_copies_the_host_platform_variables_only_when_absent(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("PROGRAMDATA", r"C:\ProgramData")
    monkeypatch.setenv("PROGRAMFILES", r"C:\Program Files")
    monkeypatch.setenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)")

    scoped = dotnet_lane._appcontainer_dotnet_env({}, tmp_path)

    assert scoped["PROGRAMDATA"] == r"C:\ProgramData"
    assert scoped["PROGRAMFILES"] == r"C:\Program Files"
    assert scoped["PROGRAMFILES(X86)"] == r"C:\Program Files (x86)"
    kept = dotnet_lane._appcontainer_dotnet_env({"PROGRAMDATA": "declared"}, tmp_path)
    assert kept["PROGRAMDATA"] == "declared"


def test_prerestore_restores_the_canonical_tree_and_runs_once(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    (workspace.repo / "app.csproj").write_text("<Project />", encoding="utf-8")
    # A same-named candidate file must never be the tree that is restored.
    (workspace.path / "app.csproj").write_text("<Project />", encoding="utf-8")
    monkeypatch.setenv("NUGET_PACKAGES", str(tmp_path / "host-packages"))
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)
    argv = dotnet_lane._appcontainer_dotnet_validation_argv(
        ["dotnet", "build", "app.csproj"], workspace.home / "dotnet-artifacts"
    )

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            argv, workspace, workspace.path, workspace.home
        )
        is None
    )
    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            argv, workspace, workspace.path, workspace.home
        )
        is None
    )

    assert len(runs) == 1
    command, kwargs = runs[0]
    assert command[0] == r"C:\tools\dotnet.exe"
    assert command[1] == "restore"
    assert command[command.index("--packages") + 1] == str(
        workspace.home / ".nuget" / "packages"
    )
    assert command[command.index("--source") + 1] == str(tmp_path / "host-packages")
    assert "-p:NuGetAudit=false" in command
    assert "-nodeReuse:false" in command
    assert command[command.index("--artifacts-path") + 1] == str(
        workspace.home / "dotnet-prerestore"
    )
    assert kwargs["shell"] is False
    assert kwargs["timeout"] > 0
    repo = Path(workspace.repo).resolve()
    worktree = Path(workspace.path).resolve()
    cwd = Path(kwargs["cwd"]).resolve()
    assert cwd.is_relative_to(repo)
    assert not str(cwd).startswith(str(worktree))
    project = Path(command[2]).resolve()
    assert project.is_relative_to(repo)
    assert project == (repo / "app.csproj").resolve()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (subprocess.TimeoutExpired("dotnet", 600), "prerestore_timeout"),
        (FileNotFoundError("dotnet"), "prerestore_failed:FileNotFoundError"),
        (PermissionError("denied"), "prerestore_failed:PermissionError"),
    ],
)
def test_prerestore_names_every_failure_instead_of_raising(
    tmp_path, monkeypatch, outcome, expected
):
    workspace = _workspace(tmp_path)
    (workspace.repo / "app.csproj").write_text("<Project />", encoding="utf-8")
    _install_fake_run(monkeypatch, error=outcome)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "build", "app.csproj"], workspace, workspace.path, workspace.home
        )
        == expected
    )


def test_prerestore_names_a_nonzero_exit(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    (workspace.repo / "app.csproj").write_text("<Project />", encoding="utf-8")
    _install_fake_run(monkeypatch, exit_code=7)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "build", "app.csproj"], workspace, workspace.path, workspace.home
        )
        == "prerestore_exit_7"
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["dotnet", "build", "--no-restore"],
        ["dotnet", "test", "--no-build"],
        ["dotnet", "clean"],
        ["dotnet", "--info"],
        ["cargo", "build"],
    ],
)
def test_prerestore_does_not_run_when_packages_are_not_needed(
    tmp_path, monkeypatch, argv
):
    workspace = _workspace(tmp_path)
    runs = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            argv, workspace, workspace.path, workspace.home
        )
        is None
    )
    assert runs == []
    assert not (workspace.home / ".nuget").exists()


def test_prerestore_never_evaluates_the_candidate_tree(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    (workspace.path / "app.csproj").write_text("<Project />", encoding="utf-8")
    runs = []
    _install_fake_run(monkeypatch, sink=runs)
    argv = dotnet_lane._appcontainer_dotnet_validation_argv(
        ["dotnet", "build", str(workspace.path / "app.csproj")],
        workspace.home / "dotnet-artifacts",
    )

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            argv, workspace, workspace.path, workspace.home
        )
        == "no_canonical_project"
    )
    assert runs == []


def test_lane_command_rewrites_dotnet_and_types_a_blocked_restore(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    (workspace.repo / "app.csproj").write_text("<Project />", encoding="utf-8")
    _install_fake_run(monkeypatch)
    declared_env = {"PATH": "x"}

    effective_argv, scoped_env = dotnet_lane._appcontainer_dotnet_validation_command(
        ["dotnet", "build", "app.csproj"],
        workspace=workspace,
        cwd=workspace.path,
        env=declared_env,
    )

    assert declared_env == {"PATH": "x"}
    assert effective_argv[:2] == ["dotnet", "build"]
    assert "--artifacts-path" in effective_argv
    assert scoped_env["NUGET_PACKAGES"] == str(workspace.home / ".nuget" / "packages")

    monkeypatch.setattr(
        dotnet_lane,
        "_appcontainer_dotnet_prerestore",
        lambda *args, **kwargs: "no_canonical_project",
    )
    with pytest.raises(OSError) as caught:
        dotnet_lane._appcontainer_dotnet_validation_command(
            ["dotnet", "test"],
            workspace=workspace,
            cwd=workspace.path,
            env=declared_env,
        )
    assert str(caught.value) == (
        "validation_executable_unavailable:dotnet_prerestore:no_canonical_project"
    )
