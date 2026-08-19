"""NF-WAVE-VERDICT-VOCABULARY: the residue of the empirical audit re-measured on
canonical v0.9.91. Five regressions, one per finding, each failing on canonical
for that reason alone.

1. A status the fold writes (reviewer_could_not_inspect) that VALID_STATUSES
   rejects.
2. quality_verdict_contract() not publishing the lens status vocabulary.
3. evidence_verdict([], []) reading as passed -- green on a vacuum.
4. The independence rung over-/under-reported because the model was resolved by
   the FIRST (lens, provider) match instead of from the report it describes.
5. The combined-tree exemption matching one literal string and missing its
   tier-interpolated sibling, turning a minimum_risk check into a permanent
   blocker.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import quality_evidence as qe  # noqa: E402
from aiworkhub import task_fsm  # noqa: E402


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


def _process_limit_finding() -> dict[str, str]:
    return {
        "id": "blind-1",
        "severity": qe.SEVERITY_LOW,
        "disposition": qe.FINDING_DISPOSITION_PROCESS_LIMIT,
        "summary": "reviewer was prevented from inspecting the packet",
        "evidence": "no packet bytes were made available to the reviewer process",
    }


# --- Finding 1: a status the fold writes that its validity set rejects --------


def test_reviewer_could_not_inspect_is_a_valid_status() -> None:
    # One-line measurement: the fold writes reviewer_could_not_inspect and the
    # module's own validity set must contain it. On canonical VALID_STATUSES is
    # frozenset({not_available, passed, failed, skipped}) and this fails.
    assert qe.STATUS_REVIEWER_COULD_NOT_INSPECT in qe.VALID_STATUSES


def test_every_lens_status_the_fold_writes_is_valid() -> None:
    # A blind required reviewer makes the fold WRITE reviewer_could_not_inspect
    # into the correctness lens row. Every status any lens row carries must be a
    # member of VALID_STATUSES, proven for reviewer_could_not_inspect
    # specifically -- so the next status the fold learns to write cannot repeat
    # the drift.
    profile = qe.resolve_risk_profile("medium")
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=[
            _report(qe.LENS_CORRECTNESS, findings=[_process_limit_finding()])
        ],
        combined_tree_checks=[_check("union-tests")],
        worker_provider="worker-a",
    )

    correctness = next(
        row for row in verdict["lenses"] if row["lens"] == qe.LENS_CORRECTNESS
    )
    assert correctness["status"] == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    for row in verdict["lenses"]:
        assert row["status"] in qe.VALID_STATUSES
    assert qe.STATUS_REVIEWER_COULD_NOT_INSPECT in qe.VALID_STATUSES


# --- Finding 2: the contract must publish the status vocabulary ---------------


def test_contract_publishes_lens_status_vocabulary_from_the_same_constant() -> None:
    # On canonical quality_verdict_contract() has no lens_statuses key: a
    # consumer cannot learn reviewer_could_not_inspect exists. It must be
    # published from VALID_STATUSES itself, never a second hand-written list.
    contract = qe.quality_verdict_contract()
    assert "lens_statuses" in contract
    assert set(contract["lens_statuses"]) == set(qe.VALID_STATUSES)
    assert qe.STATUS_REVIEWER_COULD_NOT_INSPECT in contract["lens_statuses"]


# --- Finding 3: an empty verdict must not read as passed ----------------------


def test_empty_evidence_verdict_is_not_passed() -> None:
    # One-line measurement: on canonical evidence_verdict([], [])["passed"] is
    # True -- green on a vacuum. Nothing measured is not nothing wrong.
    verdict = task_fsm.evidence_verdict([], [])
    assert verdict["passed"] is False
    assert verdict["nothing_measured"] is True


def test_evidence_verdict_distinguishes_nothing_measured_from_nothing_wrong() -> None:
    empty = task_fsm.evidence_verdict([], [])
    clean = task_fsm.evidence_verdict([{"returncode": 0}], [])
    assert empty["passed"] is False and empty["nothing_measured"] is True
    assert clean["passed"] is True and clean["nothing_measured"] is False


# --- Finding 4: the rung is derived from the report it describes ---------------


def test_independence_rung_is_derived_per_report_not_first_match() -> None:
    # Two reports for the same lens and provider on different models. On canonical
    # the model is resolved by the FIRST (lens, provider) match, so the cross-model
    # reviewer is invisible and correctness is recorded at the same_model rung.
    # A rung derived from the report it describes reaches cross_model_same_provider.
    raw_reports = [
        _report(qe.LENS_CORRECTNESS, provider="claude", model="opus-5"),
        _report(qe.LENS_CORRECTNESS, provider="claude", model="sonnet-5"),
    ]
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=qe.resolve_risk_profile("high"),
        reviewer_reports=raw_reports,
        worker_provider="claude",
        worker_model="opus-5",
    )

    rung = verdict["independence_rungs"][qe.LENS_CORRECTNESS]["rung"]
    assert rung == qe.quality_review.RUNG_CROSS_MODEL_SAME_PROVIDER
    # Each report resolves to its OWN model -- canonical measured ['opus-5',
    # 'opus-5'] (both the first match); the fix resolves ['opus-5', 'sonnet-5'].
    assert [qe._reviewer_model_for(r) for r in raw_reports] == ["opus-5", "sonnet-5"]


# --- Finding 5: the exemption matches the reason KIND, not one literal ---------


def test_risk_below_minimum_combined_tree_check_is_exempt_like_its_sibling() -> None:
    # A declared check with minimum_risk critical is skipped on a high-tier card
    # for the tier-interpolated reason risk_below_minimum:high<critical. On
    # canonical the exemption matches only the fixed changed_paths_not_applicable
    # string, so this check becomes a permanent combined_tree blocker no tier can
    # clear. Matching the reason KIND exempts it on the same footing as its sibling.
    profile = qe.resolve_risk_profile("high")
    skipped_for_risk = {
        "check_id": "critical-only",
        "kind": "security",
        "status": qe.STATUS_SKIPPED,
        "summary": f"{qe.APPLICABILITY_SKIP_RISK_BELOW_MINIMUM}:high<critical",
    }
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        combined_tree_checks=[skipped_for_risk],
    )
    assert "combined_tree:critical-only" not in verdict["blocking_evidence"]

    # The sibling is still exempt, and a skip with no applicability reason still
    # blocks -- so the kind match is exact, not an over-broadened pass.
    changed_paths_skip = {
        "check_id": "scoped-out",
        "kind": "test",
        "status": qe.STATUS_SKIPPED,
        "summary": qe.APPLICABILITY_SKIP_CHANGED_PATHS,
    }
    plain_skip = {
        "check_id": "plain",
        "kind": "test",
        "status": qe.STATUS_SKIPPED,
        "summary": "",
    }
    verdict2 = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        combined_tree_checks=[changed_paths_skip, plain_skip],
    )
    assert "combined_tree:scoped-out" not in verdict2["blocking_evidence"]
    assert "combined_tree:plain" in verdict2["blocking_evidence"]


def test_risk_below_minimum_is_exempt_only_when_no_tier_could_have_run_it() -> None:
    """The narrowing that keeps fix #6's guarantee intact.

    ``risk_below_minimum`` must not be exempt wholesale. Exempting every such
    skip discards the guarantee that a combined tree carries the parent fold's
    tier: a medium check skipped because the sub-gate ran at LOW should still
    block, because it should have run. Only a minimum the parent tier itself
    cannot reach is unclearable, and therefore exempt.
    """

    from aiworkhub.quality_evidence import _combined_tree_skip_exempt

    # Unclearable: no sub-gate tier could have run a critical check on a high card.
    assert _combined_tree_skip_exempt(
        "risk_below_minimum:high<critical", parent_tier="high"
    )
    # Mis-scoped sub-gate: the parent is at medium and the check's minimum is
    # medium, so it should have run. Still blocks.
    assert not _combined_tree_skip_exempt(
        "risk_below_minimum:low<medium", parent_tier="medium"
    )
    # Never applied to this change at any tier.
    assert _combined_tree_skip_exempt(
        "changed_paths_not_applicable", parent_tier="low"
    )
    # An applicability claim that cannot be read is not an applicability claim.
    assert not _combined_tree_skip_exempt(
        "risk_below_minimum:garbage", parent_tier="high"
    )
    assert not _combined_tree_skip_exempt("some_other_reason:x", parent_tier="high")
