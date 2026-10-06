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
from aiworkhub import worker_workspace_appcontainer_dotnet_restore as dotnet_restore

_ARTIFACTS = r"C:\req\home\dotnet-artifacts"
_SEED_DIR = "dotnet-prerestore-seed"
_SWITCHES = {
    "DOTNET_NOLOGO": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "MSBUILDDISABLENODEREUSE": "1",
    "DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER": "1",
}
# The candidate project every candidate-only case below is seeded from: one
# literal PackageReference and one literal TargetFramework, nothing else.
_EDGE_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
    "<TargetFramework>net8.0</TargetFramework></PropertyGroup>"
    '<ItemGroup><PackageReference Include="Serilog" Version="3.1.1" /></ItemGroup>'
    "</Project>"
)


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
    """An absolute candidate project is read as XML data, never restored.

    It declares no ``PackageReference``, so there is nothing to seed and no
    host restore runs at all -- and in particular the candidate file itself is
    never handed to the host CLI, which would run its project targets.
    """
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
        is None
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


# ---------------------------------------------------------------------------
# NF-2026-01366 follow-up: the three measured pre-restore causes
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_slnx_is_recognised_wherever_a_project_suffix_is(tmp_path: Path):
    """Cause 1, at the vocabulary: ``.slnx`` is a project suffix like ``.sln``."""
    assert ".slnx" in dotnet_lane._DOTNET_PROJECT_SUFFIXES
    directory = _write(tmp_path / "s" / "EntryLink.Edge.slnx", "<Solution />").parent
    assert dotnet_lane._canonical_project_in(directory)


def test_prerestore_restores_a_canonical_slnx_argument(tmp_path, monkeypatch):
    """Cause 1: a ``.slnx`` the canonical tree carries reaches the host restore."""
    workspace = _workspace(tmp_path)
    _write(
        workspace.repo / "EntryLink.Edge.slnx",
        '<Solution><Project Path="apps/edge/Edge.csproj" /></Solution>',
    )
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "EntryLink.Edge.slnx"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    assert Path(runs[0][0][2]) == workspace.repo / "EntryLink.Edge.slnx"


def test_a_named_target_never_restores_a_different_solution_in_the_cwd(
    tmp_path, monkeypatch
):
    """Cause 2: the cwd fallback must never substitute ``EntryLink.sln``."""
    workspace = _workspace(tmp_path)
    _write(workspace.repo / "EntryLink.sln", "Microsoft Visual Studio Solution File")
    _write(
        workspace.path / "EntryLink.Edge.slnx",
        '<Solution><Project Path="apps/edge/Edge.csproj" /></Solution>',
    )
    _write(workspace.path / "apps" / "edge" / "Edge.csproj", _EDGE_CSPROJ)
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "EntryLink.Edge.slnx"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    command = runs[0][0]
    assert not any("EntryLink.sln" in token for token in command)
    assert Path(command[2]).parent == workspace.home / _SEED_DIR


def test_a_candidate_only_project_is_seeded_from_xml_data_only(tmp_path, monkeypatch):
    """Cause 3: the host restore only ever sees the AIWorkHub-authored file."""
    workspace = _workspace(tmp_path)
    _write(workspace.path / "apps" / "edge" / "Edge.csproj", _EDGE_CSPROJ)
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    command, kwargs = runs[0]
    worktree = str(workspace.path)
    assert all(worktree not in token for token in command)
    assert worktree not in str(kwargs["cwd"])
    assert command[1] == "restore"
    forbidden = ("build", "test", "msbuild", "publish")
    assert not any(token in forbidden for token in command)
    seed = Path(command[2])
    assert seed.parent == workspace.home / _SEED_DIR
    generated = seed.read_text(encoding="utf-8")
    assert 'Include="Serilog" Version="3.1.1"' in generated
    assert "net8.0" in generated
    assert "Edge.csproj" not in generated


