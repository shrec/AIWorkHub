from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest


_R4_KEY = b"r4-audit-ledger-key"


def _r4_signed_line(
    entry: dict[str, object], *, key: bytes = _R4_KEY, tamper: bool = False
) -> str:
    body = dict(entry)
    digest = "0" * 64 if tamper else worker_tools._hmac_entry(body, key)
    return json.dumps(
        {**body, "hmac_sha256": digest}, ensure_ascii=False, sort_keys=True
    ) + "\n"


def _r4_ledger(tmp_path: Path, lines: list[str]) -> tuple[Path, Path]:
    key_path = tmp_path / "r4_audit_hmac.key"
    key_path.write_bytes(_R4_KEY)
    ledger = tmp_path / "r4_audit_ledger.jsonl"
    ledger.write_text("".join(lines), encoding="utf-8")
    return ledger, key_path


def _r4_entry(
    payload: dict[str, object] | None = None, **extra: object
) -> dict[str, object]:
    entry: dict[str, object] = {
        "schema_id": worker_tools.AUDIT_ENTRY_SCHEMA_ID,
        "task_id": "TARGET_TASK_1",
        "runner": "glm-5.3",
        "topic": "review_reliability",
        "request_id": "target-request-1",
        "tool": "quality_review_submit",
        "ok": True,
        "cache_hit": False,
        "hit_count": 1,
        "bytes_returned": 256,
        "violation": "",
        "authority_source": "rework_overlay",
        "authority_state": "request_scoped_worktree",
        "authority_repo": "/home/shrek/AIWorkHub",
    }
    if payload is not None:
        entry["payload"] = dict(payload)
    entry.update(extra)
    return entry


def _r4_verify(ledger: Path, key_path: Path) -> dict[str, object]:
    return worker_tools.verify_audit_ledger(
        ledger,
        key_path,
        task_id="TARGET_TASK_1",
        runner="glm-5.3",
        topic="review_reliability",
    )


def _r4_finding(payload: object) -> object:
    if isinstance(payload, dict):
        return payload.get("finding", payload)
    return payload


def test_finding_input_normalizer_json_object_string_normalizes_once(
    tmp_path: Path,
) -> None:
    string_finding = {
        "id": "string-finding",
        "severity": "high",
        "summary": "gate",
        "evidence": "src/aiworkhub/core.py:7",
        "path": "src/aiworkhub/core.py",
        "line_start": 7,
        "line_end": 7,
        "symbol": "src/aiworkhub/core.py.target",
        "confidence": "high",
        "evidence_level": "reproduced",
        "required_validation": "run the focused regression",
    }
    object_finding = {**string_finding, "id": "object-finding"}
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)
    result = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[json.dumps(string_finding, sort_keys=True), object_finding],
    )
    assert result["ok"] is True, result
    report = worker_tools.verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )
    findings = report["verified_payloads"][0]["report"]["findings"]
    assert len(findings) == 2
    assert [item["summary"] for item in findings] == ["gate", "gate"]


def test_finding_input_normalizer_rejects_malformed_scalar_nested_double_encoded(
    tmp_path: Path,
) -> None:
    bad_values = [
        "{not-json",
        "42",
        json.dumps(json.dumps({"severity": "high"})),
        json.dumps([{"severity": "high"}]),
    ]
    for index, bad in enumerate(bad_values):
        case = tmp_path / str(index)
        case.mkdir()
        packet = _packet()
        packet_path = case / "review_packet.json"
        packet_path.write_text(json.dumps(packet), encoding="utf-8")
        ctx = _worker_context(case, packet_path)
        result = worker_tools.quality_review_submit(
            ctx,
            packet_sha256=str(packet["packet_sha256"]),
            lens="correctness",
            findings=[bad],
        )
        assert result["ok"] is False
        assert result["reason"] == "finding_json_object_string_invalid"
        report = worker_tools.verify_audit_ledger(
            ctx.audit_ledger_path,
            ctx.audit_hmac_key_path,
            task_id=ctx.task_id,
            runner=ctx.runner,
            topic=ctx.topic,
            request_id=ctx.request_id,
        )
        assert report["verified_payloads"] == []


