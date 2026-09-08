"""Stateless Git repository builders shared by retention tests."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from aiworkhub import task_store

_AGED_NOW_OFFSET_DAYS = 31


def aged_now() -> float:
    """Return a clock value beyond the default 30-day retention threshold."""
    return time.time() + _AGED_NOW_OFFSET_DAYS * 86400


def git(cwd: Path, *args: str) -> None:
    """Run Git with the deterministic identity used by retention tests."""
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def repository(tmp_path: Path) -> dict[str, Path]:
    """Create the local bare remote, clone, base commit, and worktree root."""
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    base = tmp_path / "worktrees"
    base.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(tmp_path, "clone", str(remote), str(repo))
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-m", "base")
    git(repo, "push", "origin", "HEAD:refs/heads/main")
    git(repo, "fetch", "origin")
    assert task_store.initialize_repository(repo)["ok"]
    return {"repo": repo, "base": base}


def add_clean_worktree(repo: Path, base: Path, entry_id: str) -> Path:
    """Add a detached worktree with no local changes or unpushed commits."""
    entry = base / entry_id
    worktree = entry / "worktree"
    entry.mkdir()
    git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    return entry


def add_unpushed_worktree(repo: Path, base: Path, entry_id: str) -> Path:
    """Add a detached worktree with one deliberately unpushed commit."""
    entry = add_clean_worktree(repo, base, entry_id)
    worktree = entry / "worktree"
    (worktree / "note.txt").write_text("rework\n", encoding="utf-8")
    git(worktree, "add", "note.txt")
    git(worktree, "commit", "-m", "unpushed rework attempt")
    return entry
