"""Regression tests for NF-2026-01197 (field report EntryLink NF-2026-00047).

A learning commit's "rejected" outcome must bind to a LANDED manager
disposition for the exact request being committed -- the ``review_feedback``
/ ``rework_predecessor`` / ``rejection_disposition`` blocks ``core.reject_review``
writes in its own transaction -- never to the ``terminal_review`` stamp the
FINALIZER writes for any terminal request (``validation_failed``,
``worker_failed``, ``finalize_failed``) whether or not a manager ever acted on
it, and never to a pin left by an OLDER rejection episode of the same card.

These tests exercise the real ``core.reject_review`` transition (not a
hand-built fixture standing in for its output), per the task contract's
instruction to reproduce with real transitions through the store/core API.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from _taskdb_compat import upsert_card
from aiworkhub import (
    core,
    evidence_levels,
    feature_settings,
    learning_commit_store,
    manager_ai_tools,
    task_store,
)


SESSION_ID = "019f6a21-3b4d-7e3a-8d2b-7c1f9a9b0c01"


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


def _coordinator_env(root: Path, tmp_path: Path, monkeypatch) -> None:
    """Grant the in-process write gate the coordinator capability that the
    canonical rejection path requires, so these tests exercise the real
    ``core.reject_review`` rather than a stand-in.

    The token file is intentionally left at its default (umask) permissions:
    tightening them with ``os.chmod`` is a test-hygiene step, not something
    ``core.reject_review``'s gate reads back, and ``chmod`` is unavailable in
    some sandboxes this suite runs in.
    """
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token = tmp_path / "coordinator.token"
    token.write_text("coord-token\n", encoding="utf-8")
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coord-token")


def _rejectable_card(
    root: Path, *, task_id: str, request_id: str, substatus: str,
) -> None:
    """Seed a card in review whose only cause evidence is structured -- the
    exact shape the FINALIZER alone produces, with no manager action taken.
    """
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    con.row_factory = sqlite3.Row
    try:
        upsert_card(con, {
            "task_id": task_id,
            "runner": "claude_coding",
            "topic": "coding",
            "mode": "solo",
            "status": "review",
            "worker_status": "review",
            "terminal_review": {
                "substatus": substatus,
                "evidence": {"request_identity": {"request_id": request_id}},
            },
        })
    finally:
        con.close()


def _learning_commits_for(root: Path, task_id: str) -> list[str]:
    registry = task_store.storage_readiness(root)
    con = sqlite3.connect(registry.canonical_db)
    try:
        if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_commits'"
        ).fetchone() is None:
            return []
        rows = con.execute(
            "SELECT request_id FROM learning_commits WHERE task_id=?", (task_id,)
        ).fetchall()
    finally:
        con.close()
    return [row[0] for row in rows]


def test_terminalized_request_with_no_landed_manager_reject_is_not_adjudicated(
    tmp_path, monkeypatch,
):
    """Fails before the fix: a request that only ever reached the finalizer's
    ``terminal_review`` stamp, with no manager reject transition landed, must
    not resolve as "rejected". ``adjudicated_decision`` answers "" (unknown),
    both the short form and the raw commit are refused by name, and nothing
    is recorded -- the ``learning_commits`` table (the canonical receipt/
    outbox every projection is driven from) stays empty, so nothing further
    downstream in session, AI memory or KB can have been written either.
    """
    root = _setup_repo(tmp_path, monkeypatch)
    task_id = "TASK-NF01197-NOREJECT-1"
    request_id = "request-nf01197-noreject-0001"
    _rejectable_card(root, task_id=task_id, request_id=request_id, substatus="validation_failed")

    card = task_store.get_task(root, task_id)
    assert card is not None
    assert learning_commit_store.adjudicated_decision(card, request_id) == ""

    with pytest.raises(
        learning_commit_store.LearningCommitStoreError,
        match="learning_commit_request_identity_mismatch",
    ):
        learning_commit_store.resolve_short_form(
            root, task_id=task_id, request_id=request_id,
        )

    result = manager_ai_tools.learning_commit(
        task_id=task_id,
        request_id=request_id,
        repo_area="src/aiworkhub",
        outcome="rejected",
        evidence_ids=[],
        idempotency_key="learning-nf01197-noreject-0001",
        provenance="regression test",
    )
    assert result["ok"] is False
    assert result["error"] == "learning_commit_request_identity_mismatch"
    assert _learning_commits_for(root, task_id) == []


def test_stale_rejection_pin_from_an_older_request_is_never_borrowed_by_a_newer_one(
    tmp_path, monkeypatch,
):
    """Fails before the fix: a card rejected once for request R1 (category
    C1) is later relaunched; its new request R2 terminalizes but R2's own
    reject transition never lands (refused, or simply never attempted) --
    the card keeps only R1's rejection_disposition/review_feedback/
    rework_predecessor. A commit for R2 must be refused and must never carry
    C1; a commit for R1 must still resolve as rejected with C1.
    """
    root = _setup_repo(tmp_path, monkeypatch)
    _coordinator_env(root, tmp_path, monkeypatch)
    task_id = "TASK-NF01197-STALEPIN-1"
    r1 = "request-nf01197-stalepin-r1-0001"
    r2 = "request-nf01197-stalepin-r2-0002"

    _rejectable_card(root, task_id=task_id, request_id=r1, substatus="validation_failed")
    rejected = core.reject_review(task_id, "candidate code defect in r1", to="pending")
    assert rejected["ok"] is True, rejected

    card = task_store.get_task(root, task_id)
    assert card is not None
    pin = card["rejection_disposition"]
    assert pin["request_id"] == r1
    assert pin["failure_category"] == "candidate_code"

    # R2 is a later attempt on the SAME card: the finalizer stamps its own
    # terminal_review, but the manager's reject for R2 never lands, so no
    # rejection_disposition/review_feedback/rework_predecessor of R2's own is
    # ever written -- the card still carries only R1's.
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    con.row_factory = sqlite3.Row
    try:
        card["terminal_review"] = {
            "substatus": "worker_failed",
            "evidence": {"request_identity": {"request_id": r2}},
        }
        card["status"] = "review"
        card["worker_status"] = "review"
        upsert_card(con, card)
    finally:
        con.close()

    card = task_store.get_task(root, task_id)
    assert card is not None
    assert learning_commit_store.adjudicated_decision(card, r2) == ""
    assert learning_commit_store.adjudicated_decision(card, r1) == "rejected"

    r2_commit = manager_ai_tools.learning_commit(
        task_id=task_id,
        request_id=r2,
        repo_area="src/aiworkhub",
        outcome="rejected",
        evidence_ids=[],
        idempotency_key="learning-nf01197-stalepin-r2-0001",
        provenance="regression test",
    )
    assert r2_commit["ok"] is False
    assert r2_commit["error"] == "learning_commit_request_identity_mismatch"

    r1_commit = manager_ai_tools.learning_commit(
        task_id=task_id,
        request_id=r1,
        repo_area="src/aiworkhub",
        outcome="rejected",
        evidence_ids=[],
        idempotency_key="learning-nf01197-stalepin-r1-0001",
        provenance="regression test",
    )
    assert r1_commit["ok"] is True
    assert r1_commit["failure_category"] == "candidate_code"

    assert _learning_commits_for(root, task_id) == [r1]


def test_manager_rejection_back_to_rework_still_resolves_as_rejected(
    tmp_path, monkeypatch,
):
    """Positive case: ``reject_review --to pending`` never stamps
    ``terminal_review`` at all, so this is the path the OLD code could only
    reach through ``review_feedback``/``rework_predecessor`` -- it must keep
    resolving as rejected after the fix.
    """
    root = _setup_repo(tmp_path, monkeypatch)
    _coordinator_env(root, tmp_path, monkeypatch)
    task_id = "TASK-NF01197-REWORK-1"
    request_id = "request-nf01197-rework-0001"
    _rejectable_card(root, task_id=task_id, request_id=request_id, substatus="review_ready")

    rejected = core.reject_review(task_id, "a genuine candidate defect", to="pending")
    assert rejected["ok"] is True, rejected

    card = task_store.get_task(root, task_id)
    assert card is not None
    assert card.get("terminal_review") is None
    assert learning_commit_store.adjudicated_decision(card, request_id) == "rejected"

    result = manager_ai_tools.learning_commit(
        task_id=task_id,
        request_id=request_id,
        repo_area="src/aiworkhub",
        outcome="rejected",
        evidence_ids=[],
        idempotency_key="learning-nf01197-rework-0001",
        provenance="regression test",
    )
    assert result["ok"] is True
    assert result["failure_category"] == "candidate_code"


def test_manager_rejection_that_terminates_the_card_still_resolves_as_rejected(
    tmp_path, monkeypatch,
):
    """Positive case: a manager rejection that TERMINATES the card (parked
    ``blocked`` for an infrastructure cause, never routed back to rework)
    must still resolve as rejected. It binds to the ``rejection_disposition``
    pin ``core.reject_review`` writes for every disposition -- the field that
    distinguishes a manager-authored rejection from a finalizer-authored
    terminal_review -- not to the substatus string or to terminal_review,
    which this disposition may leave untouched.
    """
    root = _setup_repo(tmp_path, monkeypatch)
    _coordinator_env(root, tmp_path, monkeypatch)
    task_id = "TASK-NF01197-BLOCKED-1"
    request_id = "request-nf01197-blocked-0001"
    _rejectable_card(root, task_id=task_id, request_id=request_id, substatus="review_ready")

    rejected = core.reject_review(
        task_id, "infrastructure dependency unavailable", to="blocked",
        failure_category="dependency_or_route",
    )
    assert rejected["ok"] is True, rejected

    card = task_store.get_task(root, task_id)
    assert card is not None
    assert card.get("status") == "blocked"
    assert card["rejection_disposition"]["request_id"] == request_id
    assert learning_commit_store.adjudicated_decision(card, request_id) == "rejected"

    result = manager_ai_tools.learning_commit(
        task_id=task_id,
        request_id=request_id,
        repo_area="src/aiworkhub",
        outcome="rejected",
        evidence_ids=[],
        idempotency_key="learning-nf01197-blocked-0001",
        provenance="regression test",
    )
    assert result["ok"] is True
    assert result["failure_category"] == "dependency_or_route"


def test_accepted_finished_card_still_resolves_as_accepted(tmp_path, monkeypatch):
    """Positive case: acceptance is untouched by the rejection-binding fix --
    it is gated by ``accepted_request_id`` plus the finished lifecycle, never
    by ``_landed_manager_rejection``.
    """
    root = _setup_repo(tmp_path, monkeypatch)
    task_id = "TASK-NF01197-ACCEPTED-1"
    request_id = "request-nf01197-accepted-0001"
    record = evidence_levels.EvidenceRecord(
        evidence_level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
        severity="NONE",
        confidence="HIGH",
        reference=f"file:.aiworkhub/runtime/process_logs/attempt-artifacts/{request_id}/manifest.json",
        verified_by="codex",
        message="manager verified exact accepted outcome",
    ).to_dict()
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    con.row_factory = sqlite3.Row
    try:
        upsert_card(con, {
            "task_id": task_id,
            "runner": "worker",
            "topic": "learning",
            "mode": "edit",
            "status": "finished",
            "worker_status": "done",
            "accepted_request_id": request_id,
            "accept_evidence": {"acceptance_evidence_record": record},
        })
    finally:
        con.close()

    card = task_store.get_task(root, task_id)
    assert card is not None
    assert learning_commit_store.adjudicated_decision(card, request_id) == "accepted"

    result = manager_ai_tools.learning_commit(
        task_id=task_id,
        request_id=request_id,
        repo_area="src/aiworkhub",
        outcome="accepted",
        evidence_ids=["file:tests/test_learning_commit_decision_binding_nf01197.py"],
        idempotency_key="learning-nf01197-accepted-0001",
        provenance="regression test",
    )
    assert result["ok"] is True
