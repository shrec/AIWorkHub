"""Regression: a blind reviewer is never reported as passed, at ANY tier.

NF-2026-00344. ``blind_lenses`` was built for every judgment lens but consumed
only inside the required-lenses loop, so at medium tier (only correctness
required) a blind security or code_quality reviewer was never checked -- and a
report's mere EXISTENCE lifted its lens from skipped to passed. These tests pin
the corrected behavior: blindness is a property of the REPORT, applied to every
judgment lens, and only a reviewer that actually observed the packet lifts a
lens to passed.
"""

from __future__ import annotations

from aiworkhub import quality_evidence as qe


BLIND_USAGE = {"usage_observed": False, "input_tokens": 0, "output_tokens": 0}


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


def _process_limit_finding(finding_id: str = "pl-1") -> dict[str, str]:
    return {
        "id": finding_id,
        "severity": qe.SEVERITY_LOW,
        "disposition": qe.FINDING_DISPOSITION_PROCESS_LIMIT,
        "summary": "reviewer could not inspect the packet",
        "evidence": "no file-read tool was available for the packet path",
    }


def _lens_row(verdict: dict[str, object], lens: str) -> dict[str, object]:
    return next(row for row in verdict["lenses"] if row["lens"] == lens)


def test_medium_tier_blind_security_and_code_quality_are_never_verified() -> None:
    # At medium tier only correctness is required. A sighted correctness review
    # plus a green combined tree WOULD verify; adding blind security and
    # code_quality reviewers (usage_observed False, zero tokens) must break
    # verification rather than silently lifting those lenses to passed.
    profile = qe.resolve_risk_profile(qe.RISK_MEDIUM)
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=[
            _report(qe.LENS_CORRECTNESS),
            _report(qe.LENS_SECURITY, usage=dict(BLIND_USAGE)),
            _report(qe.LENS_CODE_QUALITY, usage=dict(BLIND_USAGE)),
        ],
        combined_tree_checks=[_check("union")],
        worker_provider="worker-a",
    )

    assert verdict["status"] == "unverified"
    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:security" in verdict["blocking_evidence"]
    assert "reviewer_could_not_inspect:code_quality" in verdict["blocking_evidence"]
    assert (
        _lens_row(verdict, qe.LENS_SECURITY)["status"]
        == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )
    assert (
        _lens_row(verdict, qe.LENS_CODE_QUALITY)["status"]
        == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )
    # Neither blind lens is ever reported passed.
    assert _lens_row(verdict, qe.LENS_SECURITY)["status"] != qe.STATUS_PASSED
    assert _lens_row(verdict, qe.LENS_CODE_QUALITY)["status"] != qe.STATUS_PASSED
    # The sighted correctness reviewer is still honoured.
    assert _lens_row(verdict, qe.LENS_CORRECTNESS)["status"] == qe.STATUS_PASSED


def test_blindness_is_applied_to_every_judgment_lens_not_only_required() -> None:
    # Same defect via the all-process_limit blindness signal on a non-required
    # lens (code_quality at medium). The loop change is what makes this block.
    profile = qe.resolve_risk_profile(qe.RISK_MEDIUM)
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=[
            _report(qe.LENS_CORRECTNESS),
            _report(qe.LENS_CODE_QUALITY, findings=[_process_limit_finding()]),
        ],
        combined_tree_checks=[_check("union")],
        worker_provider="worker-a",
    )

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:code_quality" in verdict["blocking_evidence"]
    assert (
        _lens_row(verdict, qe.LENS_CODE_QUALITY)["status"]
        == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )


def test_report_existence_alone_no_longer_lifts_a_blind_lens_to_passed() -> None:
    # At low tier NO lens is required, so an unsolicited blind review is
    # non-blocking noise. What changed is that its mere EXISTENCE no longer
    # lifts the lens skipped->passed: the lens stays skipped (never passed)
    # rather than being reported as a green row nothing observed. Mechanical
    # checks are empty here so the blind reviewer is the only thing that could
    # have moved the correctness row off skipped.
    verdict = qe.fold_quality_verdict(
        [],
        risk_profile=qe.resolve_risk_profile(qe.RISK_LOW),
        reviewer_reports=[_report(qe.LENS_CORRECTNESS, usage=dict(BLIND_USAGE))],
    )

    assert verdict["passed"] is True
    assert verdict["blocking_evidence"] == []
    assert _lens_row(verdict, qe.LENS_CORRECTNESS)["status"] != qe.STATUS_PASSED
    assert _lens_row(verdict, qe.LENS_CORRECTNESS)["status"] == qe.STATUS_SKIPPED


def test_blind_lens_at_active_review_tier_is_reviewer_could_not_inspect() -> None:
    # The counterpart at a tier that DOES run an attributable review: the same
    # blind non-required lens (security at medium) is downgraded from a would-be
    # passed row to reviewer_could_not_inspect and blocks.
    profile = qe.resolve_risk_profile(qe.RISK_MEDIUM)
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=[
            _report(qe.LENS_CORRECTNESS),
            _report(qe.LENS_SECURITY, usage=dict(BLIND_USAGE)),
        ],
        combined_tree_checks=[_check("union")],
        worker_provider="worker-a",
    )

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:security" in verdict["blocking_evidence"]
    assert (
        _lens_row(verdict, qe.LENS_SECURITY)["status"]
        == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )


def test_a_sighted_reviewer_still_lifts_its_lens_to_passed() -> None:
    # Positive control: what distinguishes a report that lifts a lens to passed
    # is that its reviewer actually observed the packet. A sighted zero-finding
    # review (no usage telemetry) still satisfies its lens.
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=qe.resolve_risk_profile(qe.RISK_LOW),
        reviewer_reports=[_report(qe.LENS_SECURITY)],
    )

    assert verdict["passed"] is True
    assert _lens_row(verdict, qe.LENS_SECURITY)["status"] == qe.STATUS_PASSED


def test_blind_required_correctness_lens_still_blocks() -> None:
    # Symmetry check: the same blindness on the tier's required lens blocks, as
    # it always did -- the fix widened the check, it did not weaken it.
    profile = qe.resolve_risk_profile(qe.RISK_MEDIUM)
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=[_report(qe.LENS_CORRECTNESS, usage=dict(BLIND_USAGE))],
        combined_tree_checks=[_check("union")],
        worker_provider="worker-a",
    )

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]
    assert (
        _lens_row(verdict, qe.LENS_CORRECTNESS)["status"]
        == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )
