"""NF-2026-01762: a manager may dismiss a process_limit finding to lift blindness.

A correctness/security report whose only findings are ``process_limit`` makes
the lens blind. One rule, ``quality_evidence.blind_reviewer_lenses``, decides
that for the fold, the accept preview and reviewer reuse; a bound ``dismissed``
disposition with counter-evidence stops a process_limit finding counting, and a
single undisposed one keeps the lens blind.
"""

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
PL_1 = "reviewer:correctness:pl-1"
PL_2 = "reviewer:correctness:pl-2"


def _finding(
    finding_id: str, disposition: str = qe.FINDING_DISPOSITION_PROCESS_LIMIT
) -> dict[str, str]:
    return {
        "id": finding_id,
        "severity": qe.SEVERITY_LOW,
        "disposition": disposition,
        "summary": "reviewer could not inspect the packet",
        "evidence": "no file-read tool was available",
    }


def _report(*findings: dict[str, str], lens: str = qe.LENS_CORRECTNESS) -> dict:
    return {
        "lens": lens,
        "provider": "reviewer-b",
        "read_only": True,
        "can_mutate_repo": False,
        "findings": list(findings),
    }


def _fold(reports: list[dict], **kwargs) -> dict:
    return qe.fold_quality_verdict(
        [{"check_id": "tests", "kind": "test", "status": qe.STATUS_PASSED}],
        risk_profile=qe.resolve_risk_profile(qe.RISK_MEDIUM),
        reviewer_reports=reports,
        combined_tree_checks=[
            {"check_id": "combined", "kind": "test", "status": qe.STATUS_PASSED}
        ],
        worker_provider="worker-a",
        **kwargs,
    )


def _dismissal(finding_id: str) -> dict[str, str]:
    return {"finding_id": finding_id, "disposition": "dismissed", "receipt_sha256": "e" * 64}


def _receipt(*findings: dict[str, str], lens: str = qe.LENS_CORRECTNESS) -> dict:
    return {
        "schema_id": "aiworkhub.quality_review_receipt.v1",
        "packet_sha256": "b" * 64,
        "target": {"request_id": "target-request", "task_id": "TARGET", "claim_epoch": 3},
        "reviewer": {
            "request_id": "review-req-1",
            "task_id": "QUALITY_REVIEW_TARGET",
            "provider": "reviewer-b",
        },
        "report": _report(*findings, lens=lens),
    }


def _driver(tmp_path: Path) -> review_orchestrator.ReviewOrchestrator:
    return review_orchestrator.ReviewOrchestrator(
        SimpleNamespace(repo=tmp_path), db_path=tmp_path / "review.sqlite3"
    )


def _dispose(driver, receipt: dict, finding_id: str = PL_1, **overrides) -> dict:
    args = {
        "task_id": "TARGET",
        "request_id": "target-request",
        "reviewer_request_id": "review-req-1",
        "finding_id": finding_id,
        "disposition": "dismissed",
        "counter_evidence": "full-tree validation receipt sha256:" + "f" * 64,
        "reason": "reviewer lacked file access, bytes validated",
        "actor": "codex:thread-exact",
        "reviewer_receipt": receipt,
        "candidate_sha256": CANDIDATE,
    }
    args.update(overrides)
    return driver.dispose_review_finding(**args)


# --------------------------------------------------------------------------
# review_finding_dispose
# --------------------------------------------------------------------------
def test_process_limit_correctness_finding_can_be_dismissed(tmp_path: Path) -> None:
    result = _dispose(_driver(tmp_path), _receipt(_finding("pl-1")))

    assert result["ok"] is True
    assert result["finding_id"] == PL_1
    assert result["disposition"] == "dismissed"


def test_process_limit_security_finding_can_be_dismissed(tmp_path: Path) -> None:
    receipt = _receipt(_finding("pl-1"), lens=qe.LENS_SECURITY)

    result = _dispose(
        _driver(tmp_path), receipt, finding_id="reviewer:security:pl-1"
    )

    assert result["ok"] is True


def test_process_limit_dismissal_needs_counter_evidence(tmp_path: Path) -> None:
    result = _dispose(
        _driver(tmp_path), _receipt(_finding("pl-1")), counter_evidence="  "
    )

    assert result["error"] == "review_finding_disposition_counter_evidence_required"


