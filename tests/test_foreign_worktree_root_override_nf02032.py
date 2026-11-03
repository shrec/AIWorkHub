"""NF-2026-02032: a foreign-owned worktree/runtime root override is ignored.

Measured 2026-11-03: a reviewer launch inherited another registered
repository's AIWORKHUB_RUNTIME_ROOT / AIWORKHUB_WORKTREE_ROOT and ended
``launch_failed`` with ``worktree_root_foreign_repository``.  For an explicit
repository whose owner identity is determined to differ, the override is now
ignored with a warning and the repository's own runtime/worktrees root is used.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aiworkhub import task_store, worker_workspace

REPO_A_ID = "repo_02032000000000000000000000000a"
REPO_B_ID = "repo_02032000000000000000000000000b"
OUTPUT = "out/result.txt"
REQUEST_ID = "f2032cc0f2032cc0f2032cc0f2032cc0"
ENVS = [worker_workspace.WORKTREE_ROOT_ENV, worker_workspace.RUNTIME_ROOT_ENV]


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def _register(root: Path, repo_id: str) -> Path:
    hub = root / ".aiworkhub"
    hub.mkdir(parents=True, exist_ok=True)
    (hub / "project.json").write_text(json.dumps({"repo_id": repo_id}), encoding="utf-8")
    return root.resolve()


def _own_root(root: Path) -> Path:
    return (root / ".aiworkhub" / "runtime" / "worktrees").resolve(strict=False)


def _point_env_at(monkeypatch: pytest.MonkeyPatch, env: str, foreign: Path) -> None:
    runtime = foreign / ".aiworkhub" / "runtime"
    target = runtime / "worktrees" if env == worker_workspace.WORKTREE_ROOT_ENV else runtime
    monkeypatch.setenv(env, str(target))


def _assert_warned(caplog: pytest.LogCaptureFixture, env: str, foreign: Path) -> None:
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(worker_workspace.FOREIGN_WORKTREE_ROOT_IGNORED)
    ]
    assert any(f":env={env}:" in m and f":owner={foreign}:" in m for m in messages), messages


@pytest.fixture(autouse=True)
def _no_ambient_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*ENVS, "AIWORKHUB_REPO_ROOT", "AIWORKHUB_REPO"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("env", ENVS)
def test_foreign_override_is_ignored_for_an_explicit_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, env: str
) -> None:
    repo_a = _register(tmp_path / "a", REPO_A_ID)
    repo_b = _register(tmp_path / "b", REPO_B_ID)
    _point_env_at(monkeypatch, env, repo_b)

    assert worker_workspace.configured_worktree_root(repo_a) == _own_root(repo_a)
    _assert_warned(caplog, env, repo_b)
    assert not (repo_b / ".aiworkhub" / "runtime").exists()


@pytest.mark.parametrize("env", ENVS)
def test_create_workspace_succeeds_under_the_repos_own_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, env: str
) -> None:
    repo_a = (tmp_path / "a").resolve()
    repo_a.mkdir()
    _git(repo_a, "init", "-q")
    _git(repo_a, "config", "user.email", "tests@example.invalid")
    _git(repo_a, "config", "user.name", "NF-2026-02032 Tests")
    (repo_a / "out").mkdir()
    (repo_a / OUTPUT).write_bytes(b"base\n")
    _git(repo_a, "add", OUTPUT)
    _git(repo_a, "commit", "-qm", "baseline")
    task_store.initialize_repository(repo_a)  # tracks its own, distinct repo_id
    repo_b = _register(tmp_path / "b", REPO_B_ID)
    _point_env_at(monkeypatch, env, repo_b)

    workspace = worker_workspace.create_workspace(
        repo_a, REQUEST_ID, {"allowed_writes": [OUTPUT]}, "deterministic_validation"
    )

    assert workspace.path == _own_root(repo_a) / REQUEST_ID / "worktree"
    assert (workspace.path / OUTPUT).read_bytes() == b"base\n"
    _assert_warned(caplog, env, repo_b)
    assert not (repo_b / ".aiworkhub" / "runtime").exists()


@pytest.mark.parametrize("env", ENVS)
def test_own_root_symlinked_into_the_foreign_repo_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    repo_a = _register(tmp_path / "a", REPO_A_ID)
    repo_b = _register(tmp_path / "b", REPO_B_ID)
    foreign_root = repo_b / ".aiworkhub" / "runtime" / "worktrees"
    foreign_root.mkdir(parents=True)
    (repo_a / ".aiworkhub" / "runtime").mkdir()
    try:
        (repo_a / ".aiworkhub" / "runtime" / "worktrees").symlink_to(
            foreign_root, target_is_directory=True
        )
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    _point_env_at(monkeypatch, env, repo_b)

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match=f"^{worker_workspace.FOREIGN_WORKTREE_ROOT_REFUSAL}:",
    ):
        worker_workspace.configured_worktree_root(repo_a)