def test_forged_or_wrong_identity_rejections_never_satisfy_nonempty_finding_gate(
    tmp_path: Path,
) -> None:
    finding = json.dumps(
        {"severity": "high", "summary": "forged", "evidence": "exact line"}
    )
    ledger, key_path = _r4_ledger(
        tmp_path,
        [
            _r4_signed_line(
                _r4_entry(payload={"finding": finding, "intent": "reject"}),
                tamper=True,
            ),
            _r4_signed_line(
                _r4_entry(payload={"finding": finding}, task_id="OTHER_TASK_1")
            ),
        ],
    )
    report = _r4_verify(ledger, key_path)
    assert report["entries_tampered"] == 1
    assert report["entries_verified"] == 0
    assert report["verified_payloads"] == []


def test_rejected_nonempty_intent_then_valid_leaves_one_verified_payload(
    tmp_path: Path,
) -> None:
    finding = {
        "id": "late-finding",
        "severity": "medium",
        "summary": "late",
        "evidence": "src/aiworkhub/core.py:7",
        "path": "src/aiworkhub/core.py",
        "line_start": 7,
        "line_end": 7,
        "symbol": "src/aiworkhub/core.py.target",
        "confidence": "high",
        "evidence_level": "reproduced",
        "required_validation": "run the focused regression",
    }
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)
    rejected = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=["{not-json"],
    )
    assert rejected["ok"] is False
    accepted = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[dict(finding)],
    )
    assert accepted["ok"] is True, accepted
    report = worker_tools.verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )
    payloads = report["verified_payloads"]
    assert len(payloads) == 1
    assert payloads[0]["report"]["findings"][0]["summary"] == "late"


def test_rejected_nonempty_intent_then_empty_finding_fails_closed(
    tmp_path: Path,
) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)
    rejected = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=["{not-json"],
    )
    assert rejected["ok"] is False
    empty = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[],
    )
    assert empty == {
        "ok": False,
        "tool": "quality_review_submit",
        "reason": "quality_review_empty_after_rejected_intent",
        "rejected_intent_authenticated": True,
        "rejected_intent_sha256": empty["rejected_intent_sha256"],
    }
    report = worker_tools.verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )
    assert report["verified_payloads"] == []


def test_invalid_hmac_forged_counts_every_line_regardless_of_payload_shape(
    tmp_path: Path,
) -> None:
    shapes = [
        {"finding": {"severity": "high", "summary": "s", "evidence": "e"}},
        {"finding": json.dumps({"severity": "high", "summary": "s"})},
        {"finding": "{not-json"},
        {"finding": "42"},
        {"finding": json.dumps(json.dumps({"severity": "high"}))},
        {},
    ]
    ledger, key_path = _r4_ledger(
        tmp_path,
        [
            *[
                _r4_signed_line(_r4_entry(payload=dict(shape)), tamper=True)
                for shape in shapes
            ],
            _r4_signed_line(
                _r4_entry(
                    payload={
                        "finding": json.dumps(
                            {"severity": "low", "summary": "s", "evidence": "e"}
                        )
                    }
                )
            ),
        ],
    )
    report = _r4_verify(ledger, key_path)
    assert report["entries_tampered"] == len(shapes)
    assert len(report["verified_payloads"]) == 1

from aiworkhub import quality_reviewer as qr
from aiworkhub import process_launcher
from aiworkhub import worker_ai_tools_mcp as worker_tools
from aiworkhub import worker_workspace
from aiworkhub.evidence_levels import EvidenceLevel
from aiworkhub.scoped_audit import (
    ChangedPath,
    KnownUnknown,
    ReviewLens,
    ScopedAuditPacket,
    TargetSymbol,
    ValidationExpectation,
)


