from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from aiworkhub import quality_reviewer as qr
from aiworkhub import process_launcher
from aiworkhub import worker_ai_tools_mcp as worker_tools
from aiworkhub import worker_workspace


def _packet() -> dict[str, object]:
    return qr.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=3,
        worker_provider="deepseek_v4pro",
        changed_path_hashes={"src/aiworkhub/core.py": "a" * 64},
        acceptance=["acceptance contract only"],
        required_outputs=["src/aiworkhub/core.py"],
        validation=[["python", "-m", "pytest", "-q"]],
        mechanical_checks=[
            {
                "check_id": "pytest",
                "kind": "test",
                "status": "passed",
                "provenance": "exact validation",
                "summary": "worker-provided prose must not survive normalization",
            }
        ],
    )


def test_read_only_research_reviewer_allows_no_repository_outputs() -> None:
    process_launcher._validate_required_outputs_contract(
            {
                "topic": "quality_review",
                "read_only": True,
                "allowed_writes": [],
            "required_outputs": [],
            "project_context": {"task_type": "research"},
        }
    )


def test_no_write_code_inspection_allows_no_repository_outputs() -> None:
    process_launcher._validate_required_outputs_contract(
            {
                "read_only": True,
                "allowed_writes": [],
            "required_outputs": [],
            "project_context": {"task_type": "code"},
        }
    )


def test_empty_outputs_still_fail_closed_when_write_scope_exists() -> None:
    with pytest.raises(process_launcher.LaunchRejected, match="required_outputs_invalid"):
        process_launcher._validate_required_outputs_contract(
            {
                "allowed_writes": ["src/example.py"],
                "required_outputs": [],
                "project_context": {"task_type": "code"},
            }
        )


def _receipt(packet: dict[str, object]) -> dict[str, object]:
    return {
        "schema_id": qr.RECEIPT_SCHEMA_ID,
        "packet_sha256": packet["packet_sha256"],
        "target": {
            "request_id": "target-request-1",
            "task_id": "TARGET_TASK_1",
            "claim_epoch": 3,
        },
        "reviewer": {
            "request_id": "review-request-1",
            "task_id": "REVIEW_TASK_1",
            "provider": "claude_sonnet5",
        },
        "report": {
            "lens": "correctness",
            "read_only": True,
            "can_mutate_repo": False,
            "findings": [],
        },
    }


def _verify(receipt: dict[str, object], packet: dict[str, object]) -> dict[str, object]:
    return qr.verify_reviewer_receipt(
        receipt,
        packet=packet,
        expected_reviewer_request_id="review-request-1",
        expected_reviewer_task_id="REVIEW_TASK_1",
        observed_provider="claude_sonnet5",
        observed_terminal_state="review_ready",
        audit_verified=True,
    )


def test_packet_is_deterministic_and_anti_anchored() -> None:
    first = _packet()
    second = _packet()
    assert first == second
    serialized = repr(first)
    assert "worker-provided prose" not in serialized
    assert "result" not in first
    assert "verdict" not in first
    assert "rationale" not in first


def test_receipt_is_bound_to_process_observed_identity() -> None:
    packet = _packet()
    verified = _verify(_receipt(packet), packet)
    assert verified["authority"] == {
        "process_identity_verified": True,
        "audit_verified": True,
        "terminal_state": "review_ready",
    }


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda row: row["reviewer"].update(provider="forged_provider"), "reviewer_provider_spoofed"),
        (lambda row: row["target"].update(claim_epoch=4), "reviewer_target_claim_epoch_mismatch"),
        (lambda row: row.update(packet_sha256="b" * 64), "reviewer_packet_digest_mismatch"),
    ],
)
def test_receipt_identity_and_digest_mismatches_fail_closed(mutation, error: str) -> None:
    packet = _packet()
    receipt = copy.deepcopy(_receipt(packet))
    mutation(receipt)
    with pytest.raises(qr.ReviewerEvidenceError, match=error):
        _verify(receipt, packet)


def test_tampered_packet_fails_before_receipt_is_trusted() -> None:
    packet = _packet()
    packet["contract"]["acceptance"] = ["changed after digest"]
    with pytest.raises(qr.ReviewerEvidenceError, match="review_packet_digest_invalid"):
        _verify(_receipt(packet), packet)


