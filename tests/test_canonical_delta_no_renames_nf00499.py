"""NF-2026-00499: a staged rename must list both the source and destination paths."""

from __future__ import annotations

import subprocess
from pathlib import Path

from aiworkhub import worker_workspace


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=repo, check=True)


def test_staged_rename_lists_both_old_and_new_paths(tmp_path: Path) -> None:
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (repo / "old").mkdir()
    (repo / "old" / "name.txt").write_text("content\n", encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    (repo / "new").mkdir()
    _git(repo, "mv", "old/name.txt", "new/name.txt")

    paths = worker_workspace._canonical_worktree_delta_paths(repo)

    assert "old/name.txt" in paths
    assert "new/name.txt" in paths