def _scoped_audits() -> dict[str, ScopedAuditPacket]:
    return {
        lens: ScopedAuditPacket(
            packet_id=f"scope-{lens}",
            task_id="TARGET_TASK_1",
            created_at="2026-08-10T00:00:00Z",
            target_symbols=(
                TargetSymbol("src/aiworkhub/core.py.target", "function"),
            ),
            changed_paths=(
                ChangedPath("src/aiworkhub/core.py", "modified", 1, 10),
            ),
            forbidden_changes=(),
            invariants=("acceptance contract only",),
            impact_evidence=(),
            test_evidence=(),
            contract_evidence=(),
            prior_lessons=(),
            review_lens=ReviewLens(
                lens,
                f"Apply the {lens} lens.",
                EvidenceLevel.STATIC_EVIDENCE,
            ),
            unknowns=(
                KnownUnknown(
                    "impact-unresolved",
                    "What consumers exist?",
                    "No graph fixture is required for this contract test.",
                ),
            ),
            validation_expectations=(
                ValidationExpectation(
                    "expect-pytest",
                    "unit",
                    "python3 -m pytest -q",
                    "return code 0",
                ),
            ),
        )
        for lens in ("correctness", "security", "code_quality")
    }


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
        terminal_validation=[
            {
                "declared_command": "python -m pytest -q",
                "executed_argv": ["python3", "-m", "pytest", "-q"],
                "returncode": 0,
                "stdout_truncated": False,
                "stderr_truncated": False,
            }
        ],
        mechanical_checks=[
            {
                "check_id": "pytest",
                "kind": "test",
                "status": "passed",
                "provenance": "exact validation",
                "summary": "worker-provided prose must not survive normalization",
            }
        ],
        scoped_audits=_scoped_audits(),
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