def test_process_limit_dismissal_needs_bound_receipt_and_candidate(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    receipt = _receipt(_finding("pl-1"))

    unbound = _dispose(driver, receipt, reviewer_request_id="other-review")
    no_candidate = _dispose(driver, receipt, candidate_sha256="not-hex")

    assert unbound["error"] == "review_finding_disposition_reviewer_not_bound"
    assert no_candidate["error"] == "review_finding_disposition_candidate_unavailable"


def test_observation_finding_is_still_refused(tmp_path: Path) -> None:
    receipt = _receipt(_finding("obs-1", qe.FINDING_DISPOSITION_OBSERVATION))

    result = _dispose(
        _driver(tmp_path), receipt, finding_id="reviewer:correctness:obs-1"
    )

    assert result["error"] == "review_finding_disposition_not_a_blocker"


def test_process_limit_in_other_lens_is_still_refused(tmp_path: Path) -> None:
    receipt = _receipt(_finding("pl-1"), lens=qe.LENS_CODE_QUALITY)

    result = _dispose(
        _driver(tmp_path), receipt, finding_id="reviewer:code_quality:pl-1"
    )

    assert result["error"] == "review_finding_disposition_not_a_blocker"


# --------------------------------------------------------------------------
# the one blindness helper, through the fold
# --------------------------------------------------------------------------
def test_undisposed_process_limit_report_is_blind() -> None:
    verdict = _fold([_report(_finding("pl-1"))])

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]


def test_dismissed_process_limit_report_is_not_blind() -> None:
    reports = [_report(_finding("pl-1"))]

    assert qe.blind_reviewer_lenses(reports) == {qe.LENS_CORRECTNESS}
    assert (
        qe.blind_reviewer_lenses(
            reports, dismissed_by_finding=qe.dismissed_finding_receipts([_dismissal(PL_1)])
        )
        == set()
    )
    verdict = _fold(reports, finding_dispositions=[_dismissal(PL_1)])
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]
    assert verdict["passed"] is True


def test_one_undisposed_process_limit_finding_keeps_the_lens_blind() -> None:
    reports = [_report(_finding("pl-1"), _finding("pl-2"))]

    verdict = _fold(reports, finding_dispositions=[_dismissal(PL_1)])

    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]
    assert verdict["passed"] is False


def test_non_dismissed_disposition_does_not_lift_blindness() -> None:
    confirmed = {**_dismissal(PL_1), "disposition": "confirmed"}

    verdict = _fold([_report(_finding("pl-1"))], finding_dispositions=[confirmed])

    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]


def test_duplicate_finding_id_across_reports_does_not_lift_blindness() -> None:
    reports = [_report(_finding("pl-1")), _report(_finding("pl-1"))]

    assert qe.blind_reviewer_lenses(
        reports, dismissed_by_finding=qe.dismissed_finding_receipts([_dismissal(PL_1)])
    ) == {qe.LENS_CORRECTNESS}


def test_dismissal_does_not_lift_blindness_for_other_lenses() -> None:
    reports = [_report(_finding("pl-1"), lens=qe.LENS_CODE_QUALITY)]
    dismissal = _dismissal("reviewer:code_quality:pl-1")

    assert qe.blind_reviewer_lenses(
        reports, dismissed_by_finding=qe.dismissed_finding_receipts([dismissal])
    ) == {qe.LENS_CODE_QUALITY}


# --------------------------------------------------------------------------
# accept_preview's helper agrees with the fold
# --------------------------------------------------------------------------
def test_preview_helper_follows_the_same_rule() -> None:
    reviewers = [{"receipt": _receipt(_finding("pl-1"), _finding("pl-2"))}]

    assert accept_review._blind_reviewer_lenses(reviewers) == {qe.LENS_CORRECTNESS}
    assert accept_review._blind_reviewer_lenses(
        reviewers, finding_dispositions=[_dismissal(PL_1)]
    ) == {qe.LENS_CORRECTNESS}
    assert (
        accept_review._blind_reviewer_lenses(
            reviewers, finding_dispositions=[_dismissal(PL_1), _dismissal(PL_2)]
        )
        == set()
    )