def test_the_candidate_closure_follows_references_and_central_versions(
    tmp_path, monkeypatch
):
    """A ``.slnx`` closure, a ProjectReference and central package versions."""
    workspace = _workspace(tmp_path)
    _write(
        workspace.path / "EntryLink.Edge.slnx",
        '<Solution><Folder Name="/apps/">'
        '<Project Path="apps\\edge\\Edge.csproj" /></Folder></Solution>',
    )
    _write(
        workspace.path / "apps" / "edge" / "Edge.csproj",
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
        "<TargetFrameworks>net8.0;net9.0</TargetFrameworks></PropertyGroup>"
        '<ItemGroup><PackageReference Include="Serilog" />'
        '<ProjectReference Include="..\\..\\libs\\Core\\Core.csproj" />'
        "</ItemGroup></Project>",
    )
    _write(
        workspace.path / "libs" / "Core" / "Core.csproj",
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
        "<TargetFramework>net8.0</TargetFramework></PropertyGroup>"
        '<ItemGroup><PackageReference Include="Polly" /></ItemGroup></Project>',
    )
    _write(
        workspace.path / "Directory.Packages.props",
        "<Project><ItemGroup>"
        '<PackageVersion Include="Serilog" Version="3.1.1" />'
        '<PackageVersion Include="Polly" Version="8.4.2" />'
        "</ItemGroup></Project>",
    )
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "EntryLink.Edge.slnx"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    generated = Path(runs[0][0][2]).read_text(encoding="utf-8")
    assert 'Include="Serilog" Version="3.1.1"' in generated
    assert 'Include="Polly" Version="8.4.2"' in generated
    declared = generated.split("<TargetFrameworks>")[1].split("<")[0]
    assert sorted(declared.split(";")) == ["net8.0", "net9.0"]


@pytest.mark.parametrize(
    ("project", "expected"),
    [
        ('<!DOCTYPE p [<!ENTITY x "y">]><Project />', "project_xml_unsafe"),
        ("<Project><ItemGroup></Project>", "project_xml_parse_error"),
        (
            "<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework>"
            "</PropertyGroup><ItemGroup>"
            '<PackageReference Include="Serilog" Version="$(SerilogVersion)" />'
            "</ItemGroup></Project>",
            "non_literal_package_version",
        ),
        (
            "<Project><PropertyGroup><TargetFramework>$(Tfm)</TargetFramework>"
            "</PropertyGroup><ItemGroup>"
            '<PackageReference Include="Serilog" Version="3.1.1" />'
            "</ItemGroup></Project>",
            "non_literal_target_framework",
        ),
        (
            "<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework>"
            "</PropertyGroup><ItemGroup>"
            '<PackageReference Include="Serilog" /></ItemGroup></Project>',
            "package_version_unresolved",
        ),
        (
            "<Project><ItemGroup>"
            '<PackageReference Include="Serilog" Version="3.1.1" />'
            "</ItemGroup></Project>",
            "target_framework_unresolved",
        ),
    ],
)
def test_each_unusable_candidate_xml_has_its_own_environment_reason(
    tmp_path, monkeypatch, project, expected
):
    """Every refusal is typed and named -- never a candidate validation_failed."""
    workspace = _workspace(tmp_path)
    _write(workspace.path / "apps" / "edge" / "Edge.csproj", project)
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        == expected
    )
    assert runs == []


def test_a_named_target_in_neither_tree_is_a_named_environment_reason(
    tmp_path, monkeypatch
):
    """Cause 2 again: a missing named target blocks, it does not fall back."""
    workspace = _workspace(tmp_path)
    _write(workspace.repo / "EntryLink.sln", "Microsoft Visual Studio Solution File")
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        == "candidate_target_missing"
    )
    assert runs == []


def test_an_oversized_candidate_project_is_refused_by_the_byte_bound(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    padding = " " * (dotnet_restore._MAX_PROJECT_BYTES + 1)
    _write(
        workspace.path / "apps" / "edge" / "Edge.csproj",
        f"<Project><!--{padding}--></Project>",
    )
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        == "project_xml_too_large"
    )
    assert runs == []


