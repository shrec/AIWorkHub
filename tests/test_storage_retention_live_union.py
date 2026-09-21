"""A combined-validation worktree remains live while its source card is in review."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import storage_retention, task_store, worker_workspace, worktree_storage
from support.retention import git, repository


def _record_card(repo: Path, source_id: str, status: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(task_store.canonical_db_path(repo))) as conn:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "combined-validation-card",
                "claude",
                "release-metadata",
                status,
                "unclaimed",
                json.dumps({"launch_request_id": source_id}),
                now,
                now,
            ),
        )


@pytest.mark.parametrize("source_id", ["379047b3e889423c95ad42f97dfb2fe0", "a" * 90])
def test_live_combined_validation_union_survives_over_cap_cleanup(
    tmp_path: Path, monkeypatch, source_id: str
) -> None:
    roots = repository(tmp_path)
    repo, base = roots["repo"], roots["base"]
    union_id = (
        f"union2_{hashlib.sha256(source_id.encode()).hexdigest()[:32]}_"
        "1234567890abcdef"
    )
    entry = base / union_id
    entry.mkdir()
    git(repo, "worktree", "add", "--detach", str(entry / "worktree"), "HEAD")
    _record_card(repo, source_id, "review")
    monkeypatch.setattr(storage_retention, "_policy", lambda _root: (30, 1))

    preview = storage_retention.preview(repo, base=base)

    assert union_id not in {item["id"] for item in preview["candidates"]}
    assert {item["id"]: item["reason"] for item in preview["protected"]}[
        union_id
    ] == "live_combined_validation"

    # A crashed union must not become permanent retention once its source
    # candidate has finished; the normal over-cap policy may then reclaim it.
    with sqlite3.connect(str(task_store.canonical_db_path(repo))) as conn:
        conn.execute(
            "UPDATE tasks SET status = 'finished' WHERE task_id = ?",
            ("combined-validation-card",),
        )
    after_finish = storage_retention.preview(repo, base=base)
    assert union_id in {item["id"] for item in after_finish["candidates"]}


def test_partial_preview_never_names_a_live_combined_union() -> None:
    source_id = "a" * 90
    union_id = (
        f"union2_{hashlib.sha256(source_id.encode()).hexdigest()[:32]}_"
        "1234567890abcdef"
    )
    progress = storage_retention._PreviewProgress()
    progress.configure(
        repo_common_dir="repo-git-dir",
        protected_ids={source_id: "live_worker"},
        lineage_verified=True,
        min_age_days=30,
        now=4_000_000,
    )
    progress.begin([union_id])
    progress.observe(
        {
            "id": union_id,
            "parent_git_dir": "repo-git-dir",
            "class": worktree_storage.CLASS_REMOVABLE_SAFE,
            "modified_at_epoch": 1,
            "size_bytes": 1,
        }
    )

    assert progress.snapshot()["candidates"] == []


def test_union_of_finished_source_not_pinned_by_matching_prefix(
    tmp_path: Path, monkeypatch
) -> None:
    roots = repository(tmp_path)
    repo, base = roots["repo"], roots["base"]
    common_prefix = "a" * 70
    finished_id = common_prefix + "finished"
    live_id = common_prefix + "still-live"
    union_id = (
        f"union2_{hashlib.sha256(finished_id.encode()).hexdigest()[:32]}_"
        "1234567890abcdef"
    )
    entry = base / union_id
    entry.mkdir()
    git(repo, "worktree", "add", "--detach", str(entry / "worktree"), "HEAD")
    _record_card(repo, live_id, "review")
    monkeypatch.setattr(storage_retention, "_policy", lambda _root: (30, 1))

    preview = storage_retention.preview(repo, base=base)

    assert union_id in {item["id"] for item in preview["candidates"]}


def test_combined_workspace_name_binds_full_source_id(
    tmp_path: Path, monkeypatch
) -> None:
    source_id = "a" * 70 + "finished"
    captured: list[str] = []

    def capture_workspace(_repo, request_id, _card, _adapter):
        captured.append(request_id)
        raise RuntimeError("request_id_captured")

    monkeypatch.setattr(worker_workspace, "_canonical_worktree_delta_paths", lambda _: [])
    monkeypatch.setattr(worker_workspace, "create_workspace", capture_workspace)
    source = SimpleNamespace(repo=tmp_path, request_id=source_id, allowed_writes=("x.py",))

    with pytest.raises(RuntimeError, match="request_id_captured"):
        worker_workspace.create_combined_validation_workspace(source, {}, ["x.py"])

    assert len(captured) == 1
    assert captured[0].startswith(
        f"union2_{hashlib.sha256(source_id.encode()).hexdigest()[:32]}_"
    )
