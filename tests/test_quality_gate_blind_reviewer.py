from __future__ import annotations

import pytest

from aiworkhub import quality_evidence as qe


def _check(
    check_id: str = "tests",
    *,
    kind: str = "test",
    status: str = qe.STATUS_PASSED,
) -> qe.EvidenceCheck:
    return qe.EvidenceCheck(
        check_id=check_id,
        kind=kind,
        status=status,
        summary="",
    )


def _report(
    lens: str,
    *,
    provider: str = "reviewer-b",
    findings: list[dict[str, str]] | None = None,
    **extra: object,
) -> dict[str, object]:
    return {
        "lens": lens,
        "provider": provider,
        "read_only": True,
        "can_mutate_repo": False,
        "findings": findings or [],
        **extra,
    }


def _finding(
    finding_id: str = "pl-1",
    *,
    severity: str = qe.SEVERITY_LOW,
    disposition: str = qe.FINDING_DISPOSITION_PROCESS_LIMIT,
) -> dict[str, str]:
    return {
        "id": finding_id,
        "severity": severity,
        "disposition": disposition,
        "summary": "reviewer could not inspect the packet",
        "evidence": "no file-read tool was available for the packet path",
    }


def _medium_verdict(reports: list[dict[str, object]]) -> dict[str, object]:
    return qe.fold_quality_verdict(
        [],
        risk_profile=qe.resolve_risk_profile(qe.RISK_MEDIUM),
        reviewer_reports=reports,
        combined_tree_checks=[_check("union-tests")],
    )


def _lens_row(verdict: dict[str, object], lens: str) -> dict[str, object]:
    return next(row for row in verdict["lenses"] if row["lens"] == lens)


def test_all_process_limit_findings_flag_lens_could_not_inspect() -> None:
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, findings=[_finding()])])

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]
    assert (
        _lens_row(verdict, qe.LENS_CORRECTNESS)["status"]
        == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )


def test_present_zero_activity_usage_flags_lens_could_not_inspect() -> None:
    usage = {"usage_observed": False, "input_tokens": 0, "output_tokens": 0}
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, usage=usage)])

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]
    assert (
        _lens_row(verdict, qe.LENS_CORRECTNESS)["status"]
        == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )


def test_missing_usage_telemetry_still_satisfies_lens() -> None:
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS)])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]
    assert _lens_row(verdict, qe.LENS_CORRECTNESS)["status"] == qe.STATUS_PASSED


def test_genuine_defect_finding_is_not_blind() -> None:
    finding = _finding("defect-1", disposition="defect")
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, findings=[finding])])

    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]
    assert verdict["refine_required"] is True
    assert (
        _lens_row(verdict, qe.LENS_CORRECTNESS)["status"]
        != qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )


def test_genuine_observation_finding_is_not_blind() -> None:
    finding = _finding("obs-1", disposition="observation")
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, findings=[finding])])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]


def test_genuine_zero_findings_review_satisfies_lens() -> None:
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS)])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]
    assert _lens_row(verdict, qe.LENS_CORRECTNESS)["status"] == qe.STATUS_PASSED


def test_low_risk_process_limit_finding_stays_non_blocking() -> None:
    verdict = qe.fold_quality_verdict(
        [],
        risk_profile=qe.resolve_risk_profile(qe.RISK_LOW),
        reviewer_reports=[_report(qe.LENS_CORRECTNESS, findings=[_finding()])],
    )

    assert verdict["passed"] is True
    assert verdict["blocking_evidence"] == []


def test_mixed_process_limit_and_observation_is_not_blind() -> None:
    findings = [
        _finding("pl-1"),
        _finding("obs-1", disposition="observation"),
    ]
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, findings=findings)])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]


def test_zero_activity_usage_with_no_findings_flags_blind() -> None:
    usage = {"usage_observed": False, "input_tokens": 0, "output_tokens": 0}
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, usage=usage)])

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]


def test_present_nonzero_usage_is_not_blind() -> None:
    usage = {"usage_observed": True, "input_tokens": 120, "output_tokens": 45}
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, usage=usage)])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]


def test_partial_zero_usage_is_not_blind() -> None:
    usage = {"usage_observed": False, "input_tokens": 0, "output_tokens": 5}
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, usage=usage)])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]


def test_usage_observed_true_with_zero_tokens_is_not_blind() -> None:
    usage = {"usage_observed": True, "input_tokens": 0, "output_tokens": 0}
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, usage=usage)])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]


def test_high_risk_blind_security_lens_is_flagged() -> None:
    reports = [
        _report(qe.LENS_CORRECTNESS),
        _report(qe.LENS_SECURITY, findings=[_finding("pl-sec")]),
        _report(qe.LENS_CODE_QUALITY),
    ]
    verdict = qe.fold_quality_verdict(
        [],
        risk_profile=qe.resolve_risk_profile(qe.RISK_HIGH),
        reviewer_reports=reports,
        combined_tree_checks=[_check("union-tests")],
        worker_provider="worker-a",
        human_approval=True,
    )

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:security" in verdict["blocking_evidence"]
    assert (
        _lens_row(verdict, qe.LENS_SECURITY)["status"]
        == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )


def test_normalize_reviewer_reports_drops_usage_key() -> None:
    usage = {"usage_observed": False, "input_tokens": 0, "output_tokens": 0}
    normalized, errors = qe.normalize_reviewer_reports(
        [_report(qe.LENS_CORRECTNESS, usage=usage)]
    )

    assert errors == []
    assert set(normalized[0].keys()) == {
        "lens",
        "provider",
        "read_only",
        "can_mutate_repo",
        "findings",
    }


def test_helper_all_process_limit_findings_true() -> None:
    assert qe.reviewer_report_could_not_inspect(
        {"findings": [{"disposition": "process_limit"}]}
    ) is True


def test_helper_zero_activity_usage_true() -> None:
    assert (
        qe.reviewer_report_could_not_inspect(
            {"usage": {"usage_observed": False, "input_tokens": 0, "output_tokens": 0}}
        )
        is True
    )


def test_helper_empty_findings_false() -> None:
    assert qe.reviewer_report_could_not_inspect({"findings": []}) is False


def test_helper_missing_usage_is_not_blind() -> None:
    assert qe.reviewer_report_could_not_inspect({}) is False


def test_helper_mixed_disposition_false() -> None:
    assert (
        qe.reviewer_report_could_not_inspect(
            {"findings": [{"disposition": "process_limit"}, {"disposition": "defect"}]}
        )
        is False
    )


def test_helper_nonzero_usage_false() -> None:
    assert (
        qe.reviewer_report_could_not_inspect(
            {"usage": {"usage_observed": True, "input_tokens": 1, "output_tokens": 0}}
        )
        is False
    )


def test_helper_non_mapping_usage_is_not_blind() -> None:
    assert qe.reviewer_report_could_not_inspect({"usage": "n/a"}) is False