def test_a_candidate_closure_wider_than_the_file_bound_is_refused(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    count = dotnet_restore._MAX_PROJECT_FILES + 2
    entries = "".join(
        f'<Project Path="p{index}/p{index}.csproj" />' for index in range(count)
    )
    _write(workspace.path / "Wide.slnx", f"<Solution>{entries}</Solution>")
    for index in range(count):
        _write(
            workspace.path / f"p{index}" / f"p{index}.csproj",
            '<Project Sdk="Microsoft.NET.Sdk" />',
        )
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "Wide.slnx"],
            workspace,
            workspace.path,
            workspace.home,
        )
        == "project_file_limit"
    )
    assert runs == []


def test_a_nonzero_synthetic_restore_is_its_own_environment_reason(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    _write(workspace.path / "apps" / "edge" / "Edge.csproj", _EDGE_CSPROJ)
    _install_fake_run(monkeypatch, exit_code=7)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        == "synthetic_restore_exit_7"
    )


def test_the_synthetic_restore_adds_no_source_beyond_the_host_packages_folder(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    _write(workspace.path / "apps" / "edge" / "Edge.csproj", _EDGE_CSPROJ)
    monkeypatch.setenv("NUGET_PACKAGES", str(tmp_path / "host-packages"))
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    dotnet_lane._appcontainer_dotnet_prerestore(
        ["dotnet", "restore", "apps/edge/Edge.csproj"],
        workspace,
        workspace.path,
        workspace.home,
    )

    command = runs[0][0]
    assert command.count("--source") == 1
    assert command[command.index("--source") + 1] == str(tmp_path / "host-packages")
    assert command[command.index("--packages") + 1] == str(
        workspace.home / ".nuget" / "packages"
    )


def test_no_environment_decision_is_derived_from_candidate_authored_output(
    tmp_path, monkeypatch
):
    """validation_runner's structural-proof rule: no NU1301 string matching."""
    workspace = _workspace(tmp_path)
    _write(workspace.path / "apps" / "edge" / "Edge.csproj", _EDGE_CSPROJ)
    noisy = "error NU1301: Unable to load the service index for source"

    class _Noisy:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, noisy, noisy)

    monkeypatch.setattr(dotnet_lane, "subprocess", _Noisy)
    monkeypatch.setattr(
        dotnet_lane, "shutil", SimpleNamespace(which=lambda _name: r"C:\x\dotnet.exe")
    )

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )
    for module in (dotnet_lane, dotnet_restore):
        assert "NU1301" not in Path(module.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Rework: a canonical-backed target still has to seed what the candidate adds,
# and a Directory.Build.props declares for every project beneath it
# ---------------------------------------------------------------------------


def test_a_canonical_target_also_seeds_the_project_the_candidate_adds(
    tmp_path, monkeypatch
):
    """EntryLink 003c: the ``.slnx`` is canonical, the card adds ``apps/edge``.

    The canonical restore stays first and unchanged; a second host restore then
    seeds what the candidate added, naming only the AIWorkHub-authored file.
    """
    workspace = _workspace(tmp_path)
    _write(workspace.repo / "EntryLink.Edge.slnx", "<Solution />")
    _write(
        workspace.path / "EntryLink.Edge.slnx",
        '<Solution><Project Path="apps/edge/Edge.csproj" /></Solution>',
    )
    _write(workspace.path / "apps" / "edge" / "Edge.csproj", _EDGE_CSPROJ)
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "EntryLink.Edge.slnx"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    assert len(runs) == 2
    assert Path(runs[0][0][2]) == workspace.repo / "EntryLink.Edge.slnx"
    seed = Path(runs[1][0][2])
    assert seed == workspace.home / _SEED_DIR / dotnet_restore.SEED_PROJECT_NAME
    assert all(str(workspace.path) not in token for token in runs[1][0])
    assert 'Include="Serilog" Version="3.1.1"' in seed.read_text(encoding="utf-8")


def test_a_supplementary_seed_drops_a_non_literal_row_instead_of_refusing(
    tmp_path, monkeypatch
):
    """A literal ``PackageReference`` added beside a ``$(Prop)`` one is seeded.

    The canonical restore has already seeded every canonical declaration, so a
    row this module cannot resolve without evaluating candidate MSBuild is
    dropped rather than turned into an environment block.
    """
    workspace = _workspace(tmp_path)
    _write(workspace.repo / "apps" / "edge" / "Edge.csproj", "<Project />")
    _write(
        workspace.path / "apps" / "edge" / "Edge.csproj",
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
        "<TargetFramework>net8.0</TargetFramework></PropertyGroup><ItemGroup>"
        '<PackageReference Include="Serilog" Version="3.1.1" />'
        '<PackageReference Include="Polly" Version="$(PollyVersion)" />'
        "</ItemGroup></Project>",
    )
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    assert len(runs) == 2
    generated = Path(runs[1][0][2]).read_text(encoding="utf-8")
    assert 'Include="Serilog" Version="3.1.1"' in generated
    assert "Polly" not in generated


def test_a_candidate_directory_build_props_declares_the_target_framework(
    tmp_path, monkeypatch
):
    """A repository that sets ``<TargetFramework>`` once, above the project.

    Reading only the project files made every new project in such a repository
    look as though it declared no target framework at all.
    """
    workspace = _workspace(tmp_path)
    _write(
        workspace.path / "Directory.Build.props",
        "<Project><PropertyGroup>"
        "<TargetFramework>net9.0</TargetFramework></PropertyGroup></Project>",
    )
    _write(
        workspace.path / "apps" / "edge" / "Edge.csproj",
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
        '<PackageReference Include="Serilog" Version="3.1.1" />'
        "</ItemGroup></Project>",
    )
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    generated = Path(runs[0][0][2]).read_text(encoding="utf-8")
    assert "<TargetFrameworks>net9.0</TargetFrameworks>" in generated


def test_a_global_package_reference_is_seeded_like_any_other_row(
    tmp_path, monkeypatch
):
    """A repo-wide analyzer in ``Directory.Packages.props`` is a package too."""
    workspace = _workspace(tmp_path)
    _write(
        workspace.path / "Directory.Packages.props",
        "<Project><ItemGroup>"
        '<GlobalPackageReference Include="Roslynator.Analyzers" Version="4.12.4" />'
        "</ItemGroup></Project>",
    )
    _write(workspace.path / "apps" / "edge" / "Edge.csproj", _EDGE_CSPROJ)
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    generated = Path(runs[0][0][2]).read_text(encoding="utf-8")
    assert 'Include="Roslynator.Analyzers" Version="4.12.4"' in generated
    assert 'Include="Serilog" Version="3.1.1"' in generated


def test_a_literal_pinning_property_seeds_every_pinned_version(
    tmp_path, monkeypatch
):
    """A pinned transitive ``PackageVersion`` row is a package row too."""
    workspace = _workspace(tmp_path)
    _write(
        workspace.path / "Directory.Packages.props",
        "<Project><PropertyGroup>"
        "<CentralPackageTransitivePinningEnabled>true"
        "</CentralPackageTransitivePinningEnabled></PropertyGroup><ItemGroup>"
        '<PackageVersion Include="Serilog" Version="3.1.1" />'
        '<PackageVersion Include="SQLitePCLRaw.lib.e_sqlite3" Version="2.1.12" />'
        '<PackageVersion Include="Microsoft.OpenApi" Version="2.7.5" />'
        "</ItemGroup></Project>",
    )
    _write(workspace.path / "apps" / "edge" / "Edge.csproj", _EDGE_CSPROJ)
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    generated = Path(runs[0][0][2]).read_text(encoding="utf-8")
    assert 'Include="SQLitePCLRaw.lib.e_sqlite3" Version="2.1.12"' in generated
    assert 'Include="Microsoft.OpenApi" Version="2.7.5"' in generated
    assert 'Include="Serilog" Version="3.1.1"' in generated


@pytest.mark.parametrize("pinning", ["", "false", "$(Pin)"])
def test_without_the_literal_true_pinning_property_the_seed_is_unchanged(
    tmp_path, monkeypatch, pinning
):
    """Only the literal ``true`` property turns pinned rows into seed rows."""
    workspace = _workspace(tmp_path)
    props = "<Project>"
    if pinning:
        props += (
            "<PropertyGroup><CentralPackageTransitivePinningEnabled>"
            f"{pinning}"
            "</CentralPackageTransitivePinningEnabled></PropertyGroup>"
        )
    props += (
        "<ItemGroup>"
        '<PackageVersion Include="SQLitePCLRaw.lib.e_sqlite3" Version="2.1.12" />'
        '<PackageVersion Include="Microsoft.OpenApi" Version="2.7.5" />'
        "</ItemGroup></Project>"
    )
    _write(workspace.path / "Directory.Packages.props", props)
    _write(workspace.path / "apps" / "edge" / "Edge.csproj", _EDGE_CSPROJ)
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    generated = Path(runs[0][0][2]).read_text(encoding="utf-8")
    assert "SQLitePCLRaw.lib.e_sqlite3" not in generated
    assert "Microsoft.OpenApi" not in generated
    assert 'Include="Serilog" Version="3.1.1"' in generated


def test_a_declared_identity_keeps_its_resolution_while_pinned(
    tmp_path, monkeypatch
):
    """A project row already declared wins over the pinned ``PackageVersion``."""
    workspace = _workspace(tmp_path)
    _write(
        workspace.path / "Directory.Packages.props",
        "<Project><PropertyGroup>"
        "<CentralPackageTransitivePinningEnabled>true"
        "</CentralPackageTransitivePinningEnabled></PropertyGroup><ItemGroup>"
        '<PackageVersion Include="Serilog" Version="2.0.0" />'
        "</ItemGroup></Project>",
    )
    _write(workspace.path / "apps" / "edge" / "Edge.csproj", _EDGE_CSPROJ)
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    generated = Path(runs[0][0][2]).read_text(encoding="utf-8")
    assert generated.count('Include="Serilog"') == 1
    assert 'Include="Serilog" Version="3.1.1"' in generated


def test_a_pinned_non_literal_version_still_refuses_a_strict_seed(
    tmp_path, monkeypatch
):
    """The existing non-literal row rule applies to pinned rows too."""
    workspace = _workspace(tmp_path)
    _write(
        workspace.path / "Directory.Packages.props",
        "<Project><PropertyGroup>"
        "<CentralPackageTransitivePinningEnabled>true"
        "</CentralPackageTransitivePinningEnabled></PropertyGroup><ItemGroup>"
        '<PackageVersion Include="SQLitePCLRaw.lib.e_sqlite3" Version="$(V)" />'
        "</ItemGroup></Project>",
    )
    _write(workspace.path / "apps" / "edge" / "Edge.csproj", _EDGE_CSPROJ)
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        == "non_literal_package_version"
    )
    assert runs == []


