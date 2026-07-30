"""Deterministic calibration matrix for the canonical Quality Gate 2.0 fold.

This module exercises every blocker family owned by ``fold_quality_verdict``.
It has no repository or model side effects, so the same matrix runs on Linux,
Windows, macOS and the Remote-SSH contract. A gate predicate is not trusted
merely because a unit test reached its code path: the reference positives must
stay green, each targeted defect must stay red, and the expected blocker must
remain observable.
"""

from __future__ import annotations

from typing import Any

from . import quality_evidence as qe

SCHEMA_ID = "aiworkhub.quality_calibration.v1"


def _check(status: str = qe.STATUS_PASSED) -> dict[str, str]:
    return {"check_id": "tests", "kind": "test", "status": status}


def _combined(status: str = qe.STATUS_PASSED) -> list[dict[str, str]]:
    return [{"check_id": "combined", "kind": "test", "status": status}]


def _report(
    lens: str = qe.LENS_CORRECTNESS,
    *,
    provider: str = "reviewer-b",
    findings: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "lens": lens,
        "provider": provider,
        "read_only": True,
        "can_mutate_repo": False,
        "findings": findings or [],
    }


def _finding(**overrides: Any) -> dict[str, str]:
    row = {
        "id": "finding-1",
        "severity": qe.SEVERITY_HIGH,
        "summary": "targeted calibration defect",
        "evidence": "fixture:targeted-defect",
    }
    row.update({str(key): str(value) for key, value in overrides.items()})
    return row


def _case(
    case_id: str,
    *,
    expected_pass: bool,
    expected_blocker: str = "",
    checks: list[Any] | None = None,
    risk: str = qe.RISK_LOW,
    reports: list[Any] | None = None,
    combined: list[Any] | None = None,
    worker_provider: str = "worker-a",
    approval: bool = False,
    config_error: str = "",
) -> dict[str, Any]:
    verdict = qe.fold_quality_verdict(
        checks if checks is not None else [_check()],
        risk_profile=qe.resolve_risk_profile(risk),
        reviewer_reports=reports or [],
        combined_tree_checks=combined or [],
        worker_provider=worker_provider,
        human_approval=approval,
        config_error=config_error,
    )
    blockers = [str(value) for value in verdict.get("blocking_evidence") or []]
    blocker_observed = not expected_blocker or any(
        value == expected_blocker or value.startswith(expected_blocker)
        for value in blockers
    )
    actual_pass = verdict.get("passed") is True
    return {
        "case_id": case_id,
        "expected_pass": expected_pass,
        "actual_pass": actual_pass,
        "expected_blocker": expected_blocker,
        "expected_blocker_observed": blocker_observed,
        "blocking_evidence": blockers,
        "calibrated": actual_pass is expected_pass and blocker_observed,
    }


