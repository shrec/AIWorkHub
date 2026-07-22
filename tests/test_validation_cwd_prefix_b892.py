"""B892 regression suite: shell-free ``cd <relative-repo-path> &&`` validation prefix.

B891 reproduced a card validation command shaped
``cd tools/geoai-task-mcp && python3 -m pytest ...`` tokenizing with ``cd``
as argv[0] -- an unresolvable executable (ENOENT), since ``cd`` is a shell
builtin, not a binary. These tests prove the parser now recognizes and
strips exactly one such leading prefix without ever invoking a shell (no
``shell=True``, no ``bash -c``/``sh -c`` wrapping), that the validated
relative directory is bound as the child's cwd identically under both
Landlock and bubblewrap, and that every unsafe shape (absolute path, parent
traversal, symlink escape, multiple chained ``cd``, stray control operators)
fails closed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_workspace  # noqa: E402


# ---------------------------------------------------------------------------
# Parser-level tests (pure functions, no filesystem/sandbox involved)
# ---------------------------------------------------------------------------


def test_cd_prefix_is_extracted_and_argv_stays_unchanged() -> None:
    argv, components = worker_workspace.parse_validation_command(
        "cd tools/geoai-task-mcp && python3 -m pytest -q tests/x.py"
    )
    assert argv == ["python3", "-m", "pytest", "-q", "tests/x.py"]
    assert components == ()


def test_cd_prefix_detail_reports_relative_path() -> None:
    detailed = worker_workspace._parse_validation_command_detailed(
        "cd tools/geoai-task-mcp && python3 -m pytest -q tests/x.py"
    )
    assert detailed == (
        ["python3", "-m", "pytest", "-q", "tests/x.py"],
        (),
        None,
        "tools/geoai-task-mcp",
    )


def test_cd_prefix_composes_with_existing_pythonpath_prefix() -> None:
    argv, components, tmpdir_override, cd_relative = (
        worker_workspace._parse_validation_command_detailed(
            "cd sub && PYTHONPATH=. python3 -m pytest -q x.py"
        )
    )
    assert argv == ["python3", "-m", "pytest", "-q", "x.py"]
    assert components == (".",)
    assert tmpdir_override is None
    assert cd_relative == "sub"


def test_cd_prefix_composes_with_existing_tmpdir_prefix() -> None:
    argv, components, tmpdir_override, cd_relative = (
        worker_workspace._parse_validation_command_detailed(
            "cd sub && TMPDIR=/dev/shm bash test.sh"
        )
    )
    assert argv == ["bash", "test.sh"]
    assert components == ()
    assert tmpdir_override == "/dev/shm"
    assert cd_relative == "sub"


def test_no_cd_prefix_is_unaffected() -> None:
    assert worker_workspace._parse_validation_command_detailed(
        "python3 -m pytest -q x.py"
    ) == (["python3", "-m", "pytest", "-q", "x.py"], (), None, None)
    assert worker_workspace._parse_validation_command_detailed(
        "PYTHONPATH=. python3 -m pytest -q x.py"
    ) == (["python3", "-m", "pytest", "-q", "x.py"], (".",), None, None)


@pytest.mark.parametrize(
    "command,match",
    [
        ("cd /abs/escape && python3 x.py", "invalid_repo_path"),
        ("cd ../escape && python3 x.py", "unsafe_repo_path"),
        ("cd a/../../escape && python3 x.py", "unsafe_repo_path"),
        ("cd a && cd b && python3 x.py", "validation_multiple_cd_prefix_forbidden"),
        ("cd a && python3 x.py && rm -rf /", "validation_shell_syntax_forbidden"),
        ("cd a && python3 x.py &", "validation_shell_syntax_forbidden"),
        ("cd a && python3 x.py && echo x &", "validation_shell_syntax_forbidden"),
        ("cd a python3 x.py", "validation_cd_prefix_malformed"),
        ("cd && python3 x.py", "validation_cd_prefix_malformed"),
        ("cd a &&", "validation_cd_prefix_without_executable"),
        ("cd a&b && python3 x.py", "validation_cd_path_forbidden_char"),
        ("cd $(whoami) && python3 x.py", "validation_cd_path_forbidden_char"),
        ("python3 x.py && rm -rf /", "validation_shell_syntax_forbidden"),
        ("python3 x.py &", "validation_shell_syntax_forbidden"),
    ],
)
def test_unsafe_cd_and_control_operator_shapes_fail_closed(
    command: str, match: str
) -> None:
    with pytest.raises(worker_workspace.WorkspaceError, match=match):
        worker_workspace._parse_validation_command_detailed(command)


def test_cd_prefix_preserves_existing_shell_metacharacter_rejection() -> None:
    # ``;``/``|``/backtick/``>``/``<`` are already rejected at the character
    # level before tokenization -- adding cd-prefix support must not weaken
    # that pre-existing gate.
    for command in (
        "cd a && python3 x.py; rm -rf /",
        "cd a && python3 x.py | rm -rf /",
        "cd a && python3 x.py > /tmp/out",
        "cd a && python3 x.py `id`",
    ):
        with pytest.raises(
            worker_workspace.WorkspaceError, match="validation_shell_syntax_forbidden"
        ):
            worker_workspace._parse_validation_command_detailed(command)


# ---------------------------------------------------------------------------
# sandbox_argv: cwd binding for both backends (argv construction only, no
# subprocess execution -- mirrors existing bubblewrap-argv-shape tests since
# bubblewrap itself needs unprivileged user namespaces this dev sandbox does
# not grant).
# ---------------------------------------------------------------------------


def _fixture_workspace(tmp_path: Path) -> worker_workspace.WorkerWorkspace:
    path = tmp_path / "worktree"
    (path / "sub").mkdir(parents=True)
    (path / "out").mkdir(parents=True)
    (path / "out" / "result.txt").write_text("v1", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True)
    return worker_workspace.WorkerWorkspace(
        request_id="b892-argv",
        repo=tmp_path,
        path=path,
        home=home,
        allowed_writes=("out/result.txt",),
        parent_baseline={},
        workspace_baseline={},
    )


def test_bubblewrap_chdir_binds_the_validated_cd_prefix_subdirectory(
    tmp_path: Path,
) -> None:
    workspace = _fixture_workspace(tmp_path)
    argv = worker_workspace.sandbox_argv(
        workspace,
        "validation",
        ["python3", "check.py"],
        backend="bubblewrap",
        validation_cwd="sub",
    )
    index = argv.index("--chdir")
    assert argv[index + 1] == f"{worker_workspace.SANDBOX_WORKSPACE}/sub"


def test_bubblewrap_chdir_defaults_to_workspace_root_without_cd_prefix(
    tmp_path: Path,
) -> None:
    workspace = _fixture_workspace(tmp_path)
    argv = worker_workspace.sandbox_argv(
        workspace, "validation", ["python3", "check.py"], backend="bubblewrap"
    )
    index = argv.index("--chdir")
    assert argv[index + 1] == worker_workspace.SANDBOX_WORKSPACE


def test_landlock_argv_carries_the_same_validated_relative_cwd(
    tmp_path: Path,
) -> None:
    workspace = _fixture_workspace(tmp_path)
    argv = worker_workspace.sandbox_argv(
        workspace,
        "validation",
        ["python3", "check.py"],
        backend="landlock",
        validation_cwd="sub",
    )
    assert "--cwd" in argv
    assert argv[argv.index("--cwd") + 1] == "sub"


def test_landlock_argv_omits_cwd_flag_without_cd_prefix(tmp_path: Path) -> None:
    workspace = _fixture_workspace(tmp_path)
    argv = worker_workspace.sandbox_argv(
        workspace, "validation", ["python3", "check.py"], backend="landlock"
    )
    assert "--cwd" not in argv


def test_sandbox_argv_rejects_symlink_escape_cwd(tmp_path: Path) -> None:
    workspace = _fixture_workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace.path / "escape_link"
    link.symlink_to(outside)
    with pytest.raises(
        worker_workspace.WorkspaceError, match="symlink_path_component_forbidden"
    ):
        worker_workspace.sandbox_argv(
            workspace,
            "validation",
            ["python3"],
            backend="bubblewrap",
            validation_cwd="escape_link",
        )


def test_sandbox_argv_rejects_missing_cwd_directory(tmp_path: Path) -> None:
    workspace = _fixture_workspace(tmp_path)
    with pytest.raises(
        worker_workspace.WorkspaceError, match="validation_cwd_not_directory"
    ):
        worker_workspace.sandbox_argv(
            workspace,
            "validation",
            ["python3"],
            backend="bubblewrap",
            validation_cwd="does_not_exist",
        )


def test_sandbox_argv_rejects_cwd_that_is_a_regular_file(tmp_path: Path) -> None:
    workspace = _fixture_workspace(tmp_path)
    with pytest.raises(
        worker_workspace.WorkspaceError, match="validation_cwd_not_directory"
    ):
        worker_workspace.sandbox_argv(
            workspace,
            "validation",
            ["python3"],
            backend="bubblewrap",
            validation_cwd="out/result.txt",
        )


# ---------------------------------------------------------------------------
# run_validations end-to-end: real Landlock execution proves the cwd lands
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tolerate_nested_seccomp_chmod_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    """See test_validation_exec_scratch_b753_v1.py -- this authoring session
    itself runs nested inside the exact Landlock+seccomp sandbox
    ``run_validations``/``sandbox_argv`` implement, and
    ``_apply_metadata_seccomp`` unconditionally denies chmod/fchmod. Swallow
    only ``PermissionError`` from chmod/fchmod so the suite still runs the
    real logic end-to-end in this nested environment: every mode bit these
    calls request is already applied atomically by the preceding
    ``mkdir``/``os.open`` mode argument, so a denied follow-up chmod never
    changes the resulting permissions. A production coordinator invoking
    these same functions runs unsandboxed and never hits this.
    """
    real_chmod = os.chmod
    real_fchmod = os.fchmod

    def _chmod(path, mode, *a, **kw):
        try:
            return real_chmod(path, mode, *a, **kw)
        except PermissionError:
            return None

    def _fchmod(fd, mode):
        try:
            return real_fchmod(fd, mode)
        except PermissionError:
            return None

    monkeypatch.setattr(os, "chmod", _chmod)
    monkeypatch.setattr(os, "fchmod", _fchmod)


def _manual_workspace(
    tmp_path: Path, request_id: str
) -> tuple[Path, worker_workspace.WorkerWorkspace]:
    """Build a ``WorkerWorkspace`` without ``create_workspace`` (which shells
    out to real ``git worktree add``): this authoring session already runs
    inside the Landlock+seccomp sandbox this module implements, so a nested
    ``git init``/``git worktree add`` cannot succeed here regardless of any
    in-process monkeypatch. Still exercises the real
    ``run_validations``/``sandbox_argv``/``_apply_landlock`` code paths this
    suite is testing, only skipping the unrelated git plumbing.
    """
    repo = tmp_path / "fake_repo"
    repo.mkdir(exist_ok=True)
    base = tmp_path / "worktrees" / request_id
    path = base / "worktree"
    home = base / "home"
    path.mkdir(parents=True)
    home.mkdir(parents=True)
    workspace = worker_workspace.WorkerWorkspace(
        request_id=request_id,
        repo=repo,
        path=path,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    return repo, workspace


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub hosted runners cannot execute nested Landlock validations",
)
def test_run_validations_executes_inside_the_cd_prefix_directory(
    tmp_path: Path,
) -> None:
    """The exact B891 shape: a card command whose real executable lives
    below a subdirectory the command first ``cd``s into."""
    repo, workspace = _manual_workspace(tmp_path, "b892-cd-exec")
    sub = workspace.path / "tools" / "geoai-task-mcp"
    sub.mkdir(parents=True)
    (sub / "check_cwd.py").write_text(
        "import os\nprint('CWD_MARKER=' + os.getcwd())\n", encoding="utf-8"
    )
    try:
        results = worker_workspace.run_validations(
            workspace,
            ["cd tools/geoai-task-mcp && python3 check_cwd.py"],
        )
        assert results[0]["returncode"] == 0
        assert results[0]["cwd"] == "tools/geoai-task-mcp"
        assert f"CWD_MARKER={sub}" in results[0]["stdout_tail"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub hosted runners cannot execute nested Landlock validations",
)
def test_run_validations_without_cd_prefix_runs_at_workspace_root(
    tmp_path: Path,
) -> None:
    repo, workspace = _manual_workspace(tmp_path, "b892-no-cd")
    (workspace.path / "check_cwd.py").write_text(
        "import os\nprint('CWD_MARKER=' + os.getcwd())\n", encoding="utf-8"
    )
    try:
        results = worker_workspace.run_validations(workspace, ["python3 check_cwd.py"])
        assert results[0]["returncode"] == 0
        assert results[0]["cwd"] is None
        assert f"CWD_MARKER={workspace.path}" in results[0]["stdout_tail"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub hosted runners cannot execute nested Landlock validations",
)
def test_run_validations_rejects_symlink_escape_cd_target(tmp_path: Path) -> None:
    repo, workspace = _manual_workspace(tmp_path, "b892-cd-symlink")
    outside = tmp_path / "outside_target"
    outside.mkdir()
    (workspace.path / "escape_link").symlink_to(outside)
    try:
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="symlink_path_component_forbidden",
        ):
            worker_workspace.run_validations(
                workspace, ["cd escape_link && python3 -c 'pass'"]
            )
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)