def test_a_supplementary_seed_drops_a_non_literal_pinned_row(
    tmp_path, monkeypatch
):
    """The pinned counterpart of the existing supplementary drop rule."""
    workspace = _workspace(tmp_path)
    _write(workspace.repo / "apps" / "edge" / "Edge.csproj", "<Project />")
    _write(
        workspace.path / "Directory.Packages.props",
        "<Project><PropertyGroup>"
        "<CentralPackageTransitivePinningEnabled>true"
        "</CentralPackageTransitivePinningEnabled></PropertyGroup><ItemGroup>"
        '<PackageVersion Include="SQLitePCLRaw.lib.e_sqlite3" Version="$(V)" />'
        "</ItemGroup></Project>",
    )
    _write(
        workspace.path / "apps" / "edge" / "Edge.csproj",
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
        "<TargetFramework>net8.0</TargetFramework></PropertyGroup><ItemGroup>"
        '<PackageReference Include="Serilog" Version="3.1.1" />'
        "</ItemGroup></Project>",
    )
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    assert len(runs) == 2
    generated = Path(runs[1][0][2]).read_text(encoding="utf-8")
    assert "SQLitePCLRaw.lib.e_sqlite3" not in generated
    assert 'Include="Serilog" Version="3.1.1"' in generated


def test_a_supplementary_seed_still_refuses_unsafe_candidate_xml(
    tmp_path, monkeypatch
):
    """Dropping a row is not dropping a bound: a ``DOCTYPE`` still refuses."""
    workspace = _workspace(tmp_path)
    _write(workspace.repo / "apps" / "edge" / "Edge.csproj", "<Project />")
    _write(
        workspace.path / "apps" / "edge" / "Edge.csproj",
        '<!DOCTYPE p [<!ENTITY x "y">]><Project />',
    )
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "apps/edge/Edge.csproj"],
            workspace,
            workspace.path,
            workspace.home,
        )
        == "project_xml_unsafe"
    )
    assert len(runs) == 1


# ---------------------------------------------------------------------------
# Rework: behind a successful canonical restore, a candidate-shaped limit is
# the container build's verdict and not a host environment block
# ---------------------------------------------------------------------------


_EDGE_SLNF = (
    '{"solution": {"path": "EntryLink.sln",'
    ' "projects": ["apps/edge/Edge.csproj"]}}'
)
# A classic solution row for a project the card deleted from the worktree.
_SLN_LISTING_OLD = (
    "Microsoft Visual Studio Solution File, Format Version 12.00\n"
    'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Old", '
    '"libs\\Old.csproj", "{1E2D3C4B-5A69-4788-8899-AABBCCDDEEFF}"\n'
    "EndProject\n"
)


def test_a_canonical_slnf_target_is_not_blocked_by_the_kind_gate(
    tmp_path, monkeypatch
):
    """A ``.slnf`` is a project suffix the seed reads only as a target name.

    The canonical filter file is restored unchanged; the supplementary seed
    behind it has no XML shape of its own to read, and a target the canonical
    restore already covered is nothing left to seed rather than a block on every
    canonical ``.slnf`` validation.
    """
    workspace = _workspace(tmp_path)
    _write(workspace.repo / "EntryLink.Edge.slnf", _EDGE_SLNF)
    _write(workspace.path / "EntryLink.Edge.slnf", _EDGE_SLNF)
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "EntryLink.Edge.slnf"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    assert len(runs) == 1
    assert Path(runs[0][0][2]) == workspace.repo / "EntryLink.Edge.slnf"


