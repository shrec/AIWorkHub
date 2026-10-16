"""NF-2026-01544 / NF-2026-01571: review-transition tail of the launcher.

A. The orchestrator may accept and archive a reviewer's own review_ready card
   while the launcher is still finalizing the same request; that re-read of the
   claim must not become ``claim_ownership_lost:state=archived``.
B. A candidate with zero changed paths seeds no review chain.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from test_aiworkhub_finalizer_canonical_authority_b863 import (  # noqa: E402,F401
    _bridge_chmod_sandbox_restriction,
    _metadata_and_events,
    _seed_task_row,
    _utcnow,
)

from aiworkhub import process_launcher, review_orchestrator, worker_workspace  # noqa: E402

RUNNER = "claude_reviewer_nf01544"
TOPIC = "task_mcp"


def _manager(tmp_path: Path, repo: Path) -> process_launcher.ProcessManager:
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        collision_guard=lambda **_: {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""},
        isolation_enabled=True,
    )


def _terminal_review(request_id: str, *, top_level: bool = False) -> dict:
    review: dict = {
        "substatus": "review_ready",
        "evidence": {"request_identity": {"request_id": request_id}},
    }
    if top_level:
        review["request_id"] = request_id
    return review


def _seed_archived(repo: Path, task_id: str, *, requested_by: str, request_id: str,
                   status: str = "archived", top_level: bool = False) -> None:
    _seed_task_row(
        repo, task_id=task_id, runner=RUNNER, topic=TOPIC, status=status,
        worker_status="archived", claimed_by=RUNNER,
        archived_at=_utcnow() if status == "archived" else "",
        card_json_extra={
            "review_requested_by": requested_by,
            "terminal_review": _terminal_review(request_id, top_level=top_level),
        },
    )


def _identity(task_id: str, request_id: str) -> dict:
    return {"task_id": task_id, "runner": RUNNER, "topic": TOPIC, "request_id": request_id}


@pytest.mark.parametrize("status", ["archived", "finished"])
@pytest.mark.parametrize("top_level", [False, True])
def test_exact_claim_state_keeps_own_review_after_orchestrator_disposed_it(tmp_path, status, top_level):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_archived(repo, "TASK_OWN", requested_by=RUNNER, request_id="req-own",
                   status=status, top_level=top_level)
    manager = _manager(tmp_path, repo)
    assert manager._exact_claim_state(_identity("TASK_OWN", "req-own")) == status


def test_exact_claim_state_follows_processing_review_archived(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = _manager(tmp_path, repo)
    meta = _identity("TASK_SEQ", "req-seq")
    _seed_task_row(repo, task_id="TASK_SEQ", runner=RUNNER, topic=TOPIC, status="processing",
                   worker_status="in_progress", claimed_by=RUNNER)
    assert manager._exact_claim_state(meta) == "processing"
    _seed_task_row(repo, task_id="TASK_SEQ", runner=RUNNER, topic=TOPIC, status="review",
                   worker_status="review_ready", claimed_by=RUNNER,
                   card_json_extra={"review_requested_by": RUNNER,
                                    "terminal_review": _terminal_review("req-seq")})
    assert manager._exact_claim_state(meta) == "review"
    _seed_archived(repo, "TASK_SEQ", requested_by=RUNNER, request_id="req-seq")
    assert manager._exact_claim_state(meta) == "archived"


@pytest.mark.parametrize(
    ("requested_by", "terminal_request", "status", "expected"),
    [
        ("another_runner", "req-x", "archived", "claim_ownership_lost:state=archived"),
        (RUNNER, "another_request", "archived", "claim_ownership_lost:state=archived"),
        (RUNNER, "", "archived", "claim_ownership_lost:state=archived"),
        ("another_runner", "req-x", "finished", "claim_ownership_lost:state=finished"),
        (RUNNER, "another_request", "finished", "claim_ownership_lost:state=finished"),
    ],
)
def test_exact_claim_state_still_raises_for_foreign_review(
    tmp_path, requested_by, terminal_request, status, expected
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_archived(repo, "TASK_FOREIGN", requested_by=requested_by,
                   request_id=terminal_request, status=status)
    manager = _manager(tmp_path, repo)
    with pytest.raises(process_launcher.WorkspaceError) as caught:
        manager._exact_claim_state(_identity("TASK_FOREIGN", "req-x"))
    assert str(caught.value) == expected


@pytest.mark.parametrize("status", ["archived", "finished"])
@pytest.mark.parametrize(
    ("top_request", "nested_request"),
    [
        ("another_request", "req-x"),
        ("req-x", "another_request"),
        ("another_request", ""),
    ],
)
def test_exact_claim_state_raises_when_top_level_binding_is_foreign(
    tmp_path, status, top_request, nested_request
):
    repo = tmp_path / "repo"
    repo.mkdir()
    review = _terminal_review(nested_request)
    review["request_id"] = top_request
    _seed_task_row(
        repo, task_id="TASK_TOP", runner=RUNNER, topic=TOPIC, status=status,
        worker_status="archived", claimed_by=RUNNER,
        archived_at=_utcnow() if status == "archived" else "",
        card_json_extra={"review_requested_by": RUNNER, "terminal_review": review},
    )
    manager = _manager(tmp_path, repo)
    with pytest.raises(process_launcher.WorkspaceError) as caught:
        manager._exact_claim_state(_identity("TASK_TOP", "req-x"))
    assert str(caught.value) == f"claim_ownership_lost:state={status}"


def test_exact_claim_state_still_raises_for_identity_and_pending(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = _manager(tmp_path, repo)
    _seed_task_row(repo, task_id="TASK_PEND", runner=RUNNER, topic=TOPIC, status="pending",
                   worker_status="queued", claimed_by=RUNNER,
                   card_json_extra={"review_requested_by": RUNNER,
                                    "terminal_review": _terminal_review("req-x")})
    with pytest.raises(process_launcher.WorkspaceError, match="claim_ownership_lost:state=pending"):
        manager._exact_claim_state(_identity("TASK_PEND", "req-x"))
    other_runner = dict(_identity("TASK_PEND", "req-x"), runner="someone_else")
    with pytest.raises(process_launcher.WorkspaceError, match="claim_ownership_lost:task_identity_changed"):
        manager._exact_claim_state(other_runner)


def _patch_common(monkeypatch, manager, *, changed):
    monkeypatch.setattr(process_launcher, "enforce_scope", lambda *a, **k: list(changed))
    monkeypatch.setattr(manager, "_consume_terminal_authority_grant", lambda *a, **k: True)
    monkeypatch.setattr(manager, "_persist_attempt_artifacts", lambda *a, **k: {"manifest_sha256": "m" * 64})
    monkeypatch.setattr(manager, "_canonical_outcome_evidence", lambda *a, **k: {})


def _with_metadata(metadata_path: Path, **extra) -> None:
    import json

    data = json.loads(metadata_path.read_text(encoding="utf-8"))
    data.update(extra)
    worker_workspace.write_json_0600(metadata_path, data)


def test_reviewer_attempt_survives_orchestrator_archive_of_its_review_card(tmp_path, monkeypatch):
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, request_id = "TASK_RV_RACE", "req-rv-race"
    _seed_archived(repo, task_id, requested_by=RUNNER, request_id=request_id)
    manager = _manager(tmp_path, repo)
    _, _, metadata_path = _metadata_and_events(
        manager, tmp_path, request_id=request_id, task_id=task_id, runner=RUNNER, topic=TOPIC,
    )
    _with_metadata(metadata_path, quality_review={"lens": "correctness", "target_task_id": "T",
                                                  "target_request_id": "R"})
    _patch_common(monkeypatch, manager, changed=[])
    # A recognisable report, so "bound" cannot be met by a default or empty one.
    sentinel = {"report": {"findings": [{"id": "sentinel-nf01544"}]}, "receipt_sha256": "s" * 64}
    monkeypatch.setattr(process_launcher, "_verified_quality_review_receipt",
                        lambda *a, **k: sentinel)
    persisted: list[dict] = []
    monkeypatch.setattr(
        manager, "_persist_attempt_artifacts",
        lambda *a, **k: persisted.append(k.get("review") or {}) or {"manifest_sha256": "m" * 64},
    )
    real_terminal = manager._review_terminal_exact
    terminal_evidence: list[dict] = []

    def _capture_terminal(metadata, state, **kwargs):
        terminal_evidence.append(kwargs.get("evidence") or {})
        return real_terminal(metadata, state, **kwargs)

    monkeypatch.setattr(manager, "_review_terminal_exact", _capture_terminal)

    event = manager._finalize_isolated_request(request_id)

    assert event is not None
    assert event["state"] == "review_ready", event
    assert not str(event.get("error") or ""), event
    assert "claim_ownership_lost" not in str(event)
    assert [review.get("quality_review_receipt") for review in persisted] == [sentinel]
    assert [evidence.get("quality_review_receipt") for evidence in terminal_evidence] == [sentinel]


def test_reviewer_attempt_with_foreign_archived_card_still_loses_ownership(tmp_path, monkeypatch):
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, request_id = "TASK_RV_FOREIGN", "req-rv-foreign"
    _seed_archived(repo, task_id, requested_by=RUNNER, request_id="a_different_request")
    manager = _manager(tmp_path, repo)
    _, _, metadata_path = _metadata_and_events(
        manager, tmp_path, request_id=request_id, task_id=task_id, runner=RUNNER, topic=TOPIC,
    )
    _with_metadata(metadata_path, quality_review={"lens": "correctness"})
    _patch_common(monkeypatch, manager, changed=[])

    event = manager._finalize_isolated_request(request_id)

    assert event["error"] == "claim_ownership_lost", event
    assert event["state"] == "finalize_failed", event


def _finalize_candidate(tmp_path, monkeypatch, changed_path_hashes, calls):
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, request_id = "TASK_CAND", "req-cand"
    _seed_task_row(repo, task_id=task_id, runner=RUNNER, topic=TOPIC, status="processing",
                   worker_status="in_progress", claimed_by=RUNNER)
    manager = _manager(tmp_path, repo)
    _metadata_and_events(manager, tmp_path, request_id=request_id, task_id=task_id,
                         runner=RUNNER, topic=TOPIC)
    paths = sorted(changed_path_hashes)
    _patch_common(monkeypatch, manager, changed=paths)
    monkeypatch.setattr(process_launcher, "validate_residual_contract", lambda *a, **k: {})
    monkeypatch.setattr(process_launcher, "validate_required_outputs", lambda *a, **k: [])
    monkeypatch.setattr(process_launcher, "_worker_mcp_live_call_gate", lambda *a, **k: {})
    monkeypatch.setattr(process_launcher, "_run_declared_validations", lambda *a, **k: [])
    monkeypatch.setattr(process_launcher, "_run_full_snapshot_validations", lambda *a, **k: ([], None))
    monkeypatch.setattr(process_launcher, "_rework_no_delta_refusal", lambda *a, **k: "")
    monkeypatch.setattr(process_launcher, "_enforce_behavioral_gate", lambda *a, **k: None)
    monkeypatch.setattr(process_launcher, "_metadata_is_readonly_research", lambda *a, **k: True)
    monkeypatch.setattr(process_launcher, "_readonly_research_result_evidence",
                        lambda *a, **k: {"meaningful_output": True})
    monkeypatch.setattr(process_launcher.quality_evidence, "run_review_ready_quality_gate",
                        lambda *a, **k: {"passed": True, "blocking_checks": []})
    monkeypatch.setattr(manager, "_candidate_reachability_inputs", lambda *a, **k: {})
    monkeypatch.setattr(
        "aiworkhub.successful_rework_recovery.successful_candidate_evidence",
        lambda workspace, metadata, request_id, changed: (list(changed), dict(changed_path_hashes), {}),
    )
    monkeypatch.setattr(manager, "_review_terminal_exact", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(review_orchestrator, "candidate_registration",
                        lambda **k: calls.append("registration") or {"candidate": "c"})
    monkeypatch.setattr(review_orchestrator, "canonical_review_db", lambda *a, **k: tmp_path / "review.db")

    class _Chain:
        chain_identity_sha256 = "c" * 64

    monkeypatch.setattr(review_orchestrator, "register_candidate",
                        lambda *a, **k: calls.append("register") or _Chain())
    return manager._finalize_isolated_request(request_id)


def test_zero_change_candidate_seeds_no_review_chain(tmp_path, monkeypatch):
    calls: list[str] = []
    event = _finalize_candidate(tmp_path, monkeypatch, {}, calls)
    assert event["state"] == "review_ready", event
    assert calls == []
    assert event["review_automation"] == {"state": "skipped", "reason": "no_repository_change"}


def test_one_path_candidate_still_seeds_review_chain(tmp_path, monkeypatch):
    calls: list[str] = []
    event = _finalize_candidate(tmp_path, monkeypatch, {"out/result.txt": "a" * 64}, calls)
    assert event["state"] == "review_ready", event
    assert calls == ["registration", "register"]
    assert event["review_automation"]["state"] == "seeded"
    assert event["review_automation"]["chain_identity_sha256"] == "c" * 64
