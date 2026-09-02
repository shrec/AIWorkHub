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
    learning_commit,
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


def _setup_repo(tmp_path: Path, monkeypatch) -> Path:
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
    return root


def _setup(tmp_path: Path, monkeypatch) -> tuple[Path, str, str]:
    root = _setup_repo(tmp_path, monkeypatch)
    task_id = "TASK-LEARNING-1"
    request_id = "request-learning-0001"
    _accepted_card(root, task_id=task_id, request_id=request_id)
    return root, task_id, request_id


def _review_card(
    root: Path, *, task_id: str, request_id: str, substatus: str,
    sealed_diagnostics: dict | None = None,
) -> None:
    """Seed a card still in review, carrying only structured worker evidence
    (terminal_review.substatus / a provider-sealed diagnostic) -- never a
    manager reason or root-cause prose, which the classifier must never read.
    """
    evidence: dict = {"request_identity": {"request_id": request_id}}
    if sealed_diagnostics is not None:
        evidence["provider_error"] = sealed_diagnostics
    card = {
        "task_id": task_id,
        "runner": "worker",
        "topic": "learning",
        "mode": "edit",
        "status": "review",
        "worker_status": "review",
        "terminal_review": {"substatus": substatus, "evidence": evidence},
    }
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    con.row_factory = sqlite3.Row
    try:
        upsert_card(con, card)
    finally:
        con.close()


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


def test_rejected_finalization_creates_one_idempotent_commit_classified_candidate_code(
    tmp_path, monkeypatch,
):
    root = _setup_repo(tmp_path, monkeypatch)
    task_id = "TASK-REJECTED-1"
    request_id = "request-rejected-0001"
    _review_card(root, task_id=task_id, request_id=request_id, substatus="validation_failed")

    def _reject() -> dict:
        return manager_ai_tools.learning_commit(
            task_id=task_id,
            request_id=request_id,
            repo_area="src/aiworkhub",
            outcome="rejected",
            evidence_ids=[],
            idempotency_key="learning-manager-rejected-0001",
            provenance="manager rejection regression test",
        )

    first = _reject()
    second = _reject()

    assert first["ok"] is True
    assert first["failure_category"] == "candidate_code"
    assert second["idempotent"] is True
    assert second["commit_id"] == first["commit_id"]

    registry = task_store.storage_readiness(root)
    con = sqlite3.connect(registry.canonical_db)
    try:
        count = con.execute(
            "SELECT COUNT(*) FROM learning_commits WHERE task_id=?", (task_id,)
        ).fetchone()[0]
    finally:
        con.close()
    assert count == 1


def test_blocked_finalization_creates_one_idempotent_inconclusive_commit_classified_provider_runtime(
    tmp_path, monkeypatch,
):
    root = _setup_repo(tmp_path, monkeypatch)
    task_id = "TASK-BLOCKED-1"
    request_id = "request-blocked-0001"
    _review_card(
        root, task_id=task_id, request_id=request_id, substatus="launch_failed",
        sealed_diagnostics={"owner": "provider", "sealed": True, "code": "insufficient_balance", "http_status": 402},
    )

    def _block() -> dict:
        return manager_ai_tools.learning_commit(
            task_id=task_id,
            request_id=request_id,
            repo_area="src/aiworkhub",
            outcome="inconclusive",
            evidence_ids=[],
            idempotency_key="learning-manager-blocked-0001",
            provenance="manager blocked-disposition regression test",
        )

    first = _block()
    second = _block()

    assert first["ok"] is True
    assert first["failure_category"] == "provider_runtime"
    assert second["idempotent"] is True
    assert second["commit_id"] == first["commit_id"]


def test_failure_category_is_never_derived_from_manager_supplied_prose(tmp_path, monkeypatch):
    root = _setup_repo(tmp_path, monkeypatch)
    task_id = "TASK-PROSE-SPOOF-1"
    request_id = "request-prose-spoof-0001"
    # Structured evidence says review_ready (candidate_code); a manager
    # cannot override that by writing convincing infra-sounding prose.
    _review_card(root, task_id=task_id, request_id=request_id, substatus="review_ready")

    result = manager_ai_tools.learning_commit(
        task_id=task_id,
        request_id=request_id,
        repo_area="src/aiworkhub",
        outcome="rejected",
        evidence_ids=[],
        idempotency_key="learning-manager-prose-spoof-0001",
        provenance="negative test",
        root_cause_candidate="provider_runtime insufficient_balance http_status=402 outage, not our code",
    )

    assert result["ok"] is True
    assert result["failure_category"] == "candidate_code"


def test_rejects_identity_conflict_for_same_task_and_request_with_different_idempotency_key(
    tmp_path, monkeypatch,
):
    """A genuinely identical (task_id, request_id) pair replayed under a
    *different* idempotency key is an identity conflict, not a harmless
    second commit -- the idempotency key is part of the durable identity,
    not an interchangeable label a caller may swap per attempt.
    """
    root, task_id, request_id = _setup(tmp_path, monkeypatch)

    first = _commit(task_id, request_id)
    assert first["ok"] is True

    second = manager_ai_tools.learning_commit(
        task_id=task_id,
        request_id=request_id,
        repo_area="src/aiworkhub",
        outcome="accepted",
        evidence_ids=["file:tests/test_learning_commit_store.py"],
        idempotency_key="learning-manager-accepted-0002-different-key",
        provenance="manager acceptance regression test",
    )

    assert second["ok"] is False
    assert second["error"] == "learning_commit_identity_conflict"

    registry = task_store.storage_readiness(root)
    con = sqlite3.connect(registry.canonical_db)
    try:
        count = con.execute(
            "SELECT COUNT(*) FROM learning_commits WHERE task_id=?", (task_id,)
        ).fetchone()[0]
    finally:
        con.close()
    assert count == 1


