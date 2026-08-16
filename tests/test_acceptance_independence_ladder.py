"""Acceptance-fold independence ladder.

These tests exercise :func:`aiworkhub.quality_evidence.fold_quality_verdict`,
the *acceptance* fold, after it was converted from a vendor comparison to the
recorded independence ladder
(:func:`aiworkhub.quality_review.resolve_independence_rung`).

The defect was that a high/critical acceptance required a reviewer whose
provider differed from the worker's.  On a single-provider install the only
sighted reviewer shares the worker's provider, so ``independent_reviewer_missing``
was permanent and no high-risk card could ever be accepted, however good it was.

The fold now records the achieved rung per required lens and blocks only when no
rung applies at all (an unattributable worker).  What actually produces
independence -- the anti-anchored, sealed packet reviewed by a separate
read-only process that submits through the authenticated ``packet_sha256``-bound
tool -- holds on every rung and is unchanged; a blind reviewer that cannot
inspect its packet is still refused for its lens.
"""

from __future__ import annotations

from aiworkhub import quality_evidence as qe
from aiworkhub import quality_review as qr


def _check(
    check_id: str = "tests",
    *,
    kind: str = "test",
    status: str = qe.STATUS_PASSED,
    summary: str = "",
) -> qe.EvidenceCheck:
    return qe.EvidenceCheck(
        check_id=check_id, kind=kind, status=status, summary=summary
    )


def _report(
    lens: str,
    *,
    provider: str,
    model: str = "",
    findings: list[dict[str, str]] | None = None,
    **extra: object,
) -> dict[str, object]:
    report: dict[str, object] = {
        "lens": lens,
        "provider": provider,
        "read_only": True,
        "can_mutate_repo": False,
        "findings": findings or [],
    }
    if model:
        report["model"] = model
    report.update(extra)
    return report


def _blind_finding() -> dict[str, str]:
    # A reviewer that could not inspect its packet self-reports process_limit;
    # a non-defect finding must be low severity to pass schema normalization.
    return {
        "id": "blocked-1",
        "severity": qe.SEVERITY_LOW,
        "disposition": "process_limit",
        "summary": "reviewer had no file-read tool and no inline packet",
        "evidence": "usage: 0 input / 0 output tokens observed",
    }


def _high_fold(reports, *, worker_provider="claude_cli", worker_model=""):
    return qe.fold_quality_verdict(
        [_check()],
        risk_profile=qe.resolve_risk_profile("high"),
        reviewer_reports=reports,
        combined_tree_checks=[_check("union-tests")],
        worker_provider=worker_provider,
        worker_model=worker_model,
        human_approval=True,
    )


def test_single_provider_high_risk_passes_at_same_model_fresh_context() -> None:
    """The exact scenario that is stuck on a single-provider install today.

    Three lens reports whose provider equals the worker provider (and here the
    same model) run the real high-risk profile.  The verdict PASSES and the
    recorded rung for every required lens is ``same_model_fresh_context``.
    """

    worker_provider = "claude_cli"
    reports = [
        _report(lens, provider=worker_provider)
        for lens in sorted(qe.JUDGMENT_LENSES)
    ]

    verdict = _high_fold(reports, worker_provider=worker_provider)

    assert verdict["passed"] is True
    assert verdict["status"] == "verified"
    assert verdict["blocking_evidence"] == []
    required = qe.resolve_risk_profile("high")["required_reviewer_lenses"]
    for lens in required:
        record = verdict["independence_rungs"][lens]
        assert record["rung"] == qr.RUNG_SAME_MODEL_FRESH_CONTEXT
        # The rung is written onto the lens row too, so an accepted card records
        # how independent each review was.
        row = next(r for r in verdict["lenses"] if r["lens"] == lens)
        assert row["independence_rung"] == qr.RUNG_SAME_MODEL_FRESH_CONTEXT
        assert row["independence"]["worker_provider"] == worker_provider


def test_cross_provider_review_records_the_best_rung() -> None:
    reports = [
        _report(lens, provider="reviewer_gpt")
        for lens in sorted(qe.JUDGMENT_LENSES)
    ]

    verdict = _high_fold(reports, worker_provider="claude_cli")

    assert verdict["passed"] is True
    for lens in qe.resolve_risk_profile("high")["required_reviewer_lenses"]:
        assert verdict["independence_rungs"][lens]["rung"] == qr.RUNG_CROSS_PROVIDER


