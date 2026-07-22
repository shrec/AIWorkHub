from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, task_store  # noqa: E402


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    return repo


def _insert_card(repo: Path, task_id, *, runner="codex_a", topic="coding", status="pending",
                  worker_status="unclaimed", allowed_writes=None, depends_on=None,
                  created_at="2026-01-01T00:00:00+00:00"):
    _readiness, db_path = task_store._require_ready(repo)
    card = {
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "allowed_writes": allowed_writes or [],
        "depends_on": depends_on or [],
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?)",
            (task_id, runner, topic, status, worker_status, json.dumps(card), created_at, created_at),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _repo_env(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    return repo


def test_task_plan_snapshot_reports_blockers_and_ready(tmp_path):
    repo = core.repo_root()
    _insert_card(repo, "t1", status="pending", allowed_writes=["a.py"])
    _insert_card(repo, "t2", status="pending", allowed_writes=["b.py"], depends_on=["t1"])

    snapshot = core.task_plan_snapshot()

    assert snapshot["ok"] is True
    assert snapshot["blockers"]["t2"] == ["t1"]
    assert "t1" in snapshot["ready"]
    assert "t2" not in snapshot["ready"]


def test_task_plan_snapshot_unblocks_after_dependency_finishes(tmp_path):
    repo = core.repo_root()
    _insert_card(repo, "t1", status="finished", worker_status="done", allowed_writes=["a.py"])
    _insert_card(repo, "t2", status="pending", allowed_writes=["b.py"], depends_on=["t1"])

    snapshot = core.task_plan_snapshot()

    assert "t2" not in snapshot["blockers"]
    assert "t2" in snapshot["ready"]


def test_eligible_dryrun_candidates_excludes_dag_blocked_task():
    rows = [
        {"task_id": "t1", "runner": "codex_a", "topic": "coding", "worker_status": "unclaimed",
         "status": "pending"},
        {"task_id": "t2", "runner": "codex_a", "topic": "coding", "worker_status": "unclaimed",
         "status": "pending"},
    ]
    eligible_no_filter = core.eligible_dryrun_candidates(rows, "codex_a", "coding")
    assert [c["task_id"] for c in eligible_no_filter] == ["t1", "t2"]

    eligible_filtered = core.eligible_dryrun_candidates(
        rows, "codex_a", "coding", ready_ids={"t1"}
    )
    assert [c["task_id"] for c in eligible_filtered] == ["t1"]


def test_auto_pickup_dryrun_skips_task_blocked_by_unfinished_dependency(tmp_path):
    repo = core.repo_root()
    _insert_card(repo, "t1", status="pending", allowed_writes=["a.py"],
                 created_at="2026-01-01T00:00:00+00:00")
    _insert_card(repo, "t2", status="pending", allowed_writes=["b.py"], depends_on=["t1"],
                 created_at="2026-01-02T00:00:00+00:00")

    result = core.auto_pickup_dryrun(runner="codex_a", topic="coding")

    assert result["would_claim_task_id"] == "t1"


def test_auto_pickup_dryrun_reports_depends_on_and_blockers_on_candidate(tmp_path):
    repo = core.repo_root()
    _insert_card(repo, "t1", status="finished", worker_status="done", allowed_writes=["a.py"],
                 created_at="2026-01-01T00:00:00+00:00")
    _insert_card(repo, "t2", status="pending", allowed_writes=["b.py"], depends_on=["t1"],
                 created_at="2026-01-02T00:00:00+00:00")

    result = core.auto_pickup_dryrun(runner="codex_a", topic="coding")

    assert result["would_claim_task_id"] == "t2"
    assert result["candidate"]["depends_on"] == ["t1"]
    assert result["filtering"]["dag_snapshot_error"] is None


def test_auto_pickup_dryrun_fails_closed_when_dag_snapshot_cannot_be_built(tmp_path, monkeypatch):
    repo = core.repo_root()
    _insert_card(repo, "t1", status="pending", allowed_writes=["a.py"])

    def _boom():
        raise task_store.TaskStoreError("boom")

    monkeypatch.setattr(core, "_full_cards_for_plan", _boom)

    result = core.auto_pickup_dryrun(runner="codex_a", topic="coding")

    assert result["would_claim_task_id"] is None
    assert result["candidate"] is None
    assert result["filtering"]["dag_snapshot_error"] == "boom"


def test_auto_pickup_fails_closed_when_dag_snapshot_cannot_be_built(tmp_path, monkeypatch):
    repo = core.repo_root()
    _insert_card(repo, "t1", status="pending", allowed_writes=["a.py"])
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")

    def _boom():
        raise task_store.TaskStoreError("boom")

    monkeypatch.setattr(core, "_full_cards_for_plan", _boom)

    result = core.auto_pickup(runner="codex_a", topic="coding")

    assert result["ok"] is False


def test_create_task_dependency_validation_rejects_legacy_card_with_invalid_depends_on():
    # A legacy card whose stored depends_on is malformed must be reported
    # invalid and block new edges through it -- create_task's existing_edges
    # building (core.py) delegates to this exact helper, so this exercises
    # the contract it relies on for the invalid-legacy-card case.
    from aiworkhub import task_plan

    edges, invalid_ids = task_plan.existing_edges_from_cards(
        {"legacy": {"depends_on": ["../etc"]}}
    )
    assert invalid_ids == {"legacy"}
    with pytest.raises(task_plan.TaskPlanError, match="dependency_has_invalid_depends_on"):
        task_plan.validate_new_dependency_edge("new", ["legacy"], edges, invalid_ids=invalid_ids)
