"""Tests for quality_reviewer module: packet file transport, E2BIG avoidance,
manager alias rejection, and sealed receipt reconciliation."""

import hashlib
import json
from pathlib import Path

import pytest

from aiworkhub import quality_reviewer
from aiworkhub.quality_reviewer import ReviewerEvidenceError


def _canonical_digest(payload: dict) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _packet_with_findings(
    packet_sha256: str | None = None,
    candidate_path: str = "src/module.py",
) -> dict:
    body = {
        "candidate": {
            "scoped_audits": {
                "correctness": {
                    "known_unknowns": [],
                    "packet": {"changed_paths": [{"path": candidate_path}]},
                },
                "security": {
                    "known_unknowns": [],
                    "packet": {"changed_paths": [{"path": candidate_path}]},
                },
                "code_quality": {
                    "known_unknowns": [],
                    "packet": {"changed_paths": [{"path": candidate_path}]},
                },
            },
            "path": candidate_path,
            "findings": [
                {
                    "id": "F001",
                    "severity": "medium",
                    "summary": "Null check missing",
                    "evidence": f"{candidate_path}:42",
                }
            ],
        },
    }
    if packet_sha256 is None:
        packet_sha256 = _canonical_digest(body)
    return {"packet_sha256": packet_sha256, **body}


class TestBuildReviewPrompt:
    def test_inline_embeds_prompt(self):
        packet = _packet_with_findings()
        prompt = quality_reviewer.build_review_prompt(
            packet, lens="correctness",
            submit_tool_name="aiworkhub_worker_quality_review_submit",
        )
        assert "aiworkhub_worker_quality_review_submit" not in prompt
        assert "exactly one JSON object" in prompt
        assert '"lens":"correctness","findings":[...]' in prompt

    def test_prompt_names_the_receipts_the_supervisor_already_produced(self):
        """Reviewers were re-deriving evidence the packet already carried.

        Measured on request fb150636: 38 model turns, 17 tool calls, only 4 of
        them reading the candidate. The rest were scaffolding -- sha256sum over
        paths whose digests were in the packet, and four attempts to re-run a
        pytest command whose exact argv, returncode and output the packet
        already carried, three of them lost hunting for an interpreter.
        """
        prompt = quality_reviewer.build_review_prompt(
            _packet_with_findings(), lens="correctness"
        )
        assert "sha256sum" in prompt, "digests must be named as already recorded"
        assert "terminal_validation" in prompt
        assert "mechanical_checks" in prompt
        assert "Do not re-run pytest" in prompt
        assert "do not go looking for an interpreter" in prompt

    def test_file_transport_references_file(self, tmp_path: Path):
        large_body = {
            "candidate": {
                "scoped_audits": {"code_quality": {"known_unknowns": [], "packet": {"changed_paths": [{"path": "src/big.py"}]}}},
                "path": "src/big.py",
                "findings": [
                    {"id": f"F{i:04d}", "severity": "low",
                     "summary": "x" * 1900, "evidence": "y" * 1900}
                    for i in range(100)
                ],
            },
        }
        packet_sha256 = _canonical_digest(large_body)
        large_body["packet_sha256"] = packet_sha256
        packet_file = tmp_path / "packet.json"
        packet_file.write_text("{}", encoding="utf-8")
        prompt = quality_reviewer.build_review_prompt(
            large_body, lens="code_quality",
            submit_tool_name="aiworkhub_worker_quality_review_submit",
            packet_file=str(packet_file), max_inline_bytes=1,
        )
        assert "QUALITY_REVIEW_PACKET_FILE:" in prompt
        assert "QUALITY_REVIEW_PACKET:" not in prompt

    def test_missing_packet_file_raises(self, tmp_path: Path):
        packet = _packet_with_findings()
        missing = tmp_path / "nonexistent.json"
        with pytest.raises(ReviewerEvidenceError, match="review_packet_file_missing"):
            quality_reviewer.build_review_prompt(
                packet, lens="correctness",
                packet_file=str(missing), max_inline_bytes=1,
            )

    def test_invalid_lens_raises(self):
        with pytest.raises(ReviewerEvidenceError, match="invalid_reviewer_lens"):
            quality_reviewer.build_review_prompt(
                _packet_with_findings(), lens="unknown",
            )


