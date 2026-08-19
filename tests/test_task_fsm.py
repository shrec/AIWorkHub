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
