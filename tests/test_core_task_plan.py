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
                  created_at="2026-01-01T00:00:00+00:00", archived_at=""):
    _readiness, db_path = task_store._require_ready(repo)
    card = {
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "allowed_writes": allowed_writes or [],
        "depends_on": depends_on or [],
        "status": status,
        "worker_status": worker_status,
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?)",
            (
                task_id, runner, topic, status, worker_status, json.dumps(card),
                created_at, created_at, archived_at or "",
            ),
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


def test_task_plan_snapshot_uses_one_bounded_card_read(monkeypatch):
    # The plan projection must stay one verified database snapshot regardless of size.
    repo = core.repo_root()
    _insert_card(repo, "t1", status="pending", allowed_writes=["a.py"])
    _insert_card(
        repo,
        "t2",
        status="pending",
        allowed_writes=["b.py"],
        depends_on=["t1"],
    )
    real_list_task_cards = task_store.list_task_cards
    calls: list[tuple[Path, int]] = []

    def _one_batch(root: Path, *, limit: int = 500):
        calls.append((Path(root), limit))
        return real_list_task_cards(root, limit=limit)

    monkeypatch.setattr(task_store, "list_task_cards", _one_batch)
    monkeypatch.setattr(
        task_store,
        "list_tasks",
        lambda *args, **kwargs: pytest.fail("task-plan N+1 summary read returned"),
    )
    monkeypatch.setattr(
        task_store,
        "get_task",
        lambda *args, **kwargs: pytest.fail("task-plan per-card read returned"),
    )

    snapshot = core.task_plan_snapshot()

    assert calls == [(repo, 5000)]
    assert snapshot["dependencies"]["t2"] == ["t1"]
    assert snapshot["ready"] == ["t1"]


def test_task_plan_snapshot_unblocks_after_dependency_finishes(tmp_path):
    repo = core.repo_root()
    _insert_card(repo, "t1", status="finished", worker_status="done", allowed_writes=["a.py"])
    _insert_card(repo, "t2", status="pending", allowed_writes=["b.py"], depends_on=["t1"])

    snapshot = core.task_plan_snapshot()

    assert "t2" not in snapshot["blockers"]
    assert "t2" in snapshot["ready"]


def test_supersede_task_persists_replacement_and_unblocks_successor(monkeypatch):
    repo = core.repo_root()
    _insert_card(repo, "old", status="pending")
    _insert_card(
        repo, "replacement", status="finished", worker_status="done"
    )
    _insert_card(repo, "successor", depends_on=["old"])
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *args, **kwargs: None)

    result = core.supersede_task(
        "old", reason="replaced by accepted implementation", by="replacement"
    )

    assert result["ok"] is True
    archived = task_store.get_task(repo, "old")
    assert archived is not None
    assert archived["archive_operation"] == "superseded"
    assert archived["superseded_by"] == "replacement"
    snapshot = core.task_plan_snapshot()
    assert snapshot["dependencies"]["successor"] == ["replacement"]
    assert "successor" in snapshot["ready"]


def test_supersede_task_rejects_replacement_cycle(monkeypatch):
    repo = core.repo_root()
    _insert_card(repo, "old", status="pending")
    _insert_card(repo, "replacement", depends_on=["old"])
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *args, **kwargs: None)

    result = core.supersede_task("old", by="replacement")

    assert result["ok"] is False
    assert "superseded_replacement_cycle_detected" in result["stderr"]
    old = task_store.get_task(repo, "old")
    assert old is not None
    assert old["status"] == "pending"


def test_supersede_task_rejects_missing_replacement_before_archive(monkeypatch):
    repo = core.repo_root()
    _insert_card(repo, "old", status="pending")
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *args, **kwargs: None)

    result = core.supersede_task("old", by="replacement-does-not-exist")

    assert result["ok"] is False
    assert result["returncode"] == 2
    assert result["stderr"] == (
        "superseded_replacement_task_not_found:replacement-does-not-exist"
    )
    old = task_store.get_task(repo, "old")
    assert old is not None
    assert old["status"] == "pending"
    assert not old.get("archived_at")


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


def test_archived_card_is_excluded_from_every_plan_snapshot_structure():
    repo = core.repo_root()
    _insert_card(
        repo, "archived", status="archived", worker_status="unclaimed",
        allowed_writes=["shared.py"], depends_on=["missing"],
        archived_at="2026-02-01T00:00:00+00:00",
    )
    _insert_card(repo, "pending", allowed_writes=["shared.py"])

    snapshot = core.task_plan_snapshot()

    assert snapshot["task_ids"] == ["pending"]
    assert snapshot["lifecycle"] == {"pending": "pending"}
    assert snapshot["dependencies"] == {"pending": []}
    assert snapshot["dependents"] == {"pending": []}
    assert snapshot["blockers"] == {}
    assert snapshot["write_scope_overlaps"] == {}
    assert snapshot["ready"] == ["pending"]


