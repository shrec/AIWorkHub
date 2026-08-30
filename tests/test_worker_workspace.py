from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_workspace  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


def test_cleanup_unregisters_a_registered_worktree_whose_directory_is_missing(
    repo: Path,
    tmp_path: Path,
) -> None:
    request_id = "req-missing-registration"
    root = tmp_path / "worktrees" / request_id
    path = root / "worktree"
    home = root / "home"
    assert _git(repo, "worktree", "add", "--detach", str(path), "HEAD").returncode == 0
    home.mkdir(parents=True)
    # Model the externally removed/crashed workspace that originally left a
    # prunable Git administrative record behind.
    import shutil

    shutil.rmtree(path)
    before = _git(repo, "worktree", "list", "--porcelain").stdout
    assert str(path) in before

    worker_workspace.cleanup_workspace(repo, path, home)

    after = _git(repo, "worktree", "list", "--porcelain").stdout
    assert str(path) not in after
    assert not root.exists()


def test_cleanup_registered_worktree_is_process_free(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    tmp_path: Path,
) -> None:
    request_id = "req-process-free-cleanup"
    root = tmp_path / "worktrees" / request_id
    path = root / "worktree"
    home = root / "home"
    assert _git(repo, "worktree", "add", "--detach", str(path), "HEAD").returncode == 0
    home.mkdir(parents=True)
    before = _git(repo, "worktree", "list", "--porcelain").stdout
    assert str(path) in before

    def subprocess_forbidden(*_args, **_kwargs):
        raise AssertionError("cleanup must not spawn Git")

    monkeypatch.setattr(worker_workspace, "_run", subprocess_forbidden)
    worker_workspace.cleanup_workspace(repo, path, home, git_timeout=0.01)

    assert not root.exists()
    after = _git(repo, "worktree", "list", "--porcelain").stdout
    assert str(path) not in after


def test_cleanup_fails_closed_on_mismatched_reciprocal_registration(
    repo: Path,
    tmp_path: Path,
) -> None:
    request_id = "req-mismatched-registration"
    root = tmp_path / "worktrees" / request_id
    path = root / "worktree"
    home = root / "home"
    assert _git(repo, "worktree", "add", "--detach", str(path), "HEAD").returncode == 0
    home.mkdir(parents=True)
    marker = path / ".git"
    admin_dir = worker_workspace._gitdir_pointer(
        marker, label="test_workspace_marker"
    )
    (admin_dir / "gitdir").write_text(
        str(tmp_path / "different" / ".git"), encoding="utf-8"
    )

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="workspace_cleanup_registration_mismatch",
    ):
        worker_workspace.cleanup_workspace(repo, path, home)
    assert root.exists()


def test_cleanup_accepts_bounded_partial_forward_registration(
    repo: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "worktrees" / "req-partial-registration"
    path = root / "worktree"
    home = root / "home"
    assert _git(repo, "worktree", "add", "--detach", str(path), "HEAD").returncode == 0
    home.mkdir(parents=True)
    admin_dir = worker_workspace._gitdir_pointer(
        path / ".git", label="test_workspace_marker"
    )
    (admin_dir / "gitdir").unlink()

    worker_workspace.cleanup_workspace(repo, path, home)

    assert not root.exists()
    assert not admin_dir.exists()


def test_cleanup_unregistered_request_workspace_needs_no_git_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "synthetic-repo"
    repo.mkdir()
    root = tmp_path / "worktrees" / "req-unregistered"
    path = root / "worktree"
    home = root / "home"
    path.mkdir(parents=True)
    home.mkdir()

    def subprocess_forbidden(*_args, **_kwargs):
        raise AssertionError("unregistered cleanup must not spawn Git")

    monkeypatch.setattr(worker_workspace, "_run", subprocess_forbidden)
    worker_workspace.cleanup_workspace(repo, path, home)

    assert not root.exists()


def test_create_workspace_timeout_uses_process_free_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    run_calls: list[tuple[str, ...]] = []
    cleanup_calls: list[tuple[Path, Path, Path]] = []

    def fail_worktree_add(argv, **_kwargs):
        run_calls.append(tuple(argv))
        raise worker_workspace.GitCommandTimeout(
            phase="workspace_provision",
            argv=list(argv),
            cwd=repo,
            timeout=1.0,
            pid=4242,
            tree_terminated=True,
        )

    monkeypatch.setattr(worker_workspace, "_run", fail_worktree_add)
    monkeypatch.setattr(
        worker_workspace,
        "cleanup_workspace",
        lambda cleanup_repo, path, home: cleanup_calls.append(
            (cleanup_repo, path, home)
        ),
    )

    with pytest.raises(worker_workspace.GitCommandTimeout):
        worker_workspace.create_workspace(
            repo,
            "req-create-timeout",
            {"allowed_writes": [], "validation": []},
            "validation",
        )

    assert len(run_calls) == 1
    assert run_calls[0][:3] == ("git", "worktree", "add")
    assert len(cleanup_calls) == 1


def test_claude_credential_projection_refresh_is_narrow_atomic_and_private(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source-home"
    source_claude = source_home / ".claude"
    source_claude.mkdir(parents=True, mode=0o700)
    (source_claude / ".credentials.json").write_text(
        '{"token":"secret-v1"}\n', encoding="utf-8"
    )
    home = tmp_path / "isolated-home"
    home.mkdir(mode=0o700)
    (home / "tmp").mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(source_home))

    first = worker_workspace.refresh_claude_credential_projection(home)
    (source_claude / ".credentials.json").write_text(
        '{"token":"secret-v2"}\n', encoding="utf-8"
    )
    second = worker_workspace.refresh_claude_credential_projection(home)

    destination = home / ".claude" / ".credentials.json"
    assert first["refreshed"] is True
    assert second["refreshed"] is True
    assert destination.read_text(encoding="utf-8") == '{"token":"secret-v2"}\n'
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert "secret" not in json.dumps(second)


def test_claude_credential_projection_rejects_destination_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source-home"
    source_claude = source_home / ".claude"
    source_claude.mkdir(parents=True, mode=0o700)
    (source_claude / ".credentials.json").write_text("{}", encoding="utf-8")
    home = tmp_path / "isolated-home"
    home.mkdir(mode=0o700)
    destination_dir = home / ".claude"
    destination_dir.mkdir(mode=0o700)
    (destination_dir / ".credentials.json").symlink_to(
        tmp_path / "outside-credential"
    )
    monkeypatch.setenv("HOME", str(source_home))

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="claude_credential_destination_symlink_forbidden",
    ):
        worker_workspace.refresh_claude_credential_projection(home)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "parent"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Task MCP Tests").returncode == 0
    (root / "read").mkdir()
    (root / "out").mkdir()
    # These bytes participate in Git/tree and workspace-baseline hashes.  Use
    # byte-exact writes so Windows newline translation cannot change the
    # checked-out worktree relative to the fixture's parent tree.
    (root / "read" / "input.txt").write_bytes(b"input-v1\n")
    (root / "out" / "result.txt").write_bytes(b"result-v1\n")
    (root / "AGENTS.md").write_bytes(b"agents-v1\n")
    (root / "parent-secret.txt").write_bytes(b"secret\n")
    assert (
        _git(
            root,
            "add",
            "read/input.txt",
            "out/result.txt",
            "AGENTS.md",
            "parent-secret.txt",
        ).returncode
        == 0
    )
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    return root


def _workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path, request: str):
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "worktrees"),
    )
    return worker_workspace.create_workspace(
        repo,
        request,
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["read/input.txt"],
        },
        "validation",
    )


def test_workspace_seeds_omitted_exact_validation_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    script = repo / "scripts" / "validate.py"
    script.parent.mkdir()
    script.write_text("print('ok')\n", encoding="utf-8")
    assert _git(repo, "add", "scripts/validate.py").returncode == 0
    assert _git(repo, "commit", "-qm", "validation script").returncode == 0
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))

    workspace = worker_workspace.create_workspace(
        repo,
        "req-validation-script",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["read/input.txt"],
            "validation": ["python scripts/validate.py"],
        },
        "validation",
    )

    assert (workspace.path / "scripts" / "validate.py").is_file()


@pytest.mark.parametrize("field", ["immutable_inputs", "read_first"])
def test_workspace_rejects_missing_exact_card_input_before_git_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path, field: str
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))

    def git_launch_forbidden(*_args, **_kwargs):
        raise AssertionError("missing input must fail before worktree launch")

    monkeypatch.setattr(worker_workspace, "_run", git_launch_forbidden)
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match=rf"workspace_required_input_missing:field={field}:index=0:path=missing/input.py",
    ):
        worker_workspace.create_workspace(
            repo,
            f"req-missing-{field}",
            {"allowed_writes": ["out/result.txt"], field: ["missing/input.py"]},
            "validation",
        )


def test_workspace_rejects_missing_validation_script_before_git_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))

    def git_launch_forbidden(*_args, **_kwargs):
        raise AssertionError("missing validation script must fail before worktree launch")

    monkeypatch.setattr(worker_workspace, "_run", git_launch_forbidden)
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="workspace_required_input_missing:field=validation:index=0:path=scripts/missing.py",
    ):
        worker_workspace.create_workspace(
            repo,
            "req-missing-validation-script",
            {
                "allowed_writes": ["out/result.txt"],
                "validation": ["python scripts/missing.py"],
            },
            "validation",
        )


def test_workspace_allows_intended_output_to_be_absent_from_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))

    workspace = worker_workspace.create_workspace(
        repo,
        "req-new-output",
        {
            "allowed_writes": ["generated/result.py"],
            "required_outputs": ["generated/result.py"],
            "read_first": ["generated/result.py"],
            "validation": ["python generated/result.py"],
        },
        "validation",
    )

    assert (workspace.path / "generated" / "result.py").is_file()


def test_pinned_workspace_is_exact_base_without_live_overlay_or_placeholders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    source = repo / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("from base_dep import VALUE\n", encoding="utf-8")
    (repo / "src" / "base_dep.py").write_text(
        "VALUE = 'base'\n", encoding="utf-8"
    )
    assert _git(repo, "add", "src/example.py", "src/base_dep.py").returncode == 0
    assert _git(repo, "commit", "-qm", "pinned base").returncode == 0
    base_oid = _git(repo, "rev-parse", "HEAD").stdout.strip()

    source.write_text("from live_dep import VALUE\n", encoding="utf-8")
    (repo / "src" / "live_dep.py").write_text(
        "VALUE = 'live'\n", encoding="utf-8"
    )
    (repo / "src" / "new.py").write_text("NEW = True\n", encoding="utf-8")
    assert (
        _git(repo, "add", "src/example.py", "src/live_dep.py", "src/new.py").returncode
        == 0
    )
    assert _git(repo, "commit", "-qm", "live successor").returncode == 0

    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "pinned-worktrees"),
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "req-pinned-baseline",
        {
            "allowed_writes": ["src/example.py", "src/new.py"],
            "read_first": ["src/example.py", "src/new.py"],
            "validation": [".venv/bin/python -m mypy src/example.py"],
        },
        "validation",
        pinned_base_oid=base_oid,
    )
    try:
        assert workspace.base_oid == base_oid
        assert (workspace.path / "src" / "example.py").read_text(
            encoding="utf-8"
        ) == "from base_dep import VALUE\n"
        assert (workspace.path / "src" / "base_dep.py").read_text(
            encoding="utf-8"
        ) == "VALUE = 'base'\n"
        assert not (workspace.path / "src" / "live_dep.py").exists()
        assert not (workspace.path / "src" / "new.py").exists()
        assert workspace.parent_baseline["src/example.py"] == (
            workspace.workspace_baseline["src/example.py"]
        )
        assert workspace.parent_baseline["src/new.py"] is None
        assert workspace.workspace_baseline["src/new.py"] is None
        assert worker_workspace.python_candidate_authority(workspace)["sources"] == []
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def _commit_validation_worker_package(repo: Path) -> None:
    source_package = Path(worker_workspace.__file__).resolve().parent
    destination_package = repo / "src" / "aiworkhub"
    destination_package.mkdir(parents=True)
    for name in (
        "__init__.py",
        "_version.py",
        "platform_io.py",
        "runtime_temp.py",
        "validation_runner.py",
        "worker_workspace.py",
    ):
        shutil.copyfile(source_package / name, destination_package / name)
    (repo / "probe_candidate_import.py").write_text(
        "import os\n"
        "from aiworkhub import worker_workspace as w\n"
        "print(w.__file__)\n"
        "print(w.NF376_CANDIDATE_SENTINEL)\n"
        "print(os.environ.get(w.PYTHON_CANDIDATE_AUTHORITY_ENV, ''))\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\npythonpath = ['src']\n",
        encoding="utf-8",
    )
    candidate_test = repo / "tests/test_new_candidate_module.py"
    candidate_test.parent.mkdir()
    candidate_test.write_text(
        "from aiworkhub.new_candidate_module import VALUE\n\n"
        "def test_candidate_value():\n"
        "    assert VALUE == 'candidate-new-module'\n",
        encoding="utf-8",
    )
    assert (
        _git(
            repo,
            "add",
            "pyproject.toml",
            "src/aiworkhub",
            "tests/test_new_candidate_module.py",
            "probe_candidate_import.py",
        ).returncode
        == 0
    )
    assert _git(repo, "commit", "-qm", "validation worker package").returncode == 0


def test_validation_workspace_seeds_exact_worker_package_support_and_imports_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    _commit_validation_worker_package(repo)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "validation-worktrees"),
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "candidate-support",
        {
            "allowed_writes": ["src/aiworkhub/worker_workspace.py"],
            "read_first": [
                "src/aiworkhub/worker_workspace.py",
                "probe_candidate_import.py",
            ],
            "validation": ["PYTHONPATH=src python3 probe_candidate_import.py"],
        },
        "glm_vscode_lm",
    )
    try:
        for relative in worker_workspace._VALIDATION_WORKER_PACKAGE_SUPPORT:
            candidate = workspace.path / relative
            assert candidate.is_file()
            assert not candidate.is_symlink()
            assert hashlib.sha256(candidate.read_bytes()).digest() == hashlib.sha256(
                (repo / relative).read_bytes()
            ).digest()

        candidate_module = workspace.path / "src/aiworkhub/worker_workspace.py"
        with candidate_module.open("a", encoding="utf-8") as stream:
            stream.write("\nNF376_CANDIDATE_SENTINEL = 'candidate-worktree'\n")

        expected_authority = worker_workspace.python_candidate_authority(workspace)

        result, = worker_workspace.run_validations(
            workspace,
            ["PYTHONPATH=src python3 probe_candidate_import.py"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="glm_vscode_lm",
        )

        assert result["returncode"] == 0
        assert str(candidate_module) in result["stdout_head"]
        assert "candidate-worktree" in result["stdout_head"]
        assert expected_authority["digest"] in result["stdout_head"]
        assert result["python_candidate_authority"] == expected_authority
        assert worker_workspace.changed_paths(workspace) == [
            "src/aiworkhub/worker_workspace.py"
        ]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_python_validation_seeds_transitive_local_import_closure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    package = repo / "src/examplepkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "candidate.py").write_text(
        "from . import core\nVALUE = core.VALUE\n", encoding="utf-8"
    )
    (package / "core.py").write_text(
        "from .support import VALUE\n", encoding="utf-8"
    )
    (package / "support.py").write_text("VALUE = 'closure-ok'\n", encoding="utf-8")
    (package / "unused.py").write_text("VALUE = 'not-seeded'\n", encoding="utf-8")
    probe = repo / "probe.py"
    probe.write_text("from examplepkg.candidate import VALUE\nprint(VALUE)\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\npythonpath = ['src']\n", encoding="utf-8"
    )
    assert _git(repo, "add", "src/examplepkg", "probe.py", "pyproject.toml").returncode == 0
    assert _git(repo, "commit", "-qm", "python closure fixture").returncode == 0
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "python-closure-worktrees"),
    )

    workspace = worker_workspace.create_workspace(
        repo,
        "python-import-closure",
        {
            "allowed_writes": ["src/examplepkg/candidate.py"],
            "read_first": ["src/examplepkg/candidate.py", "probe.py"],
            "validation": ["PYTHONPATH=src python3 probe.py"],
        },
        "glm_vscode_lm",
    )
    try:
        assert (workspace.path / "src/examplepkg/core.py").is_file()
        assert (workspace.path / "src/examplepkg/support.py").is_file()
        assert not (workspace.path / "src/examplepkg/unused.py").exists()
        result, = worker_workspace.run_validations(
            workspace,
            ["PYTHONPATH=src python3 probe.py"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="glm_vscode_lm",
        )
        assert result["returncode"] == 0
        assert "closure-ok" in result["stdout_head"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_python_validation_imports_new_sparse_candidate_module_without_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    _commit_validation_worker_package(repo)
    probe = repo / "probe_candidate_import.py"
    probe.write_text(
        "from aiworkhub.new_candidate_module import VALUE\nprint(VALUE)\n",
        encoding="utf-8",
    )
    assert _git(repo, "add", "probe_candidate_import.py").returncode == 0
    assert _git(repo, "commit", "-qm", "new module probe").returncode == 0
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "new-module-worktrees"),
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "new-module-import",
        {
            "allowed_writes": ["src/aiworkhub/new_candidate_module.py"],
            "read_first": ["probe_candidate_import.py"],
            "validation": ["python3 probe_candidate_import.py"],
        },
        "glm_vscode_lm",
    )
    try:
        assert (workspace.path / "src/aiworkhub/__init__.py").is_file()
        (workspace.path / "src/aiworkhub/new_candidate_module.py").write_text(
            "VALUE = 'candidate-new-module'\n", encoding="utf-8"
        )
        result, = worker_workspace.run_validations(
            workspace,
            ["python3 probe_candidate_import.py"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="glm_vscode_lm",
        )
        assert result["returncode"] == 0
        assert "candidate-new-module" in result["stdout_head"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_pytest_validation_resolves_sparse_candidate_config_inside_worktree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    _commit_validation_worker_package(repo)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "pytest-candidate-worktrees"),
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "pytest-candidate-import",
        {
            "allowed_writes": ["src/aiworkhub/new_candidate_module.py"],
            "read_first": ["tests/test_new_candidate_module.py"],
            "validation": [
                "python3 -m pytest -q tests/test_new_candidate_module.py"
            ],
        },
        "glm_vscode_lm",
    )
    try:
        assert (workspace.path / "pyproject.toml").is_file()
        (workspace.path / "src/aiworkhub/new_candidate_module.py").write_text(
            "VALUE = 'candidate-new-module'\n", encoding="utf-8"
        )

        result, = worker_workspace.run_validations(
            workspace,
            ["python3 -m pytest -q tests/test_new_candidate_module.py"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="glm_vscode_lm",
        )

        assert result["returncode"] == 0
        assert "1 passed" in result["stdout_head"]
        assert result["python_candidate_authority"]["sources"] == [
            {
                "path": "src/aiworkhub/new_candidate_module.py",
                # Workspace provisioning creates a bounded empty placeholder
                # for an exact new allowed output, so the later candidate
                # bytes are mechanically a modification of that baseline.
                "state": "modified",
                "bytes_sha256": hashlib.sha256(
                    b"VALUE = 'candidate-new-module'\n"
                ).hexdigest(),
            }
        ]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_python_candidate_authority_tracks_added_modified_and_deleted_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    _commit_validation_worker_package(repo)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "authority-worktrees"),
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "candidate-authority",
        {
            "allowed_writes": ["src/aiworkhub/worker_workspace.py"],
            "read_first": ["src/aiworkhub/worker_workspace.py"],
            "validation": ["PYTHONPATH=src python3 probe_candidate_import.py"],
        },
        "glm_vscode_lm",
    )
    try:
        modified = workspace.path / "src/aiworkhub/worker_workspace.py"
        modified.write_bytes(modified.read_bytes() + b"\n# modified\n")
        added = workspace.path / "src/aiworkhub/added_authority.py"
        added.write_bytes(b"VALUE = 1\n")
        (workspace.path / "src/aiworkhub/runtime_temp.py").unlink()
        (workspace.path / "src/aiworkhub/ignored.txt").write_bytes(b"not python\n")

        authority = worker_workspace.python_candidate_authority(workspace)
        states = {
            row["path"]: row["state"] for row in authority["sources"]
        }

        assert states == {
            "src/aiworkhub/added_authority.py": "added",
            "src/aiworkhub/runtime_temp.py": "deleted",
            "src/aiworkhub/worker_workspace.py": "modified",
        }
        assert len(authority["digest"]) == 64
        assert set(authority["digest"]) <= set("0123456789abcdef")
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_python_candidate_authority_tracks_preapplied_inherited_rework(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    candidate = worktree / "src/aiworkhub/new_retained.py"
    candidate.parent.mkdir(parents=True)
    home.mkdir()
    candidate.write_bytes(b"VALUE = 'retained'\n")
    candidate_hash = worker_workspace._hash_path(candidate)
    workspace = worker_workspace.WorkerWorkspace(
        request_id="retained-python-authority",
        repo=tmp_path,
        path=worktree,
        home=home,
        allowed_writes=("src/aiworkhub/new_retained.py",),
        parent_baseline={"src/aiworkhub/new_retained.py": None},
        workspace_baseline={"src/aiworkhub/new_retained.py": candidate_hash},
        tree_baseline={"src/aiworkhub/new_retained.py": candidate_hash},
        inherited_rework_paths=("src/aiworkhub/new_retained.py",),
    )

    authority = worker_workspace.python_candidate_authority(workspace)

    assert authority["sources"] == [
        {
            "path": "src/aiworkhub/new_retained.py",
            "state": "added",
            "bytes_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        }
    ]
    assert len(authority["digest"]) == 64


def test_zero_validation_workspace_does_not_seed_worker_package_support(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    _commit_validation_worker_package(repo)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "zero-validation-worktrees"),
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "zero-validation-support",
        {
            "allowed_writes": ["src/aiworkhub/worker_workspace.py"],
            "read_first": ["src/aiworkhub/worker_workspace.py"],
            "validation": [],
        },
        "glm_vscode_lm",
    )
    try:
        assert (workspace.path / "src/aiworkhub/worker_workspace.py").is_file()
        assert not (workspace.path / "src/aiworkhub/__init__.py").exists()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_npm_prefix_validation_seeds_immutable_support_and_runs_from_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm is not installed")
    extension = repo / "web-extension"
    (extension / "test").mkdir(parents=True)
    (extension / "package.json").write_text(
        json.dumps(
            {
                "name": "candidate-validation-fixture",
                "version": "1.0.0",
                "scripts": {"test": "node test/candidate.test.js"},
            }
        ),
        encoding="utf-8",
    )
    (extension / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "candidate-validation-fixture",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": {
                    "": {
                        "name": "candidate-validation-fixture",
                        "version": "1.0.0",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (extension / "test" / "candidate.test.js").write_text(
        "console.log('candidate-npm-support-ok');\n", encoding="utf-8"
    )
    assert _git(repo, "add", "web-extension").returncode == 0
    assert _git(repo, "commit", "-qm", "npm validation support").returncode == 0
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "npm-validation-worktrees"),
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "npm-validation-support",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["read/input.txt"],
            "validation": ["npm --prefix web-extension test"],
        },
        "glm_vscode_lm",
    )
    try:
        for relative in (
            "web-extension/package.json",
            "web-extension/package-lock.json",
            "web-extension/test/candidate.test.js",
        ):
            candidate = workspace.path / relative
            assert candidate.is_file()
            assert hashlib.sha256(candidate.read_bytes()).digest() == hashlib.sha256(
                (repo / relative).read_bytes()
            ).digest()

        result, = worker_workspace.run_validations(
            workspace,
            ["npm --prefix web-extension test"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="glm_vscode_lm",
        )
        assert result["returncode"] == 0
        assert "candidate-npm-support-ok" in result["stdout_head"]
        assert worker_workspace.changed_paths(workspace) == []
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_npm_prefix_validation_fails_closed_for_unbound_dependency_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    extension = repo / "dependent-extension"
    extension.mkdir()
    (extension / "package.json").write_text(
        json.dumps({"name": "dependent", "version": "1.0.0"}), encoding="utf-8"
    )
    (extension / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "dependent",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "dependent", "version": "1.0.0"},
                    "node_modules/example": {"version": "2.0.0"},
                },
            }
        ),
        encoding="utf-8",
    )
    assert _git(repo, "add", "dependent-extension").returncode == 0
    assert _git(repo, "commit", "-qm", "dependent validation fixture").returncode == 0
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "dependent-validation-worktrees"),
    )
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_npm_dependency_tree_unbound:dependent-extension:1",
    ):
        worker_workspace.create_workspace(
            repo,
            "npm-dependency-unbound",
            {
                "allowed_writes": ["out/result.txt"],
                "read_first": ["read/input.txt"],
                "validation": ["npm --prefix dependent-extension test"],
            },
            "glm_vscode_lm",
        )