def test_same_provider_different_model_records_cross_model_rung() -> None:
    reports = [
        _report(lens, provider="claude_cli", model="opus")
        for lens in sorted(qe.JUDGMENT_LENSES)
    ]

    verdict = _high_fold(
        reports, worker_provider="claude_cli", worker_model="haiku"
    )

    assert verdict["passed"] is True
    for lens in qe.resolve_risk_profile("high")["required_reviewer_lenses"]:
        assert (
            verdict["independence_rungs"][lens]["rung"]
            == qr.RUNG_CROSS_MODEL_SAME_PROVIDER
        )


def test_cross_provider_required_no_longer_gates_acceptance() -> None:
    """The ``cross_provider_required`` flag no longer refuses a same-provider
    review; the fold blocks only when no rung in the ladder applies."""

    reports = [
        _report(lens, provider="claude_cli")
        for lens in sorted(qe.JUDGMENT_LENSES)
    ]

    verdict = _high_fold(reports, worker_provider="claude_cli")

    assert verdict["passed"] is True
    assert not any(
        b.startswith("independent_reviewer_missing")
        for b in verdict["blocking_evidence"]
    )


def test_missing_worker_provider_is_the_only_no_rung_blocker() -> None:
    """An unattributable review is not a review: with no ``worker_provider`` no
    rung applies, so every required lens blocks and no rung is recorded."""

    reports = [
        _report(lens, provider="claude_cli")
        for lens in sorted(qe.JUDGMENT_LENSES)
    ]

    verdict = _high_fold(reports, worker_provider="")

    assert verdict["passed"] is False
    required = qe.resolve_risk_profile("high")["required_reviewer_lenses"]
    for lens in required:
        assert (
            f"worker_provider_missing_for_independence:{lens}"
            in verdict["blocking_evidence"]
        )
    assert verdict["independence_rungs"] == {}


def test_blind_reviewer_is_still_refused_for_its_lens() -> None:
    """Content delivery / the ladder never accept a reviewer that could not
    inspect its packet; a blind lens blocks even when its provider would
    otherwise resolve a rung, and no rung is recorded for it."""

    reports = [
        _report(qe.LENS_CORRECTNESS, provider="claude_cli"),
        _report(
            qe.LENS_SECURITY,
            provider="claude_cli",
            findings=[_blind_finding()],
        ),
        _report(qe.LENS_CODE_QUALITY, provider="claude_cli"),
    ]

    verdict = _high_fold(reports, worker_provider="claude_cli")

    assert verdict["passed"] is False
    assert (
        f"reviewer_could_not_inspect:{qe.LENS_SECURITY}"
        in verdict["blocking_evidence"]
    )
    security_row = next(
        r for r in verdict["lenses"] if r["lens"] == qe.LENS_SECURITY
    )
    assert security_row["status"] == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    assert qe.LENS_SECURITY not in verdict["independence_rungs"]
    # The sighted lenses still resolve and record their rung.
    assert (
        verdict["independence_rungs"][qe.LENS_CORRECTNESS]["rung"]
        == qr.RUNG_SAME_MODEL_FRESH_CONTEXT
    )


def test_reviewer_supplied_pass_never_overrides_the_fold() -> None:
    """Final status belongs to the fold: a reviewer-supplied ``passed``/``verdict``
    is discarded, and a blocking finding still fails the lens even when the
    reviewer shares the worker provider and would resolve a rung."""

    reports = [
        _report(qe.LENS_CORRECTNESS, provider="claude_cli"),
        _report(
            qe.LENS_SECURITY,
            provider="claude_cli",
            verdict="PASS",
            passed=True,
            findings=[
                {
                    "id": "sec-1",
                    "severity": qe.SEVERITY_HIGH,
                    "summary": "auth bypass on the changed branch",
                    "evidence": "src/aiworkhub/core.py:123 opens a rw handle",
                }
            ],
        ),
        _report(qe.LENS_CODE_QUALITY, provider="claude_cli"),
    ]

    verdict = _high_fold(reports, worker_provider="claude_cli")

    assert verdict["passed"] is False
    assert "reviewer:security:sec-1" in verdict["blocking_evidence"]
    assert "verdict" not in verdict["reviewer_reports"][0]
    assert "passed" not in verdict["reviewer_reports"][0]
