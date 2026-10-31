"""NF-2026-01889: a live-seeded, edited .gitignore must not read as modified."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import worker_workspace, worker_workspace_ignore_seed

_COMMITTED_IGNORE = b"__pycache__/\n.pytest_cache/\n"
_LIVE_IGNORE = _COMMITTED_IGNORE + b"dist/\n"


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


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "parent"
    root.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "tests@example.invalid"),
        ("config", "user.name", "Task MCP Tests"),
        ("config", "core.autocrlf", "false"),
    ):
        assert _git(root, *args).returncode == 0
    (root / "read").mkdir()
    (root / "out").mkdir()
    (root / "read" / "input.txt").write_bytes(b"input-v1\n")
    (root / "out" / "result.txt").write_bytes(b"result-v1\n")
    (root / ".gitignore").write_bytes(_COMMITTED_IGNORE)
    assert _git(root, "add", ".gitignore", "read/input.txt", "out/result.txt").returncode == 0
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    return root


@pytest.fixture()
def worktree(tmp_path: Path, repo: Path) -> Path:
    path = tmp_path / "linked"
    added = _git(repo, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    assert added.returncode == 0, added.stderr
    return path


def test_modified_seeded_gitignore_is_hidden_and_keeps_live_bytes(worktree: Path) -> None:
    (worktree / ".gitignore").write_bytes(_LIVE_IGNORE)
    assert _git(worktree, "status", "--porcelain").stdout.splitlines() == [" M .gitignore"]

    marked, notes = worker_workspace_ignore_seed.hide_live_gitignore_seeds(
        worktree, [".gitignore", "read/input.txt"], worker_workspace._run
    )

    assert marked == (".gitignore",)
    assert notes == ()
    assert (worktree / ".gitignore").read_bytes() == _LIVE_IGNORE
    assert _git(worktree, "status", "--porcelain").stdout == ""


def test_unchanged_seeded_gitignore_is_not_marked(worktree: Path) -> None:
    marked, notes = worker_workspace_ignore_seed.hide_live_gitignore_seeds(
        worktree, [".gitignore"], worker_workspace._run
    )

    assert (marked, notes) == ((), ())
    flags = _git(worktree, "ls-files", "-v", "--", ".gitignore").stdout
    assert flags.startswith("H ")


def test_git_failure_is_a_note_and_never_raises(worktree: Path) -> None:
    (worktree / ".gitignore").write_bytes(_LIVE_IGNORE)

    def failing_run(argv: list[str], **kwargs: Any) -> Any:
        raise OSError("git unavailable")

    marked, notes = worker_workspace_ignore_seed.hide_live_gitignore_seeds(
        worktree, [".gitignore"], failing_run
    )

    assert marked == ()
    assert notes == ("gitignore_seed_hide_failed:OSError",)


def test_create_workspace_hides_the_live_gitignore_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    (repo / ".gitignore").write_bytes(_LIVE_IGNORE)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    real = worker_workspace_ignore_seed.hide_live_gitignore_seeds
    calls: list[tuple[Path, list[str], tuple[str, ...]]] = []

    def spy(worktree: Path, seeded: Any, run: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
        seeded = list(seeded)
        result = real(worktree, seeded, run)
        calls.append((worktree, seeded, result[0]))
        return result

    monkeypatch.setattr(worker_workspace_ignore_seed, "hide_live_gitignore_seeds", spy)
    workspace = worker_workspace.create_workspace(
        repo,
        "req-nf01889-seed",
        {"allowed_writes": ["out/result.txt"], "read_first": ["read/input.txt"]},
        "validation",
    )
    try:
        assert len(calls) == 1
        worktree, seeded, marked = calls[0]
        assert worktree == workspace.path
        assert ".gitignore" in seeded
        assert marked == (".gitignore",)
        assert (workspace.path / ".gitignore").read_bytes() == _LIVE_IGNORE
        status = _git(workspace.path, "status", "--porcelain")
        assert status.returncode == 0, status.stderr
        assert status.stdout == ""
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)