def _sealed_reviewer_receipt() -> dict[str, object]:
    packet = _packet()
    signed_receipt = _receipt(packet)
    verified = _verify(signed_receipt, packet)
    sealed = copy.deepcopy(verified)
    # The coordinator seals the verified receipt with the adapter provider it
    # observed in its own process registry plus the authenticated submission
    # bookkeeping appended by _verified_quality_review_receipt.
    sealed["report"]["provider"] = "claude_sonnet5"
    sealed["submission_id"] = hashlib.sha256(
        json.dumps(
            signed_receipt,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    sealed["physical_submission_count"] = 1
    sealed["logical_submission_count"] = 1
    return sealed


def _reviewer_workspace_metadata() -> dict[str, object]:
    return worker_workspace.WorkerWorkspace(
        request_id="review-request-1",
        repo=Path("/var/aiworkhub/review-repo"),
        path=Path("/var/aiworkhub/review-repo/workspace"),
        home=Path("/var/aiworkhub/review-home"),
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    ).as_metadata()


def _accepted_reviewer_state() -> tuple[dict[str, object], dict[str, object]]:
    sealed = _sealed_reviewer_receipt()
    latest = {
        "request_id": "review-request-1",
        "task_id": "REVIEW_TASK_1",
        "runner": "claude_sonnet5",
        "topic": "quality_review",
        "adapter_id": "claude_sonnet5",
        "state": "accepted",
        "accepted": True,
        "promoted_paths": [],
        "workspace_retained": False,
        "cleanup_error": "",
        "quality_review_receipt": sealed,
        "reviewer_finalization": [],
        "acceptance_lock_scope": "request",
        "finished_at": "2026-08-10T00:00:00Z",
    }
    card = {
        "task_id": "REVIEW_TASK_1",
        "topic": "quality_review",
        "status": "finished",
        "worker_status": "done",
        "accepted_request_id": "review-request-1",
        "allowed_writes": [],
        "required_outputs": [],
        "terminal_review": {
            "evidence": {
                "quality_review_receipt": copy.deepcopy(sealed),
                "workspace": _reviewer_workspace_metadata(),
                "request_identity": {
                    "request_id": "review-request-1",
                    "task_id": "REVIEW_TASK_1",
                    "runner": "claude_sonnet5",
                    "topic": "quality_review",
                },
                "changed_paths": [],
                "changed_path_hashes": {},
                "quality_review": {
                    "target_claim_epoch": sealed["target"]["claim_epoch"],
                    "adapter_id": "claude_sonnet5",
                },
            },
        },
        "accept_evidence": {
            "quality_review_receipt": copy.deepcopy(sealed),
            "promoted_paths": [],
            "changed_paths": [],
            "changed_path_hashes": {},
        },
    }
    return latest, card


def test_target_acceptance_reuses_canonically_accepted_reviewer_receipt() -> None:
    latest, card = _accepted_reviewer_state()

    verified = process_launcher._verified_accepted_quality_review_receipt(
        latest,
        card,
        "review-request-1",
        "target-request-1",
        "TARGET_TASK_1",
    )

    assert verified == latest["quality_review_receipt"]
    assert verified is not latest["quality_review_receipt"]

    sealed = latest["quality_review_receipt"]
    assert sealed["report"]["provider"] == "claude_sonnet5"
    assert sealed["report"]["findings"] == []
    assert sealed["physical_submission_count"] == 1
    assert sealed["logical_submission_count"] == 1
    submission_id = sealed["submission_id"]
    assert isinstance(submission_id, str)
    assert len(submission_id) == 64
    assert submission_id == submission_id.lower()
    assert sealed["target"]["claim_epoch"] == 3
    assert latest["adapter_id"] == "claude_sonnet5"

    workspace_metadata = card["terminal_review"]["evidence"]["workspace"]
    assert workspace_metadata["allowed_writes"] == []
    assert workspace_metadata["parent_baseline"] == {}
    assert workspace_metadata["workspace_baseline"] == {}
    reconstructed = worker_workspace.WorkerWorkspace.from_metadata(
        dict(workspace_metadata)
    )
    assert reconstructed.as_metadata() == workspace_metadata


def test_accepted_reviewer_receipt_must_match_all_durable_copies() -> None:
    latest, card = _accepted_reviewer_state()
    card["accept_evidence"]["quality_review_receipt"]["report"]["findings"] = [
        {"id": "tampered"}
    ]

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="quality_reviewer_accepted_receipt_mismatch",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest,
            card,
            "review-request-1",
            "target-request-1",
            "TARGET_TASK_1",
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
    assert first["terminal_validation"] == [
        {
            "declared_command": "python -m pytest -q",
            "executed_argv": ["python3", "-m", "pytest", "-q"],
            "returncode": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
    ]


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
    authenticated_entry = json.loads(
        ctx.audit_ledger_path.read_text(encoding="utf-8").splitlines()[0]
    )
    assert authenticated_entry["authority_source"] == "runtime"
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


def test_worker_submission_preserves_bounded_nonactionable_disposition(
    tmp_path: Path,
) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)

    result = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[{
            "id": "packet-limit",
            "severity": "low",
            "disposition": "process_limit",
            "summary": "The packet omitted a requested excerpt",
            "evidence": "review packet source_evidence entry is truncated",
        }],
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
    finding = audit["verified_payloads"][0]["report"]["findings"][0]
    assert finding["disposition"] == "process_limit"
    assert finding["actionable"] is False


def test_worker_submission_normalizes_exact_evidence_and_caps_model_level(
    tmp_path: Path,
) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)

    result = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[{
            "id": "bounded-defect",
            "severity": "high",
            "summary": "Changed branch violates the invariant",
            "evidence": "src/aiworkhub/core.py:7",
            "path": "src/aiworkhub/core.py",
            "line_start": 7,
            "line_end": 7,
            "symbol": "src/aiworkhub/core.py.target",
            "confidence": "high",
            "evidence_level": "reproduced",
            "required_validation": "run the focused branch regression",
        }],
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
    finding = audit["verified_payloads"][0]["report"]["findings"][0]
    assert finding["evidence_reference"] == {
        "kind": "source",
        "path": "src/aiworkhub/core.py",
        "line_start": 7,
        "line_end": 7,
    }
    assert finding["evidence_level"] == "static_evidence"
    assert finding["confidence"] == "high"
    assert finding["claim"] == "Changed branch violates the invariant"
    assert finding["required_validation"] == "run the focused branch regression"


def test_worker_submission_rejects_finding_missing_required_text(
    tmp_path: Path,
) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)

    result = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[{
            "id": "no-text",
            "severity": "low",
            "summary": "summary present but evidence blank",
            "evidence": "   ",
        }],
    )
    assert result["ok"] is False
    assert result["reason"] == "review_finding_0_text_missing"


def test_worker_submission_rejects_finding_missing_summary_key(
    tmp_path: Path,
) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)

    result = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[{
            "severity": "low",
            "evidence": "src/aiworkhub/core.py:1",
        }],
    )
    assert result["ok"] is False
    assert result["reason"] == "review_finding_0_summary_missing"


def test_worker_submission_rejects_finding_missing_evidence_key(
    tmp_path: Path,
) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)

    result = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[{
            "severity": "low",
            "summary": "summary present but evidence key omitted",
        }],
    )
    assert result["ok"] is False
    assert result["reason"] == "review_finding_0_evidence_missing"


