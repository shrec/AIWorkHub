"""A deadline-limited retention preview must degrade to something USEFUL.

NF-2026-00284. Before the fix, a preview that hit its deadline returned
``ok:false`` with an empty candidate list -- indistinguishable, to an operator,
from a genuinely clean repository. That is the same class of lie as a build that
indexed nothing reporting success.

After the fix the walk reports each worktree it fully measures to a progress
sink (see :class:`storage_retention._PreviewProgress`). If the deadline is hit
mid-walk, ``preview`` returns the reclaim candidates it DID establish, marked
``partial=True`` and naming the worktrees ``not_covered``, instead of an empty
list. A genuinely stalled walk that established nothing still returns the empty,
fully-withheld shape, so "partial" is always visibly partial and "clean" always
means clean. Protection is verified from lineage even on the partial path: a
pinned predecessor measured before the deadline is still never a candidate.

The measurement runs off the request thread, so the deadline is forced
deterministically by blocking the per-worktree size walk after a couple of
worktrees have been measured -- never by hoping the machine is slow.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import storage_retention, task_store, worktree_storage
from support.retention import add_clean_worktree as _add_clean_worktree
from support.retention import add_unpushed_worktree as _add_unpushed_worktree
from support.retention import aged_now as _aged_now
from support.retention import git as _git
from support.retention import repository


@pytest.fixture()
def repo_with_worktrees(tmp_path: Path) -> dict[str, Path]:
    return repository(tmp_path)


def _insert_card(repo: Path, task_id: str, *, status: str, rework_predecessor_request_id: str) -> None:
    db_path = task_store.canonical_db_path(repo)
    now = datetime.now(timezone.utc).isoformat()
    card = {"rework_predecessor": {"request_id": rework_predecessor_request_id}}
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


def _blocking_size(monkeypatch, *, worktree_base: Path, block_after: int):
    """Install a ``directory_size_bytes`` that measures the first ``block_after``
    entries for real, then blocks -- so the walk fully establishes exactly those
    entries before the deadline fires. Returns the release event; callers MUST
    set it in a ``finally`` so the background measurement thread never hangs."""
    real = worktree_storage.directory_size_bytes
    release = threading.Event()
    lock = threading.Lock()
    seen = {"n": 0}

    def blocking(path, **kwargs):
        resolved = Path(path).resolve()
        if resolved.parent != worktree_base.resolve():
            return real(path, **kwargs)
        with lock:
            seen["n"] += 1
            index = seen["n"]
        if index > block_after:
            release.wait(60.0)  # never released within the preview's deadline
        return real(path, **kwargs)

    monkeypatch.setattr(worktree_storage, "directory_size_bytes", blocking)
    return release


def test_deadline_hit_returns_partial_candidates_not_an_empty_clean_list(
    repo_with_worktrees, monkeypatch
) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    for index in range(4):
        _add_clean_worktree(repo, base, f"wt-{index:02d}")

    release = _blocking_size(monkeypatch, worktree_base=base, block_after=2)
    try:
        result = storage_retention.preview(
            repo, base=base, now=_aged_now(), deadline_seconds=3.0
        )
    finally:
        release.set()

    # Incomplete -- but NOT empty, and visibly partial.
    assert result["complete"] is False
    assert result["incomplete"] is True
    assert result["incomplete_reason"] == "measurement_deadline_exceeded"
    assert result["partial"] is True
    established = {item["id"] for item in result["candidates"]}
    assert established  # the crux: never an empty list that reads as "clean"
    assert result["candidate_count"] == len(result["candidates"])
    # Only worktrees the walk actually finished are reported; each is a real,
    # aged, unprotected candidate drawn from those measured before the block.
    assert established.issubset({"wt-00", "wt-01"})
    assert "wt-02" not in established and "wt-03" not in established
    # It names what it did not cover, and still emits no actionable digest.
    assert set(result["not_covered"])
    assert {"wt-02", "wt-03"}.issubset(set(result["not_covered"]))
    assert result["preview_digest"] == ""
    # The aggregate footprint is genuinely unknown and stays withheld.
    assert result["current_bytes"] is None
    assert result["footprint"] is None


def test_a_stalled_walk_that_established_nothing_is_not_reported_as_clean(
    repo_with_worktrees, monkeypatch
) -> None:
    """When the walk establishes NO candidate the result is the fully-withheld
    shape, not a false clean report: ``partial`` is False and every candidate
    field is withheld, so a genuinely stalled measurement can never masquerade as
    an intentional empty candidate list."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    for index in range(3):
        _add_clean_worktree(repo, base, f"wt-{index:02d}")

    release = _blocking_size(
        monkeypatch, worktree_base=base, block_after=0
    )  # block on the very first
    try:
        result = storage_retention.preview(
            repo, base=base, now=_aged_now(), deadline_seconds=2.0
        )
    finally:
        release.set()

    assert result["complete"] is False
    assert result["incomplete"] is True
    assert result["partial"] is False
    assert result["candidates"] == []
    assert result["candidate_count"] is None  # withheld, not a misleading zero
    assert "candidate_count" in result["unmeasured"]


def test_partial_never_includes_a_protected_worktree_verified_by_lineage(
    repo_with_worktrees, monkeypatch
) -> None:
    """Protection holds on the partial path too: a predecessor pinned by a
    not-finished card, measured before the deadline, is covered by the walk yet
    still never a candidate -- ownership proved from the canonical task lineage,
    never from name or age."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    # Sorted order: the pinned predecessor and one clean worktree are measured
    # first, then the block fires on the third entry.
    _add_unpushed_worktree(repo, base, "a-predecessor")
    _add_clean_worktree(repo, base, "b-reclaimable")
    _add_clean_worktree(repo, base, "c-blocked")
    _insert_card(repo, "NF-1", status="pending", rework_predecessor_request_id="a-predecessor")

    release = _blocking_size(monkeypatch, worktree_base=base, block_after=2)
    try:
        result = storage_retention.preview(
            repo, base=base, now=_aged_now(), deadline_seconds=3.0
        )
    finally:
        release.set()

    assert result["complete"] is False
    assert result["partial"] is True
    established = {item["id"] for item in result["candidates"]}
    assert "a-predecessor" not in established  # pinned lineage, never reclaimed
    assert "b-reclaimable" in established  # the genuinely free worktree is surfaced
