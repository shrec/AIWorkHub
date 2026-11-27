"""Regression tests for fix batch 12 (NF-2026-00468, 00598, 00601, 00663)."""

from __future__ import annotations

from aiworkhub import quality_evidence as qe
from aiworkhub import quality_reviewer


# NF-2026-00468 -------------------------------------------------------------


def test_risk_escalation_names_signals_and_paths_when_tier_rises() -> None:
    card = {"task_type": "code", "validation": ["python -m pytest -q"]}
    paths = ["src/aiworkhub/sandbox_policy.py"]
    observation = qe.review_ready_risk_observation(card, paths)
    assert observation["error"] == ""
    assert observation["effective_tier"] == qe.RISK_HIGH
    escalation = observation["risk_escalation"]
    assert escalation["escalated"] is True
    assert escalation["requested_tier"] == qe.RISK_LOW
    assert escalation["effective_tier"] == qe.RISK_HIGH
    assert escalation["escalating_signals"]["authority_boundary"] == qe.RISK_HIGH
    assert escalation["escalating_signals"]["code_change"] == qe.RISK_MEDIUM
    assert escalation["changed_paths"] == paths


def test_risk_escalation_is_empty_when_tier_unchanged_or_unknown() -> None:
    profile = qe.resolve_risk_profile(qe.RISK_HIGH, signals=["code_change"])
    record = qe.risk_escalation_provenance(profile, ["src/x.py"])
    assert record["escalated"] is False
    assert record["escalating_signals"] == {}
    assert record["changed_paths"] == []
    assert qe.risk_escalation_provenance({}, ["src/x.py"])["escalated"] is False


# NF-2026-00598 -------------------------------------------------------------


def test_combined_tree_failure_carries_declared_invariant_diagnostics() -> None:
    profile = qe.resolve_risk_profile(qe.RISK_MEDIUM)
    failing = qe.EvidenceCheck(
        check_id="declared-invariants",
        kind="test",
        status=qe.STATUS_FAILED,
        summary="invariant no_orphan_rows violated",
        error="invariant_failed:no_orphan_rows",
    )
    passing = {"check_id": "lint", "kind": "lint", "status": qe.STATUS_PASSED}
    verdict = qe.fold_quality_verdict(
        [], risk_profile=profile, combined_tree_checks=[passing, failing]
    )
    assert "combined_tree:declared-invariants" in verdict["blocking_evidence"]
    failures = verdict["combined_tree_failures"]
    assert len(failures) == 1
    row = failures[0]
    assert row["check_id"] == "declared-invariants"
    assert row["status"] == qe.STATUS_FAILED
    assert "no_orphan_rows" in row["summary"]
    assert row["error"] == "invariant_failed:no_orphan_rows"


# NF-2026-00601 -------------------------------------------------------------


def _security(path: str) -> bool:
    return "security_sensitive" in qe.derive_risk_signals({}, [path])


def test_token_usage_meters_are_not_security_sensitive() -> None:
    assert not _security("src/aiworkhub/token_usage.py")
    assert not _security("src/aiworkhub/tokens_counter.py")
    assert not _security("src/aiworkhub/token_meter.py")


def test_auth_token_paths_remain_security_sensitive() -> None:
    assert _security("src/x/apitoken.py")
    assert _security("src/x/accessToken.ts")
    assert _security("src/x/csrftoken.py")
    assert _security("src/accounting/api_token.py")
    assert _security("src/telemetry/refresh_token.py")
    assert _security("src/aiworkhub/access_token.py")
    assert _security("src/aiworkhub/tokens/refresh.py")
    assert _security("src/aiworkhub/secret_store.py")


# NF-2026-00663 -------------------------------------------------------------


def test_receipt_normaliser_vocabulary_equals_reviewer_finding_keys() -> None:
    emitted = (
        qe._REVIEWER_FINDING_EXPLICIT_KEYS
        | set(qe._REVIEWER_FINDING_STRUCTURED_TEXT_FIELDS)
        | set(qe._REVIEWER_FINDING_REMEDY_FIELDS)
        | {"evidence_reference"}
    )
    assert emitted == quality_reviewer.QUALITY_REVIEW_FINDING_KEYS
    assert set(qe._REVIEWER_FINDING_STRUCTURED_TEXT_FIELDS) == {
        "confidence",
        "evidence_level",
        "symbol",
        "claim",
        "reproduction",
        "required_validation",
    }
    assert set(qe._REVIEWER_FINDING_REMEDY_FIELDS) == {
        "replacement",
        "removable_surface",
    }
