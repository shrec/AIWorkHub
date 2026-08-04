from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from aiworkhub import core, task_store


@pytest.fixture
def coordinator_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token_path = tmp_path / "coordinator.token"
    token_path.write_text("coordinator-token\n", encoding="utf-8")
    os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coordinator-token")
    return repo


def _insert_blocked(
    repo: Path,
    *,
    task_id: str,
    request_id: str,
    substatus: str,
) -> None:
    readiness = task_store.storage_readiness(repo)
    now = "2026-08-03T00:00:00+00:00"
    card = {
        "task_id": task_id,
        "runner": "worker_runner",
        "topic": "terminal_retry",
        "objective": "retry exact operational failure",
        "status": "blocked",
        "worker_status": substatus,
        "claimed_by": "worker_runner",
        "claim_epoch": 7,
        "launch_request_id": request_id,
        "terminal_substatus": substatus,
        "terminal_outcome": substatus,
        "blocker_reason": f"{substatus}:exact",
        "blocked_at": now,
        "blocked_by": "worker_runner",
        "terminal_failure": {
            "substatus": substatus,
            "evidence": {"request_id": request_id, "error": f"{substatus}:exact"},
        },
        "review_feedback": {"schema_id": "aiworkhub.rework_feedback_delta.v1"},
        "rework_predecessor": {"schema_id": "aiworkhub.rework_predecessor.v1"},
    }
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id,runner,topic,mode,status,worker_status,priority,objective,"
            "card_json,created_at,updated_at,claimed_by,claimed_at,started_at,completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                "worker_runner",
                "terminal_retry",
                "solo",
                "blocked",
                substatus,
                "normal",
                "retry exact operational failure",
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                now,
                now,
                "worker_runner",
                now,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row(repo: Path, task_id: str) -> sqlite3.Row:
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


@pytest.mark.parametrize(
    "substatus",
    [
        "cancelled",
        "timed_out",
        "output_budget_exceeded",
        "launch_failed",
        "worker_failed",
        "process_lost",
        "liveness_lost",
    ],
)
def test_retry_terminal_requeues_only_exact_operational_episode(
    coordinator_repo: Path, substatus: str
) -> None:
    task_id = f"RETRY_{substatus.upper()}"
    request_id = (substatus[0] * 32)[:32]
    _insert_blocked(
        coordinator_repo,
        task_id=task_id,
        request_id=request_id,
        substatus=substatus,
    )

    result = core.retry_terminal_task(task_id, request_id, substatus, "route repaired")

    assert result["ok"] is True, result
    row = _row(coordinator_repo, task_id)
    assert row["status"] == "pending"
    assert row["worker_status"] == "unclaimed"
    assert row["claimed_by"] is None
    assert row["claimed_at"] is None
    assert row["started_at"] is None
    assert row["completed_at"] is None
    card = json.loads(row["card_json"])
    assert card["claim_epoch"] == 7
    assert card["review_feedback"]["schema_id"].endswith(".v1")
    assert card["rework_predecessor"]["schema_id"].endswith(".v1")
    assert card["terminal_retry"]["request_id"] == request_id
    assert card["terminal_retry"]["terminal_substatus"] == substatus
    for cleared in (
        "launch_request_id",
        "terminal_failure",
        "terminal_substatus",
        "terminal_outcome",
        "blocker_reason",
        "blocked_at",
        "blocked_by",
    ):
        assert cleared not in card

    repeated = core.retry_terminal_task(task_id, request_id, substatus, "route repaired")
    assert repeated["ok"] is True
    assert repeated["idempotent"] is True


def test_retry_terminal_rejects_wrong_request_without_mutation(
    coordinator_repo: Path,
) -> None:
    _insert_blocked(
        coordinator_repo,
        task_id="RETRY_WRONG_REQUEST",
        request_id="a" * 32,
        substatus="worker_failed",
    )

    result = core.retry_terminal_task(
        "RETRY_WRONG_REQUEST", "b" * 32, "worker_failed"
    )

    assert result["ok"] is False
    assert "request_mismatch" in result["stderr"]
    row = _row(coordinator_repo, "RETRY_WRONG_REQUEST")
    assert row["status"] == "blocked"
    assert row["worker_status"] == "worker_failed"


@pytest.mark.parametrize("substatus", ["validation_failed", "scope_rejected", "review_ready"])
def test_retry_terminal_rejects_semantic_or_review_outcomes(
    coordinator_repo: Path, substatus: str
) -> None:
    task_id = f"RETRY_FORBIDDEN_{substatus.upper()}"
    _insert_blocked(
        coordinator_repo,
        task_id=task_id,
        request_id="c" * 32,
        substatus=substatus,
    )

    result = core.retry_terminal_task(task_id, "c" * 32, substatus)

    assert result["ok"] is False
    assert "substatus_not_operational" in result["stderr"]
    assert _row(coordinator_repo, task_id)["status"] == "blocked"
