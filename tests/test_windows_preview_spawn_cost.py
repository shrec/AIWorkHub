"""The retention preview's git subprocess cost must not scale with worktrees.

WIN-OBS-018 (follow-up to the WIN-REPORT diagnosis). The owner's Windows 11
install reported the storage retention preview hitting its 90-second deadline
with ``candidates``/``footprint``/``protected`` all unmeasured -- byte-identical
to 0.9.79 and 0.9.83. The 0.9.79 change was real (it collapsed three full
worktree walks into one) but it only ever changed how MANY passes ran, never the
RESIDUAL per-worktree cost of that one pass: ``_worktree_git_state`` spawned FIVE
git subprocesses per worktree (``rev-parse HEAD``, ``config``, ``status``,
``rev-list`` and the ``git-common-dir`` ``rev-parse``). On Linux each ``fork``
+``exec`` is cheap and the single pass finishes in ~9s; on Windows there is no
``fork``, every ``CreateProcess`` is an order of magnitude dearer and every git
binary open is scanned by Defender, so hundreds of worktrees overran the
deadline.

The fix resolves git state for the whole repository in a FIXED, small batch
(``git worktree list`` + ``git config`` + one ``git rev-list --stdin``) whose
spawn count does not grow with the number of worktrees. The one field that
genuinely needs a per-worktree spawn -- ``dirty`` -- is deferred, so preview
still returns complete measured candidates, footprint and protected sets.

A spawn-COUNT test is the only kind that transfers to Windows from a Linux run:
it proves the ``CreateProcess`` count no longer scales with N, which is the exact
quantity that overran the Windows deadline, without needing a Windows machine or
a slow wall clock. That is why this test counts spawns rather than timing them.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import storage_retention, task_store  # noqa: E402
from aiworkhub import worktree_storage as ws  # noqa: E402

# terminal_runs_days defaults to 30; +31 days ages every just-created worktree
# past the policy without mutating filesystem mtimes (os.utime is denied in the
# validation sandbox), matching the sibling retention suites.
_AGED_NOW_OFFSET_DAYS = 31


def _aged_now() -> float:
    return time.time() + _AGED_NOW_OFFSET_DAYS * 86400


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _clone_repo(tmp_path: Path) -> Path:
    """A parent repository with a local bare 'remote' so 'pushed' is verifiable
    entirely offline."""
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(tmp_path, "clone", str(remote), str(repo))
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "origin", "HEAD:refs/heads/main")
    _git(repo, "fetch", "origin")  # populate refs/remotes/origin/*
    return repo


def _base_with_worktrees(repo: Path, base: Path, count: int) -> Path:
    base.mkdir(parents=True)
    for index in range(count):
        worktree = base / f"wt-{index:03d}" / "worktree"
        worktree.parent.mkdir(parents=True)
        _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    return base


def _add_clean(repo: Path, base: Path, name: str) -> None:
    worktree = base / name / "worktree"
    worktree.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")


def _insert_card(repo: Path, task_id: str, *, status: str, launch_request_id: str) -> None:
    db_path = task_store.canonical_db_path(repo)
    now = datetime.now(timezone.utc).isoformat()
    card = {"launch_request_id": launch_request_id}
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "claude", "storage", status, "unclaimed", json.dumps(card), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _count_git_spawns(fn) -> int:
    """Count git subprocess spawns issued while ``fn`` runs.

    Wraps ``worktree_storage.subprocess.run`` (through which BOTH ``_git`` and the
    batched ``_git_stdin`` pass) so the count is exactly the ``CreateProcess``
    tally the Windows deadline is sensitive to."""
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


def test_repo_scoped_preview_git_spawns_do_not_grow_with_worktree_count(tmp_path) -> None:
    """The crux, and the only assertion that transfers to Windows: the git
    subprocess spawn count of the repository-scoped scan the preview runs is the
    SAME for a few worktrees and for many. A constant ``CreateProcess`` count is
    what stops the Windows deadline overrun -- proven here on Linux by counting,
    not by a wall clock that would only ever be fast on this machine."""
    repo = _clone_repo(tmp_path)
    small = _base_with_worktrees(repo, tmp_path / "small", 2)
    large = _base_with_worktrees(repo, tmp_path / "large", 12)

    small_spawns = _count_git_spawns(
        lambda: ws.scan_worktrees(small, repo_root=repo, with_sizes=False)
    )
    large_spawns = _count_git_spawns(
        lambda: ws.scan_worktrees(large, repo_root=repo, with_sizes=False)
    )

    # Does NOT grow with the number of worktrees -- a fixed repository-level cost.
    assert small_spawns == large_spawns
    assert large_spawns <= 6  # a handful of repository-level queries, not ~5N

    # Before/after contrast, measured in the same run: the per-worktree path the
    # preview NO LONGER takes still pays ~5 git spawns for every extra worktree.
    per_worktree_small = _count_git_spawns(lambda: ws.scan_worktrees(small, with_sizes=False))
    per_worktree_large = _count_git_spawns(lambda: ws.scan_worktrees(large, with_sizes=False))
    assert per_worktree_large - per_worktree_small >= 5 * (12 - 2)


def test_preview_returns_measured_candidates_footprint_and_protected(tmp_path) -> None:
    """The field report showed ``candidates``/``footprint``/``protected`` as the
    unmeasured placeholders ``_incomplete_preview`` emits (None/[]/"") when the
    deadline is hit. With the per-worktree spawn cost removed the walk finishes,
    so the preview returns REAL measured values: a concrete candidate list, a
    concrete footprint, and the protected worktree named."""
    repo = _clone_repo(tmp_path)
    assert task_store.initialize_repository(repo)["ok"]
    base = tmp_path / "worktrees"
    base.mkdir()
    for index in range(3):
        _add_clean(repo, base, f"free-{index}")
    _add_clean(repo, base, "live")
    # A live worker holds "live": it must be reported protected, never a candidate.
    _insert_card(repo, "CARD-LIVE", status="processing", launch_request_id="live")

    result = storage_retention.preview(repo, base=base, now=_aged_now())

    # Complete, not the incomplete/placeholder shape.
    assert result["complete"] is True
    assert not result.get("incomplete")

    # Candidates are MEASURED (a real list, count == its length), not a withheld
    # None/[] placeholder.
    assert result["candidate_count"] == len(result["candidates"]) == 3
    assert result["candidate_bytes"] > 0
    assert {item["id"] for item in result["candidates"]} == {"free-0", "free-1", "free-2"}

    # Footprint is MEASURED (real integers / dict), not withheld None.
    assert isinstance(result["current_bytes"], int) and result["current_bytes"] > 0
    assert isinstance(result["footprint"], dict)
    assert result["preview_digest"]

    # Protected is MEASURED: the live worker is named and never reclaimed.
    assert result["protected_count"] >= 1
    assert {"id": "live", "reason": "live_worker"} in result["protected"]
    assert (base / "live").is_dir()  # preview mutates nothing
