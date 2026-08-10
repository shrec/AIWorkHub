from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from _taskdb_compat import upsert_card
from aiworkhub import (
    context_graph,
    core,
    evidence_levels,
    feature_settings,
    learning_commit_store,
    manager_ai_tools,
    task_store,
)


SESSION_ID = "019f5097-6dbe-7172-870a-945afc5f3bfa"


def _manager_route(root: Path) -> dict:
    return {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "repo": str(root),
        "manager_route": {
            "provider": "codex",
            "session_id": SESSION_ID,
            "thread_id": SESSION_ID,
        },
    }


def _accepted_card(root: Path, *, task_id: str, request_id: str) -> None:
    record = evidence_levels.EvidenceRecord(
        evidence_level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
        severity="NONE",
        confidence="HIGH",
        reference=f"file:.aiworkhub/runtime/process_logs/attempt-artifacts/{request_id}/manifest.json",
        verified_by="codex",
        message="manager verified exact accepted outcome",
    ).to_dict()
    card = {
        "task_id": task_id,
        "runner": "worker",
        "topic": "learning",
        "mode": "edit",
        "status": "finished",
        "worker_status": "done",
        "accepted_request_id": request_id,
        "accept_evidence": {"acceptance_evidence_record": record},
    }
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    con.row_factory = sqlite3.Row
    try:
        upsert_card(con, card)
    finally:
        con.close()


def _setup(tmp_path: Path, monkeypatch) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    feature_settings.update(
        root,
        changes={"context_graph": True},
        expected_revision=0,
    )
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    task_id = "TASK-LEARNING-1"
    request_id = "request-learning-0001"
    _accepted_card(root, task_id=task_id, request_id=request_id)
    return root, task_id, request_id


def _commit(task_id: str, request_id: str) -> dict:
    return manager_ai_tools.learning_commit(
        task_id=task_id,
        request_id=request_id,
        repo_area="src/aiworkhub",
        outcome="accepted",
        evidence_ids=["file:tests/test_learning_commit_store.py"],
        idempotency_key="learning-manager-accepted-0001",
        provenance="manager acceptance regression test",
        root_cause_candidate="a stale result bypassed canonical identity",
        invariant_candidate="only exact manager-accepted evidence may become durable knowledge",
        lesson_candidate="gate learning promotion on the accepted request receipt",
        edge_candidates=[{
            "source": "stale result",
            "target": "canonical acceptance gate",
            "relation": "PREVENTED_BY",
        }],
        promote_ai_memory=True,
        promote_context_graph=True,
        promote_kb=True,
    )


def test_manager_learning_commit_projects_all_authorities_idempotently(
    tmp_path, monkeypatch,
):
    root, task_id, request_id = _setup(tmp_path, monkeypatch)

    first = _commit(task_id, request_id)
    second = _commit(task_id, request_id)

    assert first["ok"] is True
    assert first["state"] == "completed"
    assert set(first["projections"]) == {"session", "context_graph", "ai_memory", "kb"}
    assert all(row["state"] == "applied" for row in first["projections"].values())
    assert second["ok"] is True
    assert second["idempotent"] is True
    assert second["commit_id"] == first["commit_id"]

    memory = manager_ai_tools.ai_memory_get(
        key=f"learning.{task_id}.{first['commit_id'][:12]}"
    )
    kb = manager_ai_tools.kb_get(
        key=f"learning-contract.{task_id}.{first['commit_id'][:12]}"
    )
    graph = context_graph.search(root, "canonical acceptance gate", limit=10)
    assert memory["ok"] is True and memory["hit_count"] == 1
    assert kb["ok"] is True and kb["hit_count"] == 1
    assert graph["ok"] is True and graph["count"] >= 1
    source_node = "learning:" + hashlib.sha256(b"stale result").hexdigest()[:32]
    relations = context_graph.related(root, node_id=source_node)
    assert any(row["relation"] == "PREVENTED_BY" for row in relations["relations"])
    assert context_graph.rebuild_projection(root)["ok"] is True
    rebuilt = context_graph.related(root, node_id=source_node)
    assert any(row["relation"] == "PREVENTED_BY" for row in rebuilt["relations"])

    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    try:
        assert con.execute("SELECT COUNT(*) FROM learning_commits").fetchone()[0] == 1
    finally:
        con.close()


def test_learning_commit_rejects_unaccepted_or_mismatched_request(tmp_path, monkeypatch):
    _root, task_id, request_id = _setup(tmp_path, monkeypatch)

    result = manager_ai_tools.learning_commit(
        task_id=task_id,
        request_id=request_id + "-wrong",
        repo_area="src/aiworkhub",
        outcome="accepted",
        evidence_ids=[],
        idempotency_key="learning-manager-mismatch-0001",
        provenance="negative test",
    )

    assert result["ok"] is False
    assert result["error"] == "learning_commit_request_identity_mismatch"


def test_learning_commit_requires_explicit_content_for_promotions(tmp_path, monkeypatch):
    _root, task_id, request_id = _setup(tmp_path, monkeypatch)

    result = manager_ai_tools.learning_commit(
        task_id=task_id,
        request_id=request_id,
        repo_area="src/aiworkhub",
        outcome="accepted",
        evidence_ids=[],
        idempotency_key="learning-manager-empty-lesson-0001",
        provenance="negative test",
        promote_ai_memory=True,
    )

    assert result["ok"] is False
    assert result["error"] == "learning_commit_memory_promotion_requires_lesson"


def test_learning_commit_resumes_only_failed_projection(tmp_path, monkeypatch):
    root, task_id, request_id = _setup(tmp_path, monkeypatch)
    original = learning_commit_store.context_writes.memory_write
    attempts = 0

    def flaky_memory(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise learning_commit_store.context_writes.ContextWriteError("injected_once")
        return original(*args, **kwargs)

    monkeypatch.setattr(learning_commit_store.context_writes, "memory_write", flaky_memory)

    first = _commit(task_id, request_id)
    second = _commit(task_id, request_id)

    assert first["ok"] is False and first["state"] == "partial"
    assert first["projections"]["ai_memory"]["state"] == "failed"
    assert second["ok"] is True and second["state"] == "completed"
    assert second["projections"]["ai_memory"]["state"] == "applied"
    assert attempts == 2

    registry = task_store.storage_readiness(root)
    con = sqlite3.connect(registry.canonical_db)
    try:
        row = con.execute(
            "SELECT projections_json,state FROM learning_commits"
        ).fetchone()
        assert row is not None and row[1] == "completed"
    finally:
        con.close()
