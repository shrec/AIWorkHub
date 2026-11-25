"""NF-2026-02428: refuse a repo path that is not its own git toplevel."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aiworkhub import worker_materialize
from aiworkhub import worker_workspace as ww


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, env=ww._git_environment(), capture_output=True, text=True,
        check=True, timeout=60,
    )


def _worktree_count(repo: Path) -> int:
    listing = _git(repo, "worktree", "list", "--porcelain").stdout
    return sum(1 for line in listing.splitlines() if line.startswith("worktree "))


@pytest.fixture
def nested(tmp_path: Path) -> tuple[Path, Path]:
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q")
    (outer / "README.md").write_text("outer\n", encoding="utf-8")
    _git(outer, "add", "README.md")
    _git(outer, "commit", "-q", "-m", "init")
    inner = outer / "inner"
    inner.mkdir()
    return outer, inner


def test_create_workspace_refuses_non_toplevel(nested: tuple[Path, Path]) -> None:
    outer, inner = nested
    card = {"allowed_writes": [], "required_outputs": []}
    with pytest.raises(ww.WorkspaceError, match="^repo_not_git_toplevel$"):
        ww.create_workspace(inner, "nf02428-guard", card, "x")
    assert _worktree_count(outer) == 1
    assert sorted(p.name for p in inner.iterdir()) == []


def test_finalization_probe_blocks_non_toplevel(nested: tuple[Path, Path]) -> None:
    outer, inner = nested
    result = ww.finalization_preflight_probe(inner, "x")
    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["reason"] == "repo_not_git_toplevel"
    assert _worktree_count(outer) == 1


def test_empty_dot_git_directory_is_refused(nested: tuple[Path, Path]) -> None:
    # An empty .git dir is skipped by git discovery, which climbs to outer.
    outer, inner = nested
    (inner / ".git").mkdir()
    with pytest.raises(ww.WorkspaceError, match="^repo_not_git_toplevel$"):
        worker_materialize.assert_git_toplevel(
            inner.resolve(), ww._run, error=ww.WorkspaceError
        )
    card = {"allowed_writes": [], "required_outputs": []}
    with pytest.raises(ww.WorkspaceError, match="^repo_not_git_toplevel$"):
        ww.create_workspace(inner, "nf02428-emptygit", card, "x")
    assert _worktree_count(outer) == 1


def test_real_repo_and_linked_worktree_pass(
    nested: tuple[Path, Path], tmp_path: Path
) -> None:
    outer, _inner = nested
    linked = tmp_path / "linked"
    _git(outer, "worktree", "add", "-q", "--detach", str(linked))
    assert (linked / ".git").is_file()
    for repo in (outer, linked):
        worker_materialize.assert_git_toplevel(
            repo.resolve(), ww._run, error=ww.WorkspaceError
        )


def test_git_environment_keeps_ceiling_drops_other_git_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere.git"))
    env = ww._git_environment()
    assert env["GIT_CEILING_DIRECTORIES"] == str(tmp_path)
    assert "GIT_DIR" not in env