class TestNormalizePacketFindings:
    def test_valid_findings_bound(self):
        packet = _packet_with_findings()
        findings = [{
            "id": "F002", "severity": "high",
            "summary": "Unsafe input", "evidence": "src/module.py:12",
        }]
        result = quality_reviewer.normalize_packet_findings(
            packet, lens="correctness", findings=findings,
        )
        assert isinstance(result, list)
        assert result[0]["id"] == "F002"

    def test_structured_source_evidence_is_canonicalized_and_bound(self):
        packet = _packet_with_findings()
        findings = [{
            "severity": "high",
            "summary": "Unsafe input",
            "evidence": {
                "path": "src/module.py",
                "line_start": 12,
                "line_end": 14,
            },
        }]

        result = quality_reviewer.normalize_packet_findings(
            packet, lens="correctness", findings=findings,
        )

        assert result[0]["evidence"] == "src/module.py:12-14"
        assert result[0]["evidence_reference"] == {
            "kind": "source",
            "path": "src/module.py",
            "line_start": 12,
            "line_end": 14,
        }

    def test_structured_evidence_conflict_and_unknown_keys_fail_closed(self):
        packet = _packet_with_findings()
        base = {"severity": "high", "summary": "Unsafe input"}
        with pytest.raises(
            ReviewerEvidenceError,
            match="structured_evidence_conflict:path",
        ):
            quality_reviewer.normalize_packet_findings(
                packet,
                lens="correctness",
                findings=[{
                    **base,
                    "path": "src/other.py",
                    "evidence": {
                        "path": "src/module.py",
                        "line_start": 12,
                        "line_end": 12,
                    },
                }],
            )
        with pytest.raises(
            ReviewerEvidenceError,
            match="structured_evidence_unknown_key:column",
        ):
            quality_reviewer.normalize_packet_findings(
                packet,
                lens="correctness",
                findings=[{
                    **base,
                    "evidence": {
                        "path": "src/module.py",
                        "line_start": 12,
                        "column": 3,
                    },
                }],
            )

    def test_canonical_finding_reingress_is_idempotent_and_rederives_authority(self):
        packet = _packet_with_findings()
        canonical = quality_reviewer.normalize_packet_findings(
            packet,
            lens="correctness",
            findings=[{
                "severity": "high",
                "summary": "Unsafe input",
                "evidence": "src/module.py:12-14",
                "path": "src/module.py",
                "line_start": 12,
                "line_end": 14,
            }],
        )[0]

        contradictory = {**canonical, "actionable": False}
        renormalized = quality_reviewer.normalize_packet_findings(
            packet, lens="correctness", findings=[contradictory]
        )[0]

        assert renormalized == canonical
        assert json.dumps(renormalized, sort_keys=True) == json.dumps(
            canonical, sort_keys=True
        )
        assert renormalized["actionable"] is True

    @pytest.mark.parametrize(
        "reference, error",
        [
            (
                {"kind": "source", "path": "src/other.py", "line_start": 1, "line_end": 1},
                "path_out_of_scope",
            ),
            (
                {"kind": "source", "path": "src/module.py", "line_start": 0, "line_end": 1},
                "line_invalid",
            ),
            ({"kind": "check", "check_id": "invented"}, "check_out_of_scope"),
            ({"kind": "test_target", "path": "tests/invented.py"}, "path_out_of_scope"),
            ({"kind": "source", "path": "src/module.py"}, "evidence_reference_invalid"),
        ],
    )
    def test_canonical_evidence_reference_is_revalidated(self, reference, error):
        finding = {
            "severity": "medium",
            "summary": "Unsafe input",
            "evidence": "src/module.py:12",
            "evidence_reference": reference,
            "actionable": True,
        }
        with pytest.raises(ReviewerEvidenceError, match=error):
            quality_reviewer.normalize_packet_findings(
                _packet_with_findings(), lens="correctness", findings=[finding]
            )

    def test_arbitrary_unknown_key_remains_rejected(self):
        with pytest.raises(ReviewerEvidenceError, match="unknown_key:authority"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_findings(),
                lens="correctness",
                findings=[{
                    "severity": "medium",
                    "summary": "Unsafe input",
                    "evidence": "src/module.py:12",
                    "authority": True,
                }],
            )