def test_worker_submission_rejects_undocumented_finding_alias(
    tmp_path: Path,
) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)

    result = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[{
            "severity": "low",
            "summary": "aliased finding",
            "evidence": "src/aiworkhub/core.py:1",
            "type": "defect",
        }],
    )
    assert result["ok"] is False
    assert result["reason"] == "review_finding_0_unknown_key:type"


def test_canonical_nonempty_finding_submits_and_finalizes_once(
    tmp_path: Path,
) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)

    result = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[{
            "id": "canonical-defect",
            "severity": "high",
            "disposition": "defect",
            "summary": "Changed branch violates the invariant",
            "evidence": "src/aiworkhub/core.py:7",
            "path": "src/aiworkhub/core.py",
            "line_start": 7,
            "line_end": 7,
            "symbol": "src/aiworkhub/core.py.target",
            "confidence": "high",
            "claim": "Changed branch violates the invariant",
            "reproduction": "",
            "required_validation": "run the focused branch regression",
        }],
    )
    assert result["ok"] is True
    assert result["finding_count"] == 1

    audit = worker_tools.verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )
    finding = audit["verified_payloads"][0]["report"]["findings"][0]
    assert qr.QUALITY_REVIEW_FINDING_REQUIRED_KEYS <= set(finding)
    assert set(finding) <= qr.QUALITY_REVIEW_FINDING_KEYS
    assert finding["severity"] == "high"
    assert finding["disposition"] == "defect"
    assert finding["actionable"] is True

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
    assert verified["report"]["findings"][0]["id"] == "canonical-defect"
    assert verified["physical_submission_count"] == 1
    assert verified["logical_submission_count"] == 1


def test_canonical_finding_schema_is_single_source_and_documented() -> None:
    assert qr.QUALITY_REVIEW_FINDING_REQUIRED_KEYS <= qr.QUALITY_REVIEW_FINDING_KEYS
    for key in ("id", "severity", "disposition", "summary", "evidence"):
        assert key in qr.QUALITY_REVIEW_FINDING_INPUT_KEYS
        assert key in qr.QUALITY_REVIEW_FINDING_KEYS
    # The input vocabulary and its required keys are derived from the one typed
    # model so the prompt, MCP schema and normalizer share a single source.
    assert qr.QUALITY_REVIEW_FINDING_INPUT_REQUIRED_KEYS == {
        "severity", "summary", "evidence",
    }
    assert qr.QUALITY_REVIEW_FINDING_INPUT_KEYS == frozenset(
        qr.QualityReviewFinding.__annotations__
    )
    assert qr.QUALITY_REVIEW_FINDING_INPUT_REQUIRED_KEYS == frozenset(
        qr.QualityReviewFinding.__required_keys__
    )
    prompt = qr.build_review_prompt(_packet(), lens="correctness")
    for token in ("severity", "summary", "evidence", "disposition", "id"):
        assert token in prompt
    assert qr.QUALITY_REVIEW_FINDING_SCHEMA_DOC
    assert qr.QUALITY_REVIEW_SUBMIT_TOOL_DESCRIPTION
    assert qr.QUALITY_REVIEW_FINDING_SCHEMA_DOC in qr.QUALITY_REVIEW_SUBMIT_TOOL_DESCRIPTION
    assert qr.QUALITY_REVIEW_FINDING_SCHEMA_DOC in prompt


def test_worker_submission_rejects_ungrounded_or_out_of_scope_defect(
    tmp_path: Path,
) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)

    missing = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="security",
        findings=[{
            "id": "ungrounded",
            "severity": "high",
            "summary": "Unverified claim",
            "evidence": "the code looks unsafe",
        }],
    )
    assert missing["ok"] is False
    assert missing["reason"] == "review_finding_0_exact_evidence_required"

    outside = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="security",
        findings=[{
            "id": "outside",
            "severity": "medium",
            "summary": "Outside scope",
            "evidence": "other.py:1",
            "path": "other.py",
            "line_start": 1,
            "line_end": 1,
        }],
    )
    assert outside["ok"] is False
    assert outside["reason"] == "review_finding_0_path_out_of_scope"