def test_js_validation_seeds_transitive_local_require_closure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """GLM runtime-retention MODULE_NOT_FOUND reproduction (NF-2026-00448/458):
    a declared JS validation entrypoint's local ``require('./x')`` chain must
    be seeded transitively so the sparse worktree never fails with
    ``MODULE_NOT_FOUND`` for a sibling module the task card never declared."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    extension = repo / "vscode-extension"
    extension.mkdir()
    (extension / "extension.js").write_text(
        "const { retain } = require('./runtime-retention');\n"
        "console.log(retain());\n",
        encoding="utf-8",
    )
    (extension / "runtime-retention.js").write_text(
        "const { bridge } = require('./runtime-language-model-bridge');\n"
        "module.exports = { retain: () => bridge() };\n",
        encoding="utf-8",
    )
    (extension / "runtime-language-model-bridge.js").write_text(
        "const { boundary } = require('./runtime-provider-boundary');\n"
        "module.exports = { bridge: () => boundary() };\n",
        encoding="utf-8",
    )
    (extension / "runtime-provider-boundary.js").write_text(
        "module.exports = { boundary: () => 'runtime-retention-closure-ok' };\n",
        encoding="utf-8",
    )
    (extension / "unused.js").write_text(
        "module.exports = { unused: () => 'not-seeded' };\n", encoding="utf-8"
    )
    assert _git(repo, "add", "vscode-extension").returncode == 0
    assert _git(repo, "commit", "-qm", "js require closure fixture").returncode == 0
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "js-closure-worktrees"),
    )

    workspace = worker_workspace.create_workspace(
        repo,
        "js-require-closure",
        {
            "allowed_writes": ["vscode-extension/extension.js"],
            "read_first": ["vscode-extension/extension.js"],
            "validation": ["node vscode-extension/extension.js"],
        },
        "glm_vscode_lm",
    )
    try:
        assert (workspace.path / "vscode-extension/runtime-retention.js").is_file()
        assert (
            workspace.path / "vscode-extension/runtime-language-model-bridge.js"
        ).is_file()
        assert (
            workspace.path / "vscode-extension/runtime-provider-boundary.js"
        ).is_file()
        assert not (workspace.path / "vscode-extension/unused.js").exists()

        (result,) = worker_workspace.run_validations(
            workspace,
            ["node vscode-extension/extension.js"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="glm_vscode_lm",
        )
        assert result["returncode"] == 0, result["stderr_tail"]
        assert "MODULE_NOT_FOUND" not in result["stderr_tail"]
        assert "runtime-retention-closure-ok" in result["stdout_tail"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_js_validation_seeds_relative_package_json_require(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """Review finding (NF-2026-00448/458 rework): ``_resolve_one_local_js_require``
    matched only an exact ``.js`` suffix or ``<target>/index.js``, so an explicit
    ``require('../package.json')`` -- as used by canonical fixtures such as
    ``vscode-extension/test/stable-runtime-upgrade.test.js`` and
    ``multirepo-connecting.test.js`` -- was mangled into a nonexistent
    ``package.json.js`` and raised ``validation_js_require_unresolved`` during
    sparse workspace creation. Bounded Node-local resolution must seed the exact
    ``.json`` file the require names, while a sibling file no require ever
    reaches stays absent from the sparse worktree."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    extension = repo / "vscode-extension"
    extension.mkdir()
    (extension / "package.json").write_text(
        json.dumps({"name": "pkg-require-fixture", "version": "1.2.3"}),
        encoding="utf-8",
    )
    test_dir = extension / "test"
    test_dir.mkdir()
    (test_dir / "pkg-require.test.js").write_text(
        "const pkg = require('../package.json');\n"
        "console.log(pkg.version);\n",
        encoding="utf-8",
    )
    (test_dir / "unused-fixture.js").write_text(
        "module.exports = { unused: () => 'not-seeded' };\n", encoding="utf-8"
    )
    assert _git(repo, "add", "vscode-extension").returncode == 0
    assert (
        _git(repo, "commit", "-qm", "js relative package.json require fixture").returncode
        == 0
    )
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "js-package-json-worktrees"),
    )

    workspace = worker_workspace.create_workspace(
        repo,
        "js-require-package-json",
        {
            "allowed_writes": ["vscode-extension/test/pkg-require.test.js"],
            "read_first": ["vscode-extension/test/pkg-require.test.js"],
            "validation": ["node vscode-extension/test/pkg-require.test.js"],
        },
        "glm_vscode_lm",
    )
    try:
        assert (workspace.path / "vscode-extension/package.json").is_file()
        assert not (workspace.path / "vscode-extension/test/unused-fixture.js").exists()

        (result,) = worker_workspace.run_validations(
            workspace,
            ["node vscode-extension/test/pkg-require.test.js"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="glm_vscode_lm",
        )
        assert result["returncode"] == 0, result["stderr_tail"]
        assert "1.2.3" in result["stdout_tail"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_git_subprocess_environment_is_noninteractive_and_closed_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GIT_ASKPASS", "interactive-helper")
    monkeypatch.setenv("git_dir", str(tmp_path / "redirected"))
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    script = (
        "import os,sys;"
        "print(os.environ.get('GIT_ASKPASS'));"
        "print(os.environ.get('git_dir'));"
        "print(os.environ.get('GIT_OPTIONAL_LOCKS'));"
        "print(os.environ.get('GIT_TERMINAL_PROMPT'));"
        "print(len(sys.stdin.read()))"
    )
    result = worker_workspace._run(
        [sys.executable, "-c", script], cwd=tmp_path, phase="workspace_git"
    )
    assert result.stdout.splitlines() == ["None", "None", "0", "0", "0"]


def test_repository_head_oid_is_process_free_and_tracks_head(repo: Path) -> None:
    first = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert worker_workspace._repository_head_oid(repo) == first
    assert _git(repo, "pack-refs", "--all").returncode == 0
    assert worker_workspace._repository_head_oid(repo) == first
    assert _git(repo, "commit", "--allow-empty", "-qm", "next head").returncode == 0
    second = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert second != first
    assert worker_workspace._repository_head_oid(repo) == second


def test_preflight_cache_is_head_bound_and_failures_are_not_cached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    calls: list[str] = []

    def fake_workspace(repo_path, request_id, _card, _adapter_id):
        calls.append(request_id)
        root = tmp_path / request_id
        path = root / "worktree"
        home = root / "home"
        path.mkdir(parents=True)
        home.mkdir()
        return worker_workspace.WorkerWorkspace(
            request_id=request_id,
            repo=Path(repo_path),
            path=path,
            home=home,
            allowed_writes=(),
            parent_baseline={},
            workspace_baseline={},
            tree_baseline={},
            base_oid="a" * 40,
        )

    monkeypatch.setattr(worker_workspace, "create_workspace", fake_workspace)
    monkeypatch.setattr(
        worker_workspace, "enforce_scope", lambda _workspace, **_kwargs: []
    )
    monkeypatch.setattr(
        worker_workspace, "cleanup_workspace", lambda *_args, **_kwargs: None
    )
    worker_workspace._FINALIZATION_PROBE_CACHE.clear()
    first = worker_workspace.finalization_preflight_probe(repo, "validation")
    second = worker_workspace.finalization_preflight_probe(repo, "validation")
    assert first["ok"] is True and first["cache_hit"] is False
    assert second["ok"] is True and second["cache_hit"] is True
    assert len(calls) == 1

    assert _git(repo, "commit", "--allow-empty", "-qm", "invalidate probe").returncode == 0
    third = worker_workspace.finalization_preflight_probe(repo, "validation")
    assert third["ok"] is True and third["cache_hit"] is False
    assert len(calls) == 2

    def fail_workspace(*_args, **_kwargs):
        calls.append("failure")
        raise worker_workspace.WorkspaceError("synthetic_provision_failure")

    monkeypatch.setattr(worker_workspace, "create_workspace", fail_workspace)
    worker_workspace._FINALIZATION_PROBE_CACHE.clear()
    failed_one = worker_workspace.finalization_preflight_probe(repo, "validation")
    failed_two = worker_workspace.finalization_preflight_probe(repo, "validation")
    assert failed_one["ok"] is False and failed_one["cache_hit"] is False
    assert failed_two["ok"] is False and failed_two["cache_hit"] is False
    assert calls[-2:] == ["failure", "failure"]


def test_nonblocking_preflight_coalesces_and_publishes_success(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = 0

    def fake_probe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(2.0)
        finished.set()
        return {
            "ok": True,
            "status": "ready",
            "reason": "",
            "phase": "preflight_finalization",
            "cache_hit": False,
        }

    monkeypatch.setattr(worker_workspace, "finalization_preflight_probe", fake_probe)
    worker_workspace._FINALIZATION_PROBE_CACHE.clear()
    worker_workspace._FINALIZATION_PROBE_FAILURES.clear()
    worker_workspace._FINALIZATION_PROBE_ACTIVE.clear()

    before = time.monotonic()
    first = worker_workspace.finalization_preflight_probe_nonblocking(repo, "validation")
    assert time.monotonic() - before < 0.5
    assert first["status"] == "probing"
    assert first["reason"] == "worker_finalization_probe_started"
    assert started.wait(1.0)

    second = worker_workspace.finalization_preflight_probe_nonblocking(repo, "validation")
    assert second["status"] == "probing"
    assert second["reason"] == "worker_finalization_probe_running"
    assert calls == 1

    release.set()
    assert finished.wait(1.0)
    deadline = time.monotonic() + 1.0
    while worker_workspace._FINALIZATION_PROBE_ACTIVE and time.monotonic() < deadline:
        time.sleep(0.01)
    third = worker_workspace.finalization_preflight_probe_nonblocking(repo, "validation")
    assert third["ok"] is True
    assert third["status"] == "ready"
    assert third["cache_hit"] is True
    assert calls == 1


def test_nonblocking_preflight_caches_failure_for_bounded_cooldown(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    finished = threading.Event()
    calls = 0

    def fake_probe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        finished.set()
        return {
            "ok": False,
            "status": "blocked",
            "reason": "synthetic_probe_failure",
            "phase": "preflight_finalization",
            "cache_hit": False,
        }

    monkeypatch.setattr(worker_workspace, "finalization_preflight_probe", fake_probe)
    worker_workspace._FINALIZATION_PROBE_CACHE.clear()
    worker_workspace._FINALIZATION_PROBE_FAILURES.clear()
    worker_workspace._FINALIZATION_PROBE_ACTIVE.clear()

    first = worker_workspace.finalization_preflight_probe_nonblocking(repo, "validation")
    assert first["status"] == "probing"
    assert finished.wait(1.0)
    deadline = time.monotonic() + 1.0
    while worker_workspace._FINALIZATION_PROBE_ACTIVE and time.monotonic() < deadline:
        time.sleep(0.01)

    failed = worker_workspace.finalization_preflight_probe_nonblocking(
        repo, "validation", failure_cache_seconds=30.0
    )
    assert failed["ok"] is False
    assert failed["status"] == "blocked"
    assert failed["reason"] == "synthetic_probe_failure"
    assert failed["cache_hit"] is True
    assert calls == 1

    retry = worker_workspace.finalization_preflight_probe_nonblocking(
        repo, "validation", failure_cache_seconds=0.0
    )
    assert retry["status"] == "probing"
    deadline = time.monotonic() + 1.0
    while worker_workspace._FINALIZATION_PROBE_ACTIVE and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == 2


def test_default_workspace_root_is_repo_local_runtime_boundary(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    monkeypatch.delenv(worker_workspace.WORKTREE_ROOT_ENV, raising=False)
    monkeypatch.delenv(worker_workspace.RUNTIME_ROOT_ENV, raising=False)

    assert worker_workspace.configured_runtime_root(repo) == (
        repo / ".aiworkhub" / "runtime"
    ).resolve()
    assert worker_workspace.configured_worktree_root(repo) == (
        repo / ".aiworkhub" / "runtime" / "worktrees"
    ).resolve()


def test_workspace_can_use_exact_repo_local_runtime_root(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    monkeypatch.delenv(worker_workspace.WORKTREE_ROOT_ENV, raising=False)
    monkeypatch.delenv(worker_workspace.RUNTIME_ROOT_ENV, raising=False)

    workspace = worker_workspace.create_workspace(
        repo,
        "repo-local-runtime",
        {"allowed_writes": ["out/result.txt"]},
        "validation",
    )
    try:
        assert workspace.path == (
            repo
            / ".aiworkhub"
            / "runtime"
            / "worktrees"
            / "repo-local-runtime"
            / "worktree"
        ).resolve()
        assert repo in workspace.path.parents
        assert worker_workspace.enforce_scope(workspace) == []
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_workspace_rejects_noncanonical_root_inside_parent_repo(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(repo / "unsafe-worktrees")
    )

    with pytest.raises(
        worker_workspace.WorkspaceError, match="worktree_root_inside_parent_repo"
    ):
        worker_workspace.create_workspace(
            repo,
            "unsafe-repo-root",
            {"allowed_writes": ["out/result.txt"]},
            "validation",
        )


def test_workspace_is_detached_and_seeded_parent_changes_are_not_worker_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    (repo / "read" / "input.txt").write_text("parent-dirty-input\n", encoding="utf-8")
    (repo / "out" / "result.txt").write_text("parent-dirty-result\n", encoding="utf-8")
    workspace = _workspace(monkeypatch, tmp_path, repo, "seeded")
    try:
        assert workspace.path != repo
        assert repo not in workspace.path.parents
        assert _git(workspace.path, "symbolic-ref", "-q", "HEAD").returncode != 0
        assert worker_workspace.enforce_scope(workspace) == []

        (workspace.path / "read" / "input.txt").write_text("worker-outside\n", encoding="utf-8")
        with pytest.raises(worker_workspace.WorkspaceError, match="scope_violation"):
            worker_workspace.enforce_scope(workspace)
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_workspace_creation_uses_bounded_metadata_not_post_create_git_probes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    real_run = worker_workspace._run
    calls: list[tuple[str, ...]] = []

    def guarded_run(argv, **kwargs):
        calls.append(tuple(argv))
        if len(argv) > 1 and argv[1] in {"symbolic-ref", "rev-parse"}:
            raise AssertionError(f"post-create Git probe forbidden: {argv}")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(worker_workspace, "_run", guarded_run)
    workspace = worker_workspace.create_workspace(
        repo,
        "metadata-only-probe",
        {"allowed_writes": ["out/result.txt"]},
        "validation",
    )
    try:
        assert len(workspace.base_oid or "") in {40, 64}
        assert [call[1] for call in calls if len(call) > 1] == [
            "worktree",
            "sparse-checkout",
            "sparse-checkout",
            "read-tree",
        ]
        assert "--no-checkout" in calls[0]
        assert not (workspace.path / "parent-secret.txt").exists()
        assert (workspace.path / "out" / "result.txt").is_file()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_sparse_workspace_detects_modified_added_deleted_and_renamed_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "worktrees"),
    )
    cases = (
        ("modified", lambda root: (root / "out/result.txt").write_bytes(b"changed\n"), ["out/result.txt"]),
        ("added", lambda root: (root / "out/new.txt").write_bytes(b"new\n"), ["out/new.txt"]),
        ("deleted", lambda root: (root / "out/result.txt").unlink(), ["out/result.txt"]),
        (
            "renamed",
            lambda root: (root / "out/result.txt").replace(root / "out/renamed.txt"),
            ["out/renamed.txt", "out/result.txt"],
        ),
    )
    for request_id, mutate, expected in cases:
        workspace = worker_workspace.create_workspace(
            repo,
            request_id,
            {
                "allowed_writes": [
                    "out/result.txt",
                    "out/new.txt",
                    "out/renamed.txt",
                ],
                "read_first": ["read/input.txt"],
            },
            "validation",
        )
        try:
            assert not (workspace.path / "parent-secret.txt").exists()
            mutate(workspace.path)
            assert worker_workspace.enforce_scope(workspace) == expected
        finally:
            worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_cleanup_recovers_after_exact_worktree_remove_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "cleanup-timeout")
    real_run = worker_workspace._run

    def timeout_remove(argv, **kwargs):
        if argv[:4] == ["git", "worktree", "remove", "--force"]:
            raise worker_workspace.GitCommandTimeout(
                phase="workspace_cleanup",
                argv=argv,
                cwd=Path(kwargs["cwd"]),
                timeout=float(kwargs["timeout"]),
                pid=4242,
                tree_terminated=True,
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(worker_workspace, "_run", timeout_remove)
    worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)
    assert not workspace.path.parent.exists()
    assert str(workspace.path) not in _git(repo, "worktree", "list", "--porcelain").stdout


def test_preflight_cleanup_timeout_reports_the_actual_cleanup_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    root = tmp_path / "synthetic-preflight"
    path = root / "worktree"
    home = root / "home"
    path.mkdir(parents=True)
    home.mkdir()
    workspace = worker_workspace.WorkerWorkspace(
        request_id="preflight-synthetic",
        repo=repo,
        path=path,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
        tree_baseline={},
        provisioning_timings_ms={"worktree_create": 1.0},
        base_oid="a" * 40,
    )
    monkeypatch.setattr(worker_workspace, "create_workspace", lambda *_args, **_kwargs: workspace)
    monkeypatch.setattr(worker_workspace, "enforce_scope", lambda *_args, **_kwargs: [])

    def fail_cleanup(*_args, **_kwargs):
        raise worker_workspace.GitCommandTimeout(
            phase="workspace_cleanup",
            argv=["git", "worktree", "remove", "--force", str(path)],
            cwd=repo,
            timeout=5.0,
            pid=4242,
            tree_terminated=True,
        )

    monkeypatch.setattr(worker_workspace, "cleanup_workspace", fail_cleanup)
    result = worker_workspace.finalization_preflight_probe(
        repo,
        "synthetic-cleanup-timeout",
        cache_seconds=0,
    )
    assert result["ok"] is False
    assert result["phase"] == "workspace_cleanup"
    assert result["command"] == f"git worktree remove --force {path}"
    assert "workspace_cleanup_git_timeout" in result["reason"]


def test_isolated_worktree_metadata_rejects_symbolic_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "symbolic-head-rejection",
        {"allowed_writes": ["out/result.txt"]},
        "validation",
    )
    try:
        admin_dir = worker_workspace._gitdir_pointer(
            workspace.path / ".git", label="test_worktree_marker"
        )
        (admin_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="worktree_is_not_detached_and_isolated",
        ):
            worker_workspace._isolated_worktree_base_oid(repo, workspace.path)
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_rework_workspace_materializes_hash_pinned_predecessor_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    predecessor = worker_workspace.create_workspace(
        repo,
        "predecessor",
        {"allowed_writes": ["out/result.txt"]},
        "validation",
    )
    successor = None
    try:
        candidate = predecessor.path / "out" / "result.txt"
        candidate.write_bytes(b"reviewed candidate\n")
        candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        successor = worker_workspace.create_workspace(
            repo,
            "successor",
            {
                "allowed_writes": ["out/result.txt"],
                "rework_predecessor": {
                    "schema_id": "aiworkhub.rework_predecessor.v1",
                    "request_id": "predecessor",
                    "workspace": predecessor.as_metadata(),
                    "changed_path_hashes": {"out/result.txt": candidate_hash},
                },
            },
            "validation",
        )
        successor_output = successor.path / "out" / "result.txt"
        assert successor_output.read_text(encoding="utf-8") == "reviewed candidate\n"
        assert successor.workspace_baseline["out/result.txt"].endswith(candidate_hash)
        assert successor.inherited_rework_paths == ("out/result.txt",)
        assert worker_workspace.enforce_scope(successor) == ["out/result.txt"]

        records = worker_workspace.validate_required_outputs(
            successor, ["out/result.txt"]
        )
        assert records[0]["unchanged_allowed"] is False
        assert worker_workspace.promote(successor, ["out/result.txt"]) == [
            "out/result.txt"
        ]
        assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == (
            "reviewed candidate\n"
        )

        successor_output.write_bytes(b"reworked candidate\n")
        assert worker_workspace.enforce_scope(successor) == ["out/result.txt"]
    finally:
        if successor is not None:
            worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)


        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)


def _rewrite_rework_delta_packet(
    descriptor: dict[str, str], mutate: object
) -> dict[str, str]:
    artifact_path = Path(descriptor["path"])
    packet = json.loads(artifact_path.read_text(encoding="utf-8"))
    mutate(packet)  # type: ignore[operator]
    payload = {key: value for key, value in packet.items() if key != "canonical_digest"}
    packet["canonical_digest"] = worker_workspace._rework_delta_canonical_digest(
        payload
    )
    encoded = json.dumps(packet, indent=2, ensure_ascii=True).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    rewritten = artifact_path.parent / f"{digest}.json"
    rewritten.write_bytes(encoded)
    return {"path": str(rewritten), "digest": digest}


def test_rework_delta_artifact_round_trips_changed_and_deleted_files(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "deleted.txt").write_text("old\n", encoding="utf-8")
    changed = b"new bytes\n"
    descriptor = worker_workspace.seal_rework_delta_artifact(
        repo,
        "task-1",
        "request-1",
        1,
        [("src/changed.txt", changed), ("src/deleted.txt", None)],
        tmp_path / "artifacts",
    )

    seeded = worker_workspace.materialize_rework_delta_artifact(
        descriptor,
        repo,
        "request-1",
        "task-1",
        1,
        worktree,
        {
            "src/changed.txt": hashlib.sha256(changed).hexdigest(),
            "src/deleted.txt": None,
        },
        ("src/changed.txt", "src/deleted.txt"),
    )

    assert seeded == ["src/changed.txt", "src/deleted.txt"]
    assert (worktree / "src" / "changed.txt").read_bytes() == changed
    assert not (worktree / "src" / "deleted.txt").exists()


def test_rework_delta_artifact_rejects_tampered_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    content = b"candidate\n"
    descriptor = worker_workspace.seal_rework_delta_artifact(
        repo,
        "task-1",
        "request-1",
        1,
        [("result.txt", content)],
        tmp_path / "artifacts",
    )
    Path(descriptor["path"]).write_bytes(b"{}")

    with pytest.raises(
        worker_workspace.WorkspaceError, match="rework_delta_artifact_tampered"
    ):
        worker_workspace.materialize_rework_delta_artifact(
            descriptor,
            repo,
            "request-1",
            "task-1",
            1,
            worktree,
            {"result.txt": hashlib.sha256(content).hexdigest()},
            ("result.txt",),
        )