def test_unverified_audit_and_nonterminal_process_fail_closed() -> None:
    packet = _packet()
    receipt = _receipt(packet)
    with pytest.raises(qr.ReviewerEvidenceError, match="reviewer_audit_unverified"):
        qr.verify_reviewer_receipt(
            receipt,
            packet=packet,
            expected_reviewer_request_id="review-request-1",
            expected_reviewer_task_id="REVIEW_TASK_1",
            observed_provider="claude_sonnet5",
            observed_terminal_state="review_ready",
            audit_verified=False,
        )
    with pytest.raises(qr.ReviewerEvidenceError, match="reviewer_terminal_state_invalid"):
        qr.verify_reviewer_receipt(
            receipt,
            packet=packet,
            expected_reviewer_request_id="review-request-1",
            expected_reviewer_task_id="REVIEW_TASK_1",
            observed_provider="claude_sonnet5",
            observed_terminal_state="validation_failed",
            audit_verified=True,
        )


def _worker_context(tmp_path: Path, packet_path: Path | None) -> worker_tools.WorkerToolContext:
    ledger = tmp_path / "audit.jsonl"
    ledger.write_text("", encoding="utf-8")
    key = tmp_path / "audit.key"
    key.write_bytes(b"k" * 32)
    return worker_tools.WorkerToolContext(
        task_id="REVIEW_TASK_1",
        runner="claude_sonnet5",
        topic="quality_review",
        request_id="a" * 32,
        repo=tmp_path,
        authority_repo=tmp_path,
        source_graph_targets=(),
        session_topic="quality_review",
        audit_ledger_path=ledger,
        audit_hmac_key_path=key,
        quality_review_packet_path=packet_path,
    )


def test_worker_submission_is_packet_bound_and_hmac_audited(tmp_path: Path) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)
    result = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[],
    )
    assert result["ok"] is True
    audit = worker_tools.verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )
    assert audit["ok"] is True
    assert audit["call_count_by_tool"] == {"quality_review_submit": 1}
    assert len(audit["verified_payloads"]) == 1
    payload = audit["verified_payloads"][0]
    assert payload["target"] == {
        "request_id": "target-request-1",
        "task_id": "TARGET_TASK_1",
        "claim_epoch": 3,
    }
    assert payload["reviewer"] == {
        "request_id": "a" * 32,
        "task_id": "REVIEW_TASK_1",
    }

    workspace = worker_workspace.WorkerWorkspace(
        request_id=ctx.request_id,
        repo=tmp_path,
        path=tmp_path,
        home=tmp_path,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    verified = process_launcher._verified_quality_review_receipt(
        {
            "task_id": ctx.task_id,
            "runner": ctx.runner,
            "topic": ctx.topic,
            "adapter_id": "claude_cli",
            "worker_mcp": {
                "audit_ledger_path": str(ctx.audit_ledger_path),
                "audit_hmac_key_path": str(ctx.audit_hmac_key_path),
            },
            "quality_review": {
                "packet_path": str(packet_path),
                "lens": "correctness",
            },
        },
        workspace,
        ctx.request_id,
    )
    assert verified["reviewer"]["provider"] == "claude_cli"
    assert verified["report"]["provider"] == "claude_cli"


def test_ordinary_worker_cannot_submit_unbound_review(tmp_path: Path) -> None:
    ctx = _worker_context(tmp_path, None)
    result = worker_tools.quality_review_submit(
        ctx,
        packet_sha256="a" * 64,
        lens="correctness",
        findings=[],
    )
    assert result == {
        "ok": False,
        "tool": "quality_review_submit",
        "reason": "quality_review_packet_not_bound",
    }


def test_review_workspace_materializes_candidate_but_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "AIWorkHub Test"], cwd=repo, check=True)
    source_file = repo / "source.py"
    source_file.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))

    source = worker_workspace.create_workspace(
        repo,
        "a" * 32,
        {"allowed_writes": ["source.py"], "required_outputs": []},
        "validation",
    )
    (source.path / "source.py").write_text("value = 2\n", encoding="utf-8")
    review = None
    try:
        review, evidence = worker_workspace.create_quality_review_workspace(
            source,
            "b" * 32,
            ["source.py"],
            "validation",
        )
        assert review.allowed_writes == ()
        assert (review.path / "source.py").read_text(encoding="utf-8") == "value = 2\n"
        assert worker_workspace.enforce_scope(review) == []
        assert evidence["candidate_paths"] == ["source.py"]
        (review.path / "source.py").write_text("reviewer edit\n", encoding="utf-8")
        with pytest.raises(worker_workspace.WorkspaceError, match="scope_violation"):
            worker_workspace.enforce_scope(review)
    finally:
        if review is not None:
            worker_workspace.cleanup_workspace(repo, review.path, review.home)
        worker_workspace.cleanup_workspace(repo, source.path, source.home)
