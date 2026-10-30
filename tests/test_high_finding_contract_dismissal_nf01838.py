"""NF-2026-01838: a manager may dismiss a HIGH (never CRITICAL) finding by citing the contract."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher_accept_review as accept_review  # noqa: E402
from aiworkhub import quality_evidence as qe  # noqa: E402
from aiworkhub import review_orchestrator  # noqa: E402

CHANGED = {"src/aiworkhub/example.py": "a" * 64}
CANDIDATE = review_orchestrator.candidate_digest(CHANGED)
FINDING_1 = "reviewer:correctness:finding-1"
CITATION = "the card says X but src/aiworkhub/example.py:12-20 is the contract"


def _finding(severity: str) -> dict[str, str]:
    return {
        "id": "finding-1",
        "severity": severity,
        "summary": "reviewer claims a defect",
        "evidence": "fixture:claimed-defect",
    }


def _report(severity: str) -> dict:
    return {
        "lens": qe.LENS_CORRECTNESS,
        "provider": "reviewer-b",
        "read_only": True,
        "can_mutate_repo": False,
        "findings": [_finding(severity)],
    }


def _receipt(severity: str) -> dict:
    return {
        "schema_id": "aiworkhub.quality_review_receipt.v1",
        "packet_sha256": "b" * 64,
        "target": {"request_id": "target-request", "task_id": "TARGET", "claim_epoch": 3},
        "reviewer": {
            "request_id": "review-req-1",
            "task_id": "QUALITY_REVIEW_TARGET",
            "provider": "reviewer-b",
        },
        "report": {
            **_report(severity),
            "findings": [{**_finding(severity), "disposition": "defect", "actionable": True}],
        },
        "authority": {
            "process_identity_verified": True,
            "audit_verified": True,
            "terminal_state": "review_ready",
        },
        "submission_id": "c" * 64,
        "physical_submission_count": 1,
        "logical_submission_count": 1,
    }


def _fold(severity: str, **kwargs) -> dict:
    return qe.fold_quality_verdict(
        [{"check_id": "tests", "kind": "test", "status": qe.STATUS_PASSED}],
        risk_profile=qe.resolve_risk_profile(qe.RISK_MEDIUM),
        reviewer_reports=[_report(severity)],
        combined_tree_checks=[
            {"check_id": "combined", "kind": "test", "status": qe.STATUS_PASSED}
        ],
        worker_provider="worker-a",
        **kwargs,
    )


def _dispose(tmp_path: Path, severity: str, **overrides) -> dict:
    driver = review_orchestrator.ReviewOrchestrator(
        SimpleNamespace(repo=tmp_path), db_path=tmp_path / "review.sqlite3"
    )
    args = {
        "task_id": "TARGET",
        "request_id": "target-request",
        "reviewer_request_id": "review-req-1",
        "finding_id": FINDING_1,
        "disposition": "dismissed",
        "counter_evidence": CITATION,
        "reason": "card text was wrong, code is right",
        "actor": "codex:thread-exact",
        "reviewer_receipt": _receipt(severity),
        "candidate_sha256": CANDIDATE,
    }
    args.update(overrides)
    return driver.dispose_review_finding(**args)


def _preview(severity: str, rows: list[dict], **kwargs) -> dict:
    reviewers = [
        {
            "task_id": "QUALITY_REVIEW_TARGET",
            "lens": qe.LENS_CORRECTNESS,
            "request_id": "review-req-1",
            "state": "review_ready",
            "usable": True,
            "receipt": _receipt(severity),
        }
    ]
    return accept_review.fold_accept_blockers(
        reviewers=reviewers,
        reviewer_request_ids=None,
        risk_profile=qe.resolve_risk_profile(qe.RISK_MEDIUM),
        terminal_substatus="review_ready",
        finding_dispositions=rows,
        **kwargs,
    )


def _row(receipt_sha256: str = "e" * 64) -> dict:
    return {
        "finding_id": FINDING_1,
        "reviewer_request_id": "review-req-1",
        "disposition": "dismissed",
        "receipt_sha256": receipt_sha256,
    }


def test_high_dismissal_with_citation_is_recorded_manager_signed(tmp_path: Path) -> None:
    stored = _dispose(tmp_path, qe.SEVERITY_HIGH)

    assert stored["ok"] is True
    assert stored["manager_signed"] is True
    assert stored["contract_citations"] == ["src/aiworkhub/example.py:12-20"]
    assert stored["severity"] == qe.SEVERITY_HIGH


def test_high_dismissal_accepts_card_and_kb_quotes(tmp_path: Path) -> None:
    for quote in ("card: the contract allows two writers", "see kb: single-writer rule"):
        result = _dispose(tmp_path, qe.SEVERITY_HIGH, counter_evidence=quote)
        assert result["ok"] is True
        assert len(result["contract_citations"]) == 1


def test_high_dismissal_without_citation_is_refused(tmp_path: Path) -> None:
    for text in ("measured: it is fine", "see example.py near the top", "card: "):
        assert _dispose(tmp_path, qe.SEVERITY_HIGH, counter_evidence=text) == {
            "ok": False,
            "error": "review_finding_disposition_contract_citation_required",
        }


def test_high_confirmed_disposition_is_refused(tmp_path: Path) -> None:
    assert _dispose(tmp_path, qe.SEVERITY_HIGH, disposition="confirmed") == {
        "ok": False,
        "error": "review_finding_disposition_blocking_severity",
    }


def test_critical_is_refused_even_with_citation(tmp_path: Path) -> None:
    assert _dispose(tmp_path, qe.SEVERITY_CRITICAL) == {
        "ok": False,
        "error": "review_finding_disposition_blocking_severity",
    }


def test_non_high_receipt_shape_is_unchanged(tmp_path: Path) -> None:
    stored = _dispose(tmp_path, qe.SEVERITY_LOW, counter_evidence="measured: fine")

    assert stored["ok"] is True
    assert "manager_signed" not in stored
    assert "contract_citations" not in stored


def test_fold_lifts_high_with_dismissal_and_records_manager_signed() -> None:
    verdict = _fold(
        qe.SEVERITY_HIGH,
        finding_dispositions=[
            {"finding_id": FINDING_1, "disposition": "dismissed", "receipt_sha256": "e" * 64}
        ],
    )

    assert verdict["blocking_evidence"] == []
    assert verdict["passed"] is True
    assert verdict["manager_dismissed_findings"] == [
        {
            "finding_id": FINDING_1,
            "lens": qe.LENS_CORRECTNESS,
            "severity": qe.SEVERITY_HIGH,
            "disposition": "dismissed",
            "classification": "manager_dismissed_contract_counter_evidence",
            "receipt_sha256": "e" * 64,
            "manager_signed": True,
        }
    ]


def test_fold_never_lifts_critical_with_a_dismissal_row() -> None:
    verdict = _fold(
        qe.SEVERITY_CRITICAL,
        finding_dispositions=[
            {"finding_id": FINDING_1, "disposition": "dismissed", "receipt_sha256": "e" * 64}
        ],
    )

    assert FINDING_1 in verdict["blocking_evidence"]
    assert verdict["passed"] is False
    assert "manager_dismissed_findings" not in verdict


def test_manager_correction_still_cannot_lift_high() -> None:
    verdict = _fold(qe.SEVERITY_HIGH, corrected_finding_ids=[FINDING_1])

    assert FINDING_1 in verdict["blocking_evidence"]
    assert verdict["passed"] is False
    assert "manager_corrected_findings" not in verdict


def test_accept_preview_lifts_high_and_agrees_with_the_fold() -> None:
    fold = _preview(qe.SEVERITY_HIGH, [_row()])

    assert [row for row in fold["blockers"] if row["kind"] == "refinement_required"] == []
    assert fold["finding_dispositions"] == [
        {
            "finding_id": FINDING_1,
            "reviewer_request_id": "review-req-1",
            "lens": qe.LENS_CORRECTNESS,
            "severity": qe.SEVERITY_HIGH,
            "disposition": "dismissed",
            "receipt_sha256": "e" * 64,
            "blocker_lifted": True,
            "lifted_by": "manager_contract_dismissal",
        }
    ]


def test_accept_preview_keeps_critical_blocking_with_a_dismissal_row() -> None:
    fold = _preview(qe.SEVERITY_CRITICAL, [_row()])

    refinement = [row for row in fold["blockers"] if row["kind"] == "refinement_required"]
    assert [row["detail"] for row in refinement] == [FINDING_1]
    assert fold["finding_dispositions"][0]["blocker_lifted"] is False
    assert "lifted_by" not in fold["finding_dispositions"][0]


def test_accept_preview_manager_correction_cannot_lift_high() -> None:
    row = {**_row(), "disposition": "confirmed"}
    fold = _preview(
        qe.SEVERITY_HIGH,
        [row],
        manager_correction={"finding_ids": [FINDING_1]},
    )

    refinement = [row for row in fold["blockers"] if row["kind"] == "refinement_required"]
    assert [row["detail"] for row in refinement] == [FINDING_1]