def test_rework_delta_artifact_rejects_cross_identity_and_scope(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    content = b"candidate\n"
    expected = {"result.txt": hashlib.sha256(content).hexdigest()}
    descriptor = worker_workspace.seal_rework_delta_artifact(
        repo,
        "task-1",
        "request-1",
        1,
        [("result.txt", content)],
        tmp_path / "artifacts",
    )
    for authority_repo, request_id, task_id, claim_epoch in (
        (tmp_path / "other", "request-1", "task-1", 1),
        (repo, "request-2", "task-1", 1),
        (repo, "request-1", "task-2", 1),
        (repo, "request-1", "task-1", 2),
    ):
        with pytest.raises(
            worker_workspace.WorkspaceError, match="rework_delta_identity_mismatch"
        ):
            worker_workspace.materialize_rework_delta_artifact(
                descriptor,
                authority_repo,
                request_id,
                task_id,
                claim_epoch,
                worktree,
                expected,
                ("result.txt",),
            )
    with pytest.raises(
        worker_workspace.WorkspaceError, match="rework_predecessor_outside_scope"
    ):
        worker_workspace.materialize_rework_delta_artifact(
            descriptor,
            repo,
            "request-1",
            "task-1",
            1,
            worktree,
            expected,
            ("other.txt",),
        )


def test_rework_delta_artifact_rejects_incomplete_unexpected_and_duplicate(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    content = b"candidate\n"
    content_hash = hashlib.sha256(content).hexdigest()
    descriptor = worker_workspace.seal_rework_delta_artifact(
        repo,
        "task-1",
        "request-1",
        1,
        [("result.txt", content)],
        tmp_path / "artifacts",
    )
    common = (repo, "request-1", "task-1", 1, worktree)

    with pytest.raises(
        worker_workspace.WorkspaceError, match="rework_delta_artifact_incomplete"
    ):
        worker_workspace.materialize_rework_delta_artifact(
            descriptor,
            *common,
            {"result.txt": content_hash, "missing.txt": None},
            ("result.txt", "missing.txt"),
        )
    with pytest.raises(
        worker_workspace.WorkspaceError, match="rework_delta_unexpected_path"
    ):
        worker_workspace.materialize_rework_delta_artifact(
            descriptor, *common, {}, ("result.txt",)
        )

    duplicate = _rewrite_rework_delta_packet(
        descriptor, lambda packet: packet["files"].append(dict(packet["files"][0]))
    )
    with pytest.raises(
        worker_workspace.WorkspaceError, match="rework_delta_duplicate_path"
    ):
        worker_workspace.materialize_rework_delta_artifact(
            duplicate,
            *common,
            {"result.txt": content_hash},
            ("result.txt",),
        )


def test_rework_delta_artifact_rejects_invalid_paths_and_bounds(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for claim_epoch in (True, 0, "1"):
        with pytest.raises(
            worker_workspace.WorkspaceError, match="rework_delta_identity_missing"
        ):
            worker_workspace.seal_rework_delta_artifact(
                repo,
                "task-1",
                "request-1",
                claim_epoch,  # type: ignore[arg-type]
                [("result.txt", b"candidate")],
                tmp_path / "artifacts",
            )
    for raw_path in (None, "", "../outside", "/absolute", ".git/config"):
        with pytest.raises(worker_workspace.WorkspaceError):
            worker_workspace.seal_rework_delta_artifact(
                repo,
                "task-1",
                "request-1",
                1,
                [(raw_path, None)],  # type: ignore[list-item]
                tmp_path / "artifacts",
            )
    with pytest.raises(
        worker_workspace.WorkspaceError, match="rework_delta_file_count_exceeds_limit"
    ):
        worker_workspace.seal_rework_delta_artifact(
            repo,
            "task-1",
            "request-1",
            1,
            [(f"rows/{index}.txt", None) for index in range(513)],
            tmp_path / "artifacts",
        )
    with pytest.raises(
        worker_workspace.WorkspaceError, match="rework_delta_content_exceeds_limit"
    ):
        worker_workspace.seal_rework_delta_artifact(
            repo,
            "task-1",
            "request-1",
            1,
            [("large.bin", b"x" * (worker_workspace.MAX_REWORK_OVERLAY_CONTENT_BYTES + 1))],
            tmp_path / "artifacts",
        )


def test_rework_delta_artifact_materializes_after_predecessor_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    predecessor = worker_workspace.create_workspace(
        repo,
        "delta-predecessor",
        {"allowed_writes": ["out/result.txt"]},
        "validation",
    )
    candidate = predecessor.path / "out" / "result.txt"
    candidate.write_bytes(b"sealed candidate\n")
    content = candidate.read_bytes()
    content_hash = hashlib.sha256(content).hexdigest()
    predecessor_metadata = predecessor.as_metadata()
    descriptor = worker_workspace.seal_rework_delta_artifact(
        repo,
        "task-1",
        "delta-predecessor",
        1,
        [("out/result.txt", content)],
        tmp_path / "artifacts",
    )
    worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)

    successor = worker_workspace.create_workspace(
        repo,
        "delta-successor",
        {
            "allowed_writes": ["out/result.txt"],
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": "delta-predecessor",
                "task_id": "task-1",
                "claim_epoch": 1,
                "workspace": predecessor_metadata,
                "changed_path_hashes": {"out/result.txt": content_hash},
                "delta_artifact": descriptor,
            },
        },
        "validation",
    )
    try:
        assert (successor.path / "out" / "result.txt").read_bytes() == content
        assert successor.inherited_rework_paths == ("out/result.txt",)
    finally:
        worker_workspace.cleanup_workspace(repo, successor.path, successor.home)


def test_validate_required_outputs_replay_authorization_permits_hash_pinned_unchanged_predecessor_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    predecessor = worker_workspace.create_workspace(
        repo,
        "replay-predecessor",
        {"allowed_writes": ["out/result.txt"]},
        "validation",
    )
    successor = None
    try:
        candidate = predecessor.path / "out" / "result.txt"
        # Byte-identical to the canonical repo: no real delta to promote, so
        # the ordinary inherited-change carve-out (B561) never applies here.
        candidate.write_bytes(b"result-v1\n")
        candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        successor = worker_workspace.create_workspace(
            repo,
            "replay-successor",
            {
                "allowed_writes": ["out/result.txt"],
                "rework_predecessor": {
                    "schema_id": "aiworkhub.rework_predecessor.v1",
                    "request_id": "replay-predecessor",
                    "workspace": predecessor.as_metadata(),
                    "changed_path_hashes": {"out/result.txt": candidate_hash},
                },
            },
            "validation",
        )
        assert successor.inherited_rework_paths == ("out/result.txt",)

        with pytest.raises(
            worker_workspace.WorkspaceError, match="required_output_mismatch:"
        ):
            worker_workspace.validate_required_outputs(successor, ["out/result.txt"])

        authorization = {
            "task_id": "T-REPLAY",
            "actor": "codex",
            "predecessor_request_id": "replay-predecessor",
            "changed_path_hashes": {"out/result.txt": candidate_hash},
            "authorized_at": "2026-08-07T00:00:00+00:00",
            "next_claim_epoch": 3,
            "one_episode_binding": True,
        }
        current_identity = {
            "replay_task_id": "T-REPLAY",
            "replay_actor": "codex",
            "replay_predecessor_request_id": "replay-predecessor",
            "replay_claim_epoch": 3,
        }

        records = worker_workspace.validate_required_outputs(
            successor,
            ["out/result.txt"],
            replay_authorization=authorization,
            **current_identity,
        )
        assert records[0]["unchanged_allowed"] is True
        assert records[0]["replay_evidence"]["sha256"] == candidate_hash
        assert records[0]["replay_evidence"]["claim_epoch"] == 3
        assert records[0]["replay_evidence"]["task_id"] == "T-REPLAY"

        for bad_authorization in (
            None,
            {**authorization, "task_id": "WRONG"},
            {**authorization, "actor": "someone-else"},
            {**authorization, "predecessor_request_id": "other-request"},
            {**authorization, "changed_path_hashes": {"out/result.txt": "0" * 64}},
            {**authorization, "one_episode_binding": False},
            {**authorization, "next_claim_epoch": 4},
        ):
            with pytest.raises(
                worker_workspace.WorkspaceError, match="required_output_mismatch:"
            ):
                worker_workspace.validate_required_outputs(
                    successor,
                    ["out/result.txt"],
                    replay_authorization=bad_authorization,
                    **current_identity,
                )

        for bad_key, bad_value in (
            ("replay_task_id", "WRONG"),
            ("replay_actor", "someone-else"),
            ("replay_predecessor_request_id", "other-request"),
            ("replay_claim_epoch", 4),
        ):
            mismatched_identity = {**current_identity, bad_key: bad_value}
            with pytest.raises(
                worker_workspace.WorkspaceError, match="required_output_mismatch:"
            ):
                worker_workspace.validate_required_outputs(
                    successor,
                    ["out/result.txt"],
                    replay_authorization=authorization,
                    **mismatched_identity,
                )

        # Ordinary non-authorized rework still fails closed.
        with pytest.raises(
            worker_workspace.WorkspaceError, match="required_output_mismatch:"
        ):
            worker_workspace.validate_required_outputs(successor, ["out/result.txt"])
    finally:
        if successor is not None:
            worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)

def test_residual_contract_allows_only_declared_json_pointer_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    predecessor = worker_workspace.create_workspace(
        repo,
        "json-predecessor",
        {"allowed_writes": ["out/result.json"]},
        "validation",
    )
    successor = None
    try:
        candidate = predecessor.path / "out" / "result.json"
        candidate.write_text(
            json.dumps({"rows": [{"id": 1, "value": "keep"}, {"id": 2, "value": "bad"}]}),
            encoding="utf-8",
        )
        candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        card = {
            "allowed_writes": ["out/result.json"],
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": "json-predecessor",
                "workspace": predecessor.as_metadata(),
                "changed_path_hashes": {"out/result.json": candidate_hash},
                "residual_identities": [
                    {"path": "out/result.json", "pointer": "/rows/1"},
                ],
            },
        }
        successor = worker_workspace.create_workspace(
            repo, "json-successor", card, "validation"
        )
        manifest = worker_workspace.build_residual_contract_manifest(successor, card)
        output = successor.path / "out" / "result.json"
        payload = json.loads(output.read_text(encoding="utf-8"))
        payload["rows"][1]["value"] = "fixed"
        output.write_text(json.dumps(payload), encoding="utf-8")
        assert worker_workspace.validate_residual_contract(successor, manifest)[0]["pass"]

        payload["rows"][0]["value"] = "unexpected"
        output.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="residual_contract_non_residual_changed",
        ):
            worker_workspace.validate_residual_contract(successor, manifest)
    finally:
        if successor is not None:
            worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)


def test_residual_contract_supports_whole_file_code_rework(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    predecessor = worker_workspace.create_workspace(
        repo,
        "code-predecessor",
        {"allowed_writes": ["src/repair.py"]},
        "validation",
    )
    successor = None
    try:
        candidate = predecessor.path / "src" / "repair.py"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("VALUE = 'bad'\n", encoding="utf-8")
        candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        card = {
            "allowed_writes": ["src/repair.py"],
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": "code-predecessor",
                "workspace": predecessor.as_metadata(),
                "changed_path_hashes": {"src/repair.py": candidate_hash},
                "residual_identities": [
                    {
                        "path": "src/repair.py",
                        "pointer": "/CORRECTNESS-REPAIR-001",
                    },
                ],
            },
        }
        successor = worker_workspace.create_workspace(
            repo, "code-successor", card, "validation"
        )
        manifest = worker_workspace.build_residual_contract_manifest(successor, card)
        assert manifest[0]["scope"] == "whole_file"
        unchanged = worker_workspace.validate_residual_contract(successor, manifest)[0]
        assert unchanged["pass"] is True
        assert unchanged["scope"] == "whole_file"
        assert unchanged["changed"] is False
        assert unchanged["observed_file_hash"] == unchanged["predecessor_file_hash"]

        output = successor.path / "src" / "repair.py"
        output.write_text("VALUE = 'fixed'\n", encoding="utf-8")
        result = worker_workspace.validate_residual_contract(successor, manifest)[0]
        assert result["pass"] is True
        assert result["scope"] == "whole_file"
        assert result["changed"] is True
        assert result["observed_file_hash"] != result["predecessor_file_hash"]
    finally:
        if successor is not None:
            worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)


def test_claude_workspace_preseeds_exact_project_trust_without_parent_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    workspace = worker_workspace.create_workspace(
        repo,
        "claude-trust",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["read/input.txt"],
        },
        "claude_cli",
    )
    try:
        config_path = workspace.home / ".claude.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config == {
            "projects": {
                str(repo.resolve()): {
                    "hasTrustDialogAccepted": True,
                    "projectOnboardingSeenCount": 1,
                }
            }
        }
        assert os.name == "nt" or stat.S_IMODE(config_path.stat().st_mode) == 0o600
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_promotion_is_scope_checked_parent_guarded_and_restart_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "promote")
    try:
        (workspace.path / "out" / "result.txt").write_text("worker-result\n", encoding="utf-8")
        changed = worker_workspace.enforce_scope(workspace)
        assert changed == ["out/result.txt"]
        assert worker_workspace.promote(workspace, changed) == changed
        assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "worker-result\n"
        assert worker_workspace.promote(workspace, changed) == changed
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)

    conflict = _workspace(monkeypatch, tmp_path, repo, "conflict")
    try:
        (conflict.path / "out" / "result.txt").write_text("worker-two\n", encoding="utf-8")
        (repo / "out" / "result.txt").write_text("owner-two\n", encoding="utf-8")
        with pytest.raises(worker_workspace.WorkspaceError, match="parent_changed_since_launch"):
            worker_workspace.promote(conflict, ["out/result.txt"])
        assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "owner-two\n"
    finally:
        worker_workspace.cleanup_workspace(repo, conflict.path, conflict.home)


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true"
    or worker_workspace.nested_sandbox_requires_host_boundary(),
    reason="The current host boundary cannot execute nested Landlock workers",
)
def test_landlock_fallback_allows_declared_output_and_denies_parent_and_git_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "landlock")
    parent_secret = repo / "parent-secret.txt"
    parent_mode = stat.S_IMODE(parent_secret.stat().st_mode)
    script = """
from pathlib import Path
import os
import sys
Path('out/result.txt').write_text('allowed\\n', encoding='utf-8')
denied = 0
for target in (Path(sys.argv[1]), Path('.git')):
    try:
        target.write_text('forbidden\\n', encoding='utf-8')
    except PermissionError:
        denied += 1
try:
    os.chmod(sys.argv[1], 0o600)
except PermissionError:
    denied += 1
if denied != 3:
    raise SystemExit(17)
print('landlock-denied-parent-git-and-metadata')
"""
    argv = worker_workspace.sandbox_argv(
        workspace,
        "validation",
        [sys.executable, "-c", script, str(parent_secret)],
        backend="landlock",
    )
    try:
        result = subprocess.run(
            argv,
            cwd="/",
            env=worker_workspace.sanitized_env("validation", home=workspace.home),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
        )
        assert result.returncode == 0, result.stderr
        assert "landlock-denied-parent-git-and-metadata" in result.stdout
        assert (workspace.path / "out" / "result.txt").read_text(encoding="utf-8") == "allowed\n"
        assert parent_secret.read_text(encoding="utf-8") == "secret\n"
        assert stat.S_IMODE(parent_secret.stat().st_mode) == parent_mode
        assert (workspace.path / ".git").is_file()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 3,
    reason="Root-file replacement requires Landlock truncate support",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true"
    or worker_workspace.nested_sandbox_requires_host_boundary(),
    reason="The current host boundary cannot execute nested Landlock workers",
)
def test_landlock_root_file_allows_in_place_save_but_denies_sibling_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    workspace = worker_workspace.create_workspace(
        repo,
        "landlock-root-file",
        {
            "allowed_writes": ["AGENTS.md"],
            "read_first": ["read/input.txt"],
        },
        "validation",
    )
    script = """
from pathlib import Path
Path('AGENTS.md').write_text('agents-v2\\n', encoding='utf-8')
denied = 0
for target in (Path('.AGENTS.md.editor-temp'), Path('.git')):
    try:
        target.write_text('forbidden\\n', encoding='utf-8')
    except PermissionError:
        denied += 1
if denied != 2:
    raise SystemExit(17)
print('landlock-root-file-bounded')
"""
    argv = worker_workspace.sandbox_argv(
        workspace,
        "validation",
        [sys.executable, "-c", script],
        backend="landlock",
    )
    try:
        result = subprocess.run(
            argv,
            cwd="/",
            env=worker_workspace.sanitized_env("validation", home=workspace.home),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
        )
        assert result.returncode == 0, result.stderr
        assert "landlock-root-file-bounded" in result.stdout
        assert (workspace.path / "AGENTS.md").read_text(encoding="utf-8") == "agents-v2\n"
        assert not (workspace.path / ".AGENTS.md.editor-temp").exists()
        assert (workspace.path / ".git").is_file()
        assert worker_workspace.enforce_scope(workspace) == ["AGENTS.md"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_secure_sandbox_selection_fails_closed_without_bwrap_or_landlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_workspace, "_is_windows_host", lambda: False)
    monkeypatch.setenv(worker_workspace.SANDBOX_BACKEND_ENV, "auto")
    monkeypatch.setattr(worker_workspace, "_bubblewrap_usable", lambda _path: False)
    monkeypatch.setattr(worker_workspace, "landlock_abi_version", lambda: 0)
    with pytest.raises(worker_workspace.WorkspaceError, match="secure_sandbox_unavailable"):
        worker_workspace.select_sandbox_backend()


def test_workspace_rejects_git_metadata_as_an_allowed_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    with pytest.raises(worker_workspace.WorkspaceError, match="git_metadata_write_forbidden"):
        worker_workspace.create_workspace(
            repo,
            "git-metadata",
            {"allowed_writes": [".git/config"]},
            "validation",
        )


def test_sanitized_env_is_allowlisted_and_json_files_are_0600(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "allowed-adapter-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-allowed")
    monkeypatch.setenv("AIWORKHUB_REPO", "/sensitive/parent")
    monkeypatch.setenv("AIWORKHUB_ALLOW_LAUNCH", "1")
    home = tmp_path / "home"
    if os.name != "nt":
        home.mkdir(mode=0o700)
        (home / "tmp").mkdir(mode=0o700)
    env = worker_workspace.sanitized_env("claude_cli", home=home)
    assert env["HOME"] == str(home)
    assert env["ANTHROPIC_API_KEY"] == "allowed-adapter-secret"
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "AIWORKHUB_REPO" not in env
    assert "AIWORKHUB_ALLOW_LAUNCH" not in env

    target = tmp_path / "private" / "request.json"
    target.parent.mkdir()
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o644)
    worker_workspace.write_json_0600(target, {"ok": True})
    assert os.name == "nt" or stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps provisioning behavior")