class TestVerifyReviewerReceipt:
    def test_valid_receipt_passes(self) -> None:
        """A properly constructed receipt with matching process-observed
        facts must pass verification and return the canonical sealed receipt."""
        packet = quality_reviewer.build_review_packet(
            request_id="req-vrfy-001",
            task_id="task-vrfy-001",
            claim_epoch=1,
            worker_provider="deepseek_vscode_lm",
            changed_path_hashes={"src/module.py": "a" * 64},
        )
        receipt = {
            "schema_id": quality_reviewer.RECEIPT_SCHEMA_ID,
            "packet_sha256": packet["packet_sha256"],
            "target": {
                "request_id": "req-vrfy-001",
                "task_id": "task-vrfy-001",
                "claim_epoch": 1,
            },
            "reviewer": {
                "request_id": "rev-vrfy-001",
                "task_id": "rev-task-vrfy-001",
                "provider": "deepseek_vscode_lm",
            },
            "report": {
                "lens": "correctness",
                "read_only": True,
                "can_mutate_repo": False,
                "findings": [],
            },
            "authority": {
                "process_identity_verified": True,
                "audit_verified": True,
                "terminal_state": "review_ready",
            },
        }
        result = quality_reviewer.verify_reviewer_receipt(
            receipt,
            packet=packet,
            expected_reviewer_request_id="rev-vrfy-001",
            expected_reviewer_task_id="rev-task-vrfy-001",
            observed_provider="deepseek_vscode_lm",
            observed_terminal_state="review_ready",
            audit_verified=True,
        )
        assert result["schema_id"] == quality_reviewer.RECEIPT_SCHEMA_ID
        assert result["packet_sha256"] == packet["packet_sha256"]

    def test_missing_schema_rejected(self) -> None:
        """A receipt without the canonical schema_id must be rejected —
        the protocol fails closed rather than accepting untagged JSON."""
        packet = quality_reviewer.build_review_packet(
            request_id="req-vrfy-002",
            task_id="task-vrfy-002",
            claim_epoch=1,
            worker_provider="deepseek_vscode_lm",
            changed_path_hashes={"src/module.py": "a" * 64},
        )
        receipt = {"report": {"lens": "correctness", "findings": []}}
        with pytest.raises(ReviewerEvidenceError, match="reviewer_receipt_schema_mismatch"):
            quality_reviewer.verify_reviewer_receipt(
                receipt,
                packet=packet,
                expected_reviewer_request_id="rev-vrfy-002",
                expected_reviewer_task_id="rev-task-vrfy-002",
                observed_provider="deepseek_vscode_lm",
                observed_terminal_state="review_ready",
                audit_verified=True,
            )

    def test_wrong_schema_rejected(self) -> None:
        """A receipt with a fabricated schema_id must be rejected."""
        packet = quality_reviewer.build_review_packet(
            request_id="req-vrfy-003",
            task_id="task-vrfy-003",
            claim_epoch=1,
            worker_provider="deepseek_vscode_lm",
            changed_path_hashes={"src/module.py": "a" * 64},
        )
        receipt = {"schema_id": "aiworkhub.wrong_schema.v1", "report": {}}
        with pytest.raises(ReviewerEvidenceError, match="reviewer_receipt_schema_mismatch"):
            quality_reviewer.verify_reviewer_receipt(
                receipt,
                packet=packet,
                expected_reviewer_request_id="rev-vrfy-003",
                expected_reviewer_task_id="rev-task-vrfy-003",
                observed_provider="deepseek_vscode_lm",
                observed_terminal_state="review_ready",
                audit_verified=True,
            )
