"""NF-2026-00340 / B921: an unknown substatus must NEVER be delivered as
``review_ready``.

``resolve_callback_transition`` previously mapped any unrecognized substatus
to ``review_ready`` -- telling a manager that unreviewed/failed work was ready
to inspect, the worst available shape. Two substatuses (``dependency_blocked``,
``liveness_lost``) had been patched in while the fail-open default was left, so
every future failure substatus repeated the defect -- and the root audit showed
five launcher outcomes were already waiting. The fallback is now fail-closed:
unknown resolves to the ``blocked`` callback class (a real failure a manager
must look at), never ``review_ready``; known success outcomes map to
``review_ready`` explicitly through the owner map, not through the fallback.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import callback_store, task_fsm  # noqa: E402


# A substatus that is absent from EVERY map: the owner vocabulary, the launcher
# subset, and the callback alias table. This is the regression the audit named.
_ABSENT_FROM_EVERY_MAP = "brand_new_failure_substatus_zzz"


def test_absent_substatus_is_present_in_no_map():
    assert _ABSENT_FROM_EVERY_MAP not in task_fsm.KNOWN_TERMINAL_SUBSTATUSES
    assert _ABSENT_FROM_EVERY_MAP not in task_fsm.TERMINAL_SUBSTATUS_TO_CALLBACK_CLASS
    assert _ABSENT_FROM_EVERY_MAP not in callback_store._CALLBACK_TRANSITION_MAP


def test_unknown_substatus_never_resolves_to_review_ready():
    resolved = callback_store.resolve_callback_transition(_ABSENT_FROM_EVERY_MAP)
    assert resolved != "review_ready"
    # It resolves to a blocked-class outcome and remains a deliverable class.
    assert resolved == "blocked"
    assert resolved in callback_store.CALLBACK_ELIGIBLE_TRANSITIONS


def test_normalize_is_fail_closed_for_unknown():
    # The raw mapper returns None (no invented class) for an unknown substatus;
    # only ``resolve`` applies the fail-closed ``blocked`` default.
    assert callback_store.normalize_callback_transition(_ABSENT_FROM_EVERY_MAP) is None


@pytest.mark.parametrize(
    "substatus,expected",
    [
        # The five launcher outcomes the root audit named: each now resolves to
        # its REAL failure class, never review_ready.
        ("token_budget_exceeded", "token_budget_exceeded"),
        ("output_budget_exceeded", "output_budget_exceeded"),
        ("exited_without_review", "launch_failed"),
        ("finalize_abandoned", "blocked"),
        ("monitor_error", "blocked"),
    ],
)
def test_launcher_failure_outcomes_resolve_to_their_real_class(substatus, expected):
    resolved = callback_store.resolve_callback_transition(substatus)
    assert resolved == expected
    assert resolved != "review_ready"


@pytest.mark.parametrize("substatus", ["review_ready", "exited"])
def test_success_outcomes_still_resolve_to_review_ready(substatus):
    # Success spellings resolve to review_ready EXPLICITLY via the owner map --
    # not via any fallback (``exited`` used to rely on the fallback and is now
    # mapped directly).
    assert callback_store.resolve_callback_transition(substatus) == "review_ready"


def test_no_known_blocked_class_substatus_is_delivered_as_review_ready():
    # Every terminal substatus that the owner classifies as blocked must never
    # come back as review_ready from the resolver (the exact B921 shape).
    blocked_substatuses = [
        sub
        for sub, cls in task_fsm.TERMINAL_SUBSTATUS_TO_CALLBACK_CLASS.items()
        if cls == "blocked"
    ]
    assert blocked_substatuses  # guard against a vacuous pass
    for sub in blocked_substatuses:
        assert callback_store.resolve_callback_transition(sub) == "blocked"


def test_every_known_substatus_resolves_to_an_eligible_class():
    for sub in task_fsm.KNOWN_TERMINAL_SUBSTATUSES:
        assert callback_store.resolve_callback_transition(sub) in callback_store.CALLBACK_ELIGIBLE_TRANSITIONS