def test_sanitized_env_verifies_preprovisioned_home_without_chmod(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    tmp = home / "tmp"
    home.mkdir(mode=0o700)
    tmp.mkdir(mode=0o700)

    def _deny_chmod(_path: object, _mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(worker_workspace.os, "chmod", _deny_chmod)

    env = worker_workspace.sanitized_env(
        "validation", home=home, verify_preprovisioned_home=True
    )

    assert env["HOME"] == str(home.resolve())
    assert env["TMPDIR"] == str(tmp.resolve())
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE(tmp.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps provisioning behavior")
@pytest.mark.parametrize(
    "shape,match",
    [
        ("missing_home", "sanitized_home_missing"),
        ("home_file", "sanitized_home_not_directory"),
        ("home_symlink", "sanitized_home_symlink_forbidden"),
        ("home_group_readable", "sanitized_home_not_private"),
        ("missing_tmp", "sanitized_tmp_missing"),
        ("tmp_file", "sanitized_tmp_not_directory"),
        ("tmp_symlink", "sanitized_tmp_symlink_forbidden"),
        ("tmp_group_readable", "sanitized_tmp_not_private"),
    ],
)
def test_sanitized_env_rejects_unsafe_posix_home_shapes(
    tmp_path: Path,
    shape: str,
    match: str,
) -> None:
    home = tmp_path / "home"
    tmp = home / "tmp"
    if shape == "missing_home":
        pass
    elif shape == "home_file":
        home.write_text("not a directory", encoding="utf-8")
    elif shape == "home_symlink":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        home.symlink_to(target, target_is_directory=True)
    else:
        home.mkdir(mode=0o700)
        if shape == "home_group_readable":
            os.chmod(home, 0o750)
        elif shape == "missing_tmp":
            pass
        elif shape == "tmp_file":
            tmp.write_text("not a directory", encoding="utf-8")
        elif shape == "tmp_symlink":
            target = tmp_path / "target_tmp"
            target.mkdir(mode=0o700)
            tmp.symlink_to(target, target_is_directory=True)
        else:
            tmp.mkdir(mode=0o700)
            os.chmod(tmp, 0o750)

    with pytest.raises(worker_workspace.WorkspaceError, match=match):
        worker_workspace.sanitized_env(
            "validation", home=home, verify_preprovisioned_home=True
        )


def test_bubblewrap_home_string_is_single_sourced_for_env_and_bind_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B314_F004 regression: sanitized_env(home=None) (used for the
    bubblewrap backend) and sandbox_argv's bwrap bind-mount target used to
    each call Path.home() independently and only *happened* to agree. Both
    must now derive from the one shared bubblewrap_home_env_value() so they
    can never silently diverge -- a real divergence would break credential
    lookups for a worker running under bubblewrap.
    """
    shared = worker_workspace.bubblewrap_home_env_value()

    env = worker_workspace.sanitized_env("claude_cli", home=None)
    assert env["HOME"] == shared

    workspace = worker_workspace.WorkerWorkspace(
        request_id="bwrap-home-check",
        repo=tmp_path,
        path=tmp_path / "worktree",
        home=tmp_path / "home",
        allowed_writes=("out/result.txt",),
        parent_baseline={},
        workspace_baseline={},
    )
    (workspace.path / "out").mkdir(parents=True)
    (workspace.path / "out" / "result.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv(worker_workspace.BWRAP_ENV, "/usr/bin/bwrap")
    argv = worker_workspace.sandbox_argv(
        workspace, "claude_cli", ["/bin/true"], backend="bubblewrap"
    )
    bind_index = argv.index("--bind")
    assert argv[bind_index + 1] == str(workspace.home)
    assert argv[bind_index + 2] == shared


def test_unlink_if_regular_removes_files_but_never_follows_symlinks(
    tmp_path: Path,
) -> None:
    """B314_F007 regression: the spec/cancel-marker cleanup helper must
    remove a plain regular file, but must never act through a symlink --
    neither deleting the symlink's target nor silently no-oping in a way
    that could be mistaken for success on the wrong path."""
    regular = tmp_path / "spec.json"
    regular.write_text("{}", encoding="utf-8")
    worker_workspace.unlink_if_regular(regular)
    assert not regular.exists()

    # Missing path is a silent no-op (mirrors the old missing_ok=True call).
    worker_workspace.unlink_if_regular(regular)

    target = tmp_path / "sensitive_target.txt"
    target.write_text("do-not-delete", encoding="utf-8")
    link = tmp_path / "spec_symlink.json"
    link.symlink_to(target)

    worker_workspace.unlink_if_regular(link)

    assert link.is_symlink(), "a symlinked spec path must be left untouched"
    assert target.exists() and target.read_text(encoding="utf-8") == "do-not-delete"


# ── B561: ignored required-output promotion regression ──────────────────


def _repo_with_gitignore(tmp_path: Path) -> Path:
    """Fixture repo with a .gitignore that excludes ``*.bin`` files."""
    root = tmp_path / "parent"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Task MCP Tests").returncode == 0
    (root / "read").mkdir()
    (root / "out").mkdir()
    (root / "read" / "input.txt").write_text("input-v1\n", encoding="utf-8")
    (root / "out" / "result.txt").write_text("result-v1\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.bin\n", encoding="utf-8")
    assert _git(root, "add", "read/input.txt", "out/result.txt", ".gitignore").returncode == 0
    assert _git(root, "commit", "-qm", "fixture-with-gitignore").returncode == 0
    return root


def _ignored_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
    request: str,
    extra_allowed: str | None = None,
) -> worker_workspace.WorkerWorkspace:
    allowed = ["out/result.txt"]
    if extra_allowed is not None:
        allowed.append(extra_allowed)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "worktrees"),
    )
    return worker_workspace.create_workspace(
        repo,
        request,
        {"allowed_writes": allowed, "read_first": ["read/input.txt"]},
        "validation",
    )


def test_validate_required_outputs_finds_gitignored_file_missed_by_changed_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B561: a gitignored required output (e.g. ``out/data.bin`` matched by
    glob ``out/*.bin``) is found and validated by validate_required_outputs
    even though changed_paths (git-diff + git-ls-files --exclude-standard)
    does not discover it."""
    repo = _repo_with_gitignore(tmp_path)
    workspace = _ignored_workspace(
        monkeypatch, tmp_path, repo, "b561-ignored", extra_allowed="out/*.bin"
    )
    try:
        # Worker writes a real binary payload (not a placeholder).
        (workspace.path / "out").mkdir(parents=True, exist_ok=True)
        (workspace.path / "out" / "data.bin").write_bytes(b"\x01\x02\x03\x04\x05\x06\x07\x08")

        # changed_paths should NOT see the gitignored file.
        changed = worker_workspace.changed_paths(workspace)
        assert "out/data.bin" not in changed, (
            "gitignored .bin must not appear in git-diff-derived changed_paths"
        )

        # validate_required_outputs MUST find and record it.
        records = worker_workspace.validate_required_outputs(
            workspace, ["out/*.bin"]
        )
        assert len(records) == 1
        assert records[0]["path"] == "out/data.bin"
        assert records[0]["bytes"] == 8
        assert records[0]["sha256"] is not None
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_promotion_includes_validated_required_output_not_in_changed_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B561: when a validated required output path is missing from
    changed_paths (gitignored), the caller unions it into the promotion set
    and promote() succeeds with all fail-closed guards intact."""
    repo = _repo_with_gitignore(tmp_path)
    workspace = _ignored_workspace(
        monkeypatch, tmp_path, repo, "b561-promote", extra_allowed="out/*.bin"
    )
    try:
        (workspace.path / "out").mkdir(parents=True, exist_ok=True)
        (workspace.path / "out" / "data.bin").write_bytes(b"\x01\x02\x03\x04\x05\x06\x07\x08")
        (workspace.path / "out" / "result.txt").write_bytes(b"worker-result\n")

        changed = worker_workspace.enforce_scope(workspace)
        assert "out/data.bin" not in changed  # gitignored

        required_records = worker_workspace.validate_required_outputs(
            workspace, ["out/*.bin"]
        )
        assert len(required_records) == 1

        # Simulate the B561 union fix the coordinator applies.
        validated_paths = {rec["path"] for rec in required_records}
        changed = sorted(set(changed) | validated_paths)

        promoted = worker_workspace.promote(workspace, changed)
        assert "out/data.bin" in promoted
        assert "out/result.txt" in promoted
        assert (repo / "out" / "data.bin").read_bytes() == b"\x01\x02\x03\x04\x05\x06\x07\x08"
        assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "worker-result\n"
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_required_output_symlink_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B561 guard: a required output that is a symlink must be rejected."""
    repo = _repo_with_gitignore(tmp_path)
    workspace = _ignored_workspace(
        monkeypatch, tmp_path, repo, "b561-symlink", extra_allowed="out/*.bin"
    )
    try:
        (workspace.path / "out").mkdir(parents=True, exist_ok=True)
        target = workspace.path / "out" / "real.bin"
        target.write_bytes(b"\x01")
        link = workspace.path / "out" / "link.bin"
        link.symlink_to(target)

        with pytest.raises(worker_workspace.WorkspaceError, match="required_output_symlink"):
            worker_workspace.validate_required_outputs(workspace, ["out/link.bin"])
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_required_output_unchanged_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B561 guard: a required output identical to workspace_baseline is rejected."""
    repo = _repo_with_gitignore(tmp_path)
    workspace = _ignored_workspace(
        monkeypatch, tmp_path, repo, "b561-unchanged", extra_allowed="out/data.bin"
    )
    try:
        (workspace.path / "out").mkdir(parents=True, exist_ok=True)
        # _touch_placeholder created out/data.bin with 0 bytes.
        # validate_required_outputs must reject zero-byte (not allow_empty)
        # OR unchanged-from-placeholder. The placeholder is in workspace_baseline
        # with its empty-file hash, and the file is still empty, so the hash
        # matches → required_output_unchanged.
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="required_output_(unchanged|zero_bytes)",
        ):
            worker_workspace.validate_required_outputs(workspace, ["out/data.bin"])
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_required_output_parent_changed_during_promotion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B561 guard: parent-baseline hash protection still rejects a promoted
    required output when the parent file was modified concurrently."""
    repo = _repo_with_gitignore(tmp_path)
    workspace = _ignored_workspace(
        monkeypatch, tmp_path, repo, "b561-parent-changed", extra_allowed="out/*.bin"
    )
    try:
        (workspace.path / "out").mkdir(parents=True, exist_ok=True)
        (workspace.path / "out" / "data.bin").write_bytes(b"\x01\x02\x03\x04\x05\x06\x07\x08")

        required_records = worker_workspace.validate_required_outputs(
            workspace, ["out/*.bin"]
        )
        validated_paths = {rec["path"] for rec in required_records}
        changed = sorted({"out/result.txt"} | validated_paths)

        # Concurrently modify the parent repo.
        (repo / "out" / "result.txt").write_text("owner-edit\n", encoding="utf-8")

        with pytest.raises(
            worker_workspace.WorkspaceError, match="parent_changed_since_launch"
        ):
            worker_workspace.promote(workspace, changed)

        # Parent must be unchanged after rejection.
        assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "owner-edit\n"
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_required_output_out_of_scope_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B561 guard: a required output not in allowed_writes is rejected immediately."""
    repo = _repo_with_gitignore(tmp_path)
    workspace = _ignored_workspace(
        monkeypatch, tmp_path, repo, "b561-out-of-scope"
    )
    try:
        with pytest.raises(
            worker_workspace.WorkspaceError, match="required_output_not_allowed"
        ):
            worker_workspace.validate_required_outputs(workspace, ["secret/escape.txt"])
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_required_output_validation_aggregates_every_mismatch_category(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worktree"
    home = tmp_path / "home"
    path.mkdir()
    home.mkdir()
    (path / "unchanged.py").write_bytes(b"same")
    (path / "empty.py").write_bytes(b"")
    (path / "valid.py").write_bytes(b"changed")
    unchanged_hash = worker_workspace._hash_path(path / "unchanged.py")
    workspace = worker_workspace.WorkerWorkspace(
        request_id="aggregate-required-output-mismatch",
        repo=tmp_path,
        path=path,
        home=home,
        allowed_writes=("missing.py", "unchanged.py", "empty.py", "valid.py"),
        parent_baseline={"unchanged.py": unchanged_hash},
        workspace_baseline={"unchanged.py": unchanged_hash},
    )

    with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
        worker_workspace.validate_required_outputs(
            workspace,
            ["missing.py", "unchanged.py", "empty.py", "valid.py", "outside.py"],
        )

    message = str(excinfo.value)
    assert message.startswith("required_output_mismatch:")
    diagnostics = json.loads(message.split(":", 1)[1])
    assert diagnostics["missing_required_artifacts"] == ["missing.py"]
    assert diagnostics["unchanged_mandatory_outputs"] == ["unchanged.py"]
    assert diagnostics["scope_violations"] == [
        {"path": "empty.py", "reason": "required_output_zero_bytes"},
        {"path": "outside.py", "reason": "required_output_not_allowed"},
    ]
    assert [record["path"] for record in diagnostics["primary_validation_result"]] == [
        "valid.py"
    ]


def test_allow_unchanged_required_outputs_accepts_exact_baseline_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "allow-unchanged")
    try:
        records = worker_workspace.validate_required_outputs(
            workspace,
            ["out/result.txt"],
            allow_unchanged=("out/result.txt",),
        )
        assert records == [
            {
                "pattern": "out/result.txt",
                "path": "out/result.txt",
                "bytes": len((workspace.path / "out" / "result.txt").read_bytes()),
                "sha256": workspace.workspace_baseline["out/result.txt"],
                "unchanged_allowed": True,
            }
        ]
        changed = worker_workspace.enforce_scope(workspace)
        promotable = sorted(
            set(changed) | {rec["path"] for rec in records if not rec["unchanged_allowed"]}
        )
        assert promotable == []
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_allow_unchanged_required_outputs_accepts_changed_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "allow-unchanged-changed")
    try:
        (workspace.path / "out" / "result.txt").write_text("worker-result\n", encoding="utf-8")
        records = worker_workspace.validate_required_outputs(
            workspace,
            ["out/result.txt"],
            allow_unchanged=("out/result.txt",),
        )
        target = workspace.path / "out" / "result.txt"
        changed_digest = hashlib.sha256(target.read_bytes()).hexdigest()
        changed_hash = f"file:{stat.S_IMODE(target.stat().st_mode):o}:{changed_digest}"
        assert records == [
            {
                "pattern": "out/result.txt",
                "path": "out/result.txt",
                "bytes": len(target.read_bytes()),
                "sha256": changed_hash,
                "unchanged_allowed": False,
            }
        ]
        changed = worker_workspace.enforce_scope(workspace)
        promotable = sorted(
            set(changed) | {rec["path"] for rec in records if not rec["unchanged_allowed"]}
        )
        assert promotable == ["out/result.txt"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_all_unchanged_required_outputs_is_no_effect_for_caller(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "all-unchanged")
    try:
        records = worker_workspace.validate_required_outputs(
            workspace,
            ["out/result.txt"],
            allow_unchanged=("out/result.txt",),
        )
        changed = worker_workspace.enforce_scope(workspace)
        promotable = sorted(
            set(changed) | {rec["path"] for rec in records if not rec["unchanged_allowed"]}
        )
        with pytest.raises(worker_workspace.WorkspaceError, match="no_effect"):
            if not promotable:
                raise worker_workspace.WorkspaceError("no_effect")
            worker_workspace.promote(workspace, promotable)
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_validation_pythonpath_prefix_is_removed_from_executable_argv() -> None:
    argv, components = worker_workspace.parse_validation_command(
        "PYTHONPATH=tools/geoai-task-mcp/src python3 -m pytest -q tools/x_test.py"
    )
    assert argv == ["python3", "-m", "pytest", "-q", "tools/x_test.py"]
    assert components == ("tools/geoai-task-mcp/src",)
    assert worker_workspace.validation_argv("python3 AITools/taskctl.py verify") == [
        "python3", "AITools/taskctl.py", "verify"
    ]


def test_windows_validation_tokenizer_preserves_native_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_workspace, "_is_windows_host", lambda: True)
    assert worker_workspace.validation_argv(
        r'"C:\Program Files\Python\python.exe" verify_route.py'
    ) == [r"C:\Program Files\Python\python.exe", "verify_route.py"]
    assert worker_workspace.validation_argv(
        r"C:\Python312\python.exe verify_route.py"
    ) == [r"C:\Python312\python.exe", "verify_route.py"]


@pytest.mark.parametrize(
    "command,match",
    [
        ("FOO=bar python3 x.py", "validation_env_assignment_not_supported"),
        ("PYTHONPATH=a PYTHONPATH=b python3 x.py", "validation_env_assignment_multiple"),
        ("PYTHONPATH=a FOO=bar python3 x.py", "validation_env_assignment_multiple"),
        ("PYTHONPATH=only", "validation_env_assignment_without_executable"),
    ],
)
def test_validation_pythonpath_rejects_unknown_or_multiple_assignments(
    command: str, match: str
) -> None:
    with pytest.raises(worker_workspace.WorkspaceError, match=match):
        worker_workspace.parse_validation_command(command)


@pytest.mark.parametrize(
    "command,match",
    [
        ("PYTHONPATH=../escape python3 x.py", "traversal_forbidden"),
        ("PYTHONPATH=a/../../escape python3 x.py", "traversal_forbidden"),
        ("PYTHONPATH= python3 x.py", "pythonpath_empty"),
        ("PYTHONPATH=a::b python3 x.py", "empty_component"),
        ("PYTHONPATH=tools/* python3 x.py", "forbidden_char"),
        ("PYTHONPATH=$(whoami) python3 x.py", "forbidden_char"),
        ("PYTHONPATH=${HOME} python3 x.py", "forbidden_char"),
    ],
)
def test_validation_pythonpath_rejects_unsafe_values(command: str, match: str) -> None:
    with pytest.raises(worker_workspace.WorkspaceError, match=match):
        worker_workspace.parse_validation_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "PYTHONPATH=read; rm -rf / python3 x.py",
        "PYTHONPATH=read python3 x.py | rm -rf /",
        "PYTHONPATH=read python3 x.py > /tmp/out",
    ],
)
def test_validation_pythonpath_preserves_shell_metacharacter_rejection(command: str) -> None:
    with pytest.raises(worker_workspace.WorkspaceError, match="validation_shell_syntax_forbidden"):
        worker_workspace.parse_validation_command(command)


def test_validation_pythonpath_resolution_is_beneath_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    (worktree / "read").mkdir(parents=True)
    workspace = worker_workspace.WorkerWorkspace(
        request_id="pythonpath-resolve",
        repo=tmp_path,
        path=worktree,
        home=tmp_path / "home",
        allowed_writes=("out/result.txt",),
        parent_baseline={},
        workspace_baseline={},
    )
    assert worker_workspace.resolve_validation_pythonpath(
        workspace, "landlock", ("read",)
    ) == str(worktree / "read")
    assert worker_workspace.resolve_validation_pythonpath(
        workspace, "bubblewrap", ("read",)
    ) == f"{worker_workspace.SANDBOX_WORKSPACE}/read"
    if os.name == "nt":
        return
    approved_site = tmp_path / "approved-site-packages"
    approved_site.mkdir()
    monkeypatch.setattr(
        worker_workspace.site, "getusersitepackages", lambda: str(approved_site)
    )
    approved_site = approved_site.resolve()
    components = worker_workspace.parse_validation_command(
        f"PYTHONPATH={os.pathsep.join((str(approved_site), '.', 'read'))} "
        "python3 -m pytest -q x.py"
    )[1]
    assert worker_workspace.resolve_validation_pythonpath(
        workspace, "landlock", components
    ) == os.pathsep.join((str(approved_site), str(worktree), str(worktree / "read")))
    assert worker_workspace.resolve_validation_pythonpath(
        workspace, "bubblewrap", components
    ) == os.pathsep.join(
        ("/validation-pythonpath/0", worker_workspace.SANDBOX_WORKSPACE,
         f"{worker_workspace.SANDBOX_WORKSPACE}/read")
    )
    bubblewrap_argv = worker_workspace.sandbox_argv(
        workspace,
        "validation",
        ["python3", "x.py"],
        backend="bubblewrap",
        validation_readonly_dirs=(approved_site,),
    )
    bind = ["--ro-bind", str(approved_site), "/validation-pythonpath/0"]
    assert any(bubblewrap_argv[i:i + 3] == bind for i in range(len(bubblewrap_argv) - 2))
    with pytest.raises(worker_workspace.WorkspaceError, match="absolute_component_forbidden"):
        worker_workspace.resolve_validation_pythonpath(
            workspace, "landlock", ("/etc",)
        )
    with pytest.raises(worker_workspace.WorkspaceError, match="not_directory"):
        worker_workspace.resolve_validation_pythonpath(
            workspace, "landlock", ("missing",)
        )


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true"
    or worker_workspace.nested_sandbox_requires_host_boundary(),
    reason="The current host boundary cannot execute nested Landlock validations",
)
def test_validation_pythonpath_override_is_scoped_to_one_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    workspace = _workspace(monkeypatch, tmp_path, repo, "pythonpath-scope")
    (workspace.path / "check_pythonpath.py").write_text(
        "import os\nprint('PP=' + os.environ.get('PYTHONPATH', ''))\n",
        encoding="utf-8",
    )
    try:
        backend = worker_workspace.select_sandbox_backend()
        # The production guard requires the approved user site to exist on disk.
        # Under a worker sandbox HOME points at the throwaway workspace home, so
        # that directory is absent and every card whose validation list includes
        # this file failed deterministically.  Provision the precondition instead
        # of asserting the host happens to have it.
        approved_site = Path(worker_workspace.site.getusersitepackages())
        approved_site.mkdir(parents=True, exist_ok=True)
        approved_site = approved_site.resolve()
        components = (str(approved_site), ".", "read")
        expected = worker_workspace.resolve_validation_pythonpath(
            workspace, backend, components
        )
        first, second = worker_workspace.run_validations(
            workspace,
            [
                f"PYTHONPATH={approved_site}:.:read python3 check_pythonpath.py",
                "python3 check_pythonpath.py",
            ],
        )
        assert first["argv"][0] == sys.executable
        assert second["argv"][0] == sys.executable
        assert first["env_override"] == {
            "variable": "PYTHONPATH", "components": list(components)
        }
        assert f"PP={expected}" in first["stdout_tail"]
        assert second["env_override"] is None
        assert expected not in second["stdout_tail"]
        assert "PYTHONPATH" not in os.environ
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_empty_validation_list_never_resolves_host_sandbox(monkeypatch, tmp_path, repo):
    workspace = _workspace(monkeypatch, tmp_path, repo, "empty-validations")
    monkeypatch.setattr(
        worker_workspace,
        "select_sandbox_backend",
        lambda: (_ for _ in ()).throw(
            worker_workspace.WorkspaceError(
                "windows_appcontainer_sandbox_unavailable"
            )
        ),
    )
    try:
        assert worker_workspace.run_validations(workspace, []) == []
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_editor_route_validation_uses_retained_workspace_without_host_sandbox(
    monkeypatch, tmp_path, repo
):
    workspace = _workspace(monkeypatch, tmp_path, repo, "editor-route-validation")
    (workspace.path / "verify_route.py").write_text(
        "from pathlib import Path\n"
        "assert Path.cwd() == Path(__file__).resolve().parent\n"
        "print('route-aware-validation-ok')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        worker_workspace,
        "select_sandbox_backend",
        lambda: (_ for _ in ()).throw(
            worker_workspace.WorkspaceError(
                "windows_appcontainer_sandbox_unavailable"
            )
        ),
    )
    try:
        result, = worker_workspace.run_validations(
            workspace,
            [f"{sys.executable} verify_route.py"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="vscode_lm",
        )
        assert result["returncode"] == 0
        assert result["sandbox_backend"] == "vscode_lm_in_process"
        assert result["execution_boundary"] == (
            "trusted_manager_shell_free_validation"
        )
        assert "route-aware-validation-ok" in result["stdout_tail"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_native_adapter_cannot_borrow_editor_validation_boundary(
    monkeypatch, tmp_path, repo
):
    workspace = _workspace(monkeypatch, tmp_path, repo, "native-route-denied")
    try:
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="vscode_lm_in_process_validation_adapter_forbidden:claude_cli",
        ):
            worker_workspace.run_validations(
                workspace,
                [f"{sys.executable} -c pass"],
                backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
                adapter_id="claude_cli",
            )
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_validation_cd_prefix_is_removed_from_executable_argv() -> None:
    argv, components, tmpdir_override, cwd = worker_workspace._parse_validation_command_detailed(
        "cd read && python3 x.py"
    )
    assert argv == ["python3", "x.py"]
    assert components == ()
    assert tmpdir_override is None
    assert cwd == "read"
    assert worker_workspace.parse_validation_command("cd read && python3 x.py") == (
        ["python3", "x.py"], ()
    )


def test_validation_cd_prefix_combines_with_env_assignment() -> None:
    argv, components, tmpdir_override, cwd = worker_workspace._parse_validation_command_detailed(
        "PYTHONPATH=tools/src cd sub/dir && python3 -m pytest -q x_test.py"
    )
    assert argv == ["python3", "-m", "pytest", "-q", "x_test.py"]
    assert components == ("tools/src",)
    assert tmpdir_override is None
    assert cwd == "sub/dir"


@pytest.mark.parametrize(
    "command,match",
    [
        ("cd /abs && python3 x.py", "invalid_repo_path"),
        ("cd ../escape && python3 x.py", "unsafe_repo_path"),
        ("cd . && python3 x.py", "unsafe_repo_path"),
        ("cd sub &&", "validation_cd_command_empty"),
        ("cd sub python3 x.py", "validation_cd_prefix_malformed"),
        ("cd && python3 x.py", "validation_cd_prefix_malformed"),
        ("python3 x.py && python3 y.py", "validation_shell_chain_forbidden"),
        ("cd sub && python3 x.py && python3 y.py", "validation_shell_chain_forbidden"),
    ],
)
def test_validation_cd_prefix_rejects_unsafe_forms(command: str, match: str) -> None:
    with pytest.raises(worker_workspace.WorkspaceError, match=match):
        worker_workspace._parse_validation_command_detailed(command)


def test_validation_cd_prefix_rejects_symlink_escape_at_sandbox_argv_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "cd-symlink")
    try:
        outside = tmp_path / "outside"
        outside.mkdir()
        (workspace.path / "escape-link").symlink_to(outside, target_is_directory=True)
        with pytest.raises(worker_workspace.WorkspaceError, match="symlink_path_component_forbidden"):
            worker_workspace.sandbox_argv(
                workspace,
                "validation",
                ["python3", "x.py"],
                backend="landlock",
                validation_cwd="escape-link",
            )
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_validation_cd_prefix_rejects_non_directory_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "cd-not-dir")
    try:
        with pytest.raises(worker_workspace.WorkspaceError, match="validation_cwd_not_directory"):
            worker_workspace.sandbox_argv(
                workspace,
                "validation",
                ["python3", "x.py"],
                backend="landlock",
                validation_cwd="out/result.txt",
            )
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_validation_cd_prefix_sets_bubblewrap_chdir_beneath_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "cd-bwrap-chdir")
    try:
        argv = worker_workspace.sandbox_argv(
            workspace,
            "validation",
            ["python3", "x.py"],
            backend="bubblewrap",
            validation_cwd="read",
        )
        assert "--chdir" in argv
        assert argv[argv.index("--chdir") + 1] == f"{worker_workspace.SANDBOX_WORKSPACE}/read"
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true"
    or worker_workspace.nested_sandbox_requires_host_boundary(),
    reason="The current host boundary cannot execute nested Landlock validations",
)
def test_validation_cd_prefix_changes_child_cwd_under_landlock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "cd-landlock-exec")
    try:
        result, = worker_workspace.run_validations(
            workspace,
            ["cd read && python3 -c \"print(__import__('os').getcwd())\""],
        )
        assert result["returncode"] == 0, result["stderr_tail"]
        assert result["stdout_tail"].strip() == str((workspace.path / "read").resolve())
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_validation_batch_retains_each_failed_command_and_bounded_streams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "validation-evidence")
    scratch = tmp_path / "validation-scratch"
    scratch.mkdir()
    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", lambda: "landlock")
    monkeypatch.setattr(
        worker_workspace, "provision_validation_exec_scratch", lambda _workspace: scratch
    )
    monkeypatch.setattr(worker_workspace, "cleanup_validation_exec_scratch", lambda _path: None)
    monkeypatch.setattr(
        worker_workspace,
        "sandbox_argv",
        lambda _workspace, _adapter, argv, **_kwargs: list(argv),
    )
    monkeypatch.setattr(worker_workspace, "sanitized_env", lambda *_args, **_kwargs: {})
    outputs = iter(
        [
            subprocess.CompletedProcess([], 1, "A" * 9_000, "first-error"),
            subprocess.CompletedProcess([], 2, "second-out", "B" * 9_000),
        ]
    )
    monkeypatch.setattr(worker_workspace.subprocess, "run", lambda *_args, **_kwargs: next(outputs))

    with pytest.raises(worker_workspace.ValidationRunError) as caught:
        worker_workspace.run_validations(
            workspace,
            ["python3 -c 'raise SystemExit(1)'", "python3 -c 'raise SystemExit(2)'"],
        )

    rows = caught.value.results
    assert [row["returncode"] for row in rows] == [1, 2]
    assert [row["command"] for row in rows] == [
        "python3 -c 'raise SystemExit(1)'",
        "python3 -c 'raise SystemExit(2)'",
    ]
    assert rows[0]["stdout_truncated"] is True
    assert len(rows[0]["stdout_head"]) == 4_096
    assert len(rows[0]["stdout_tail"]) == 4_096
    assert rows[1]["stderr_truncated"] is True
    assert rows[0]["failure_receipt"]["failure_class"] == "nonzero_exit"
    delta = worker_workspace.validation_failure_delta_packet(rows)
    assert delta["failure_count"] == 2
    assert delta["automatic_repair_authorized"] is False
    assert delta["packet_bytes"] <= 6 * 1024


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"timed_out": True, "argv": ["pytest"]}, "timeout"),
        (
            {"returncode": 1, "argv": ["python"], "stderr_tail": "SyntaxError: bad"},
            "syntax_error",
        ),
        (
            {"returncode": 1, "argv": ["ruff", "check"], "stderr_tail": "bad"},
            "lint_failure",
        ),
        (
            {"returncode": 1, "argv": ["pytest"], "stdout_tail": "1 failed"},
            "test_failure",
        ),
        (
            {"returncode": 126, "argv": ["tool"], "stderr_tail": "permission denied"},
            "permission_denied",
        ),
        (
            {
                "returncode": 2,
                "argv": ["mypy"],
                "stderr_tail": (
                    "Traceback (most recent call last):\n"
                    "mypy: INTERNAL ERROR: boom"
                ),
            },
            "type_check_internal_error",
        ),
        (
            {"returncode": None, "argv": ["tool"], "launch_error": "PermissionError"},
            "permission_denied",
        ),
        (
            {"returncode": None, "argv": ["tool"], "launch_error": "FileNotFoundError"},
            "executable_unavailable",
        ),
    ],
)
def test_validation_failure_receipt_classifies_stable_categories(
    record: dict, expected: str
) -> None:
    receipt = worker_workspace._validation_failure_receipt(record)
    assert receipt is not None
    assert receipt["schema_id"] == "aiworkhub.validation_failure_receipt.v1"
    assert receipt["failure_class"] == expected
    assert len(receipt["command_sha256"]) == 64
    assert len(receipt["receipt_sha256"]) == 64


def test_validation_failure_delta_redacts_long_tokens_and_bounds_population() -> None:
    secret = "s" * 80
    rows = [
        {
            "returncode": 1,
            "argv": ["pytest", f"tests/test_{index}.py"],
            "stderr_tail": (
                f"failure {index} " + ("shorttoken " * 300) + f" token={secret}"
            ),
        }
        for index in range(12)
    ]
    packet = worker_workspace.validation_failure_delta_packet(rows)
    serialized = json.dumps(packet, sort_keys=True)
    assert packet["observed_failure_count"] == 12
    assert packet["failure_count"] < 12
    assert packet["truncated"] is True
    assert packet["packet_bytes"] <= 6 * 1024
    assert secret not in serialized
    assert "<redacted>" in serialized


# ── validation sandbox portability regression (request cfaa21da...) ──────────


def _bare_workspace(tmp_path: Path, request_id: str) -> worker_workspace.WorkerWorkspace:
    return worker_workspace.WorkerWorkspace(
        request_id=request_id,
        repo=tmp_path,
        path=tmp_path,
        home=tmp_path,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_repo_relative_venv_ruff_resolves_against_candidate_repo(tmp_path: Path) -> None:
    """Root cause B: a declared repo-relative ``.venv/bin/ruff`` must resolve to
    the canonical candidate repository executable (absolute host path), not be
    executed verbatim relative to the sandbox cwd (which yields rc=126)."""
    repo = tmp_path / "candidate-repo"
    bin_dir = repo / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    ruff = bin_dir / "ruff"
    ruff.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(ruff, 0o755)

    declared = [".venv/bin/ruff", "check", "src/aiworkhub/worker_workspace.py"]
    executed, roots = (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            list(declared), repo
        )
    )
    assert executed == [
        str(ruff.resolve()),
        "check",
        "src/aiworkhub/worker_workspace.py",
    ]
    assert roots == ((repo / ".venv").resolve(),)
    # Declared vs executed argv remain distinct and truthful for the receipt.
    assert executed[0] != declared[0]
    assert executed[1:] == declared[1:]


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_repo_relative_executable_passes_through_unrelated_and_rejects_untrusted(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "candidate-repo"
    (repo / ".venv" / "bin").mkdir(parents=True)

    # An absolute path is never rewritten.
    assert worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
        ["/usr/bin/ruff", "check"], repo
    ) == (["/usr/bin/ruff", "check"], ())
    # A relative path that is not <venv>/bin/<approved-name> passes through.
    assert worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
        ["scripts/run.sh", "arg"], repo
    ) == (["scripts/run.sh", "arg"], ())
    # A relative path to a non-approved executable name passes through.
    assert worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
        [".venv/bin/python3", "-m", "pytest"], repo
    ) == ([".venv/bin/python3", "-m", "pytest"], ())
    # Traversal escapes never resolve to a trusted executable.
    assert worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
        ["../evil/.venv/bin/ruff", "check"], repo
    ) == (["../evil/.venv/bin/ruff", "check"], ())
    # A missing executable is not fabricated; the head passes through.
    assert worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
        [".venv/bin/mypy", "src"], repo
    ) == ([".venv/bin/mypy", "src"], ())


def test_bare_python_heads_resolve_to_trusted_coordinator_interpreter() -> None:
    """NF-2026-00448: ``python``/``python3``/``python3.NN`` validation heads
    resolve directly to ``sys.executable`` -- the credential-free validation
    PATH does not reliably expose a working bare ``python3``. Explicit
    relative and absolute interpreter declarations are untouched here."""
    for head in ("python", "python3", "python3.11", "python3.9"):
        assert worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            [head, "-c", "pass"]
        ) == ([sys.executable, "-c", "pass"], ())
    # Near-misses never match the bare-interpreter regex.
    for unmatched in ("python2", "pythonic", "python3.11.2", "python33"):
        assert worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            [unmatched, "-c", "pass"]
        ) == ([unmatched, "-c", "pass"], ())
    # An absolute interpreter declaration keeps its existing fail-closed rule
    # (passed through byte-for-byte, never rewritten).
    assert worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
        ["/usr/bin/python3", "-c", "pass"]
    ) == (["/usr/bin/python3", "-c", "pass"], ())
    # A relative interpreter declaration with a path separator also keeps its
    # existing fail-closed rule: it is resolved (or left untouched) by the
    # repo-relative trusted-executable path, never by the bare-head regex.
    assert worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
        [".venv/bin/python3", "-c", "pass"]
    ) == ([".venv/bin/python3", "-c", "pass"], ())


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_bare_python_module_mypy_uses_trusted_executable_and_preserves_args(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    mypy = bin_dir / "mypy"
    mypy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(mypy, 0o755)

    declared = ["python3", "-m", "mypy", "--strict", "src/example.py"]
    executed, roots = (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            declared, tmp_path
        )
    )

    assert executed == [str(mypy.resolve()), "--strict", "src/example.py"]
    assert roots == ((tmp_path / ".venv").resolve(),)
    assert executed[1:] == declared[3:]


def test_python_module_mypy_rewrite_is_exact_and_preserves_existing_rules(
    tmp_path: Path,
) -> None:
    unchanged = (
        ["/usr/bin/python3", "-m", "mypy", "src"],
        [".venv/bin/python3", "-m", "mypy", "src"],
        ["python2", "-m", "mypy", "src"],
    )
    for declared in unchanged:
        assert worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            list(declared), tmp_path
        ) == (list(declared), ())

    for tail in (("-m", "pytest", "src"), ("-m", "mypy.api", "src"), ("-mypy", "src")):
        declared = ["python3", *tail]
        assert worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            declared, tmp_path
        ) == ([sys.executable, *declared[1:]], ())


def test_bare_python_module_mypy_requires_trusted_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_validation_runtime_roots",
        lambda _repo: (tmp_path / "missing-venv",),
    )
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="validation_executable_unavailable:mypy",
    ):
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            ["python3", "-m", "mypy", "src"], tmp_path
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true"
    or worker_workspace.nested_sandbox_requires_host_boundary(),
    reason="The current host boundary cannot execute nested Landlock validations",
)
def test_run_validations_declared_vs_executed_relative_venv_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    """Keep declared and executed argv distinct after trusted-path rewriting."""
    bin_dir = repo / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    ruff = bin_dir / "ruff"
    ruff.write_text("#!/bin/sh\necho ran-canonical-ruff\nexit 0\n", encoding="utf-8")
    os.chmod(ruff, 0o755)
    expected_executable = ruff.resolve()

    workspace = _workspace(monkeypatch, tmp_path, repo, "declared-vs-executed-ruff")
    try:
        (result,) = worker_workspace.run_validations(
            workspace, [".venv/bin/ruff check read/input.txt"]
        )
        assert result["returncode"] == 0, result["stderr_tail"]
        assert result["declared_argv"] == [
            ".venv/bin/ruff",
            "check",
            "read/input.txt",
        ]
        assert result["executed_argv"] == [
            str(expected_executable),
            "check",
            "read/input.txt",
        ]
        assert result["argv"] == result["executed_argv"]
        assert result["argv_rewritten"] is True
        assert "ran-canonical-ruff" in result["stdout_tail"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod metadata semantics")
def test_probe_metadata_capable_dir_rejects_chmod_hostile_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Root cause A: the metadata probe must reject a scratch root whose
    filesystem cannot honour the chmod git init performs on .git/config.lock."""
    good = tmp_path / "ok"
    good.mkdir()
    assert worker_workspace._probe_metadata_capable_dir(good) is True

    def _deny_chmod(_path: object, _mode: int, *args: object, **kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(worker_workspace.os, "chmod", _deny_chmod)
    assert worker_workspace._probe_metadata_capable_dir(good) is False


def test_windows_exec_probe_executes_private_native_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    comspec = tmp_path / "cmd.exe"
    executable_payload = b"fake\nportable\nexecutable"
    comspec.write_bytes(executable_payload)

    def _run(argv, **kwargs):
        assert Path(argv[0]).read_bytes() == executable_payload
        calls.append((list(argv), dict(kwargs["env"])))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(worker_workspace.sys, "platform", "win32")
    monkeypatch.setenv("COMSPEC", str(comspec))
    monkeypatch.setattr(worker_workspace.subprocess, "run", _run)

    assert worker_workspace._probe_exec_capable_dir(tmp_path) is True
    argv, env = calls[0]
    assert argv[0].endswith(".exe")
    assert argv[1:] == ["/d", "/c", "exit 0"]
    assert env["COMSPEC"] == str(comspec)
    assert list(tmp_path.iterdir()) == [comspec]


def test_windows_metadata_probe_uses_atomic_replace_not_posix_chmod(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(worker_workspace.sys, "platform", "win32")
    monkeypatch.setattr(
        worker_workspace.os,
        "chmod",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Windows metadata probe must not require chmod")
        ),
    )

    assert worker_workspace._probe_metadata_capable_dir(tmp_path) is True
    assert list(tmp_path.iterdir()) == []


def test_windows_scratch_prefers_request_private_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _bare_workspace(tmp_path, "windows-private-home")
    fallback = tmp_path / "global-temp"
    fallback.mkdir()
    monkeypatch.setattr(worker_workspace.sys, "platform", "win32")
    monkeypatch.delenv(
        worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV, raising=False
    )
    monkeypatch.setattr(
        worker_workspace, "_DEFAULT_EXEC_SCRATCH_ROOTS", (fallback,)
    )
    monkeypatch.setattr(
        worker_workspace, "_probe_exec_capable_dir", lambda _directory: True
    )
    monkeypatch.setattr(
        worker_workspace, "_probe_metadata_capable_dir", lambda _directory: True
    )

    scratch = worker_workspace.provision_validation_exec_scratch(workspace)
    try:
        assert scratch.parent == workspace.home.resolve()
    finally:
        worker_workspace.cleanup_validation_exec_scratch(scratch)


def test_empty_validation_never_provisions_exec_scratch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _bare_workspace(tmp_path, "empty-validation-no-scratch")
    monkeypatch.setattr(
        worker_workspace,
        "provision_validation_exec_scratch",
        lambda _workspace: (_ for _ in ()).throw(
            AssertionError("empty validation must not provision scratch")
        ),
    )

    assert worker_workspace.run_validations(workspace, []) == []


def test_provision_validation_exec_scratch_skips_metadata_hostile_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A /dev/shm-like root that execs but rejects chmod must be skipped in
    favour of the next chmod-capable candidate, never handed back."""
    monkeypatch.setattr(worker_workspace.sys, "platform", "linux")
    hostile = tmp_path / "shm-like"
    portable = tmp_path / "tmp-like"
    hostile.mkdir()
    portable.mkdir()
    monkeypatch.delenv(worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV, raising=False)
    monkeypatch.setenv(worker_workspace.TEMP_ROOT_ENV, str(hostile))
    monkeypatch.setattr(
        worker_workspace, "_DEFAULT_EXEC_SCRATCH_ROOTS", (hostile, portable)
    )
    monkeypatch.setattr(worker_workspace, "_probe_exec_capable_dir", lambda _d: True)
    monkeypatch.setattr(
        worker_workspace,
        "_probe_metadata_capable_dir",
        lambda directory: not worker_workspace._path_is_relative_to(
            directory.resolve(), hostile.resolve()
        ),
    )
    workspace = _bare_workspace(tmp_path, "scratch-meta")
    scratch = worker_workspace.provision_validation_exec_scratch(workspace)
    try:
        assert worker_workspace._path_is_relative_to(
            scratch.resolve(), portable.resolve()
        )
        assert not worker_workspace._path_is_relative_to(
            scratch.resolve(), hostile.resolve()
        )
        # The rejected candidate leaves no scratch directory behind.
        assert not any(
            path.name.startswith(worker_workspace._EXEC_SCRATCH_NAME_PREFIX)
            for path in hostile.rglob("*")
        )
    finally:
        worker_workspace.cleanup_validation_exec_scratch(scratch)


def test_provision_validation_exec_scratch_fails_closed_without_metadata_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(worker_workspace.sys, "platform", "linux")
    only = tmp_path / "only"
    only.mkdir()
    monkeypatch.delenv(worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV, raising=False)
    monkeypatch.setattr(worker_workspace, "_DEFAULT_EXEC_SCRATCH_ROOTS", (only,))
    monkeypatch.setattr(worker_workspace, "_probe_exec_capable_dir", lambda _d: True)
    monkeypatch.setattr(worker_workspace, "_probe_metadata_capable_dir", lambda _d: False)
    workspace = _bare_workspace(tmp_path, "scratch-none")
    with pytest.raises(
        worker_workspace.WorkspaceError, match="validation_exec_scratch_unavailable"
    ) as caught:
        worker_workspace.provision_validation_exec_scratch(workspace)
    assert "no_metadata" in str(caught.value)
    # Fail-closed: no half-provisioned scratch directory is left behind.
    assert list(only.iterdir()) == []


def test_run_validations_classifies_scratch_chmod_denial_as_environment_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NF-2026-00458: an outer-sandbox EPERM/chmod denial on every exec-scratch
    candidate must terminalize as the recoverable
    ``validation_environment_blocked`` state -- never an untyped
    ``WorkspaceError`` a caller cannot distinguish from a genuine candidate
    gate failure. No candidate command has run yet, so ``results`` is empty."""
    monkeypatch.setattr(worker_workspace.sys, "platform", "linux")
    only = tmp_path / "only"
    only.mkdir()
    monkeypatch.delenv(worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV, raising=False)
    monkeypatch.setattr(worker_workspace, "_DEFAULT_EXEC_SCRATCH_ROOTS", (only,))
    monkeypatch.setattr(worker_workspace, "_probe_exec_capable_dir", lambda _d: True)
    monkeypatch.setattr(worker_workspace, "_probe_metadata_capable_dir", lambda _d: False)
    workspace = _bare_workspace(tmp_path, "scratch-blocked")
    with pytest.raises(worker_workspace.ValidationEnvironmentBlocked) as caught:
        worker_workspace.run_validations(
            workspace,
            ["echo ok"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="glm_vscode_lm",
        )
    assert caught.value.terminal_state == worker_workspace.VALIDATION_ENVIRONMENT_BLOCKED
    assert caught.value.restriction == "refused_chmod"
    assert caught.value.recoverable is True
    assert caught.value.requires_supersede is False
    assert caught.value.results == []


def test_provisioned_scratch_supports_git_init(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: the provisioned scratch (selected via the real metadata
    probe) is git-init-capable in a pytest temp dir, without granting broader
    repository write authority."""
    root = tmp_path / "scratch-root"
    root.mkdir()
    monkeypatch.setenv(worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV, str(root))
    monkeypatch.setattr(worker_workspace, "_DEFAULT_EXEC_SCRATCH_ROOTS", (root,))
    # Exercise the real metadata probe; only the exec probe is stubbed so the
    # test does not depend on the exec-capability of the test filesystem.
    monkeypatch.setattr(worker_workspace, "_probe_exec_capable_dir", lambda _d: True)
    workspace = _bare_workspace(tmp_path, "scratch-git")
    scratch = worker_workspace.provision_validation_exec_scratch(workspace)
    try:
        assert worker_workspace._path_is_relative_to(scratch.resolve(), root.resolve())
        target = scratch / "repo"
        result = _git(scratch, "init", "-q", str(target))
        assert result.returncode == 0, result.stderr
        assert (target / ".git").is_dir()
    finally:
        worker_workspace.cleanup_validation_exec_scratch(scratch)


class TestCopyOne:
    @staticmethod
    def test_copies_bytes(tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.write_bytes(b"hello")
        dst = tmp_path / "dst"
        worker_workspace._copy_one(src, dst)
        assert dst.read_bytes() == b"hello"

    @staticmethod
    @pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode semantics")
    def test_executable_mode(tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.write_bytes(b"#!/bin/sh\necho hi\n")
        src.chmod(0o755)
        dst = tmp_path / "dst"
        worker_workspace._copy_one(src, dst)
        st = os.stat(dst)
        assert stat.S_IMODE(st.st_mode) == 0o755

    @staticmethod
    def test_existing_destination(tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.write_bytes(b"new")
        dst = tmp_path / "dst"
        dst.write_bytes(b"old")
        worker_workspace._copy_one(src, dst)
        assert dst.read_bytes() == b"new"

    @staticmethod
    def test_hardlink_safety(tmp_path: Path) -> None:
        if not hasattr(os, "link"):
            pytest.skip("os.link not available")
        src = tmp_path / "src"
        src.write_bytes(b"content")
        dst = tmp_path / "dst"
        dst.write_bytes(b"initial")
        link = tmp_path / "link"
        os.link(dst, link)
        worker_workspace._copy_one(src, dst)
        assert dst.read_bytes() == b"content"
        assert link.read_bytes() == b"initial"

    @staticmethod
    def test_source_symlink_fails(tmp_path: Path) -> None:
        if not hasattr(os, "symlink"):
            pytest.skip("os.symlink not available")
        src = tmp_path / "src"
        src.write_bytes(b"x")
        sym = tmp_path / "sym"
        os.symlink(src, sym)
        with pytest.raises(worker_workspace.WorkspaceError, match="symlink_seed_forbidden"):
            worker_workspace._copy_one(sym, tmp_path / "dst")

    @staticmethod
    def test_destination_symlink_fails(tmp_path: Path) -> None:
        if not hasattr(os, "symlink"):
            pytest.skip("os.symlink not available")
        src = tmp_path / "src"
        src.write_bytes(b"x")
        dst = tmp_path / "dst"
        os.symlink(src, dst)
        with pytest.raises(worker_workspace.WorkspaceError, match="destination_symlink_forbidden"):
            worker_workspace._copy_one(src, dst)

    @staticmethod
    def test_nonregular_source_fails(tmp_path: Path) -> None:
        # FIFO: skip if unsupported, always retain directory coverage
        if hasattr(os, "mkfifo"):
            fifo = tmp_path / "fifo"
            os.mkfifo(fifo)
            with pytest.raises(worker_workspace.WorkspaceError, match="non_regular_seed_forbidden"):
                worker_workspace._copy_one(fifo, tmp_path / "dst")
        dir_ = tmp_path / "dir"
        dir_.mkdir()
        with pytest.raises(worker_workspace.WorkspaceError, match="non_regular_seed_forbidden"):
            worker_workspace._copy_one(dir_, tmp_path / "dst")

    @staticmethod
    def test_failure_cleanup(tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.write_bytes(b"data")
        dst_dir = tmp_path / "dst_dir"
        dst_dir.mkdir()
        dst = dst_dir / "dst"
        dst.mkdir()
        with pytest.raises(OSError):  # os.replace on a directory
            worker_workspace._copy_one(src, dst)
        remaining = list(dst_dir.iterdir())
        assert [p.name for p in remaining if not p.is_symlink()] == ["dst"]

    @staticmethod
    def test_nested_parent(tmp_path: Path) -> None:
        """_copy_one creates nested parent directories when needed."""
        src = tmp_path / "src"
        src.write_bytes(b"nested")
        dst = tmp_path / "a" / "b" / "c"
        worker_workspace._copy_one(src, dst)
        assert dst.read_bytes() == b"nested"

    @staticmethod
    def test_partial_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Complete-write loop handles partial os.write returns."""
        src = tmp_path / "src"
        src.write_bytes(b"x" * 8192)
        dst = tmp_path / "dst"

        orig_write = os.write
        call_count = [0]

        def partial_write(fd: int, data: bytes) -> int:
            call_count[0] += 1
            if call_count[0] == 1 and len(data) > 1:
                return orig_write(fd, data[:1])  # write 1 byte on first call
            return orig_write(fd, data)

        monkeypatch.setattr(os, "write", partial_write)
        worker_workspace._copy_one(src, dst)
        assert dst.read_bytes() == b"x" * 8192
        assert call_count[0] >= 2  # at least one retry


# ---------------------------------------------------------------------------
# NF128: candidate pytest wrapper helpers and run_validations integration
# ---------------------------------------------------------------------------


class TestCandidatePytestWrapperCommand:
    @staticmethod
    def test_exact_candidate_pytest_wrapper_is_recognized() -> None:
        """python3 tools/candidate_pytest.py tests/ -- exact match"""
        assert worker_workspace._is_candidate_pytest_wrapper_command(
            ["python3", "tools/candidate_pytest.py", "tests/"]
        ) is True

    @staticmethod
    def test_candidate_pytest_wrapper_with_extra_args() -> None:
        assert worker_workspace._is_candidate_pytest_wrapper_command(
            ["python3", "tools/candidate_pytest.py", "-v", "-k", "test_x"]
        ) is True


class TestCandidatePytestWrapperNearMatchesFailClosed:
    @staticmethod
    def test_python_not_python3_rejected() -> None:
        """python (not python3) fails closed"""
        assert worker_workspace._is_candidate_pytest_wrapper_command(
            ["python", "tools/candidate_pytest.py", "tests/"]
        ) is False

    @staticmethod
    def test_python3_11_rejected() -> None:
        """python3.11 fails closed -- only exact python3"""
        assert worker_workspace._is_candidate_pytest_wrapper_command(
            ["python3.11", "tools/candidate_pytest.py", "tests/"]
        ) is False

    @staticmethod
    def test_absolute_wrapper_path_rejected() -> None:
        assert worker_workspace._is_candidate_pytest_wrapper_command(
            ["python3", "/abs/tools/candidate_pytest.py"]
        ) is False

    @staticmethod
    def test_relative_with_extra_components_rejected() -> None:
        assert worker_workspace._is_candidate_pytest_wrapper_command(
            ["python3", "scripts/tools/candidate_pytest.py"]
        ) is False

    @staticmethod
    def test_short_argv_rejected() -> None:
        assert worker_workspace._is_candidate_pytest_wrapper_command([]) is False
        assert worker_workspace._is_candidate_pytest_wrapper_command(["python3"]) is False

    @staticmethod
    def test_pytest_plain_not_misclassified() -> None:
        """Ordinary pytest is never classified as candidate"""
        assert worker_workspace._is_candidate_pytest_wrapper_command(
            ["pytest", "tests/"]
        ) is False

    @staticmethod
    def test_python3_m_pytest_not_misclassified() -> None:
        """python3 -m pytest is never classified as candidate"""
        assert worker_workspace._is_candidate_pytest_wrapper_command(
            ["python3", "-m", "pytest"]
        ) is False


class TestResolveCandidatePytestWrapper:
    @staticmethod
    def test_resolves_valid_regular_wrapper_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        wrapper = tools_dir / "candidate_pytest.py"
        wrapper.write_text("# candidate wrapper\nimport pytest\n", encoding="utf-8")
        resolved, entry = worker_workspace._resolve_candidate_pytest_wrapper(tmp_path)
        assert resolved == wrapper.resolve()
        assert entry["name"] == "aiworkhub_candidate_pytest_wrapper"
        assert entry["spec"] is not None

    @staticmethod
    def test_missing_wrapper_raises(tmp_path: Path) -> None:
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="candidate_pytest_wrapper_unavailable",
        ):
            worker_workspace._resolve_candidate_pytest_wrapper(tmp_path)

    @staticmethod
    def test_symlink_wrapper_rejected(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not hasattr(os, "symlink"):
            pytest.skip("os.symlink not available")
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        real_wrapper = tmp_path / "real_candidate_pytest.py"
        real_wrapper.write_text("# real wrapper\n", encoding="utf-8")
        symlink_wrapper = tools_dir / "candidate_pytest.py"
        os.symlink(real_wrapper, symlink_wrapper)
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="candidate_pytest_wrapper_symlink_forbidden",
        ):
            worker_workspace._resolve_candidate_pytest_wrapper(tmp_path)

    @staticmethod
    def test_directory_not_regular_file_rejected(tmp_path: Path) -> None:
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        dir_wrapper = tools_dir / "candidate_pytest.py"
        dir_wrapper.mkdir()
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="candidate_pytest_wrapper_not_regular",
        ):
            worker_workspace._resolve_candidate_pytest_wrapper(tmp_path)

    @staticmethod
    @pytest.mark.skipif(os.name == "nt", reason="POSIX owner/mode semantics")
    def test_world_writable_wrapper_rejected(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        wrapper = tools_dir / "candidate_pytest.py"
        wrapper.write_text("# wrapper\n", encoding="utf-8")
        wrapper.chmod(0o666)  # world-writable
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="candidate_pytest_wrapper_world_writable",
        ):
            worker_workspace._resolve_candidate_pytest_wrapper(tmp_path)

    @staticmethod
    def test_wrong_owner_rejected_posix(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if os.name == "nt":
            pytest.skip("POSIX owner semantics")
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        wrapper = tools_dir / "candidate_pytest.py"
        wrapper.write_text("# wrapper\n", encoding="utf-8")
        # Capture the real uid and original function before monkeypatching
        # so the lambda does not recursively call the patched os.getuid.
        _real_getuid = os.getuid
        real_uid = _real_getuid()
        monkeypatch.setattr(os, "getuid", lambda _real=_real_getuid: _real() + 1)
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="candidate_pytest_wrapper_untrusted_owner",
        ):
            worker_workspace._resolve_candidate_pytest_wrapper(tmp_path)


class TestCandidatePytestWrapperModuleInstallUninstall:
    @staticmethod
    def test_install_and_uninstall_restores_sys_modules(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        wrapper = tools_dir / "candidate_pytest.py"
        wrapper.write_text("# wrapper\n", encoding="utf-8")
        _resolved, entry = worker_workspace._resolve_candidate_pytest_wrapper(tmp_path)
        module_name = entry["name"]
        # Ensure not already present
        assert module_name not in sys.modules
        worker_workspace._install_candidate_pytest_wrapper_module(entry)
        assert module_name in sys.modules
        worker_workspace._uninstall_candidate_pytest_wrapper_module(entry)
        assert module_name not in sys.modules

    @staticmethod
    def test_uninstall_none_is_noop() -> None:
        """_uninstall_candidate_pytest_wrapper_module(None) does not raise"""
        worker_workspace._uninstall_candidate_pytest_wrapper_module(None)

    @staticmethod
    def test_double_install_raises(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        wrapper = tools_dir / "candidate_pytest.py"
        wrapper.write_text("# wrapper\n", encoding="utf-8")
        _resolved, entry = worker_workspace._resolve_candidate_pytest_wrapper(tmp_path)
        try:
            worker_workspace._install_candidate_pytest_wrapper_module(entry)
            with pytest.raises(
                worker_workspace.WorkspaceError,
                match="candidate_pytest_wrapper_module_conflict",
            ):
                worker_workspace._install_candidate_pytest_wrapper_module(entry)
        finally:
            worker_workspace._uninstall_candidate_pytest_wrapper_module(entry)


def _preprovisioned_private_home(tmp_path: Path) -> Path:
    """Truthful landlock-style home: owner-private, with a private ``tmp``.

    Under the landlock backend ``run_validations`` verifies (fail-closed) that
    ``workspace.home`` and ``workspace.home/tmp`` are already-provisioned,
    owner-private directories. A real ``create_workspace`` provides them; the
    regression fixtures below constructed ``WorkerWorkspace(home=tmp_path)``
    directly and therefore passed only under bubblewrap. Preprovisioning the
    directories here makes the fixtures backend-truthful without weakening the
    production verification.
    """
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / "tmp").mkdir(mode=0o700)
    return home


class TestFocusedRegressionExercisesCandidate:
    """Integration tests for run_validations with candidate pytest wrapper."""

    @staticmethod
    def test_candidate_wrapper_gets_candidate_first_pythonpath(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """NF128: exact candidate wrapper PYTHONPATH = declared first, pytest last."""
        # 1. Create workspace BEFORE monkeypatching subprocess
        workspace = worker_workspace.WorkerWorkspace(
            request_id="nf128-candidate-py-order",
            repo=tmp_path,
            path=tmp_path,
            home=_preprovisioned_private_home(tmp_path),
            allowed_writes=(),
            parent_baseline={},
            workspace_baseline={},
        )

        # 2. Create the wrapper file and a declared PYTHONPATH component
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        wrapper = tools_dir / "candidate_pytest.py"
        wrapper.write_text("import pytest\n", encoding="utf-8")
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        # 3. Pre-create a tmp scratch
        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir(mode=0o700)

        # 4. Mock provision/cleanup to use the pre-created scratch
        monkeypatch.setattr(
            worker_workspace,
            "provision_validation_exec_scratch",
            lambda _ws: scratch_dir,
        )
        cleanup_called = [False]

        def _fake_cleanup(p: Path | None) -> None:
            cleanup_called[0] = True

        monkeypatch.setattr(
            worker_workspace,
            "cleanup_validation_exec_scratch",
            _fake_cleanup,
        )

        # 5. Mock resolve_trusted_pytest_runtime_root to a real tmp_path
        pytest_root = tmp_path / "pytest_site"
        pytest_root.mkdir()
        monkeypatch.setattr(
            worker_workspace,
            "resolve_trusted_pytest_runtime_root",
            lambda: pytest_root,
        )

        # 6. Mock _approved_pythonpath_site to accept only that exact root
        def _approved_site(component: str) -> Path:
            c = Path(component)
            if c == pytest_root:
                return pytest_root
            raise worker_workspace.WorkspaceError(
                f"validation_pythonpath_absolute_component_forbidden:{component}"
            )

        monkeypatch.setattr(
            worker_workspace,
            "_approved_pythonpath_site",
            _approved_site,
        )

        # 7. Mock resolve_validation_pythonpath to capture effective_components
        #    order without asserting sandbox-virtualized values.
        captured_components: tuple[str, ...] | None = None

        from collections.abc import Callable as _Callable
        _orig_resolve = worker_workspace.resolve_validation_pythonpath

        def _capture_resolve(
            ws: worker_workspace.WorkerWorkspace,
            backend: str,
            components: tuple[str, ...],
        ) -> str:
            nonlocal captured_components
            captured_components = components
            return _orig_resolve(ws, backend, components)

        monkeypatch.setattr(
            worker_workspace,
            "resolve_validation_pythonpath",
            _capture_resolve,
        )

        # 8. Capture subprocess.run only around run_validations
        captured_env: dict[str, str] | None = None

        orig_run = worker_workspace.subprocess.run

        def _capture_run(argv, **kwargs):
            nonlocal captured_env
            captured_env = dict(kwargs.get("env", {}))
            return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

        monkeypatch.setattr(worker_workspace.subprocess, "run", _capture_run)

        try:
            results = worker_workspace.run_validations(
                workspace,
                ["PYTHONPATH=src python3 tools/candidate_pytest.py tests/"],
            )
            assert len(results) == 1
            assert results[0]["returncode"] == 0
            assert captured_components is not None, (
                "resolve_validation_pythonpath was not called"
            )
            # Candidate: declared components first, trusted pytest last.
            assert len(captured_components) >= 2
            assert captured_components[0] == 'src', (
                f"declared 'src' expected first, got {captured_components}"
            )
            assert captured_components[-1] == str(pytest_root), (
                f"pytest root expected last, got {captured_components}"
            )
        finally:
            # Restore subprocess.run before cleanup
            monkeypatch.setattr(worker_workspace.subprocess, "run", orig_run)
            assert cleanup_called[0] is True

    @staticmethod
    def test_ordinary_pytest_keeps_trusted_first_pythonpath(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Ordinary pytest keeps trusted-root-first PYTHONPATH."""
        workspace = worker_workspace.WorkerWorkspace(
            request_id="nf128-ordinary-py-order",
            repo=tmp_path,
            path=tmp_path,
            home=_preprovisioned_private_home(tmp_path),
            allowed_writes=(),
            parent_baseline={},
            workspace_baseline={},
        )

        # Create a declared PYTHONPATH component
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir(mode=0o700)

        monkeypatch.setattr(
            worker_workspace,
            "provision_validation_exec_scratch",
            lambda _ws: scratch_dir,
        )
        monkeypatch.setattr(
            worker_workspace,
            "cleanup_validation_exec_scratch",
            lambda _p: None,
        )

        pytest_root = tmp_path / "pytest_site"
        pytest_root.mkdir()
        monkeypatch.setattr(
            worker_workspace,
            "resolve_trusted_pytest_runtime_root",
            lambda: pytest_root,
        )

        def _approved_site(component: str) -> Path:
            c = Path(component)
            if c == pytest_root:
                return pytest_root
            raise worker_workspace.WorkspaceError(
                f"validation_pythonpath_absolute_component_forbidden:{component}"
            )

        monkeypatch.setattr(
            worker_workspace,
            "_approved_pythonpath_site",
            _approved_site,
        )

        # Mock resolve_validation_pythonpath to capture effective_components
        # order without asserting sandbox-virtualized values.
        captured_components: tuple[str, ...] | None = None

        _orig_resolve = worker_workspace.resolve_validation_pythonpath

        def _capture_resolve(
            ws: worker_workspace.WorkerWorkspace,
            backend: str,
            components: tuple[str, ...],
        ) -> str:
            nonlocal captured_components
            captured_components = components
            return _orig_resolve(ws, backend, components)

        monkeypatch.setattr(
            worker_workspace,
            "resolve_validation_pythonpath",
            _capture_resolve,
        )

        captured_env: dict[str, str] | None = None

        orig_run = worker_workspace.subprocess.run

        def _capture_run(argv, **kwargs):
            nonlocal captured_env
            captured_env = dict(kwargs.get("env", {}))
            return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

        monkeypatch.setattr(worker_workspace.subprocess, "run", _capture_run)

        try:
            results = worker_workspace.run_validations(
                workspace,
                ["PYTHONPATH=src pytest tests/"],
            )
            assert len(results) == 1
            assert results[0]["returncode"] == 0
            assert captured_components is not None, (
                "resolve_validation_pythonpath was not called"
            )
            # Ordinary pytest: trusted root FIRST, declared components after.
            assert len(captured_components) >= 2
            assert captured_components[0] == str(pytest_root), (
                f"pytest root expected first, got {captured_components}"
            )
            assert captured_components[-1] == 'src', (
                f"declared 'src' expected last, got {captured_components}"
            )
        finally:
            monkeypatch.setattr(worker_workspace.subprocess, "run", orig_run)

    @staticmethod
    def test_near_match_candidate_falls_through_to_ordinary_pytest(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """python tools/candidate_pytest.py is NOT a candidate wrapper."""
        workspace = worker_workspace.WorkerWorkspace(
            request_id="nf128-near-fallthrough",
            repo=tmp_path,
            path=tmp_path,
            home=_preprovisioned_private_home(tmp_path),
            allowed_writes=(),
            parent_baseline={},
            workspace_baseline={},
        )

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir(mode=0o700)

        monkeypatch.setattr(
            worker_workspace,
            "provision_validation_exec_scratch",
            lambda _ws: scratch_dir,
        )
        monkeypatch.setattr(
            worker_workspace,
            "cleanup_validation_exec_scratch",
            lambda _p: None,
        )

        pytest_root = tmp_path / "pytest_site"
        pytest_root.mkdir()
        monkeypatch.setattr(
            worker_workspace,
            "resolve_trusted_pytest_runtime_root",
            lambda: pytest_root,
        )

        def _approved_site(component: str) -> Path:
            c = Path(component)
            if c == pytest_root:
                return pytest_root
            raise worker_workspace.WorkspaceError(
                f"validation_pythonpath_absolute_component_forbidden:{component}"
            )

        monkeypatch.setattr(
            worker_workspace,
            "_approved_pythonpath_site",
            _approved_site,
        )

        captured_env: dict[str, str] | None = None

        orig_run = worker_workspace.subprocess.run

        def _capture_run(argv, **kwargs):
            nonlocal captured_env
            captured_env = dict(kwargs.get("env", {}))
            return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

        monkeypatch.setattr(worker_workspace.subprocess, "run", _capture_run)

        try:
            # "python" not "python3" -> not candidate, but also not "pytest" bare
            # so _is_pytest_validation_command won't match either.  The command
            # runs as-is with no PYTHONPATH overlay.
            results = worker_workspace.run_validations(
                workspace,
                ["python tools/candidate_pytest.py tests/"],
            )
            assert len(results) == 1
            # No PYTHONPATH because neither candidate nor pytest matched
            assert captured_env is not None
            assert "PYTHONPATH" not in captured_env
        finally:
            monkeypatch.setattr(worker_workspace.subprocess, "run", orig_run)

    @staticmethod
    def test_candidate_wrapper_missing_file_raises_before_execution(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When tools/candidate_pytest.py is missing, ValidationRunError."""
        workspace = worker_workspace.WorkerWorkspace(
            request_id="nf128-missing-wrapper",
            repo=tmp_path,
            path=tmp_path,
            home=tmp_path,
            allowed_writes=(),
            parent_baseline={},
            workspace_baseline={},
        )

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir(mode=0o700)

        monkeypatch.setattr(
            worker_workspace,
            "provision_validation_exec_scratch",
            lambda _ws: scratch_dir,
        )
        monkeypatch.setattr(
            worker_workspace,
            "cleanup_validation_exec_scratch",
            lambda _p: None,
        )

        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="candidate_pytest_wrapper_unavailable",
        ):
            worker_workspace.run_validations(
                workspace,
                ["python3 tools/candidate_pytest.py tests/"],
            )


# ── NF180 mypy/temp/cache isolation and truthful failure reporting ─────────


def _landlock_run_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scratch_dir: Path,
) -> worker_workspace.WorkerWorkspace:
    """Wire the landlock validation path to deterministic, in-process mocks."""
    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", lambda: "landlock")
    monkeypatch.setattr(
        worker_workspace,
        "provision_validation_exec_scratch",
        lambda _ws: scratch_dir,
    )
    monkeypatch.setattr(
        worker_workspace,
        "cleanup_validation_exec_scratch",
        lambda _p: None,
    )
    monkeypatch.setattr(
        worker_workspace,
        "sandbox_argv",
        lambda _ws, _adapter, argv, **_kwargs: list(argv),
    )
    return worker_workspace.WorkerWorkspace(
        request_id="nf180-validation-harness",
        repo=tmp_path,
        path=tmp_path,
        home=_preprovisioned_private_home(tmp_path),
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )


def test_validation_env_isolates_mypy_cache_and_temp_to_request_scratch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir(mode=0o700)
    workspace = _landlock_run_harness(monkeypatch, tmp_path, scratch_dir)

    captured_env: dict[str, str] = {}

    def _capture_run(argv, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(worker_workspace.subprocess, "run", _capture_run)

    results = worker_workspace.run_validations(workspace, ["/usr/bin/mypy src"])

    assert len(results) == 1
    assert results[0]["returncode"] == 0
    assert captured_env["MYPY_CACHE_DIR"] == str(scratch_dir)
    assert captured_env["TMPDIR"] == str(scratch_dir)
    assert captured_env["RUFF_CACHE_DIR"] == str(scratch_dir)


def test_parallel_validation_requests_use_distinct_cache_and_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scratch_a = tmp_path / "scratch-a"
    scratch_b = tmp_path / "scratch-b"
    scratch_a.mkdir(mode=0o700)
    scratch_b.mkdir(mode=0o700)
    workspace = _landlock_run_harness(monkeypatch, tmp_path, scratch_a)

    scratch_sequence = iter([scratch_a, scratch_b])
    monkeypatch.setattr(
        worker_workspace,
        "provision_validation_exec_scratch",
        lambda _ws: next(scratch_sequence),
    )

    envs: list[dict[str, str]] = []

    def _capture_run(argv, **kwargs):
        envs.append(dict(kwargs.get("env", {})))
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(worker_workspace.subprocess, "run", _capture_run)

    worker_workspace.run_validations(workspace, ["/usr/bin/mypy src"])
    worker_workspace.run_validations(workspace, ["/usr/bin/mypy src"])

    assert len(envs) == 2
    assert envs[0]["MYPY_CACHE_DIR"] == str(scratch_a)
    assert envs[1]["MYPY_CACHE_DIR"] == str(scratch_b)
    assert envs[0]["MYPY_CACHE_DIR"] != envs[1]["MYPY_CACHE_DIR"]
    assert envs[0]["TMPDIR"] != envs[1]["TMPDIR"]


def test_mypy_internal_error_retains_bounded_traceback_and_environment_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir(mode=0o700)
    workspace = _landlock_run_harness(monkeypatch, tmp_path, scratch_dir)

    internal_stderr = (
        "Traceback (most recent call last):\n"
        '  File "/venv/bin/mypy", line 8, in <module>\n'
        "    main()\n"
        "OSError: [Errno 30] Read-only file system: '.mypy_cache'\n"
        "mypy: INTERNAL ERROR: could not write cache\n"
    )

    def _capture_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr=internal_stderr)

    monkeypatch.setattr(worker_workspace.subprocess, "run", _capture_run)

    with pytest.raises(worker_workspace.ValidationRunError) as caught:
        worker_workspace.run_validations(workspace, ["/usr/bin/mypy src"])

    record = caught.value.results[0]
    assert record["returncode"] == 2
    assert record["failure_receipt"]["failure_class"] == "type_check_internal_error"
    assert "INTERNAL ERROR" in record["internal_error"]["traceback_tail"]
    assert record["internal_error"]["environment"]["MYPY_CACHE_DIR"] == str(scratch_dir)
    assert set(record["internal_error"]["environment"]) == {
        "MYPY_CACHE_DIR",
        "TMPDIR",
        "RUFF_CACHE_DIR",
        "python_version",
    }


def test_validation_permission_failure_does_not_obscure_candidate_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir(mode=0o700)
    workspace = _landlock_run_harness(monkeypatch, tmp_path, scratch_dir)

    calls = {"n": 0}

    def _fake_run(argv, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "Permission denied")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="1 failed")

    monkeypatch.setattr(worker_workspace.subprocess, "run", _fake_run)

    with pytest.raises(worker_workspace.ValidationRunError) as caught:
        worker_workspace.run_validations(
            workspace,
            ["/usr/bin/mypy src", "bash -c 'exit 1'"],
        )

    results = caught.value.results
    assert len(results) == 2
    assert results[0]["launch_error"] == "PermissionError"
    assert results[0]["failure_receipt"]["failure_class"] == "permission_denied"
    assert results[1]["returncode"] == 1
    assert results[1]["failure_receipt"]["failure_class"] == "nonzero_exit"
    # NF-WAVE-SANDBOX-TRUTH: a genuine gate failure in the batch keeps the
    # terminal state ``validation_failed`` even though an environment restriction
    # (the refused spawn) is also present. The recoverable, supersede-free
    # environment-blocked state is claimed ONLY when EVERY failure is
    # environmental, so validation_failed is never weakened. Before this change
    # the batch was simply ``validation_failed`` with no terminal_state field;
    # after, it is still validation_failed and explicitly not reclassified.
    assert not isinstance(caught.value, worker_workspace.ValidationEnvironmentBlocked)
    assert caught.value.terminal_state == "validation_failed"
    assert caught.value.requires_supersede is True


def test_run_validations_explicit_backend_unsupported_is_named_not_failed(
    tmp_path: Path,
) -> None:
    """NF-2026-00271: an unsupported explicit ``backend=`` must surface as the
    recoverable ``validation_unsupported_in_sandbox`` restriction -- never as the
    acceptance-blocking ``validation_failed`` -- and keep the exact backend token
    so a recovered card names the precise reason. It is a plain ``WorkspaceError``
    (not a ``ValidationRunError``), so the finalizer routes it to the retryable
    ``finalize_failed`` bucket that preserves the retained workspace/hashes."""
    workspace = _bare_workspace(tmp_path, "unsupported-backend")

    with pytest.raises(worker_workspace.WorkspaceError) as caught:
        worker_workspace.run_validations(
            workspace, ["pytest -q"], backend="bogus-backend"
        )

    message = str(caught.value)
    assert message.startswith(worker_workspace.VALIDATION_UNSUPPORTED_IN_SANDBOX + ":")
    assert "unsupported_sandbox_backend:bogus-backend" in message
    assert worker_workspace.VALIDATION_FAILED not in message
    assert not isinstance(caught.value, worker_workspace.ValidationRunError)
    assert not isinstance(caught.value, worker_workspace.ValidationEnvironmentBlocked)


def test_run_validations_sandbox_selection_failure_is_unsupported_not_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the sandbox backend cannot be provisioned (no bwrap/landlock), the
    validation run must be named ``validation_unsupported_in_sandbox`` with the
    exact selection restriction preserved -- never ``validation_failed`` -- so a
    provider-free ``retry_finalization`` can re-run it in a corrected sandbox."""
    workspace = _bare_workspace(tmp_path, "sandbox-unsupported")

    def _raise_selection_error() -> None:
        raise worker_workspace.WorkspaceError(
            "secure_sandbox_unavailable:bubblewrap_unusable:landlock_unsupported"
        )

    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", _raise_selection_error)

    with pytest.raises(worker_workspace.WorkspaceError) as caught:
        worker_workspace.run_validations(workspace, ["pytest -q"])

    message = str(caught.value)
    assert message.startswith(worker_workspace.VALIDATION_UNSUPPORTED_IN_SANDBOX + ":")
    assert (
        "secure_sandbox_unavailable:bubblewrap_unusable:landlock_unsupported"
        in message
    )
    assert worker_workspace.VALIDATION_FAILED not in message
    assert not isinstance(caught.value, worker_workspace.ValidationRunError)
    assert not isinstance(caught.value, worker_workspace.ValidationEnvironmentBlocked)


# ── NF-2026-00285: create/promote consistency window ───────────────────────


def test_create_workspace_refuses_to_seed_during_promotion_in_flight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    # Model a promotion writing into the parent tree: an in-flight marker for
    # some other request exists while a create is attempted.
    inflight = worker_workspace._promotion_inflight_dir(repo)
    inflight.mkdir(parents=True, exist_ok=True)
    marker = inflight / "other-request"
    marker.write_text("", encoding="utf-8")

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="worktree_seed_refused_promotion_in_flight",
    ):
        worker_workspace.create_workspace(
            repo,
            "req-during-promo",
            {"allowed_writes": ["out/result.txt"], "read_first": ["read/input.txt"]},
            "validation",
        )

    # Once the promotion completes and the marker is gone, seeding succeeds and
    # sees a consistent tree.
    marker.unlink()
    workspace = worker_workspace.create_workspace(
        repo,
        "req-after-promo",
        {"allowed_writes": ["out/result.txt"], "read_first": ["read/input.txt"]},
        "validation",
    )
    try:
        assert workspace.path.is_dir()
        assert (workspace.path / "read" / "input.txt").read_bytes() == b"input-v1\n"
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_promote_marks_and_clears_the_in_flight_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "promo-window")
    marker = worker_workspace._promotion_inflight_dir(repo) / workspace.request_id
    observed = {"during_write": None}
    try:
        (workspace.path / "out" / "result.txt").write_bytes(b"result-v2\n")

        real_copyfile = worker_workspace.shutil.copyfile

        def _spy_copyfile(source, destination, *args, **kwargs):
            observed["during_write"] = marker.exists()
            return real_copyfile(source, destination, *args, **kwargs)

        monkeypatch.setattr(worker_workspace.shutil, "copyfile", _spy_copyfile)

        promoted = worker_workspace.promote(workspace, ["out/result.txt"])

        assert promoted == ["out/result.txt"]
        # The marker existed WHILE the parent tree was being written...
        assert observed["during_write"] is True
        # ...and was cleared once promotion finished, so later creates are not
        # blocked forever.
        assert not marker.exists()
        assert (repo / "out" / "result.txt").read_bytes() == b"result-v2\n"
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_create_workspace_rechecks_promotion_marker_appearing_during_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    # NF-2026-00285 check-then-act closure: the up-front guard passes (no marker
    # yet), then a promotion begins WHILE the new worktree is still copying the
    # declared inputs from the live parent tree -- the exact window a single
    # up-front check cannot see. The seed must be refused with the named reason,
    # not returned as a half-promoted, inconsistent tree.
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    inflight = worker_workspace._promotion_inflight_dir(repo)
    real_copy_one = worker_workspace._copy_one
    planted = {"done": False}

    def _plant_marker_mid_seed(source, destination):
        if not planted["done"]:
            inflight.mkdir(parents=True, exist_ok=True)
            (inflight / "concurrent-promotion").write_text("", encoding="utf-8")
            planted["done"] = True
        return real_copy_one(source, destination)

    monkeypatch.setattr(worker_workspace, "_copy_one", _plant_marker_mid_seed)

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="worktree_seed_refused_promotion_in_flight",
    ):
        worker_workspace.create_workspace(
            repo,
            "req-marker-mid-seed",
            {"allowed_writes": ["out/result.txt"], "read_first": ["read/input.txt"]},
            "validation",
        )

    # The up-front check ran before any copy (so it saw no marker)...
    assert planted["done"] is True
    # ...and the half-seeded worktree was cleaned up rather than handed back.
    assert not (tmp_path / "worktrees" / "req-marker-mid-seed").exists()


# ── NF-WAVE-SANDBOX-TRUTH (rework, HIGH SECURITY): the host validator probe
# must never execute candidate-authored code ───────────────────────────────


def test_host_probe_pythonpath_drops_candidate_writable_components(
    tmp_path: Path,
) -> None:
    """Only trusted host-absolute validator roots may reach the probe's
    sys.path; every candidate-writable (``.``/relative) component is dropped."""
    worktree = tmp_path / "worktree"
    (worktree / "sub").mkdir(parents=True)
    workspace = worker_workspace.WorkerWorkspace(
        request_id="host-probe-drop",
        repo=tmp_path,
        path=worktree,
        home=tmp_path / "home",
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    trusted = tmp_path / "trusted-site"
    trusted.mkdir()
    result = worker_workspace._host_probe_pythonpath(
        workspace, (str(trusted), ".", "sub")
    )
    parts = result.split(os.pathsep) if result else []
    # The trusted absolute root survives; no candidate-writable path is on it.
    assert parts == [str(trusted)]
    assert str(worktree) not in result
    assert str(worktree / "sub") not in result


def test_validator_probe_never_executes_candidate_sitecustomize(
    tmp_path: Path,
) -> None:
    """The probe runs on the HOST. A candidate that plants ``sitecustomize.py``
    in its worktree root must never get it executed by the probe interpreter's
    site initialization (arbitrary code execution as the coordinator user)."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    marker = tmp_path / "SITECUSTOMIZE_EXECUTED"
    (worktree / "sitecustomize.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('rce', encoding='utf-8')\n",
        encoding="utf-8",
    )
    workspace = worker_workspace.WorkerWorkspace(
        request_id="probe-sitecustomize",
        repo=tmp_path,
        path=worktree,
        home=tmp_path / "home",
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )

    # Control: prove the planted sitecustomize IS functional -- with the worktree
    # on PYTHONPATH and ordinary site processing, it executes and writes the
    # marker. Otherwise this regression could pass vacuously.
    control = dict(os.environ)
    control["PYTHONPATH"] = str(worktree)
    control.pop("PYTHONSAFEPATH", None)
    control.pop("PYTHONNOUSERSITE", None)
    subprocess.run(
        [sys.executable, "-c", "pass"],
        env=control,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert marker.exists(), "planted sitecustomize is inert; the test would be vacuous"
    marker.unlink()

    # The real probe: the candidate-writable "." component is dropped from the
    # probe PYTHONPATH and PYTHONSAFEPATH suppresses the implicit cwd entry, so
    # the worktree is never on sys.path and sitecustomize never runs.
    worker_workspace._probe_absent_validator_modules(
        workspace, ["python3", "-m", "pytest"], (".",)
    )
    assert not marker.exists(), (
        "candidate sitecustomize.py executed on the host during the validator probe"
    )


def test_terminal_state_literals_match_validation_runner() -> None:
    # worker_workspace.py re-states VALIDATION_FAILED / VALIDATION_ENVIRONMENT_BLOCKED
    # as bare literals because it must load as a direct Landlock-wrapper script with
    # no package context (see the comment above the constants and
    # tests/test_runtime_temp.py::test_worker_workspace_direct_script_resolves_sibling_runtime_temp,
    # which catches a sibling-import fallback failing in that mode). This guard
    # enforces the promise made in that comment: the duplication must never drift
    # from validation_runner's authoritative source of truth.
    from aiworkhub import validation_runner

    assert worker_workspace.VALIDATION_FAILED == validation_runner.VALIDATION_FAILED
    assert (
        worker_workspace.VALIDATION_ENVIRONMENT_BLOCKED
        == validation_runner.VALIDATION_ENVIRONMENT_BLOCKED
    )


def _install_venv_python(root: Path, relative: str, marker: str) -> Path:
    path = root.joinpath(*relative.replace("\\", "/").split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho {marker}\nexit 0\n", encoding="utf-8")
    os.chmod(path, 0o755)
    return path.resolve()


def _run_interpreter_validation(workspace, command: str):
    return worker_workspace.run_validations(
        workspace,
        [command],
        backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
        adapter_id="glm_vscode_lm",
    )


def test_run_validations_workspace_local_venv_python_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-python-local")
    try:
        expected = _install_venv_python(
            workspace.path, ".venv/bin/python", "ran-workspace-local-python"
        )
        _install_venv_python(
            workspace.repo, ".venv/bin/python", "ran-canonical-python"
        )
        (result,) = _run_interpreter_validation(
            workspace, ".venv/bin/python -c pass"
        )
        assert result["returncode"] == 0, result["stderr_tail"]
        assert result["declared_argv"] == [".venv/bin/python", "-c", "pass"]
        assert result["executed_argv"] == [str(expected), "-c", "pass"]
        assert result["argv"] == result["executed_argv"]
        assert result["argv_rewritten"] is True
        assert result["interpreter_authority"] == {
            "schema_id": "aiworkhub.validation_interpreter_authority.v1",
            "declared": ".venv/bin/python",
            "source": "workspace_local",
            "execution_path": str(expected),
            "authenticated_endpoint": str(expected),
            "resolved": str(expected),
        }
        assert "ran-workspace-local-python" in result["stdout_tail"]
        assert "ran-canonical-python" not in result["stdout_tail"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_run_validations_canonical_venv_python_when_workspace_local_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-python-canonical")
    try:
        endpoint = _install_venv_python(
            workspace.repo, ".venv/bin/python", "ran-canonical-python"
        )
        execution_path = workspace.repo / ".venv" / "bin" / "python"
        (result,) = _run_interpreter_validation(
            workspace, ".venv/bin/python -c pass"
        )
        assert result["returncode"] == 0, result["stderr_tail"]
        assert result["declared_argv"] == [".venv/bin/python", "-c", "pass"]
        assert result["executed_argv"] == [str(execution_path), "-c", "pass"]
        assert result["interpreter_authority"]["source"] == "canonical_repository"
        assert result["interpreter_authority"]["execution_path"] == str(execution_path)
        assert result["interpreter_authority"]["authenticated_endpoint"] == str(endpoint)
        assert result["interpreter_authority"]["resolved"] == str(endpoint)
        assert "ran-canonical-python" in result["stdout_tail"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_run_validations_windows_venv_python_spelling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-python-windows")
    try:
        expected = _install_venv_python(
            workspace.path, ".venv/Scripts/python.exe", "ran-windows-python"
        )
        (result,) = _run_interpreter_validation(
            workspace, ".venv/Scripts/python.exe -c pass"
        )
        assert result["returncode"] == 0, result["stderr_tail"]
        assert result["declared_argv"] == [".venv/Scripts/python.exe", "-c", "pass"]
        assert result["executed_argv"] == [str(expected), "-c", "pass"]
        assert result["interpreter_authority"]["source"] == "workspace_local"
        assert "ran-windows-python" in result["stdout_tail"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_run_validations_unrecognized_and_absolute_python_pass_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-python-passthrough")
    try:
        unrecognized = _install_venv_python(
            workspace.path, ".venv/bin/python3", "ran-unrecognized-python3"
        )
        (relative_result,) = _run_interpreter_validation(
            workspace, ".venv/bin/python3 -c pass"
        )
        assert relative_result["returncode"] == 0, relative_result["stderr_tail"]
        assert relative_result["declared_argv"] == [".venv/bin/python3", "-c", "pass"]
        assert relative_result["executed_argv"] == [".venv/bin/python3", "-c", "pass"]
        assert relative_result["argv_rewritten"] is False
        assert relative_result["interpreter_authority"] is None
        assert "ran-unrecognized-python3" in relative_result["stdout_tail"]
        assert unrecognized.name == "python3"

        (absolute_result,) = _run_interpreter_validation(
            workspace, f"{sys.executable} -c pass"
        )
        assert absolute_result["returncode"] == 0, absolute_result["stderr_tail"]
        assert absolute_result["declared_argv"][0] == sys.executable
        assert absolute_result["executed_argv"][0] == sys.executable
        assert absolute_result["interpreter_authority"] is None

        (non_leading,) = _run_interpreter_validation(
            workspace, "echo .venv/bin/python"
        )
        assert non_leading["returncode"] == 0, non_leading["stderr_tail"]
        assert non_leading["declared_argv"] == ["echo", ".venv/bin/python"]
        assert non_leading["executed_argv"] == ["echo", ".venv/bin/python"]
        assert non_leading["interpreter_authority"] is None
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_run_validations_shell_operator_is_not_rewritten_as_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-python-shell-op")
    try:
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="validation_shell_syntax_forbidden",
        ):
            _run_interpreter_validation(
                workspace, "echo hi && .venv/bin/python -c pass"
            )
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_run_validations_missing_venv_python_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-python-missing")
    try:
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="validation_environment:interpreter_missing",
        ):
            _run_interpreter_validation(workspace, ".venv/bin/python -c pass")
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bit")
def test_run_validations_non_executable_venv_python_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-python-noexec")
    try:
        path = _install_venv_python(
            workspace.path, ".venv/bin/python", "should-not-run"
        )
        os.chmod(path, 0o644)
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="validation_environment:interpreter_not_executable",
        ):
            _run_interpreter_validation(workspace, ".venv/bin/python -c pass")
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_run_validations_symlink_escaping_venv_python_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-python-symlink")
    try:
        outside = tmp_path / "outside-python"
        outside.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(outside, 0o755)
        link = workspace.path / ".venv" / "bin" / "python"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="validation_environment:interpreter_symlink_escape",
        ):
            _run_interpreter_validation(workspace, ".venv/bin/python -c pass")
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_run_validations_workspace_local_coordinator_symlink_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-local-coordinator-link")
    try:
        link = workspace.path / ".venv" / "bin" / "python"
        link.parent.mkdir(parents=True)
        link.symlink_to(Path(sys.executable).resolve(strict=True))
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="validation_environment:interpreter_symlink_escape",
        ):
            _run_interpreter_validation(workspace, ".venv/bin/python -c pass")
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_run_validations_canonical_trusted_owner_venv_python_preserves_execution_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-canonical-coordinator-link")
    try:
        execution_path = workspace.repo / ".venv" / "bin" / "python"
        execution_path.parent.mkdir(parents=True)
        coordinator = Path(sys.executable).resolve(strict=True)
        endpoint = tmp_path / "trusted-coordinator-python"
        shutil.copy2(coordinator, endpoint)
        endpoint.chmod(0o700)
        monkeypatch.setattr(worker_workspace.sys, "executable", str(endpoint))
        assert endpoint.stat().st_uid in {0, os.getuid()}
        assert stat.S_IMODE(endpoint.stat().st_mode) == 0o700
        execution_path.symlink_to(endpoint)
        (result,) = _run_interpreter_validation(
            workspace, ".venv/bin/python -c pass"
        )
        assert result["returncode"] == 0, result["stderr_tail"]
        assert result["declared_argv"] == [".venv/bin/python", "-c", "pass"]
        assert result["executed_argv"] == [str(execution_path), "-c", "pass"]
        assert result["argv"] == result["executed_argv"]
        assert result["argv_rewritten"] is True
        assert result["interpreter_authority"] == {
            "schema_id": "aiworkhub.validation_interpreter_authority.v1",
            "declared": ".venv/bin/python",
            "source": "canonical_repository",
            "execution_path": str(execution_path),
            "authenticated_endpoint": str(endpoint),
            "resolved": str(endpoint),
        }
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_run_validations_canonical_other_endpoint_symlink_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-canonical-other-link")
    try:
        outside = tmp_path / "other-python"
        outside.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(outside, 0o755)
        link = workspace.repo / ".venv" / "bin" / "python"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="validation_environment:interpreter_symlink_escape",
        ):
            _run_interpreter_validation(workspace, ".venv/bin/python -c pass")
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_run_validations_canonical_symlinked_parent_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-canonical-parent-link")
    try:
        outside = tmp_path / "external-venv"
        python = outside / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.symlink_to(Path(sys.executable).resolve(strict=True))
        (workspace.repo / ".venv").symlink_to(outside, target_is_directory=True)
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="validation_environment:interpreter_symlink_escape",
        ):
            _run_interpreter_validation(workspace, ".venv/bin/python -c pass")
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner metadata")
def test_run_validations_wrong_owner_venv_python_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-python-owner")
    try:
        _install_venv_python(workspace.path, ".venv/bin/python", "should-not-run")
        real_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="validation_environment:interpreter_untrusted_owner",
        ):
            _run_interpreter_validation(workspace, ".venv/bin/python -c pass")
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_run_validations_world_writable_venv_python_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "venv-python-world")
    try:
        path = _install_venv_python(
            workspace.path, ".venv/bin/python", "should-not-run"
        )
        os.chmod(path, 0o757)
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="validation_environment:interpreter_world_writable",
        ):
            _run_interpreter_validation(workspace, ".venv/bin/python -c pass")
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Landlock validation sandbox")
@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub hosted runners cannot execute nested Landlock Git helpers",
)
def test_run_validations_nested_git_sparse_checkout_under_scratch_denies_canonical(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    secret = repo / "canonical-secret.txt"
    secret.write_text("keep\n", encoding="utf-8")
    base = tmp_path / "worktrees" / "nf395-nested-git"
    path = base / "worktree"
    home = base / "home"
    path.mkdir(parents=True)
    home.mkdir(parents=True, mode=0o700)
    (home / "tmp").mkdir(mode=0o700)
    workspace = worker_workspace.WorkerWorkspace(
        request_id="nf395-nested-git",
        repo=repo,
        path=path,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    (path / "exec_git.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "root = Path(os.environ['TMPDIR']) / 'pytest-tmp' / 'nested-git'\n"
        "root.mkdir(parents=True, exist_ok=True)\n"
        "args = [str(root.parent / 'linked-worktree') if value == '__LINKED__' else value for value in sys.argv[1:]]\n"
        "os.chdir(root)\n"
        "os.execvp('git', ['git', *args])\n",
        encoding="utf-8",
    )
    (path / "prepare_nested_git.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "root = Path(os.environ['TMPDIR']) / 'pytest-tmp' / 'nested-git'\n"
        "(root / 'src').mkdir()\n"
        "(root / 'src' / 'tracked.txt').write_text('ok\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (path / "verify_nested_git.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "root = Path(os.environ['TMPDIR']) / 'pytest-tmp' / 'nested-git'\n"
        "linked = root.parent / 'linked-worktree'\n"
        "if not (linked / 'src' / 'tracked.txt').is_file():\n"
        "    sys.exit(31)\n"
        "print('nested-git-sparse-ok')\n"
        "print('scratch=' + os.environ['TMPDIR'])\n",
        encoding="utf-8",
    )
    (path / "deny_canonical.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "denied = Path(sys.argv[1])\n"
        "try:\n"
        "    denied.write_text('mutated\\n', encoding='utf-8')\n"
        "except PermissionError:\n"
        "    print('write-denied')\n"
        "except Exception as exc:\n"
        "    sys.stderr.write(type(exc).__name__ + ':' + str(exc) + '\\n')\n"
        "    sys.exit(28)\n"
        "else:\n"
        "    sys.exit(25)\n"
        "try:\n"
        "    os.chmod(denied, 0o600)\n"
        "except PermissionError:\n"
        "    print('chmod-denied')\n"
        "except Exception as exc:\n"
        "    sys.stderr.write(type(exc).__name__ + ':' + str(exc) + '\\n')\n"
        "    sys.exit(29)\n"
        "else:\n"
        "    sys.exit(26)\n",
        encoding="utf-8",
    )
    try:
        *git_results, deny_result = worker_workspace.run_validations(
            workspace,
            [
                "python3 exec_git.py init -b main",
                "python3 prepare_nested_git.py",
                "python3 exec_git.py config user.email nf395@example.invalid",
                "python3 exec_git.py config user.name NF395",
                "python3 exec_git.py add src/tracked.txt",
                "python3 exec_git.py commit -m init",
                "python3 exec_git.py sparse-checkout init --cone",
                "python3 exec_git.py sparse-checkout set src",
                "python3 exec_git.py worktree add --detach __LINKED__ HEAD",
                "python3 verify_nested_git.py",
                f"python3 deny_canonical.py {secret}",
            ],
            backend="landlock",
        )
        assert len(git_results) == 10
        for git_result in git_results:
            assert isinstance(git_result["returncode"], int)
            assert git_result["returncode"] >= 0, git_result
            assert git_result["returncode"] < 128, git_result
            assert git_result["returncode"] == 0, git_result["stderr_tail"]
        assert "nested-git-sparse-ok" in git_results[-1]["stdout_tail"]
        assert isinstance(deny_result["returncode"], int)
        assert deny_result["returncode"] >= 0, deny_result
        assert deny_result["returncode"] < 128, deny_result
        assert deny_result["returncode"] == 0, deny_result["stderr_tail"]
        assert "write-denied" in deny_result["stdout_tail"]
        assert "chmod-denied" in deny_result["stdout_tail"]
        assert secret.read_text(encoding="utf-8") == "keep\n"
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_outer_validation_authority_ignores_candidate_env_and_writable_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("AIWORKHUB_OUTER_VALIDATION_AUTHORITY", "1")
    monkeypatch.setenv(worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV, str(tmp_path))
    assert worker_workspace.authenticated_outer_validation_context() is None
    assert worker_workspace.nested_sandbox_requires_host_boundary() is False

    planted = worker_workspace.plant_outer_validation_authority(
        tmp_path, exec_scratch=tmp_path / "scratch"
    )
    assert planted.is_file()
    assert worker_workspace.verify_outer_validation_authority_file(planted) is None
    assert worker_workspace.authenticated_outer_validation_context() is None

    forged = tmp_path / worker_workspace.OUTER_VALIDATION_AUTHORITY_RELATIVE
    payload = json.loads(forged.read_text(encoding="utf-8"))
    payload["mac"] = "0" * 64
    try:
        forged.chmod(0o600)
    except PermissionError:
        pass
    forged.write_text(json.dumps(payload), encoding="utf-8")
    assert worker_workspace.verify_outer_validation_authority_file(forged) is None


def test_outer_validation_authority_requires_landlock_denied_sibling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    planted = worker_workspace.plant_outer_validation_authority(
        tmp_path, exec_scratch=tmp_path / "scratch"
    )
    monkeypatch.setattr(
        worker_workspace,
        "_directory_write_denied_by_landlock",
        lambda _directory: True,
    )
    verified = worker_workspace.verify_outer_validation_authority_file(planted)
    assert verified is not None
    assert verified["schema_id"] == worker_workspace.OUTER_VALIDATION_AUTHORITY_SCHEMA
    assert verified["exec_scratch"] == str((tmp_path / "scratch").resolve())
    assert worker_workspace.authenticated_outer_validation_context() == verified
    assert worker_workspace.nested_sandbox_requires_host_boundary() is True

    outside = tmp_path / "outside-worktrees"
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(outside))
    redirected = worker_workspace.configured_worktree_root(tmp_path)
    assert redirected == (tmp_path / "scratch" / "nested-worktrees").resolve()

    inside = tmp_path / "scratch" / "local-worktrees"
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(inside))
    assert worker_workspace.configured_worktree_root(tmp_path) == inside.resolve()


