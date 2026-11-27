"""Fix batch 13 regression tests (NF-2026-00696 and siblings)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import storage_retention, worktree_storage


SOURCE_REQUEST_ID = "req-acceptance-source-0001"


def _union2_id(source_id: str) -> str:
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:32]
    return f"union2_{digest}_{'a' * 16}"


def _stage_quarantine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    fresh_protected: dict[str, str],
    lineage_verified: bool,
) -> tuple[Path, Path]:
    """A union candidate the (stale) digest snapshot admitted for quarantine."""
    repo = tmp_path / "repo"
    repo.mkdir()
    base = tmp_path / "worktrees"
    base.mkdir()
    union_id = _union2_id(SOURCE_REQUEST_ID)
    union_dir = base / union_id
    (union_dir / "worktree").mkdir(parents=True)
    candidate = {
        "id": union_id,
        "head": "h" * 40,
        "size_bytes": 7,
        "modified_at_epoch": int(union_dir.lstat().st_mtime),
    }
    current = {"preview_digest": "d" * 64, "candidates": [candidate]}

    def fake_measure(_key: Any, _fn: Any, _deadline: Any) -> tuple[dict[str, Any], bool]:
        return current, True

    qroot = tmp_path / "quarantine"
    qroot.mkdir()
    monkeypatch.setattr(storage_retention, "_measure_within_deadline", fake_measure)
    monkeypatch.setattr(storage_retention, "_repo_id", lambda _root: "repo_test")
    monkeypatch.setattr(storage_retention, "_ensure_quarantine_root", lambda _r, _b: qroot)
    monkeypatch.setattr(storage_retention, "_manifest_authentication", lambda _r, _m: "auth")
    monkeypatch.setattr(storage_retention, "_append_audit", lambda _r, _e: None)
    monkeypatch.setattr(
        storage_retention,
        "_protected_attempt_ids",
        lambda _root: (dict(fresh_protected), lineage_verified, {}, set(), set()),
    )
    monkeypatch.setattr(worktree_storage, "_git_common_dir", lambda _root: "common")
    monkeypatch.setattr(
        worktree_storage,
        "_worktree_git_state",
        lambda _path: {"head": "h" * 40, "parent_git_dir": "common"},
    )
    monkeypatch.setattr(
        worktree_storage, "_classify", lambda _state: worktree_storage.CLASS_REMOVABLE_SAFE
    )
    return repo, union_dir


def test_nf00696_quarantine_rechecks_live_union_against_fresh_lineage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The source attempt re-entered review after the digest snapshot: the
    # acceptance validator's union must survive the commit-time move.
    repo, union_dir = _stage_quarantine(
        monkeypatch,
        tmp_path,
        fresh_protected={SOURCE_REQUEST_ID: "live_worker"},
        lineage_verified=True,
    )
    result = storage_retention.quarantine(
        repo, preview_digest="d" * 64, confirm=True, base=union_dir.parent
    )
    assert result["quarantined"] == 0
    assert union_dir.is_dir()


def test_nf00696_quarantine_refuses_union_when_lineage_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, union_dir = _stage_quarantine(
        monkeypatch, tmp_path, fresh_protected={}, lineage_verified=False
    )
    result = storage_retention.quarantine(
        repo, preview_digest="d" * 64, confirm=True, base=union_dir.parent
    )
    assert result["quarantined"] == 0
    assert union_dir.is_dir()


def test_nf00696_quarantine_still_moves_dead_union(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Control: with verified lineage and no live source the union is reclaimed,
    # so the two refusals above are caused by the new recheck alone.
    repo, union_dir = _stage_quarantine(
        monkeypatch, tmp_path, fresh_protected={}, lineage_verified=True
    )
    result = storage_retention.quarantine(
        repo, preview_digest="d" * 64, confirm=True, base=union_dir.parent
    )
    assert result["quarantined"] == 1
    assert not union_dir.exists()
