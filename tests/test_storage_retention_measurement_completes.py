"""The retention preview measurement must actually FINISH, and never reclaim a
worktree it does not provably own.

NF-2026-00284. The tool reported ``ok:false / incomplete / measurement_deadline_
exceeded`` on a 162-worktree, 20 GB tree: the on-disk footprint walk could not
complete inside the deadline, so no candidate was ever produced, nothing was
ever quarantined, and the footprint grew -- a self-reinforcing loop.

Where the time went (measured against the code, not the deadline): the footprint
ran the worktree walk TWICE (a repository-scoped scan and a global scan), each
re-running ~5 git subprocesses per worktree AND a per-file ``stat`` walk over
every worktree, and THEN the ``.aiworkhub/runtime`` subtotal walked the same
worktree bytes a third time. The structural fix is that ``scan_worktrees`` now
returns the repo-scoped and global aggregates from ONE pass (see
:func:`worktree_storage.scan_worktrees`), the runtime subtotal prunes the
worktree base it already measured, and ``directory_size_bytes`` walks with
``os.scandir`` -- not a bigger deadline. These tests assert the walk happens
once, the measurement completes, and ownership-verified protection holds.

Age is injected via an explicit ``now`` rather than ``os.utime``: workers run
under a landlock sandbox that forbids changing filesystem mtimes.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import storage_retention, task_store, worktree_storage

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


@pytest.fixture()
def repo_with_worktrees(tmp_path: Path) -> dict[str, Path]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    base = tmp_path / "worktrees"
    base.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(tmp_path, "clone", str(remote), str(repo))
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "origin", "HEAD:refs/heads/main")
    _git(repo, "fetch", "origin")
    assert task_store.initialize_repository(repo)["ok"]
    return {"repo": repo, "base": base}


def _add_clean_worktree(repo: Path, base: Path, entry_id: str) -> Path:
    """A retained worktree with no local changes and nothing unpushed: it is
    ``removable_safe`` and, once aged past policy, an unambiguous reclaim
    candidate."""
    entry = base / entry_id
    worktree = entry / "worktree"
    entry.mkdir()
    _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    return entry


def _add_unpushed_worktree(repo: Path, base: Path, entry_id: str) -> Path:
    """A superseded rework attempt: a deliberately-unpushed local commit, exactly
    what a pinned predecessor looks like on disk."""
    entry = base / entry_id
    worktree = entry / "worktree"
    entry.mkdir()
    _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "note.txt").write_text("rework\n", encoding="utf-8")
    _git(worktree, "add", "note.txt")
    _git(worktree, "commit", "-m", "unpushed rework attempt")
    return entry


def _insert_card(
    repo: Path,
    task_id: str,
    *,
    status: str,
    launch_request_id: str = "",
    accepted_request_id: str = "",
    rework_predecessor_request_id: str = "",
) -> None:
    db_path = task_store.canonical_db_path(repo)
    now = datetime.now(timezone.utc).isoformat()
    card: dict[str, object] = {}
    if launch_request_id:
        card["launch_request_id"] = launch_request_id
    if accepted_request_id:
        card["accepted_request_id"] = accepted_request_id
    if rework_predecessor_request_id:
        card["rework_predecessor"] = {"request_id": rework_predecessor_request_id}
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


def test_footprint_measures_the_tree_in_a_single_pass(repo_with_worktrees, monkeypatch) -> None:
    """The primary cause was two full walks per preview (repo-scoped + global),
    each re-running per-worktree git state. One preview must now trigger exactly
    ONE ``scan_worktrees`` pass -- the structural change that lets the measurement
    finish, asserted directly rather than by a faster wall clock."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    for index in range(4):
        _add_clean_worktree(repo, base, f"wt-{index:02d}")

    real_scan = worktree_storage.scan_worktrees
    passes: list[int] = []

    def counting_scan(*args, **kwargs):
        passes.append(1)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(worktree_storage, "scan_worktrees", counting_scan)

    result = storage_retention.preview(repo, base=base, now=_aged_now())

    assert result["complete"] is True
    assert len(passes) == 1  # one walk, not two
    assert result["candidate_count"] == 4


def test_preview_completes_and_produces_real_candidates(repo_with_worktrees) -> None:
    """Against a tree of many worktrees the measurement completes within a tight
    deadline and returns real candidates -- the loop the card describes (never
    completes -> never a candidate -> footprint grows) is broken."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    for index in range(12):
        _add_clean_worktree(repo, base, f"wt-{index:02d}")

    started = time.monotonic()
    result = storage_retention.preview(repo, base=base, now=_aged_now(), deadline_seconds=60.0)
    elapsed = time.monotonic() - started

    assert result["complete"] is True
    assert not result.get("incomplete")
    assert result["candidate_count"] == 12
    assert result["candidate_bytes"] > 0
    assert result["preview_digest"]
    assert elapsed < 60.0  # finished well inside the bound, not at it


def test_pinned_rework_predecessor_is_never_a_candidate_by_verified_lineage(
    repo_with_worktrees,
) -> None:
    """A predecessor pinned by a not-yet-finished card is protected by CANONICAL
    task lineage read from the tasks table -- never by its directory name or its
    age. Reclaiming it is the exact data loss this planner exists to prevent."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    _add_unpushed_worktree(repo, base, "predecessor")
    _add_clean_worktree(repo, base, "reclaimable")
    # A card rejected back to pending pins its predecessor attempt.
    _insert_card(repo, "NF-1", status="pending", rework_predecessor_request_id="predecessor")

    result = storage_retention.preview(repo, base=base, now=_aged_now())
    candidate_ids = {item["id"] for item in result["candidates"]}

    assert result["complete"] is True
    assert "predecessor" not in candidate_ids  # protected by lineage, not by age
    assert {"id": "predecessor", "reason": "rework_predecessor_retained"} in result["protected"]
    # The genuinely superseded/unpinned clean worktree is still reclaimed.
    assert "reclaimable" in candidate_ids
    assert (base / "predecessor").is_dir()  # preview mutates nothing


def test_worktree_a_live_process_holds_is_never_a_candidate(repo_with_worktrees) -> None:
    """The current claimed attempt of a card actively ``processing`` is the
    worktree a live process is using. Ownership is proved by the card's
    ``launch_request_id`` in the canonical lifecycle -- never inferred from the
    name or age -- so it can never be reclaimed out from under a running task."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    _add_clean_worktree(repo, base, "live-attempt")
    _add_clean_worktree(repo, base, "idle-attempt")
    _insert_card(repo, "NF-live", status="processing", launch_request_id="live-attempt")

    result = storage_retention.preview(repo, base=base, now=_aged_now())
    candidate_ids = {item["id"] for item in result["candidates"]}

    assert result["complete"] is True
    assert "live-attempt" not in candidate_ids
    assert {"id": "live-attempt", "reason": "live_worker"} in result["protected"]
    assert "idle-attempt" in candidate_ids  # an unheld clean worktree stays reclaimable
    assert (base / "live-attempt").is_dir()