def test_identical_quality_review_retries_are_one_logical_receipt(
    tmp_path: Path,
) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)
    first = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[],
    )
    second = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[],
    )
    assert first["ok"] is True
    assert first["durable"] is True
    assert first["deduplicated"] is False
    assert second["ok"] is True
    assert second["durable"] is True
    assert second["deduplicated"] is True
    assert second["submission_id"] == first["submission_id"]

    audit = worker_tools.verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )
    assert audit["call_count_by_tool"] == {"quality_review_submit": 1}

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
    assert verified["report"]["findings"] == []
    assert verified["submission_id"] == first["submission_id"]
    assert verified["physical_submission_count"] == 1
    assert verified["logical_submission_count"] == 1


def test_quality_review_submit_never_acknowledges_failed_audit_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)

    def fail_append(_path: Path, _line: str) -> None:
        raise PermissionError("simulated denied audit append")

    monkeypatch.setattr(worker_tools, "_append_line_0600", fail_append)
    result = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[],
    )

    assert result["ok"] is False
    assert result["durable"] is False
    assert result["reason"] == "quality_review_submission_not_durable"
    assert len(result["submission_id"]) == 64


def test_missing_quality_review_submission_cannot_finalize_review_ready(
    tmp_path: Path,
) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)
    workspace = worker_workspace.WorkerWorkspace(
        request_id=ctx.request_id,
        repo=tmp_path,
        path=tmp_path,
        home=tmp_path,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="review_protocol:provider_events_unavailable",
    ):
        process_launcher._verified_quality_review_receipt(
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


def test_conflicting_quality_review_retries_fail_closed(tmp_path: Path) -> None:
    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)
    assert worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[],
    )["ok"] is True
    conflict = worker_tools.quality_review_submit(
        ctx,
        packet_sha256=str(packet["packet_sha256"]),
        lens="correctness",
        findings=[{
            "id": "conflicting-retry",
            "severity": "low",
            "summary": "conflicting retry",
            "evidence": "src/aiworkhub/core.py:1",
        }],
    )
    assert conflict["ok"] is False
    assert conflict["reason"] == "quality_review_submission_conflict"

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
    assert verified["physical_submission_count"] == 1
    assert verified["logical_submission_count"] == 1


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
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "AIWorkHub Test"], cwd=repo, check=True
    )
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

    def _reject_metadata_copy(*_args, **_kwargs):
        raise AssertionError("review overlay must not use metadata-preserving copy2")

    # The review overlay must materialize byte-identical content without
    # requesting copystat/utime metadata preservation (denied by the Landlock
    # validation boundary), so copy2 must never be called here.
    monkeypatch.setattr(worker_workspace.shutil, "copy2", _reject_metadata_copy)
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


def _quality_review_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "AIWorkHub Test"], cwd=repo, check=True
    )
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    return repo