def run_calibration() -> dict[str, Any]:
    """Run the bounded positive/negative predicate matrix and report rates."""

    good_medium = [_report()]
    good_high = [_report(lens) for lens in sorted(qe.JUDGMENT_LENSES)]
    malformed_finding_cases = [
        ("reviewer_finding_not_object", ["bad"], "reviewer_schema:0:0:not_object"),
        ("reviewer_finding_severity", [_finding(severity="unknown")], "reviewer_schema:0:0:invalid_severity"),
        ("reviewer_finding_id", [_finding(id="")], "reviewer_schema:0:0:id_missing"),
        ("reviewer_finding_summary", [_finding(summary="")], "reviewer_schema:0:0:summary_missing"),
        ("reviewer_finding_evidence", [_finding(evidence="")], "reviewer_schema:0:0:evidence_missing"),
    ]
    cases = [
        _case("reference_low", expected_pass=True),
        _case("mechanical_failed", expected_pass=False, expected_blocker="tests", checks=[_check(qe.STATUS_FAILED)]),
        _case("mechanical_unavailable", expected_pass=False, expected_blocker="tests", checks=[_check(qe.STATUS_NOT_AVAILABLE)]),
        _case("mechanical_schema", expected_pass=False, expected_blocker="mechanical_schema:", checks=[{"kind": "test", "status": qe.STATUS_PASSED}]),
        _case("reference_medium", expected_pass=True, risk=qe.RISK_MEDIUM, reports=good_medium, combined=_combined()),
        _case("required_reviewer", expected_pass=False, expected_blocker="required_reviewer_missing:correctness", risk=qe.RISK_MEDIUM, combined=_combined()),
        _case("reviewer_invalid_lens", expected_pass=False, expected_blocker="reviewer_schema:0:invalid_lens", risk=qe.RISK_MEDIUM, reports=[_report("invalid")], combined=_combined()),
        _case("reviewer_provider", expected_pass=False, expected_blocker="reviewer_schema:0:provider_missing", risk=qe.RISK_MEDIUM, reports=[_report(provider="")], combined=_combined()),
        _case("reviewer_read_only", expected_pass=False, expected_blocker="reviewer_schema:0:not_read_only", risk=qe.RISK_MEDIUM, reports=[{**_report(), "read_only": False}], combined=_combined()),
        _case("reviewer_findings_shape", expected_pass=False, expected_blocker="reviewer_schema:0:findings_invalid", risk=qe.RISK_MEDIUM, reports=[{**_report(), "findings": "bad"}], combined=_combined()),
        *[
            _case(case_id, expected_pass=False, expected_blocker=blocker, risk=qe.RISK_MEDIUM, reports=[_report(findings=findings)], combined=_combined())
            for case_id, findings, blocker in malformed_finding_cases
        ],
        _case("reviewer_report_overflow", expected_pass=False, expected_blocker="reviewer_schema:too_many_reports", risk=qe.RISK_MEDIUM, reports=[_report() for _ in range(qe.MAX_REVIEW_REPORTS + 1)], combined=_combined()),
        _case("reviewer_blocking_finding", expected_pass=False, expected_blocker="reviewer:correctness:finding-1", risk=qe.RISK_MEDIUM, reports=[_report(findings=[_finding()])], combined=_combined()),
        _case("reviewer_refinement", expected_pass=False, expected_blocker="refinement_required:reviewer:correctness:finding-1", risk=qe.RISK_MEDIUM, reports=[_report(findings=[_finding(severity=qe.SEVERITY_LOW)])], combined=_combined()),
        _case("combined_missing", expected_pass=False, expected_blocker="combined_tree_evidence_missing", risk=qe.RISK_MEDIUM, reports=good_medium),
        _case("combined_failed", expected_pass=False, expected_blocker="combined_tree:combined", risk=qe.RISK_MEDIUM, reports=good_medium, combined=_combined(qe.STATUS_FAILED)),
        _case("combined_schema", expected_pass=False, expected_blocker="combined_tree_schema:", risk=qe.RISK_MEDIUM, reports=good_medium, combined=[{"kind": "test", "status": qe.STATUS_PASSED}]),
        _case("reference_high", expected_pass=True, risk=qe.RISK_HIGH, reports=good_high, combined=_combined(), approval=True),
        _case("worker_provider_missing", expected_pass=False, expected_blocker="worker_provider_missing_for_independence:", risk=qe.RISK_HIGH, reports=good_high, combined=_combined(), worker_provider="", approval=True),
        _case("independent_reviewer", expected_pass=False, expected_blocker="independent_reviewer_missing:", risk=qe.RISK_HIGH, reports=[_report(lens, provider="worker-a") for lens in sorted(qe.JUDGMENT_LENSES)], combined=_combined(), approval=True),
        _case("human_approval", expected_pass=False, expected_blocker="explicit_human_approval_missing", risk=qe.RISK_HIGH, reports=good_high, combined=_combined()),
        _case("quality_config", expected_pass=False, expected_blocker="quality_config_error", config_error="broken policy"),
    ]
    false_greens = [row["case_id"] for row in cases if not row["expected_pass"] and row["actual_pass"]]
    false_reds = [row["case_id"] for row in cases if row["expected_pass"] and not row["actual_pass"]]
    uncalibrated = [row["case_id"] for row in cases if not row["calibrated"]]
    negatives = sum(1 for row in cases if not row["expected_pass"])
    positives = len(cases) - negatives
    return {
        "ok": not uncalibrated,
        "schema_id": SCHEMA_ID,
        "case_count": len(cases),
        "positive_count": positives,
        "negative_count": negatives,
        "false_green_count": len(false_greens),
        "false_red_count": len(false_reds),
        "false_green_rate": len(false_greens) / negatives if negatives else 0.0,
        "false_red_rate": len(false_reds) / positives if positives else 0.0,
        "false_green_cases": false_greens,
        "false_red_cases": false_reds,
        "uncalibrated_cases": uncalibrated,
        "cases": cases,
    }


__all__ = ["SCHEMA_ID", "run_calibration"]
