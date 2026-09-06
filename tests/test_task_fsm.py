"""NF-2026-00339: the terminal-review FSM must accept every substatus the
launcher can actually produce, and stay fail-closed on everything else.

Before this fix ``check_terminal_review_transition`` called five real
launcher outcomes illegal (``exited_without_review``, ``finalize_abandoned``,
``monitor_error``, ``token_budget_exceeded``, ``output_budget_exceeded``).
Nothing failed only because those paths happened to route through
``mark_terminal_failure`` instead of ``mark_terminal_review`` -- the barrier
held by routing accident, not by contract. The comment at the vocabulary said
"not exhaustive-enforced (new adapters may add outcomes)" while the code was
fail-closed; that contradiction is why new outcomes were added where they are
produced and not where they are validated. Resolution: the code stays
fail-closed and the vocabulary is the single, exhaustive owner; the comment
now says so.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import task_fsm  # noqa: E402


# The five outcomes the launcher emits that task_fsm previously called illegal.
_PREVIOUSLY_ILLEGAL_LAUNCHER_OUTCOMES = [
    "exited_without_review",
    "finalize_abandoned",
    "monitor_error",
    "token_budget_exceeded",
    "output_budget_exceeded",
]


@pytest.mark.parametrize("substatus", _PREVIOUSLY_ILLEGAL_LAUNCHER_OUTCOMES)
@pytest.mark.parametrize("source_status", ["pending", "processing"])
def test_check_accepts_every_launcher_outcome(substatus, source_status):
    # A regression for each of the five: legal from every in-flight source
    # status, by contract -- not by which routing call happens to run.
    assert task_fsm.check_terminal_review_transition(source_status, substatus) == (True, "ok")


def test_every_launcher_producible_substatus_is_a_known_terminal_substatus():
    # The launcher can never emit a state the FSM would reject as unknown.
    assert task_fsm.LAUNCHER_TERMINAL_SUBSTATUSES <= task_fsm.KNOWN_TERMINAL_SUBSTATUSES
    for substatus in task_fsm.LAUNCHER_TERMINAL_SUBSTATUSES:
        legal, reason = task_fsm.check_terminal_review_transition("processing", substatus)
        assert legal is True, f"{substatus}: {reason}"


def test_unknown_substatus_is_refused_not_accepted():
    # Fail-closed: an outcome absent from the exhaustive vocabulary is illegal
    # regardless of source status -- it is never silently admitted.
    legal, reason = task_fsm.check_terminal_review_transition("processing", "totally_unknown_outcome_zzz")
    assert legal is False
    assert reason == "illegal_transition:unknown_substatus=totally_unknown_outcome_zzz"


def test_empty_substatus_is_refused():
    legal, reason = task_fsm.check_terminal_review_transition("processing", "   ")
    assert legal is False
    assert reason == "illegal_transition:empty_substatus"


def test_blocked_is_not_a_legal_source_for_re_terminalization():
    # A card already in the terminal ``blocked`` outcome cannot be
    # re-terminalized through a terminal-review transition (that would clobber
    # a prior record). This is the trap surface addressed in
    # test_blocked_is_not_a_trap.py, asserted here at the pure FSM boundary.
    legal, reason = task_fsm.check_terminal_review_transition("blocked", "review_ready")
    assert legal is False
    assert reason == "illegal_transition:from=blocked:to=review"


@pytest.mark.parametrize("substatus", _PREVIOUSLY_ILLEGAL_LAUNCHER_OUTCOMES)
def test_launcher_failure_outcome_can_never_be_reported_as_passing(substatus):
    # deterministic_verification must treat each launcher failure outcome as a
    # known failure class: applicable, but never a pass, whatever evidence is
    # attached. A budget/monitor failure can never masquerade as review-ready.
    verdict = task_fsm.deterministic_verification(
        substatus,
        [{"returncode": 0}],
        [{"path": "x", "sha256": "a" * 64, "bytes": 1}],
    )
    assert verdict["applicable"] is True
    assert verdict["pass"] is False
    assert verdict["reason"] == "known_failure_substatus"


def test_deterministic_verification_marks_unknown_substatus_inapplicable():
    verdict = task_fsm.deterministic_verification("totally_unknown_outcome_zzz", [], [])
    assert verdict["applicable"] is False
    assert verdict["pass"] is False
    assert verdict["reason"] == "unknown_substatus"


# ---------------------------------------------------------------------------
# NF-2026-00621: the reason a terminal outcome carries must be DERIVED from the
# evidence in the same record, never asserted from the substatus alone.
#
# Measured on this repository's canonical ledger before the fix: of 1,589
# ``validation_failed`` terminal_review records, 189 (11.9%) reported
# ``failed_validation_count == 0`` with ``validation_count > 0`` -- every
# declared validation ran and none failed -- and 333 (21.0%) carried no
# measurement at all. All of them were recorded with
# ``reason = "known_failure_substatus"``, which is why the mechanical-vs-genuine
# split could not be computed from the ledger at all.
# ---------------------------------------------------------------------------

_PASSING_VALIDATION = {"returncode": 0}
_FAILING_VALIDATION = {"returncode": 1}
_PRESENT_OUTPUT = {"path": "src/x.py", "sha256": "a" * 64, "bytes": 12}


def test_validation_failed_that_contradicts_its_own_evidence_is_not_a_known_failure():
    # The 189-record class: three validations ran, none failed, no required
    # output missing -- yet the outcome claims a validation failure.
    verdict = task_fsm.deterministic_verification(
        "validation_failed",
        [_PASSING_VALIDATION, _PASSING_VALIDATION, _PASSING_VALIDATION],
        [_PRESENT_OUTPUT],
    )
    assert verdict["evidence_verdict"]["validation_count"] == 3
    assert verdict["evidence_verdict"]["failed_validation_count"] == 0
    assert verdict["evidence_verdict"]["missing_required_output_count"] == 0
    assert verdict["evidence_support"] == task_fsm.EVIDENCE_SUPPORT_CONTRADICTED
    assert verdict["reason"] == "substatus_contradicted_by_evidence"
    # Still fail-closed: refusing the CLAIM never promotes the attempt.
    assert verdict["pass"] is False
    assert verdict["applicable"] is True


def test_validation_failed_with_no_measurement_is_reported_as_unmeasured():
    # The 333-record class: nothing was measured, so nothing proves a
    # validation failure either way.
    verdict = task_fsm.deterministic_verification("validation_failed", [], [])
    assert verdict["evidence_verdict"]["nothing_measured"] is True
    assert verdict["evidence_support"] == task_fsm.EVIDENCE_SUPPORT_UNMEASURED
    assert verdict["reason"] == "substatus_unsupported_no_evidence_recorded"
    assert verdict["pass"] is False


def test_validation_failed_backed_by_a_failing_validation_is_unchanged():
    # The genuine case must keep its historical reason so every existing
    # reader of a correctly-recorded failure keeps working byte-for-byte.
    verdict = task_fsm.deterministic_verification(
        "validation_failed", [_PASSING_VALIDATION, _FAILING_VALIDATION], [_PRESENT_OUTPUT]
    )
    assert verdict["evidence_support"] == task_fsm.EVIDENCE_SUPPORT_SUPPORTED
    assert verdict["reason"] == "known_failure_substatus"
    assert verdict["pass"] is False


def test_validation_failed_backed_by_a_missing_required_output_is_supported():
    verdict = task_fsm.deterministic_verification(
        "validation_failed", [_PASSING_VALIDATION], [{"path": "src/x.py"}]
    )
    assert verdict["evidence_verdict"]["missing_required_output_count"] == 1
    assert verdict["evidence_support"] == task_fsm.EVIDENCE_SUPPORT_SUPPORTED
    assert verdict["reason"] == "known_failure_substatus"


@pytest.mark.parametrize(
    "substatus",
    ["worker_failed", "cancelled", "timed_out", "launch_failed", "liveness_lost"],
)
def test_process_failure_classes_are_never_called_contradicted(substatus):
    # A worker that died before running anything genuinely has nothing to
    # measure. Only ``validation_failed`` makes a claim about validation
    # evidence, so only it can be contradicted by that evidence.
    verdict = task_fsm.deterministic_verification(substatus, [_PASSING_VALIDATION], [])
    assert verdict["evidence_support"] == task_fsm.EVIDENCE_SUPPORT_NOT_APPLICABLE
    assert verdict["reason"] == "known_failure_substatus"


def test_evidence_support_is_total_and_never_raises_on_malformed_verdicts():
    for bogus in ({}, {"failed_validation_count": "3"}, {"validation_count": -1}, {"x": None}):
        assert task_fsm.evidence_support("validation_failed", bogus) in {
            task_fsm.EVIDENCE_SUPPORT_UNMEASURED,
            task_fsm.EVIDENCE_SUPPORT_CONTRADICTED,
            task_fsm.EVIDENCE_SUPPORT_SUPPORTED,
        }
    assert (
        task_fsm.evidence_support("", {}) == task_fsm.EVIDENCE_SUPPORT_NOT_APPLICABLE
    )


def test_every_verification_result_carries_an_evidence_support_field():
    # The returned mapping stays a superset of the historical shape: existing
    # readers keep their keys, and the new field is always present.
    historical = {"applicable", "pass", "substatus", "reason", "claim_epoch", "evidence_verdict"}
    for substatus, validations, outputs in (
        ("review_ready", [_PASSING_VALIDATION], [_PRESENT_OUTPUT]),
        ("review_ready", [], []),
        ("validation_failed", [_FAILING_VALIDATION], []),
        ("totally_unknown_outcome_zzz", [], []),
    ):
        verdict = task_fsm.deterministic_verification(substatus, validations, outputs)
        assert historical.issubset(verdict.keys())
        assert "evidence_support" in verdict