def test_sandbox_argv_plants_outer_authority_only_when_requested(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "outer-auth-flag")
    try:
        plain = worker_workspace.sandbox_argv(
            workspace,
            "validation",
            [sys.executable, "-c", "print(1)"],
            backend="landlock",
        )
        assert "--outer-validation-authority" not in plain
        flagged = worker_workspace.sandbox_argv(
            workspace,
            "validation",
            [sys.executable, "-c", "print(1)"],
            backend="landlock",
            outer_validation_authority=True,
        )
        assert "--outer-validation-authority" in flagged
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def _deny_landlock_sibling_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker_workspace,
        "_directory_write_denied_by_landlock",
        lambda _directory: True,
    )


def _plant_nested_landlock_layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    nested = scratch / "nested-worktrees" / "req" / "worktree"
    workspace.mkdir()
    nested.mkdir(parents=True)
    planted = worker_workspace.plant_outer_validation_authority(
        workspace, exec_scratch=scratch
    )
    locator = scratch / worker_workspace.NESTED_LANDLOCK_AUTHORITY_LOCATOR_RELATIVE
    return planted, locator, nested, scratch


def test_nested_landlock_locator_resolves_from_separate_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    planted, _locator, nested, scratch = _plant_nested_landlock_layout(tmp_path)
    _deny_landlock_sibling_writes(monkeypatch)
    monkeypatch.chdir(nested)
    verified = worker_workspace.authenticated_outer_validation_context()
    assert verified is not None
    assert verified["workspace"] == str((tmp_path / "workspace").resolve())
    assert verified["exec_scratch"] == str(scratch.resolve())
    assert worker_workspace.verify_outer_validation_authority_file(planted) == verified
    assert worker_workspace.nested_sandbox_requires_host_boundary() is True


def test_nested_landlock_locator_rejects_ambient_non_nested_scratch_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _planted, _locator, _nested, scratch = _plant_nested_landlock_layout(tmp_path)
    _deny_landlock_sibling_writes(monkeypatch)
    ambient = scratch / "pytest-tmp" / "ambient"
    ambient.mkdir(parents=True)
    monkeypatch.chdir(ambient)
    assert worker_workspace.authenticated_outer_validation_context() is None
    assert worker_workspace.nested_sandbox_requires_host_boundary() is False


def test_nested_landlock_locator_rejects_owner_mode_symlink_hmac_escape_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _planted, locator, nested, scratch = _plant_nested_landlock_layout(tmp_path)
    _deny_landlock_sibling_writes(monkeypatch)
    workspace = tmp_path / "workspace"
    monkeypatch.chdir(nested)
    real_owned = worker_workspace._coordinator_owned_regular_file
    locator_key = locator.resolve()

    try:
        locator.chmod(locator.stat().st_mode | stat.S_IWGRP)
        mode_forced = False
    except PermissionError:
        mode_forced = True

        def reject_locator_mode(path: Path):
            if path.resolve() == locator_key:
                return None
            return real_owned(path)

        monkeypatch.setattr(
            worker_workspace, "_coordinator_owned_regular_file", reject_locator_mode
        )
    assert worker_workspace.authenticated_outer_validation_context() is None
    if mode_forced:
        monkeypatch.setattr(
            worker_workspace, "_coordinator_owned_regular_file", real_owned
        )
    try:
        locator.chmod(0o444)
    except PermissionError:
        pass

    original_geteuid = os.geteuid
    monkeypatch.setattr(os, "geteuid", lambda: original_geteuid() + 1)
    assert worker_workspace.authenticated_outer_validation_context() is None
    monkeypatch.setattr(os, "geteuid", original_geteuid)

    real = locator.with_name(locator.name + ".real")
    locator.rename(real)
    locator.symlink_to(real)
    assert worker_workspace.authenticated_outer_validation_context() is None
    locator.unlink()
    real.rename(locator)

    payload = json.loads(locator.read_text(encoding="utf-8"))
    payload["mac"] = "0" * 64
    try:
        locator.chmod(0o600)
    except PermissionError:
        pass
    locator.write_text(json.dumps(payload), encoding="utf-8")
    try:
        locator.chmod(0o444)
    except PermissionError:
        pass
    assert worker_workspace.authenticated_outer_validation_context() is None

    worker_workspace.plant_outer_validation_authority(
        workspace, exec_scratch=scratch
    )
    status = locator.lstat()
    escaped = {
        "schema_id": worker_workspace.NESTED_LANDLOCK_AUTHORITY_LOCATOR_SCHEMA,
        "kind": worker_workspace._NESTED_LANDLOCK_AUTHORITY_LOCATOR_KIND,
        "authority": str((tmp_path / "outside" / "authority.json").resolve()),
        "exec_scratch": str(scratch.resolve()),
        "workspace": str(workspace.resolve()),
    }
    escaped["mac"] = worker_workspace._outer_validation_authority_mac(
        escaped, identity=status
    )
    try:
        locator.chmod(0o600)
    except PermissionError:
        pass
    locator.write_text(
        json.dumps(escaped, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    try:
        locator.chmod(0o444)
    except PermissionError:
        pass
    assert worker_workspace.authenticated_outer_validation_context() is None

    worker_workspace.plant_outer_validation_authority(
        workspace, exec_scratch=scratch
    )
    copied_bytes = locator.read_bytes()
    locator.unlink()
    locator.write_bytes(copied_bytes)
    try:
        locator.chmod(0o444)
    except PermissionError:
        pass
    assert worker_workspace.authenticated_outer_validation_context() is None


def test_nested_landlock_locator_lookup_is_bounded_to_cwd_ancestors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _planted, _locator, _nested, scratch = _plant_nested_landlock_layout(tmp_path)
    _deny_landlock_sibling_writes(monkeypatch)
    limit = worker_workspace._NESTED_LANDLOCK_AUTHORITY_LOCATOR_MAX_ANCESTORS
    too_deep = scratch
    for index in range(limit + 2):
        too_deep = too_deep / f"d{index}"
    too_deep.mkdir(parents=True)
    monkeypatch.chdir(too_deep)
    assert worker_workspace.authenticated_outer_validation_context() is None
    within = scratch / "nested-worktrees" / "req" / "worktree"
    monkeypatch.chdir(within)
    verified = worker_workspace.authenticated_outer_validation_context()
    assert verified is not None
    assert verified["exec_scratch"] == str(scratch.resolve())


def test_ambient_ancestor_locator_does_not_divert_unrelated_create_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_workspace = tmp_path / "host-workspace"
    host_scratch = tmp_path / "host-scratch"
    nested = host_scratch / "nested-worktrees" / "host-req" / "worktree"
    host_workspace.mkdir()
    nested.mkdir(parents=True)
    worker_workspace.plant_outer_validation_authority(
        host_workspace, exec_scratch=host_scratch
    )
    _deny_landlock_sibling_writes(monkeypatch)
    monkeypatch.chdir(nested)
    ambient = worker_workspace.authenticated_outer_validation_context()
    assert ambient is not None
    assert ambient["workspace"] == str(host_workspace.resolve())
    assert ambient["exec_scratch"] == str(host_scratch.resolve())

    outside = tmp_path / "outside-worktrees"
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(outside))
    assert worker_workspace.configured_worktree_root(host_workspace) == (
        host_scratch / "nested-worktrees"
    ).resolve()

    unrelated_repo = tmp_path / "unrelated-repo"
    unrelated_repo.mkdir()
    worktrees = tmp_path / "unrelated-worktrees"
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(worktrees))
    assert worker_workspace.configured_worktree_root(unrelated_repo) == worktrees.resolve()
    assert host_scratch.resolve() not in worktrees.parents
    assert worktrees.resolve() != (host_scratch / "nested-worktrees").resolve()


# ---------------------------------------------------------------------------
# NF-2026-00423: coherent current-canonical dependency generation.
#
# Post-NF376, a sparse candidate whose allowed production file imports a
# non-writable dependency overlaid the allowed file from the live parent tree
# while leaving the imported dependency at the stale detached-Git-HEAD copy.
# The exact reproduction: current ``process_launcher.py`` imports
# ``task_store.is_bool_safe_int`` (present only in the current canonical tree,
# not at HEAD). The dependency must be seeded from the same coherent
# current-canonical generation -- without ``task_store.py`` becoming writable,
# a candidate change, or promotable.
# ---------------------------------------------------------------------------

_TASK_STORE_HEAD = "STORE = 'v1'\n"
_TASK_STORE_CURRENT = (
    "def is_bool_safe_int(value):\n"
    "    return isinstance(value, int) and not isinstance(value, bool)\n"
)
_LAUNCHER_HEAD = "LAUNCH_OK = False\n"
_LAUNCHER_CURRENT = (
    "from prodpkg.task_store import is_bool_safe_int\n"
    "LAUNCH_OK = is_bool_safe_int(5)\n"
)


def _seed_nf423_coherent_dependency(repo: Path) -> None:
    """Commit the HEAD generation, then dirty the tree to current canonical.

    HEAD is internally consistent (the launcher does not yet import the
    dependency symbol), so a HEAD-only control resolves cleanly. The current
    canonical working tree adds ``is_bool_safe_int`` to the dependency and the
    launcher's import of it; only a stale-HEAD/current mix fails.
    """
    package = repo / "src/prodpkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"")
    (package / "task_store.py").write_text(_TASK_STORE_HEAD, encoding="utf-8")
    (package / "process_launcher.py").write_text(_LAUNCHER_HEAD, encoding="utf-8")
    (repo / "probe_launcher.py").write_text(
        "from prodpkg.process_launcher import LAUNCH_OK\n"
        "print('LAUNCH_OK', LAUNCH_OK)\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\npythonpath = ['src']\n", encoding="utf-8"
    )
    assert (
        _git(
            repo,
            "add",
            "src/prodpkg",
            "probe_launcher.py",
            "pyproject.toml",
        ).returncode
        == 0
    )
    assert _git(repo, "commit", "-qm", "nf423 head generation").returncode == 0
    # Advance the working tree to the current canonical generation, uncommitted
    # so the detached HEAD checkout differs from the live parent tree.
    (package / "task_store.py").write_text(_TASK_STORE_CURRENT, encoding="utf-8")
    (package / "process_launcher.py").write_text(
        _LAUNCHER_CURRENT, encoding="utf-8"
    )