def test_task_show_preserves_archived_status_and_archive_audit_fields():
    repo = core.repo_root()
    archived_at = "2026-02-01T00:00:00+00:00"
    _insert_card(
        repo, "archived", status="archived", worker_status="unclaimed",
        allowed_writes=["shared.py"], archived_at=archived_at,
    )

    card = task_store.get_task(repo, "archived")

    assert card["status"] == "archived"
    assert card["worker_status"] == "unclaimed"
    assert card["archived_at"] == archived_at


def test_lifecycle_cases_remain_distinct_when_archived_cards_are_filtered():
    cards = [
        {"task_id": "rejected_archived", "status": "archived", "worker_status": "rejected"},
        {"task_id": "direct_archived", "status": "archived", "worker_status": "unclaimed"},
        {"task_id": "done", "status": "finished", "worker_status": "done"},
        {"task_id": "pending", "status": "pending", "worker_status": "unclaimed"},
        {
            "task_id": "rejected_pending", "runner": "codex_a", "topic": "coding",
            "status": "pending", "worker_status": "rejected",
        },
        {"task_id": "processing", "status": "processing", "worker_status": "claimed"},
        {"task_id": "review", "status": "review", "worker_status": "review"},
    ]

    snapshot = core.task_plan.build_snapshot(cards)

    assert snapshot["task_ids"] == [
        "done", "pending", "processing", "rejected_pending", "review",
    ]
    assert snapshot["lifecycle"] == {
        "done": "finished",
        "pending": "pending",
        "processing": "processing",
        "rejected_pending": "pending",
        "review": "review",
    }
    assert snapshot["ready"] == ["pending", "rejected_pending"]
    assert [
        c["task_id"]
        for c in core.eligible_dryrun_candidates(
            cards, "codex_a", ready_ids=set(snapshot["ready"])
        )
    ] == []


def test_live_b935_b936_shape_only_pending_b936_is_ready():
    repo = core.repo_root()
    shared = ["src/aiworkhub/core.py", "tests/test_core_task_plan.py"]
    _insert_card(
        repo, "B935", status="archived", worker_status="rejected",
        allowed_writes=shared, archived_at="2026-07-23T00:00:00+00:00",
    )
    _insert_card(repo, "B936", status="pending", allowed_writes=shared)

    snapshot = core.task_plan_snapshot()

    assert "B935" not in snapshot["task_ids"]
    assert snapshot["ready"] == ["B936"]
    assert snapshot["write_scope_overlaps"] == {}


def test_archived_rework_does_not_block_pending_codex_runner_auto_pickup():
    repo = core.repo_root()
    shared = ["src/aiworkhub/core.py"]
    _insert_card(
        repo, "legacy_rework", runner="legacy", status="archived",
        worker_status="rejected", allowed_writes=shared,
        archived_at="2026-07-23T00:00:00+00:00",
    )
    _insert_card(repo, "codex_runner", runner="codex_a", allowed_writes=shared)

    result = core.auto_pickup_dryrun(runner="codex_a", topic="coding")

    assert result["would_claim_task_id"] == "codex_runner"


def test_launch_collision_guard_ignores_unrelated_planned_collision():
    repo = core.repo_root()
    _insert_card(repo, "blocked_parent", allowed_writes=["shared.py"])
    _insert_card(
        repo,
        "dependency_blocked_child",
        allowed_writes=["shared.py"],
        depends_on=["blocked_parent"],
    )
    _insert_card(repo, "independent", allowed_writes=["other.py"])

    global_report = core.collision_guard(print_json=True)
    launch_report = core.launch_collision_guard(
        task_id="independent", print_json=True
    )

    assert global_report["ok"] is False
    assert launch_report["ok"] is True


def test_launch_collision_guard_ignores_dependency_blocked_pending_scope():
    repo = core.repo_root()
    _insert_card(repo, "unfinished", allowed_writes=["dependency.py"])
    _insert_card(
        repo,
        "future",
        allowed_writes=["shared.py"],
        depends_on=["unfinished"],
    )
    _insert_card(repo, "candidate", allowed_writes=["shared.py"])

    result = core.launch_collision_guard(task_id="candidate", print_json=True)

    assert result["ok"] is True


def test_launch_collision_guard_blocks_processing_owner():
    repo = core.repo_root()
    _insert_card(
        repo,
        "owner",
        status="processing",
        worker_status="claimed",
        allowed_writes=["shared.py"],
    )
    _insert_card(repo, "candidate", allowed_writes=["shared.py"])

    result = core.launch_collision_guard(task_id="candidate", print_json=True)

    assert result["ok"] is False
    payload = json.loads(result["stdout"])
    assert payload["blockers"][0]["task_id"] == "owner"


def test_launch_collision_guard_deterministically_selects_ready_pending_winner():
    repo = core.repo_root()
    _insert_card(repo, "a_first", allowed_writes=["shared.py"])
    _insert_card(repo, "z_second", allowed_writes=["shared.py"])

    first = core.launch_collision_guard(task_id="a_first", print_json=True)
    second = core.launch_collision_guard(task_id="z_second", print_json=True)

    assert first["ok"] is True
    assert second["ok"] is False