def test_a_project_the_card_deleted_does_not_block_a_canonical_solution(
    tmp_path, monkeypatch
):
    """A solution row whose project the card deleted is candidate-caused.

    The canonical restore has already seeded every canonical declaration, so the
    container build is what decides a missing project -- as ``validation_failed``
    -- instead of this seed filing it as an environment block.
    """
    workspace = _workspace(tmp_path)
    _write(workspace.repo / "EntryLink.sln", _SLN_LISTING_OLD)
    _write(workspace.repo / "libs" / "Old.csproj", "<Project />")
    # The candidate copy still lists libs/Old.csproj; the card deleted the file.
    _write(workspace.path / "EntryLink.sln", _SLN_LISTING_OLD)
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "EntryLink.sln"],
            workspace,
            workspace.path,
            workspace.home,
        )
        is None
    )

    assert len(runs) == 1
    assert Path(runs[0][0][2]) == workspace.repo / "EntryLink.sln"


def test_a_strict_seed_still_names_a_project_the_solution_lists_but_lacks(
    tmp_path, monkeypatch
):
    """Nothing else seeds a candidate-only solution, so the row still refuses."""
    workspace = _workspace(tmp_path)
    _write(workspace.path / "EntryLink.sln", _SLN_LISTING_OLD)
    runs: list[tuple[list[str], dict]] = []
    _install_fake_run(monkeypatch, sink=runs)

    assert (
        dotnet_lane._appcontainer_dotnet_prerestore(
            ["dotnet", "restore", "EntryLink.sln"],
            workspace,
            workspace.path,
            workspace.home,
        )
        == "candidate_project_missing"
    )
    assert runs == []
