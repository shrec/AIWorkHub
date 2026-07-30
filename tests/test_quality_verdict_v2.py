from __future__ import annotations

import json

import pytest

from aiworkhub import quality_evidence as qe


def _check(
    check_id: str = "tests",
    *,
    kind: str = "test",
    status: str = qe.STATUS_PASSED,
) -> qe.EvidenceCheck:
    return qe.EvidenceCheck(check_id=check_id, kind=kind, status=status)


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
    finding_id: str = "bug-1",
    *,
    severity: str = qe.SEVERITY_HIGH,
) -> dict[str, str]:
    return {
        "id": finding_id,
        "severity": severity,
        "summary": "boundary behavior is incorrect",
        "evidence": "tests/test_boundary.py::test_empty does not exercise the changed branch",
    }


def test_low_risk_good_mechanical_evidence_is_verified() -> None:
    verdict = qe.fold_quality_verdict([_check()])

    assert verdict["status"] == "verified"
    assert verdict["passed"] is True
    assert verdict["blocking_evidence"] == []
    assert verdict["risk_profile"]["effective_tier"] == "low"


def test_mechanical_failure_is_always_blocking() -> None:
    verdict = qe.fold_quality_verdict([_check(status=qe.STATUS_FAILED)])

    assert verdict["passed"] is False
    assert verdict["blocking_evidence"] == ["tests"]


def test_unavailable_required_mechanical_evidence_is_never_passed() -> None:
    verdict = qe.fold_quality_verdict(
        [_check("security", kind="security", status=qe.STATUS_NOT_AVAILABLE)]
    )

    assert verdict["passed"] is False
    assert verdict["blocking_evidence"] == ["security"]


def test_risk_signals_can_raise_but_never_lower_requested_tier() -> None:
    raised = qe.resolve_risk_profile("low", signals=["security_sensitive"])
    retained = qe.resolve_risk_profile("high", signals=["public_api"])
    released = qe.resolve_risk_profile("low", signals=["release"])

    assert raised["effective_tier"] == "high"
    assert retained["effective_tier"] == "high"
    assert released["effective_tier"] == "critical"


def test_unknown_risk_input_fails_closed() -> None:
    with pytest.raises(qe.MalformedConfigError):
        qe.resolve_risk_profile("tiny")
    with pytest.raises(qe.MalformedConfigError):
        qe.resolve_risk_profile("low", signals=["worker_says_safe"])


def test_medium_risk_requires_correctness_review_and_combined_tree() -> None:
    profile = qe.resolve_risk_profile("medium")
    verdict = qe.fold_quality_verdict([_check()], risk_profile=profile)

    assert verdict["passed"] is False
    assert "required_reviewer_missing:correctness" in verdict["blocking_evidence"]
    assert "combined_tree_evidence_missing" in verdict["blocking_evidence"]


def test_medium_risk_passes_with_read_only_review_and_green_union() -> None:
    profile = qe.resolve_risk_profile("medium")
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=[_report(qe.LENS_CORRECTNESS)],
        combined_tree_checks=[_check("union-tests")],
        worker_provider="worker-a",
    )

    assert verdict["passed"] is True
    assert verdict["blocking_evidence"] == []


def test_high_risk_requires_cross_provider_for_every_judgment_lens() -> None:
    profile = qe.resolve_risk_profile("high")
    reports = [
        _report(lens, provider="worker-a")
        for lens in sorted(qe.JUDGMENT_LENSES)
    ]
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=reports,
        combined_tree_checks=[_check("union-tests")],
        worker_provider="worker-a",
    )

    assert verdict["passed"] is False
    assert all(
        f"independent_reviewer_missing:{lens}" in verdict["blocking_evidence"]
        for lens in qe.JUDGMENT_LENSES
    )


def test_high_risk_cross_provider_clean_reports_are_verified() -> None:
    profile = qe.resolve_risk_profile("high")
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=[_report(lens) for lens in sorted(qe.JUDGMENT_LENSES)],
        combined_tree_checks=[_check("union-tests")],
        worker_provider="worker-a",
    )

    assert verdict["passed"] is True
    assert verdict["status"] == "verified"


