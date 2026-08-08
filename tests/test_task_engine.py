from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_engine, task_store  # noqa: E402


def _repo_with_task(tmp_path: Path, *, status: str = "processing") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-07-22T00:00:00+00:00"
    card = {
        "task_id": "TASK_B891",
        "runner": "codex_worker_b891",
        "topic": "task_mcp",
        "allowed_writes": ["out.txt"],
        "callback_required": True,
        "coordinator_provider": "codex",
        "origin_thread_id": "thread-b891",
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, started_at, origin_thread_id) "
            "VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?)",
            (
                "TASK_B891",
                "codex_worker_b891",
                "task_mcp",
                status,
                "claimed" if status == "processing" else "unclaimed",
                json.dumps(card),
                now,
                now,
                "codex_worker_b891" if status == "processing" else "",
                now if status == "processing" else "",
                now if status == "processing" else "",
                "thread-b891",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return repo


@pytest.mark.parametrize(
    "substatus",
    [
        "worker_failed",
        "validation_failed",
        "required_output_unchanged",
        "blocked",
        "cancelled",
        "launch_failed",
        "liveness_lost",
    ],
)
def test_terminal_outcomes_route_to_review_with_exact_substatus(tmp_path: Path, substatus: str) -> None:
    repo = _repo_with_task(tmp_path)
    result = task_engine.mark_terminal_review(
        repo,
        "TASK_B891",
        "codex_worker_b891",
        substatus,
        evidence={"error": substatus, "request_id": "req-b891"},
    )
    assert result["ok"] is True
    assert result["callback_enqueued"] is True

    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "review"
    assert card["worker_status"] == "review"
    assert card["terminal_substatus"] == substatus
    assert card["terminal_review"]["evidence"]["error"] == substatus
    events = task_store.get_task_events(repo, "TASK_B891")
    assert [event["event"] for event in events] == ["callback_enqueued", "terminal_review"]


def _repo_with_review_ready_task(tmp_path: Path, *, request_id: str = "req-accept-b1") -> Path:
    repo = _repo_with_task(tmp_path, status="review")
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET claimed_by='codex_worker_b891' WHERE task_id=?",
            ("TASK_B891",),
        )
        card = json.loads(
            conn.execute(
                "SELECT card_json FROM tasks WHERE task_id=?", ("TASK_B891",)
            ).fetchone()[0]
        )
        card["terminal_review"] = {
            "substatus": "review_ready",
            "evidence": {
                "changed_paths": ["out.txt"],
                "changed_path_hashes": {"out.txt": "deadbeef"},
                "request_identity": {
                    "request_id": request_id,
                    "task_id": "TASK_B891",
                    "runner": "codex_worker_b891",
                    "topic": "task_mcp",
                },
            },
        }
        conn.execute(
            "UPDATE tasks SET status='review', worker_status='review', card_json=? "
            "WHERE task_id=?",
            (json.dumps(card), "TASK_B891"),
        )
        conn.commit()
    finally:
        conn.close()
    return repo


def test_accept_review_promotes_and_finishes(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    result = task_engine.accept_review(
        repo,
        "TASK_B891",
        runner="codex_worker_b891",
        topic="task_mcp",
        request_id="req-accept-b1",
        evidence={"promoted_paths": ["out.txt"]},
    )
    assert result["ok"] is True

    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "finished"
    assert card["worker_status"] == "done"
    assert card["accepted_request_id"] == "req-accept-b1"
    events = task_store.get_task_events(repo, "TASK_B891")
    assert events[0]["event"] == "accept_review"


def test_accept_review_idempotent_retry_returns_already_accepted(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    first = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1",
    )
    assert first["ok"] is True
    retry = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1",
    )
    assert retry["ok"] is True
    assert json.loads(retry["stdout"])["already_accepted"] is True


def test_accept_review_rejects_different_request_after_finish(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    first = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1",
    )
    assert first["ok"] is True
    other = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-other",
    )
    assert other["ok"] is False
    assert "already_finished" in other["stderr"]


def test_accept_review_rejects_non_review_ready_substatus(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        card = json.loads(
            conn.execute(
                "SELECT card_json FROM tasks WHERE task_id=?", ("TASK_B891",)
            ).fetchone()[0]
        )
        card["terminal_review"]["substatus"] = "worker_failed"
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card), "TASK_B891"),
        )
        conn.commit()
    finally:
        conn.close()
    result = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1",
    )
    assert result["ok"] is False
    assert "terminal_substatus_not_review_ready" in result["stderr"]
    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "review"


def _repo_with_reviewer_children(
    tmp_path: Path, *, parent_task_id: str = "PARENT_T1",
    child_verified: str = "REVIEWER_V1", child_sibling: str = "REVIEWER_S1",
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-08T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    try:
        # Parent task in review_ready
        parent_card = {
            "task_id": parent_task_id,
            "runner": "worker_p1",
            "topic": "task_mcp",
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "request_identity": {
                        "request_id": "req-parent-1",
                        "task_id": parent_task_id,
                        "runner": "worker_p1",
                        "topic": "task_mcp",
                    },
                },
            },
        }
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, claimed_by, claimed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (parent_task_id, "worker_p1", "task_mcp", "review", "review",
             "", "", json.dumps(parent_card), now, now, "worker_p1", now),
        )
        # Verified reviewer child
        verified_card = {
            "task_id": child_verified,
            "runner": "reviewer_v1",
            "topic": "quality_review",
            "quality_review": {
                "target_task_id": parent_task_id,
                "target_request_id": "req-parent-1",
            },
        }
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, claimed_by, claimed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (child_verified, "reviewer_v1", "quality_review", "review", "review",
             "", "", json.dumps(verified_card), now, now, "reviewer_v1", now),
        )
        # Sibling (redundant) reviewer child
        sibling_card = {
            "task_id": child_sibling,
            "runner": "reviewer_s1",
            "topic": "quality_review",
            "quality_review": {
                "target_task_id": parent_task_id,
                "target_request_id": "req-parent-1",
            },
        }
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, claimed_by, claimed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (child_sibling, "reviewer_s1", "quality_review", "review", "review",
             "", "", json.dumps(sibling_card), now, now, "reviewer_s1", now),
        )
        conn.commit()
    finally:
        conn.close()
    return repo