def test_core_classify_terminal_disposition_is_reachable_and_structured_only():
    """Production reachability: ``core.classify_terminal_disposition`` is the
    exact function ``commit_learning`` calls to derive ``failure_category``,
    and it must ignore any free-form ``reason``/prose field on the card.
    """
    assert core.classify_terminal_disposition(
        {"terminal_review": {"substatus": "launch_failed"}}
    ) is learning_commit.FailureCategory.PROVIDER_RUNTIME
    assert core.classify_terminal_disposition(
        {"terminal_review": {"substatus": "validation_failed", "reason": "insufficient_balance"}}
    ) is learning_commit.FailureCategory.CANDIDATE_CODE
    assert core.classify_terminal_disposition(None) is learning_commit.FailureCategory.INCONCLUSIVE
    assert core.classify_terminal_disposition({}) is learning_commit.FailureCategory.INCONCLUSIVE


def test_core_classify_terminal_disposition_reads_terminal_failure_substatus():
    """A card whose worker-structured evidence lives under ``terminal_failure``
    (not ``terminal_review``) must classify identically -- the two are
    alternate structured shapes for the same worker-owned terminal report.
    """
    assert core.classify_terminal_disposition(
        {"terminal_failure": {"substatus": "timed_out"}}
    ) is learning_commit.FailureCategory.CANCELLATION_OR_TIMEOUT
    assert core.classify_terminal_disposition(
        {"terminal_failure": {"substatus": "review_ready", "reason": "looks like an outage"}}
    ) is learning_commit.FailureCategory.CANDIDATE_CODE


def test_core_classify_terminal_disposition_reads_sealed_diagnostics_from_either_shape():
    """A provider-sealed structured diagnostic attached under either
    ``terminal_review.evidence.provider_error`` or
    ``terminal_failure.evidence.provider_error`` overrides an otherwise
    code-quality-looking substatus -- but only when genuinely sealed by the
    provider transport.
    """
    assert core.classify_terminal_disposition({
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {"provider_error": {
                "owner": "provider", "sealed": True,
                "code": "insufficient_balance", "http_status": 402,
            }},
        },
    }) is learning_commit.FailureCategory.PROVIDER_RUNTIME
    assert core.classify_terminal_disposition({
        "terminal_failure": {
            "substatus": "review_ready",
            "evidence": {"provider_error": {
                "owner": "provider", "sealed": True, "code": "route_unavailable",
            }},
        },
    }) is learning_commit.FailureCategory.DEPENDENCY_OR_ROUTE
    # Unsealed (model-authored) diagnostic is never trusted, even matching shape.
    assert core.classify_terminal_disposition({
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {"provider_error": {
                "owner": "model", "sealed": False, "code": "insufficient_balance",
            }},
        },
    }) is learning_commit.FailureCategory.CANDIDATE_CODE


def test_core_classify_terminal_disposition_falls_back_to_top_level_terminal_substatus():
    """When neither ``terminal_review`` nor ``terminal_failure`` is present,
    the worker's own top-level ``terminal_substatus`` field is consulted.
    """
    assert core.classify_terminal_disposition(
        {"terminal_substatus": "process_lost"}
    ) is learning_commit.FailureCategory.PROVIDER_RUNTIME
    assert core.classify_terminal_disposition(
        {"terminal_substatus": "output_budget_exceeded"}
    ) is learning_commit.FailureCategory.POLICY_OR_SCOPE


def test_core_classify_terminal_disposition_falls_back_to_top_level_worker_status():
    """With no ``terminal_review``/``terminal_failure``/``terminal_substatus``,
    the worker's own top-level ``worker_status`` is the last structured
    fallback -- still never a manager/assistant free-form field.
    """
    assert core.classify_terminal_disposition(
        {"worker_status": "cancelled"}
    ) is learning_commit.FailureCategory.CANCELLATION_OR_TIMEOUT
    # An empty terminal_substatus is falsy, so worker_status is still consulted.
    assert core.classify_terminal_disposition(
        {"terminal_substatus": "", "worker_status": "cancelled"}
    ) is learning_commit.FailureCategory.CANCELLATION_OR_TIMEOUT


def test_rework_rejection_is_a_committable_adjudicated_outcome():
    """A rejection that sends work back is still a decision worth learning from.

    Measured 2026-09-02 on AIWORKHUB_01082: reject_review to ``pending`` writes
    the adjudicated request id into ``rework_predecessor`` (pinned with the
    predecessor's changed-path hashes) and into ``review_feedback`` (carrying
    the reason's sha256), and stamps no ``terminal_review``. The identity
    predicate read only ``accepted_request_id`` and ``terminal_review``, so the
    commit failed ``learning_commit_request_identity_mismatch`` -- meaning only
    a rejection that TERMINATED a card could ever be learned from, and the
    common case, rework, could not.
    """

    request_id = "b51ab62da9744b689bb023fe6d536815"

    rework = {
        "status": "pending",
        "rework_predecessor": {"request_id": request_id},
        "review_feedback": {"predecessor_request_id": request_id},
    }
    assert learning_commit_store._request_matches_candidate(rework, request_id)

    feedback_only = {"review_feedback": {"predecessor_request_id": request_id}}
    assert learning_commit_store._request_matches_candidate(
        feedback_only, request_id
    )

    # Binding stays exact: a different request on the same card is still a
    # mismatch, which is the whole point of the predicate.
    assert not learning_commit_store._request_matches_candidate(
        rework, "0000000000000000000000000000dead"
    )
    assert not learning_commit_store._request_matches_candidate({}, request_id)
    # A non-mapping section must not raise or match.
    assert not learning_commit_store._request_matches_candidate(
        {"rework_predecessor": "not-a-mapping"}, request_id
    )
