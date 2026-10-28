"""RM-2026-00015: every terminal substatus has a cause by judgement, not omission.

The vocabulary is read from its single owner, ``task_fsm``; nothing here
restates it. A new ``KNOWN_TERMINAL_SUBSTATUSES`` member without an explicit
``_SUBSTATUS_CAUSE`` entry fails this module instead of landing as
``cause_not_established``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_fsm  # noqa: E402
from aiworkhub import terminal_failure_classification as tfc  # noqa: E402

_VOCABULARY = task_fsm.KNOWN_TERMINAL_SUBSTATUSES


def _unmapped(vocabulary: frozenset[str]) -> set[str]:
    return {substatus for substatus in vocabulary if substatus not in tfc._SUBSTATUS_CAUSE}


def test_vocabulary_is_read_from_the_owner_and_is_not_empty():
    assert _VOCABULARY is task_fsm.KNOWN_TERMINAL_SUBSTATUSES
    assert _VOCABULARY


def test_every_terminal_substatus_has_an_explicit_cause_entry():
    assert _unmapped(_VOCABULARY) == set()


def test_a_new_vocabulary_member_without_an_entry_is_reported():
    added = "substatus_nobody_mapped_yet"
    assert added not in tfc._SUBSTATUS_CAUSE
    assert _unmapped(_VOCABULARY | {added}) == {added}


@pytest.mark.parametrize("substatus", sorted(_VOCABULARY))
def test_each_entry_names_a_cause_the_action_matrix_owns(substatus):
    assert tfc._SUBSTATUS_CAUSE[substatus] in tfc.DISPOSITION_CAUSES


@pytest.mark.parametrize("substatus", sorted(_VOCABULARY))
def test_the_disposition_follows_the_explicit_entry(substatus):
    cause = tfc._SUBSTATUS_CAUSE[substatus]
    verdict = tfc.failure_disposition_from_substatus(terminal_substatus=substatus)
    assert verdict["cause"] == cause
    if cause == tfc.CAUSE_NOT_ESTABLISHED:
        # An explicit no-cause entry claims no evidence it does not have.
        assert verdict["evidence_authority"] == tfc.EVIDENCE_AUTHORITY_NONE
    else:
        assert verdict["evidence_authority"] == tfc.EVIDENCE_AUTHORITY_AIWORKHUB_REASON
        assert verdict["evidence"] == f"terminal_substatus={substatus}"


@pytest.mark.parametrize(
    "groups",
    [
        tfc.CANDIDATE_TERMINAL_SUBSTATUSES,
        tfc.VALIDATION_ENVIRONMENT_TERMINAL_SUBSTATUSES,
        tfc.PROVIDER_RUNTIME_TERMINAL_SUBSTATUSES,
        tfc.CANCELLATION_TERMINAL_SUBSTATUSES,
        tfc.POLICY_TERMINAL_SUBSTATUSES,
    ],
)
def test_published_groups_are_subsets_of_the_owner_vocabulary(groups):
    assert groups <= _VOCABULARY


def test_a_substatus_matching_a_reason_agrees_with_that_reason():
    for substatus in _VOCABULARY & set(tfc._REASON_CAUSE):
        assert tfc._SUBSTATUS_CAUSE[substatus] == tfc._REASON_CAUSE[substatus], substatus