def test_sparse_dependency_closure_is_one_coherent_current_canonical_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    _seed_nf423_coherent_dependency(repo)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "nf423-worktrees"),
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "nf423-coherent-dependency",
        {
            # task_store.py is intentionally NOT writable: it is a read-only
            # imported dependency, never a candidate change.
            "allowed_writes": ["src/prodpkg/process_launcher.py"],
            "read_first": [
                "src/prodpkg/process_launcher.py",
                "probe_launcher.py",
            ],
            "validation": ["PYTHONPATH=src python3 probe_launcher.py"],
        },
        "glm_vscode_lm",
    )
    try:
        dependency = workspace.path / "src/prodpkg/task_store.py"
        # The dependency is materialized from the current canonical tree, not
        # the stale detached-HEAD copy -- one coherent generation.
        assert dependency.is_file()
        assert dependency.read_text(encoding="utf-8") == _TASK_STORE_CURRENT
        assert dependency.read_text(encoding="utf-8") != _TASK_STORE_HEAD
        assert (
            workspace.path / "src/prodpkg/process_launcher.py"
        ).read_text(encoding="utf-8") == _LAUNCHER_CURRENT

        # The unmodified dependency stays read-only: outside allowed_writes and
        # absent from the candidate delta, so it can never be promoted.
        assert "src/prodpkg/task_store.py" not in workspace.allowed_writes
        assert worker_workspace.changed_paths(workspace) == []

        result, = worker_workspace.run_validations(
            workspace,
            ["PYTHONPATH=src python3 probe_launcher.py"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="glm_vscode_lm",
        )
        assert result["returncode"] == 0, result
        assert "LAUNCH_OK True" in result["stdout_head"]

        # A candidate edit to the allowed file is the sole promotable change;
        # the imported dependency never appears in the candidate delta.
        with (
            workspace.path / "src/prodpkg/process_launcher.py"
        ).open("a", encoding="utf-8") as stream:
            stream.write("NF423_SENTINEL = 'candidate'\n")
        assert worker_workspace.changed_paths(workspace) == [
            "src/prodpkg/process_launcher.py"
        ]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# ── NF430 request-owned worker temp authority ──────────────────────────────


def test_worker_temp_environment_provisions_request_owned_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    request_id = "req-nf430-workspace"
    env = worker_workspace.worker_temp_environment(repo, request_id)
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"]
    tmp = Path(env["TMPDIR"])
    assert tmp.name == "tmp"
    assert tmp.parent.name == request_id
    parts = tmp.parts
    assert ".aiworkhub" in parts and "temp" in parts and "worker" in parts
    # Outside the candidate worktree by construction.
    assert "worktree" not in parts
    assert tmp.is_dir()
    assert stat.S_IMODE(tmp.stat().st_mode) == 0o700
    # provision=False resolves the same authority path without creating it.
    other = tmp_path / "repo2"
    other.mkdir()
    lazy = worker_workspace.worker_temp_environment(other, "req-x", provision=False)
    assert Path(lazy["TMPDIR"]).name == "tmp"
    assert not Path(lazy["TMPDIR"]).exists()


def test_worker_temp_dispose_removes_only_the_named_request_root(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    worker_workspace.worker_temp_environment(repo, "req-keep")
    worker_workspace.worker_temp_environment(repo, "req-drop")
    keep = worker_workspace.worker_temp_root(repo, "req-keep")
    drop = worker_workspace.worker_temp_root(repo, "req-drop")
    assert keep.is_dir() and drop.is_dir()
    assert worker_workspace.dispose_worker_temp(repo, "req-drop") is True
    assert not drop.exists()
    # Collision-free: disposing one request never touches a sibling request.
    assert keep.is_dir()
    # An absent/foreign root is a fail-closed no-op, never an error.
    assert worker_workspace.dispose_worker_temp(repo, "req-never") is False


def test_sandbox_argv_landlock_authorizes_only_a_provisioned_worker_temp(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    request_id = "req-nf430-sandbox"
    workspace = worker_workspace.WorkerWorkspace(
        request_id=request_id,
        repo=repo,
        path=worktree,
        home=_preprovisioned_private_home(tmp_path),
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    before = worker_workspace.sandbox_argv(
        workspace, "validation", ["/bin/true"], backend="landlock"
    )
    assert "--worker-temp" not in before
    root = worker_workspace.provision_worker_temp(repo, request_id).root
    after = worker_workspace.sandbox_argv(
        workspace, "validation", ["/bin/true"], backend="landlock"
    )
    assert "--worker-temp" in after
    granted = Path(after[after.index("--worker-temp") + 1])
    assert granted.resolve() == root.resolve()
    # Only the exact temp root is authorized -- never the repository itself, so
    # canonical/worktree writes are not widened.
    assert str(repo) not in after


def test_cleanup_workspace_disposes_the_request_worker_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "req-nf430-cleanup")
    worker_workspace.worker_temp_environment(repo, workspace.request_id)
    root = worker_workspace.worker_temp_root(repo, workspace.request_id)
    assert root.is_dir()
    worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)
    # The temp authority shares the workspace lifecycle: gone with the worktree.
    assert not root.exists()


def test_in_tree_worker_tmp_is_git_visible_and_scope_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    """NF430: a candidate that smuggles pytest temp into the worktree at
    ``tests/.aiworkhub_worker_tmp/**`` is never hidden -- it stays visible to
    Git and enforce_scope rejects it, with no ignore-prefix or .gitignore
    bypass."""
    workspace = _workspace(monkeypatch, tmp_path, repo, "req-nf430-intree")
    try:
        smuggled = (
            workspace.path
            / "tests"
            / ".aiworkhub_worker_tmp"
            / "pytest-of-worker"
            / "junk.txt"
        )
        smuggled.parent.mkdir(parents=True)
        smuggled.write_text("in-tree temp must not hide from git\n", encoding="utf-8")
        assert "tests/.aiworkhub_worker_tmp/pytest-of-worker/junk.txt" in (
            worker_workspace.changed_paths(workspace)
        )
        with pytest.raises(worker_workspace.WorkspaceError, match="scope_violation"):
            worker_workspace.enforce_scope(workspace)
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# ── NF430 metadata broker: worker-temp authorization + denial telemetry ─────


def test_metadata_broker_denial_reason_is_bounded_and_path_free() -> None:
    """NF430: a broker denial retains its stable reason code with no path/secret.

    The blanket ``except`` no longer collapses every cause into an opaque
    generic EPERM; the reason feeds bounded ``stderr`` telemetry, so it must be
    the ``WorkspaceError`` prefix (never the ``:detail`` half that embeds the
    target path) and an errno name for ``OSError``.
    """
    reason = worker_workspace._metadata_broker_denial_reason(
        worker_workspace.WorkspaceError(
            "metadata_broker_outside_scratch:/canonical/secret/config.lock"
        )
    )
    assert reason == "metadata_broker_outside_scratch"
    assert "secret" not in reason and "/" not in reason
    assert (
        worker_workspace._metadata_broker_denial_reason(
            OSError(1, "Operation not permitted")
        )
        == "oserror_EPERM"
    )


@pytest.mark.skipif(os.name == "nt", reason="openat2 target acquisition is POSIX")
@pytest.mark.skipif(
    not worker_workspace._openat2_available(),
    reason="openat2(2) unavailable on this kernel",
)
def test_metadata_broker_verify_target_any_authorizes_both_request_roots(
    tmp_path: Path,
) -> None:
    """NF430: a scratch-owned ``config.lock`` beneath EITHER the exec scratch or
    the request-owned worker temp authority is acquired, while every malicious
    case (outside all roots, symlinked component) is denied -- authority is
    never widened to the repository or an arbitrary path."""
    exec_scratch = (tmp_path / "exec").resolve()
    exec_scratch.mkdir()
    worker_temp = (tmp_path / "worker" / "req" / "tmp").resolve()
    worker_temp.mkdir(parents=True)
    lock = worker_temp / "pytest-of-worker" / "nested" / ".git" / "config.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("[core]\n", encoding="utf-8")

    specs = [
        worker_workspace._open_broker_scratch_root(exec_scratch),
        worker_workspace._open_broker_scratch_root(worker_temp),
    ]
    try:
        # Beneath the worker temp authority (the second root) -> acquired.
        fd, mutate = worker_workspace._metadata_broker_verify_target_any(
            str(lock.resolve()), specs
        )
        assert fd >= 0
        assert mutate is True
        os.close(fd)
        # A target beneath neither authorized root is denied, not widened.
        outside = tmp_path / "outside.lock"
        outside.write_text("x", encoding="utf-8")
        with pytest.raises(
            worker_workspace.WorkspaceError, match="metadata_broker_outside_scratch"
        ):
            worker_workspace._metadata_broker_verify_target_any(
                str(outside.resolve()), specs
            )
        # A symlinked component beneath a root is a beneath-but-invalid case:
        # the kernel (RESOLVE_NO_SYMLINKS) denies it, never falls through.
        evil = worker_temp / "evil"
        os.symlink("/etc", evil)
        with pytest.raises(
            worker_workspace.WorkspaceError, match="metadata_broker_openat2_failed"
        ):
            worker_workspace._metadata_broker_verify_target_any(
                str(evil / "passwd"), specs
            )
    finally:
        for spec_fd, _root in specs:
            os.close(spec_fd)


# ── NF448: bounded PPID ancestry for nested setsid descendants ─────────────


def _read_pipe_line(read_fd: int, timeout: float = 5.0) -> bytes:
    selector = selectors.DefaultSelector()
    selector.register(read_fd, selectors.EVENT_READ)
    try:
        deadline = time.monotonic() + timeout
        data = b""
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if not selector.select(timeout=max(0.0, remaining)):
                break
            chunk = os.read(read_fd, 256)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        return data
    finally:
        selector.close()


def _kill_and_reap(pid: int, *, is_direct_child: bool) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    if is_direct_child:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


@pytest.mark.skipif(os.name == "nt", reason="PPID ancestry walk is POSIX /proc")
def test_authenticate_pid_accepts_nested_setsid_descendant_via_ppid_ancestry(
) -> None:
    """NF-2026-00448: a nested descendant that calls ``setsid`` (e.g. a
    sub-subprocess ``git``/``pytest`` spawns) gets a fresh ``pgid`` equal to
    its own pid, so the pgid fast path no longer matches it. It must still be
    accepted because its live PPID chain -- leaf -> mid -> broker child --
    reaches the exact broker child pid."""
    read_fd, write_fd = os.pipe()
    top_pid = os.fork()
    if top_pid == 0:
        os.close(read_fd)
        os.setsid()  # emulate the broker child's own session leadership
        inner_read, inner_write = os.pipe()
        mid_pid = os.fork()
        if mid_pid == 0:
            os.close(inner_read)
            leaf_read, leaf_write = os.pipe()
            leaf_pid = os.fork()
            if leaf_pid == 0:
                os.close(leaf_read)
                os.setsid()  # nested descendant starts its own session
                os.write(leaf_write, b"ready\n")
                os.close(leaf_write)
                time.sleep(30)
                os._exit(0)
            os.close(leaf_write)
            os.read(leaf_read, 6)
            os.close(leaf_read)
            os.write(inner_write, f"{leaf_pid}\n".encode())
            os.close(inner_write)
            time.sleep(30)
            os._exit(0)
        os.close(inner_write)
        payload = _read_pipe_line(inner_read)
        os.close(inner_read)
        os.write(write_fd, f"{mid_pid} {payload.decode().strip()}\n".encode())
        os.close(write_fd)
        time.sleep(30)
        os._exit(0)
    os.close(write_fd)
    try:
        payload = _read_pipe_line(read_fd)
        assert payload, "child process tree failed to report pids in time"
        mid_pid_str, leaf_pid_str = payload.decode().strip().split()
        mid_pid, leaf_pid = int(mid_pid_str), int(leaf_pid_str)

        # The pgid fast path still covers the direct, non-setsid descendant.
        assert worker_workspace._metadata_broker_process_pgid(mid_pid) == top_pid
        worker_workspace._metadata_broker_authenticate_pid(mid_pid, top_pid)

        # The nested setsid descendant breaks the pgid fast path...
        assert worker_workspace._metadata_broker_process_pgid(leaf_pid) != top_pid
        # ...but its bounded, live PPID ancestry still reaches the broker child.
        worker_workspace._metadata_broker_authenticate_pid(leaf_pid, top_pid)
    finally:
        os.close(read_fd)
        for pid, is_direct_child in ((top_pid, True),):
            _kill_and_reap(pid, is_direct_child=is_direct_child)


def _spawn_setsid_leaf() -> int:
    """Fork a direct child that starts its own session and reports ready."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        os.setsid()
        os.write(write_fd, b"ready\n")
        os.close(write_fd)
        time.sleep(30)
        os._exit(0)
    os.close(write_fd)
    try:
        assert _read_pipe_line(read_fd), "leaf process failed to start in time"
    finally:
        os.close(read_fd)
    return pid


@pytest.mark.skipif(os.name == "nt", reason="PPID ancestry walk is POSIX /proc")
def test_authenticate_pid_rejects_unrelated_live_foreign_process() -> None:
    """A live process that merely happens to be alive (and owned by the same
    uid) but whose ancestry never passes through the broker child is denied
    -- liveness/ownership alone must never be mistaken for ancestry. Both
    pids are independent, direct children of the test process (siblings),
    so neither is on the other's PPID chain."""
    unrelated_pid = _spawn_setsid_leaf()
    sibling_broker_child_pid = _spawn_setsid_leaf()
    try:
        with pytest.raises(
            worker_workspace.WorkspaceError, match="metadata_broker_foreign_pid"
        ):
            worker_workspace._metadata_broker_authenticate_pid(
                unrelated_pid, sibling_broker_child_pid
            )
    finally:
        _kill_and_reap(unrelated_pid, is_direct_child=True)
        _kill_and_reap(sibling_broker_child_pid, is_direct_child=True)


def test_ppid_ancestry_reaches_accepts_multi_hop_live_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = {300: 200, 200: 100}
    monkeypatch.setattr(
        worker_workspace, "_metadata_broker_process_ppid", lambda pid: graph[pid]
    )
    assert worker_workspace._metadata_broker_ppid_ancestry_reaches(300, 100) is True


def test_ppid_ancestry_reaches_rejects_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = {100: 200, 200: 100}
    monkeypatch.setattr(
        worker_workspace, "_metadata_broker_process_ppid", lambda pid: graph[pid]
    )
    assert worker_workspace._metadata_broker_ppid_ancestry_reaches(100, 999) is False


def test_ppid_ancestry_reaches_rejects_reparented_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chain that bottoms out at pid 1 (init) never reached the broker
    child -- an intermediate ancestor died and the descendant was reparented,
    so it must be rejected rather than trusted."""
    graph = {500: 400, 400: 1}
    monkeypatch.setattr(
        worker_workspace, "_metadata_broker_process_ppid", lambda pid: graph[pid]
    )
    assert worker_workspace._metadata_broker_ppid_ancestry_reaches(500, 999) is False


def test_ppid_ancestry_reaches_rejects_dead_or_malformed_proc_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_ppid(pid: int) -> int:
        raise worker_workspace.WorkspaceError(f"metadata_broker_pid_unavailable:{pid}")

    monkeypatch.setattr(worker_workspace, "_metadata_broker_process_ppid", fake_ppid)
    assert worker_workspace._metadata_broker_ppid_ancestry_reaches(700, 999) is False


def test_ppid_ancestry_reaches_rejects_chain_exceeding_bounded_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chain that WOULD reach the broker child, but only beyond the bounded
    walk depth, is rejected fail-closed rather than walked indefinitely."""
    depth_limit = worker_workspace._METADATA_BROKER_MAX_ANCESTRY_DEPTH
    child_pid = 2
    graph: dict[int, int] = {}
    base = 100000
    chain = [base + offset for offset in range(depth_limit + 3)]
    for current, parent in zip(chain, chain[1:]):
        graph[current] = parent
    graph[chain[-1]] = child_pid  # only reachable past the bounded depth
    monkeypatch.setattr(
        worker_workspace, "_metadata_broker_process_ppid", lambda pid: graph[pid]
    )
    assert (
        worker_workspace._metadata_broker_ppid_ancestry_reaches(chain[0], child_pid)
        is False
    )


def test_process_ppid_and_pgid_reject_malformed_proc_stat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    real_open = open
    fake_pid = 999999919

    def fake_open(path, *args, **kwargs):
        if path == f"/proc/{fake_pid}/stat":
            return io.BytesIO(b"garbage-with-no-closing-paren")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    with pytest.raises(
        worker_workspace.WorkspaceError, match="metadata_broker_stat_malformed"
    ):
        worker_workspace._metadata_broker_process_ppid(fake_pid)
    with pytest.raises(
        worker_workspace.WorkspaceError, match="metadata_broker_stat_malformed"
    ):
        worker_workspace._metadata_broker_process_pgid(fake_pid)


# ── NF-2026-00448: hardlinked regular file chmod permission-bit no-op ──────


def _hardlink_or_skip(target: Path, link_path: Path) -> None:
    if not hasattr(os, "link"):
        pytest.skip("os.link not available")
    try:
        os.link(target, link_path)
    except (OSError, PermissionError) as exc:
        pytest.skip(f"os.link denied in this sandbox: {exc}")


@pytest.mark.skipif(os.name == "nt", reason="hardlink st_nlink guard is POSIX")
def test_verify_fd_accepts_exact_mode_noop_on_hardlinked_regular_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "config.lock"
    target.write_text("[core]\n", encoding="utf-8")
    target.chmod(0o644)
    _hardlink_or_skip(target, tmp_path / "config.lock.link")
    fd = os.open(target, os.O_RDONLY)
    try:
        current_mode = stat.S_IMODE(os.fstat(fd).st_mode)
        mutate = worker_workspace._metadata_broker_verify_fd(
            fd, str(target), current_mode
        )
        assert mutate is True
        # Authentication admits only the exact mode; the broker must still
        # execute the real fchmod branch on this descriptor.
        assert stat.S_IMODE(os.fstat(fd).st_mode) == current_mode
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name == "nt", reason="hardlink st_nlink guard is POSIX")
def test_verify_fd_denies_any_mode_change_on_hardlinked_regular_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "config.lock"
    target.write_text("[core]\n", encoding="utf-8")
    target.chmod(0o644)
    _hardlink_or_skip(target, tmp_path / "config.lock.link")
    fd = os.open(target, os.O_RDONLY)
    try:
        with pytest.raises(
            worker_workspace.WorkspaceError, match="metadata_broker_hardlink_forbidden"
        ):
            worker_workspace._metadata_broker_verify_fd(fd, str(target), 0o600)
        # An unknown requested mode (no mode context available) stays denied,
        # exactly as before this fix -- never a blanket hardlink allowance.
        with pytest.raises(
            worker_workspace.WorkspaceError, match="metadata_broker_hardlink_forbidden"
        ):
            worker_workspace._metadata_broker_verify_fd(fd, str(target))
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name == "nt", reason="hardlink st_nlink guard is POSIX")
def test_verify_fd_hardlink_noop_still_enforces_foreign_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.lock"
    target.write_text("[core]\n", encoding="utf-8")
    target.chmod(0o644)
    _hardlink_or_skip(target, tmp_path / "config.lock.link")
    fd = os.open(target, os.O_RDONLY)
    try:
        current_mode = stat.S_IMODE(os.fstat(fd).st_mode)
        original_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: original_uid + 1)
        with pytest.raises(
            worker_workspace.WorkspaceError, match="metadata_broker_foreign_owner"
        ):
            worker_workspace._metadata_broker_verify_fd(fd, str(target), current_mode)
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name == "nt", reason="openat2 target acquisition is POSIX")
@pytest.mark.skipif(
    not worker_workspace._openat2_available(),
    reason="openat2(2) unavailable on this kernel",
)
def test_verify_target_any_hardlink_same_mode_noop_accepted_different_mode_denied(
    tmp_path: Path,
) -> None:
    scratch = (tmp_path / "scratch").resolve()
    scratch.mkdir()
    target = scratch / "config.lock"
    target.write_text("[core]\n", encoding="utf-8")
    target.chmod(0o644)
    _hardlink_or_skip(target, scratch / "config.lock.link")
    specs = [worker_workspace._open_broker_scratch_root(scratch)]
    try:
        fd, mutate = worker_workspace._metadata_broker_verify_target_any(
            str(target.resolve()), specs, 0o644
        )
        try:
            assert fd >= 0
            assert mutate is True
        finally:
            os.close(fd)
        with pytest.raises(
            worker_workspace.WorkspaceError, match="metadata_broker_hardlink_forbidden"
        ):
            worker_workspace._metadata_broker_verify_target_any(
                str(target.resolve()), specs, 0o600
            )
        # root-beneath/symlink/owner/inode checks remain fully enforced.
        outside = tmp_path / "outside.lock"
        outside.write_text("x", encoding="utf-8")
        with pytest.raises(
            worker_workspace.WorkspaceError, match="metadata_broker_outside_scratch"
        ):
            worker_workspace._metadata_broker_verify_target_any(
                str(outside.resolve()), specs, 0o644
            )
    finally:
        for spec_fd, _root in specs:
            os.close(spec_fd)


@pytest.mark.skipif(os.name == "nt", reason="fchmod descriptor race is POSIX")
@pytest.mark.skipif(
    not worker_workspace._openat2_available(),
    reason="openat2(2) unavailable on this kernel",
)
def test_fchmod_hardlink_noop_unlink_race_uses_authenticated_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scratch = (tmp_path / "scratch").resolve()
    scratch.mkdir()
    target = scratch / "config.lock"
    sibling = scratch / "config.lock.link"
    target.write_text("original\n", encoding="utf-8")
    target.chmod(0o644)
    _hardlink_or_skip(target, sibling)
    child_fd = os.open(target, os.O_RDONLY)
    original_identity = os.fstat(child_fd).st_dev, os.fstat(child_fd).st_ino
    specs = [worker_workspace._open_broker_scratch_root(scratch)]
    request = worker_workspace._SeccompNotif()
    request.id = 448
    request.pid = os.getpid()
    request.data.nr = 448
    request.data.args[0] = child_fd
    request.data.args[1] = 0o644
    checks = 0
    real_fchmod = os.fchmod
    fchmod_identities: list[tuple[int, int]] = []

    def check_notification(_library, _listener_fd, _notification_id):
        nonlocal checks
        checks += 1
        if checks == 2:
            target.unlink()
            sibling.unlink()
            target.write_text("replacement\n", encoding="utf-8")
            target.chmod(0o600)

    def observed_fchmod(fd: int, mode: int) -> None:
        info = os.fstat(fd)
        fchmod_identities.append((info.st_dev, info.st_ino))
        real_fchmod(fd, mode)

    monkeypatch.setattr(
        worker_workspace,
        "_metadata_broker_syscall_names",
        lambda _library: {448: "fchmod"},
    )
    monkeypatch.setattr(
        worker_workspace, "_metadata_broker_check_notification", check_notification
    )
    monkeypatch.setattr(os, "fchmod", observed_fchmod)
    try:
        worker_workspace._metadata_broker_apply(
            object(), -1, request, os.getpid(), specs
        )
        assert checks == 2
        assert fchmod_identities == [original_identity]
        assert target.read_text(encoding="utf-8") == "replacement\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(os.fstat(child_fd).st_mode) == 0o644
    finally:
        os.close(child_fd)
        for spec_fd, _root in specs:
            os.close(spec_fd)


@pytest.mark.skipif(os.name == "nt", reason="openat2 target acquisition is POSIX")
@pytest.mark.skipif(
    not worker_workspace._openat2_available(),
    reason="openat2(2) unavailable on this kernel",
)
def test_open_broker_scratch_root_rejects_symlinked_root(tmp_path: Path) -> None:
    """NF430: a symlinked authorized root fails closed rather than becoming
    authority for its target."""
    real = (tmp_path / "real").resolve()
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    with pytest.raises(worker_workspace.WorkspaceError):
        worker_workspace._open_broker_scratch_root(link)


def test_pytest_validation_seeds_transitive_local_test_import_closure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """Request b58052793245430ea57f689fa516b4e4: pytest collection fails when a
    declared test file imports a sibling test helper that is absent from the
    sparse workspace. Extend seeding to include transitive imports for test
    files extracted from pytest validation commands, so helper modules are
    provisioned without broad copying."""
    _commit_validation_worker_package(repo)
    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "_taskdb_compat.py").write_text(
        "SCHEMA_VERSION = 'v1'\n", encoding="utf-8"
    )
    (tests_dir / "test_learning_commit_store.py").write_text(
        "from _taskdb_compat import SCHEMA_VERSION\n\n"
        "def test_schema():\n"
        "    assert SCHEMA_VERSION == 'v1'\n",
        encoding="utf-8",
    )
    (tests_dir / "test_unused.py").write_text(
        "def test_unused():\n"
        "    pass\n",
        encoding="utf-8",
    )
    assert _git(repo, "add", "tests").returncode == 0
    assert _git(repo, "commit", "-qm", "test closure fixture").returncode == 0
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "pytest-closure-worktrees"),
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "pytest-test-closure",
        {
            "allowed_writes": ["src/aiworkhub/new_candidate_module.py"],
            "read_first": ["src/aiworkhub/worker_workspace.py"],
            "validation": ["python3 -m pytest -q tests/test_learning_commit_store.py"],
        },
        "glm_vscode_lm",
    )
    try:
        assert (workspace.path / "tests/test_learning_commit_store.py").is_file()
        assert (workspace.path / "tests/_taskdb_compat.py").is_file()
        assert not (workspace.path / "tests/test_unused.py").exists()
        (workspace.path / "src/aiworkhub/new_candidate_module.py").write_text(
            "VALUE = 'candidate-new-module'\n", encoding="utf-8"
        )
        result, = worker_workspace.run_validations(
            workspace,
            ["python3 -m pytest -q tests/test_learning_commit_store.py"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="glm_vscode_lm",
        )
        assert result["returncode"] == 0
        assert "1 passed" in result["stdout_head"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_pytest_validation_with_direct_pytest_command_seeds_test_closure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """Direct pytest invocation (without python -m) also seeds test import closure."""
    _commit_validation_worker_package(repo)
    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "_helper.py").write_text("HELPER = True\n", encoding="utf-8")
    (tests_dir / "test_with_helper.py").write_text(
        "from _helper import HELPER\n\n"
        "def test_helper():\n"
        "    assert HELPER\n",
        encoding="utf-8",
    )
    assert _git(repo, "add", "tests").returncode == 0
    assert _git(repo, "commit", "-qm", "pytest direct test").returncode == 0
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "pytest-direct-worktrees"),
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "pytest-direct-closure",
        {
            "allowed_writes": ["src/aiworkhub/new_candidate_module.py"],
            "read_first": ["src/aiworkhub/worker_workspace.py"],
            "validation": ["pytest tests/test_with_helper.py"],
        },
        "glm_vscode_lm",
    )
    try:
        assert (workspace.path / "tests/test_with_helper.py").is_file()
        assert (workspace.path / "tests/_helper.py").is_file()
        (workspace.path / "src/aiworkhub/new_candidate_module.py").write_text(
            "VALUE = 'ok'\n", encoding="utf-8"
        )
        result, = worker_workspace.run_validations(
            workspace,
            ["pytest tests/test_with_helper.py"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="glm_vscode_lm",
        )
        assert result["returncode"] == 0
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_pytest_validation_seeds_closure_with_pytest_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """Test file closure seeding works with pytest flags and node selectors."""
    _commit_validation_worker_package(repo)
    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "_support.py").write_text("VALUE = 'support'\n", encoding="utf-8")
    (tests_dir / "test_flags.py").write_text(
        "from _support import VALUE\n\n"
        "def test_one():\n"
        "    assert VALUE == 'support'\n"
        "def test_two():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    assert _git(repo, "add", "tests").returncode == 0
    assert _git(repo, "commit", "-qm", "pytest flags test").returncode == 0
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "pytest-flags-worktrees"),
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "pytest-flags-closure",
        {
            "allowed_writes": ["src/aiworkhub/new_candidate_module.py"],
            "read_first": ["src/aiworkhub/worker_workspace.py"],
            "validation": ["python3 -m pytest -v tests/test_flags.py::test_one"],
        },
        "glm_vscode_lm",
    )
    try:
        assert (workspace.path / "tests/test_flags.py").is_file()
        assert (workspace.path / "tests/_support.py").is_file()
        (workspace.path / "src/aiworkhub/new_candidate_module.py").write_text(
            "VALUE = 'ok'\n", encoding="utf-8"
        )
        result, = worker_workspace.run_validations(
            workspace,
            ["python3 -m pytest -v tests/test_flags.py::test_one"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="glm_vscode_lm",
        )
        assert result["returncode"] == 0
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_pytest_validation_rejects_test_paths_outside_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    """Pytest validation must reject test paths that escape the repository."""
    _commit_validation_worker_package(repo)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "pytest-escape-worktrees"),
    )
    with pytest.raises(
        worker_workspace.WorkspaceError, match="unsafe_repo_path"
    ):
        worker_workspace.create_workspace(
            repo,
            "pytest-escape",
            {
                "allowed_writes": ["src/aiworkhub/new_candidate_module.py"],
                "read_first": ["tests/test.py"],
                "validation": ["python3 -m pytest ../outside/test.py"],
            },
            "glm_vscode_lm",
        )