def test_disposition_reviewer_children_finalizes_verified_and_supersedes_siblings(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    result = task_engine.disposition_reviewer_children(
        repo,
        "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    assert result["ok"] is True

    verified = task_store.get_task(repo, "REVIEWER_V1")
    assert verified is not None
    assert verified["status"] == "finished"
    assert verified["worker_status"] == "done"

    sibling = task_store.get_task(repo, "REVIEWER_S1")
    assert sibling is not None
    assert sibling["status"] == "superseded"
    assert sibling["worker_status"] == "superseded"


def test_disposition_reviewer_children_reads_terminal_review_evidence_binding(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", ("REVIEWER_V1",)
        ).fetchone()
        card = json.loads(row[0])
        binding = card.pop("quality_review")
        card["terminal_review"] = {"evidence": {"quality_review": binding}}
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card), "REVIEWER_V1"),
        )
        conn.commit()
    finally:
        conn.close()

    result = task_engine.disposition_reviewer_children(
        repo,
        "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    payload = json.loads(result["stdout"])
    assert payload["finalized"] == ["REVIEWER_V1"]
    verified = task_store.get_task(repo, "REVIEWER_V1")
    assert verified is not None
    assert verified["status"] == "finished"
    assert verified["worker_status"] == "done"


def test_disposition_reviewer_children_rejects_conflicting_durable_bindings(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", ("REVIEWER_V1",)
        ).fetchone()
        card = json.loads(row[0])
        card["terminal_review"] = {
            "evidence": {
                "quality_review": {
                    "target_task_id": "OTHER_PARENT",
                    "target_request_id": "req-other",
                }
            }
        }
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card), "REVIEWER_V1"),
        )
        conn.commit()
    finally:
        conn.close()

    result = task_engine.disposition_reviewer_children(
        repo,
        "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    payload = json.loads(result["stdout"])
    assert payload["finalized"] == []
    assert payload["errors"] == ["REVIEWER_V1:reviewer_binding_conflict"]
    verified = task_store.get_task(repo, "REVIEWER_V1")
    assert verified is not None
    assert verified["status"] == "review"


def test_disposition_reviewer_children_idempotent_retry(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    first = task_engine.disposition_reviewer_children(
        repo, "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    assert first["ok"] is True
    second = task_engine.disposition_reviewer_children(
        repo, "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    assert second["ok"] is True
    payload = json.loads(second["stdout"])
    assert "REVIEWER_V1" in payload["skipped"]
    assert "REVIEWER_S1" in payload["skipped"]


def test_disposition_reviewer_children_preserves_receipt_evidence(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    task_engine.disposition_reviewer_children(
        repo, "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    verified = task_store.get_task(repo, "REVIEWER_V1")
    assert verified is not None
    assert verified.get("topic") == "quality_review"
    qr = verified.get("quality_review") or {}
    assert qr.get("target_task_id") == "PARENT_T1"
    disposition_meta = verified.get("reviewer_disposition") or {}
    assert disposition_meta.get("parent_task_id") == "PARENT_T1"
    sibling = task_store.get_task(repo, "REVIEWER_S1")
    assert sibling is not None
    qr_sib = sibling.get("quality_review") or {}
    assert qr_sib.get("target_task_id") == "PARENT_T1"


def test_disposition_reviewer_children_ignores_non_quality_review_tasks(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    # Add a task with topic task_mcp that is NOT quality_review
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-08T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("IMPL_T1", "worker_i1", "task_mcp", "review", "review",
             "", "", json.dumps({"task_id": "IMPL_T1"}), now, now),
        )
        conn.commit()
    finally:
        conn.close()

    result = task_engine.disposition_reviewer_children(
        repo, "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    assert result["ok"] is True
    impl = task_store.get_task(repo, "IMPL_T1")
    assert impl is not None
    assert impl["status"] == "review"  # unchanged


def test_disposition_reviewer_children_ignores_request_mismatched_sibling(
    tmp_path: Path,
) -> None:
    repo = _repo_with_reviewer_children(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", ("REVIEWER_S1",)
        ).fetchone()
        card = json.loads(row[0])
        card["quality_review"]["target_request_id"] = "req-other-candidate"
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card), "REVIEWER_S1"),
        )
        conn.commit()
    finally:
        conn.close()

    result = task_engine.disposition_reviewer_children(
        repo,
        "PARENT_T1",
        verified_reviewer_task_ids=["REVIEWER_V1"],
        parent_request_id="req-parent-1",
        disposition="accepted",
    )
    assert result["ok"] is True
    sibling = task_store.get_task(repo, "REVIEWER_S1")
    assert sibling is not None
    assert sibling["status"] == "review"
    assert sibling["worker_status"] == "review"