def test_nf469_review_workspace_materializes_explicit_target_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _quality_review_repo(tmp_path, monkeypatch)
    (repo / "README.md").write_text("contract\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "SOURCE_GRAPH.md").write_text("source graph\n", encoding="utf-8")
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md", "docs/SOURCE_GRAPH.md", "source.py"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)

    source = worker_workspace.create_workspace(
        repo,
        "c" * 32,
        {"allowed_writes": ["source.py"], "required_outputs": []},
        "validation",
    )
    review = None
    try:
        (source.path / "source.py").write_text("value = 2\n", encoding="utf-8")
        review, evidence = worker_workspace.create_quality_review_workspace(
            source,
            "d" * 32,
            ["source.py"],
            "validation",
            ["README.md", "docs/SOURCE_GRAPH.md"],
        )

        assert review.allowed_writes == ()
        assert (review.path / "source.py").read_text(encoding="utf-8") == "value = 2\n"
        assert (review.path / "README.md").read_text(encoding="utf-8") == "contract\n"
        assert (
            review.path / "docs" / "SOURCE_GRAPH.md"
        ).read_text(encoding="utf-8") == "source graph\n"
        assert evidence["read_only_input_paths"] == [
            "README.md",
            "docs/SOURCE_GRAPH.md",
        ]
        assert evidence["read_only_input_hashes"] == {
            "README.md": worker_workspace._hash_path(review.path / "README.md"),
            "docs/SOURCE_GRAPH.md": worker_workspace._hash_path(
                review.path / "docs" / "SOURCE_GRAPH.md"
            ),
        }
        assert worker_workspace.enforce_scope(review) == []
    finally:
        if review is not None:
            worker_workspace.cleanup_workspace(repo, review.path, review.home)
        worker_workspace.cleanup_workspace(repo, source.path, source.home)


def test_quality_review_read_only_input_paths_are_strict_path_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _quality_review_repo(tmp_path, monkeypatch)
    (repo / "README.md").write_text("readme\n", encoding="utf-8")
    (repo / "Makefile").write_text("all:\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "path with spaces.md").write_text("spaces\n", encoding="utf-8")
    (repo / "docs" / "contract.md").write_text("contract\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "add",
            "README.md",
            "Makefile",
            "docs/path with spaces.md",
            "docs/contract.md",
        ],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)

    assert worker_workspace.quality_review_read_only_input_paths(
        repo, read_first=["README.md"], immutable_input_paths=["Makefile"]
    ) == ["Makefile", "README.md"]
    assert worker_workspace.quality_review_read_only_input_paths(
        repo,
        read_first=[str(repo / "docs" / "path with spaces.md")],
        immutable_input_paths=[],
    ) == ["docs/path with spaces.md"]
    assert worker_workspace.quality_review_read_only_input_paths(
        repo, read_first=["docs/*.md"], immutable_input_paths=[]
    ) == ["docs/contract.md", "docs/path with spaces.md"]

    for invalid in (
        "/absolute",
        "../outside",
        "missing.md",
    ):
        with pytest.raises(worker_workspace.WorkspaceError):
            worker_workspace.quality_review_read_only_input_paths(
                repo, read_first=[invalid], immutable_input_paths=[]
            )

    symlink = repo / "link.md"
    try:
        symlink.symlink_to("README.md")
    except (OSError, NotImplementedError):
        return
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="symlink_path_component_forbidden",
    ):
        worker_workspace.quality_review_read_only_input_paths(
            repo, read_first=["link.md"], immutable_input_paths=[]
        )


def test_rm33_new_production_and_test_candidates_prepare_read_only_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _quality_review_repo(tmp_path, monkeypatch)
    (repo / "README.md").write_text("canonical input\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    candidates = ["src/new_feature.py", "tests/test_new_feature.py"]
    source = worker_workspace.create_workspace(
        repo,
        "e" * 32,
        {"allowed_writes": candidates, "required_outputs": []},
        "validation",
    )
    review = None
    try:
        for relative, body in zip(candidates, ("VALUE = 33\n", "def test_value(): pass\n")):
            path = source.path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        canonical_inputs = worker_workspace.quality_review_read_only_input_paths(
            repo,
            read_first=["README.md", *candidates],
            candidate_changed_paths=candidates,
        )
        assert canonical_inputs == ["README.md"]
        review, evidence = worker_workspace.create_quality_review_workspace(
            source, "f" * 32, candidates, "validation", canonical_inputs
        )
        assert review.allowed_writes == ()
        assert evidence["candidate_paths"] == candidates
        assert evidence["read_only_input_paths"] == ["README.md"]
        assert (review.path / "README.md").read_text(encoding="utf-8") == "canonical input\n"
        assert (review.path / candidates[0]).read_text(encoding="utf-8") == "VALUE = 33\n"
        assert (review.path / candidates[1]).read_text(encoding="utf-8") == "def test_value(): pass\n"
        assert worker_workspace.enforce_scope(review) == []
    finally:
        if review is not None:
            worker_workspace.cleanup_workspace(repo, review.path, review.home)
        worker_workspace.cleanup_workspace(repo, source.path, source.home)


def test_candidate_authority_does_not_excuse_invalid_canonical_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _quality_review_repo(tmp_path, monkeypatch)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    (repo / "README.md").write_text("readme\n", encoding="utf-8")
    link = repo / "link.md"
    link.symlink_to("README.md")

    for declaration in ("missing.md", "../outside.md", str(outside), "link.md"):
        with pytest.raises(worker_workspace.WorkspaceError):
            worker_workspace.quality_review_read_only_input_paths(
                repo,
                read_first=[declaration],
                candidate_changed_paths=["src/new_feature.py"],
            )