def test_model_supplied_pass_cannot_override_blocking_finding() -> None:
    verdict = qe.fold_quality_verdict(
        [_check()],
        reviewer_reports=[
            _report(
                qe.LENS_CORRECTNESS,
                findings=[_finding()],
                verdict="PASS",
                passed=True,
            )
        ],
    )

    assert verdict["passed"] is False
    assert verdict["blocking_evidence"] == ["reviewer:correctness:bug-1"]
    assert "verdict" not in verdict["reviewer_reports"][0]


def test_nonblocking_correctness_finding_still_requires_refinement() -> None:
    verdict = qe.fold_quality_verdict(
        [_check()],
        reviewer_reports=[
            _report(
                qe.LENS_CORRECTNESS,
                findings=[_finding(severity=qe.SEVERITY_LOW)],
            )
        ],
    )

    assert verdict["passed"] is False
    assert verdict["refine_required"] is True
    assert verdict["blocking_evidence"] == [
        "refinement_required:reviewer:correctness:bug-1"
    ]


def test_malformed_or_mutating_reviewer_report_fails_closed() -> None:
    report = _report(qe.LENS_SECURITY)
    report["can_mutate_repo"] = True
    verdict = qe.fold_quality_verdict([_check()], reviewer_reports=[report])

    assert verdict["passed"] is False
    assert verdict["blocking_evidence"] == ["reviewer_schema:0:not_read_only"]


def test_required_combined_tree_skipped_or_failed_is_blocking() -> None:
    profile = qe.resolve_risk_profile("medium")
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=[_report(qe.LENS_CORRECTNESS)],
        combined_tree_checks=[_check("union", status=qe.STATUS_SKIPPED)],
    )

    assert verdict["passed"] is False
    assert verdict["blocking_evidence"] == ["combined_tree:union"]


def test_completion_gate_exposes_same_pure_verdict_without_breaking_low_risk(tmp_path) -> None:
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = qe.run_completion_quality_gate(tmp_path, changed_paths=["good.py"])

    assert packet["passed"] is True
    assert packet["quality_verdict"]["passed"] is True
    assert packet["risk_profile"]["effective_tier"] == "low"
    assert packet["blocking_checks"] == packet["quality_verdict"]["blocking_evidence"]


def test_completion_gate_fail_closes_invalid_requested_risk(tmp_path) -> None:
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = qe.run_completion_quality_gate(
        tmp_path,
        changed_paths=["good.py"],
        requested_risk_tier="worker-invented",
    )

    assert packet["passed"] is False
    assert packet["blocking_checks"] == ["quality_verdict_schema_error"]


def test_public_contract_names_pure_verdict_and_all_profiles() -> None:
    contract = qe.quality_verdict_contract()

    assert contract["verdict_owner"] == "pure_deterministic_fold"
    assert contract["model_verdict_accepted"] is False
    assert contract["quality_lenses"] == list(qe.QUALITY_LENSES)
    assert list(contract["profiles"]) == list(qe.RISK_TIERS)


def test_negative_fixture_matrix_good_green_each_bad_red(tmp_path) -> None:
    """Deterministic gate trust: reference passes; each targeted defect fails."""

    good = tmp_path / "good"
    syntax_bad = tmp_path / "syntax_bad"
    unavailable_bad = tmp_path / "unavailable_bad"
    for root in (good, syntax_bad, unavailable_bad):
        root.mkdir()
    (good / "module.py").write_text("answer = 42\n", encoding="utf-8")
    (syntax_bad / "module.py").write_text("def broken(:\n", encoding="utf-8")
    (unavailable_bad / "module.py").write_text("answer = 42\n", encoding="utf-8")
    (unavailable_bad / ".aiworkhub").mkdir()
    (unavailable_bad / ".aiworkhub" / "quality.json").write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "id": "required-security",
                        "kind": "security",
                        "command": ["not-a-real-quality-command"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    matrix = {
        "good": qe.run_completion_quality_gate(good, changed_paths=["module.py"]),
        "bad_syntax": qe.run_completion_quality_gate(
            syntax_bad, changed_paths=["module.py"]
        ),
        "bad_unavailable": qe.run_completion_quality_gate(
            unavailable_bad, changed_paths=["module.py"]
        ),
    }

    assert matrix["good"]["passed"] is True
    assert matrix["bad_syntax"]["passed"] is False
    assert matrix["bad_unavailable"]["passed"] is False
