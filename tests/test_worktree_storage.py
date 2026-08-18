"""WIN-OBS-018: worktree_storage's repository-scoped scan removes the residual
per-worktree git-subprocess cost, while the global (CLI cleanup) path keeps its
exact per-worktree safety classification.

The retention preview measures worktrees repository-scoped
(``scan_worktrees(repo_root=...)``). That path previously paid ~5 git
subprocesses PER worktree via ``_worktree_git_state``; on Windows, where every
``CreateProcess`` is dear and AV-scanned, that overran the preview deadline. The
scan now resolves git state for the whole repository in a fixed batch
(``git worktree list`` + ``git config`` + one ``git rev-list --stdin``) and
defers the one un-batchable field, ``dirty``.

The global path (``repo_root=None``) that ``execute_cleanup`` hard-deletes on is
deliberately left exact: it still proves each worktree clean AND pushed before
calling it ``removable_safe``, so these tests pin BOTH the new constant-cost
repo-scoped behaviour and the unchanged per-worktree classification.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worktree_storage as ws  # noqa: E402


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _clone_repo(tmp_path: Path, name: str = "repo") -> Path:
    remote = tmp_path / f"{name}-remote.git"
    repo = tmp_path / name
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(tmp_path, "clone", str(remote), str(repo))
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "origin", "HEAD:refs/heads/main")
    _git(repo, "fetch", "origin")
    return repo


def _add_worktree(repo: Path, base: Path, name: str) -> Path:
    worktree = base / name / "worktree"
    worktree.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    return worktree


def _count_git_spawns(fn) -> int:
    real = ws.subprocess.run
    tally = {"n": 0}

    def wrapped(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
            tally["n"] += 1
        return real(cmd, *args, **kwargs)

    ws.subprocess.run = wrapped
    try:
        fn()
    finally:
        ws.subprocess.run = real
    return tally["n"]


@pytest.fixture()
def worktrees(tmp_path):
    """One worktree of each safety state under a base owned by ``parent``, plus a
    foreign worktree owned by a different clone and a broken orphan."""
    parent = _clone_repo(tmp_path, "parent")
    base = tmp_path / "wt-base"
    base.mkdir()

    _add_worktree(parent, base, "W_SAFE")  # clean + pushed

    dirty = _add_worktree(parent, base, "W_DIRTY")  # uncommitted edit
    (dirty / "file.txt").write_text("uncommitted\n", encoding="utf-8")

    unpushed = _add_worktree(parent, base, "W_UNPUSHED")  # local commit, never pushed
    (unpushed / "new.txt").write_text("local\n", encoding="utf-8")
    _git(unpushed, "add", "new.txt")
    _git(unpushed, "commit", "-m", "unpushed local work")

    orphan = base / "W_ORPHAN" / "worktree"  # broken .git pointer
    orphan.mkdir(parents=True)
    (orphan / ".git").write_text("gitdir: /nonexistent/gitdir\n", encoding="utf-8")

    foreign = _clone_repo(tmp_path, "foreign")  # owned by a different repository
    foreign_tree = base / "W_FOREIGN" / "worktree"
    foreign_tree.parent.mkdir(parents=True)
    _git(foreign, "worktree", "add", "--detach", str(foreign_tree), "HEAD")

    return {"base": base, "parent": parent}


def _by_id(scan):
    return {wt["id"]: wt for wt in scan["worktrees"]}


def test_repo_scoped_scan_git_spawns_are_constant_in_worktree_count(tmp_path) -> None:
    """The repository-scoped scan issues a FIXED number of git subprocesses
    regardless of how many worktrees it measures -- and the contrasting global
    path still pays ~5 per worktree."""
    repo = _clone_repo(tmp_path)
    small = tmp_path / "small"
    large = tmp_path / "large"
    small.mkdir()
    large.mkdir()
    for index in range(2):
        _add_worktree(repo, small, f"s-{index}")
    for index in range(12):
        _add_worktree(repo, large, f"l-{index}")

    small_spawns = _count_git_spawns(
        lambda: ws.scan_worktrees(small, repo_root=repo, with_sizes=False)
    )
    large_spawns = _count_git_spawns(
        lambda: ws.scan_worktrees(large, repo_root=repo, with_sizes=False)
    )
    assert small_spawns == large_spawns
    assert large_spawns <= 6

    # The per-worktree path DOES scale: ~5 git spawns for every extra worktree.
    per_wt_small = _count_git_spawns(lambda: ws.scan_worktrees(small, with_sizes=False))
    per_wt_large = _count_git_spawns(lambda: ws.scan_worktrees(large, with_sizes=False))
    assert per_wt_large - per_wt_small >= 5 * (12 - 2)


def test_repo_scoped_scan_attributes_owns_defers_dirty_and_excludes_others(worktrees) -> None:
    """Repository scope includes exactly this repository's worktrees (foreign and
    orphaned excluded), resolves their HEAD/origin/attribution from the batch, and
    marks ``dirty`` deferred rather than measuring it per worktree."""
    scan = ws.scan_worktrees(worktrees["base"], repo_root=worktrees["parent"])

    assert scan["scope"] == "repository"
    ids = _by_id(scan)
    assert set(ids) == {"W_SAFE", "W_DIRTY", "W_UNPUSHED"}
    for wt in ids.values():
        assert wt["git_ok"] is True
        assert wt["head"]  # resolved from the batched `git worktree list`
        assert wt["dirty"] is False and wt["dirty_deferred"] is True
        # dirty is unproven, so no repo-scoped worktree is claimed removable-safe.
        assert wt["class"] == ws.CLASS_RETAINED_UNSAVED


def test_repo_scoped_scan_batches_unpushed_detection(worktrees) -> None:
    """The unpushed set is resolved for all worktrees in one ``rev-list --stdin``
    query: the worktree with a local-only commit is flagged unpushed; the clean
    pushed one is not."""
    ids = _by_id(ws.scan_worktrees(worktrees["base"], repo_root=worktrees["parent"]))
    assert ids["W_UNPUSHED"]["unpushed"] is True
    assert ids["W_SAFE"]["unpushed"] is False


def test_global_scan_preserves_exact_safety_classification(worktrees) -> None:
    """The ``repo_root=None`` path ``execute_cleanup`` deletes on is unchanged: it
    still measures ``dirty`` per worktree, so a clean+pushed worktree is
    ``removable_safe`` while dirty/unpushed/orphaned are never removable."""
    scan = ws.scan_worktrees(worktrees["base"])  # global path
    cls = {i: wt["class"] for i, wt in _by_id(scan).items()}
    assert cls["W_SAFE"] == ws.CLASS_REMOVABLE_SAFE
    assert cls["W_DIRTY"] == ws.CLASS_RETAINED_UNSAVED
    assert cls["W_UNPUSHED"] == ws.CLASS_RETAINED_UNSAVED
    assert cls["W_ORPHAN"] == ws.CLASS_ORPHANED
    # The global path measures dirty (never defers it).
    assert _by_id(scan)["W_SAFE"].get("dirty_deferred") is False
    # Dirty/unpushed/orphaned work is never removable on the delete path.
    plan = ws.plan_cleanup(scan)
    removable = {wt["id"] for wt in plan["would_remove"]}
    assert "W_SAFE" in removable
    assert {"W_DIRTY", "W_UNPUSHED", "W_ORPHAN"}.isdisjoint(removable)
