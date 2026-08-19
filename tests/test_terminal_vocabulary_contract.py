"""NF-2026-00339: the terminal-substatus vocabulary is ONE concept with ONE
owner.

The set of terminal substatuses was hand-copied into six places and three of
them had drifted -- agreement was maintained by comments, which is exactly how
they diverged. ``task_fsm`` now OWNS the vocabulary (and its named policy
subsets and the substatus->callback-class map); every other site imports it.

This is the deliverable that stops the recurrence: it asserts the six consumers
agree AND proves it fails when any single one drifts, by mutating one copy and
observing the failure. Without this test the next fix in this area breeds the
next divergence.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _ensure_deepseek_credentials_stub() -> None:
    """Some isolated worktrees are missing the uncommitted
    ``deepseek_credentials.py``; importing ``process_launcher`` would then fail
    for a reason unrelated to this contract. Only stub when genuinely
    unimportable (mirrors test_task_liveness_reconciler.py)."""
    try:
        importlib.import_module("aiworkhub.deepseek_credentials")
        return
    except ImportError:
        pass

    stub = types.ModuleType("aiworkhub.deepseek_credentials")

    class CredentialError(Exception):
        def __init__(self, reason: str = "deepseek_credential_stub_environment") -> None:
            super().__init__(reason)
            self.reason = reason

    def load_credential(repo=None):  # noqa: ANN001, ARG001
        raise CredentialError("deepseek_credential_stub_environment")

    def adapter_readiness(repo=None):  # noqa: ANN001, ARG001
        return {"ok": True, "readonly": True, "adapters": []}

    stub.CredentialError = CredentialError
    stub.load_credential = load_credential
    stub.adapter_readiness = adapter_readiness
    sys.modules["aiworkhub.deepseek_credentials"] = stub


_ensure_deepseek_credentials_stub()

from aiworkhub import callback_store, process_launcher, task_fsm, task_store  # noqa: E402


# The single owner.
OWNER = task_fsm


def _agreement_failures() -> list[str]:
    """Read the six sites LIVE (so a monkeypatched copy is observed) and return
    the names of any that disagree with the owner. Empty == the six agree."""
    failures: list[str] = []

    # task_store._ATOMIC_CALLBACK_TRANSITIONS -> owner callback classes.
    if task_store._ATOMIC_CALLBACK_TRANSITIONS != OWNER.TERMINAL_CALLBACK_CLASSES:
        failures.append("task_store._ATOMIC_CALLBACK_TRANSITIONS")

    # mark_terminal_failure's accepted set -> owner failure subset.
    if task_store.MARK_TERMINAL_FAILURE_SUBSTATUSES != OWNER.MARK_TERMINAL_FAILURE_SUBSTATUSES:
        failures.append("task_store.MARK_TERMINAL_FAILURE_SUBSTATUSES")

    # callback_store.CALLBACK_ELIGIBLE_TRANSITIONS -> owner callback classes.
    if callback_store.CALLBACK_ELIGIBLE_TRANSITIONS != OWNER.TERMINAL_CALLBACK_CLASSES:
        failures.append("callback_store.CALLBACK_ELIGIBLE_TRANSITIONS")

    # callback_store._CALLBACK_TRANSITION_MAP must reproduce every owner
    # substatus->class mapping exactly (it may add only input-spelling aliases).
    for substatus, cls in OWNER.TERMINAL_SUBSTATUS_TO_CALLBACK_CLASS.items():
        if callback_store._CALLBACK_TRANSITION_MAP.get(substatus) != cls:
            failures.append("callback_store._CALLBACK_TRANSITION_MAP")
            break

    # process_launcher.TERMINAL_PROCESS_STATES -> owner launcher subset.
    if process_launcher.TERMINAL_PROCESS_STATES != OWNER.LAUNCHER_TERMINAL_SUBSTATUSES:
        failures.append("process_launcher.TERMINAL_PROCESS_STATES")

    return failures


def test_the_six_consumers_agree_with_the_single_owner():
    assert _agreement_failures() == []


# Each of the five importing sites, with a value that drifts away from the
# owner. Mutating one and observing the failure is the proof the contract test
# actually guards that site (acceptance: "proven by a test that mutates one
# copy and observes the failure").
_DRIFT_CASES = [
    ("task_store", "_ATOMIC_CALLBACK_TRANSITIONS", frozenset({"review_ready"})),
    ("task_store", "MARK_TERMINAL_FAILURE_SUBSTATUSES", frozenset({"timed_out"})),
    ("callback_store", "CALLBACK_ELIGIBLE_TRANSITIONS", frozenset({"review_ready"})),
    ("process_launcher", "TERMINAL_PROCESS_STATES", frozenset({"review_ready"})),
]


@pytest.mark.parametrize("module_name,attr,drifted", _DRIFT_CASES)
def test_drift_in_any_single_copy_is_detected(monkeypatch, module_name, attr, drifted):
    module = {"task_store": task_store, "callback_store": callback_store,
              "process_launcher": process_launcher}[module_name]
    assert _agreement_failures() == []  # sanity: agree before the drift
    monkeypatch.setattr(module, attr, drifted)
    failures = _agreement_failures()
    assert f"{module_name}.{attr}" in failures


def test_drift_in_the_callback_transition_map_is_detected(monkeypatch):
    assert _agreement_failures() == []
    drifted = {**callback_store._CALLBACK_TRANSITION_MAP, "review_ready": "blocked"}
    monkeypatch.setattr(callback_store, "_CALLBACK_TRANSITION_MAP", drifted)
    assert "callback_store._CALLBACK_TRANSITION_MAP" in _agreement_failures()


# --- Structural invariants of the owner vocabulary -------------------------

def test_named_subsets_are_subsets_of_the_owner_vocabulary():
    assert OWNER.LAUNCHER_TERMINAL_SUBSTATUSES <= OWNER.KNOWN_TERMINAL_SUBSTATUSES
    assert OWNER.MARK_TERMINAL_FAILURE_SUBSTATUSES <= OWNER.KNOWN_TERMINAL_SUBSTATUSES
    assert OWNER.TERMINAL_CALLBACK_CLASSES <= OWNER.KNOWN_TERMINAL_SUBSTATUSES


def test_owner_map_covers_exactly_the_vocabulary_and_only_valid_classes():
    assert set(OWNER.TERMINAL_SUBSTATUS_TO_CALLBACK_CLASS.keys()) == set(OWNER.KNOWN_TERMINAL_SUBSTATUSES)
    assert set(OWNER.TERMINAL_SUBSTATUS_TO_CALLBACK_CLASS.values()) <= set(OWNER.TERMINAL_CALLBACK_CLASSES)


def test_callback_map_reproduces_the_full_vocabulary():
    # Every known substatus is classified by the callback layer (its keys are a
    # superset of the vocabulary: owner mappings plus input-spelling aliases),
    # and every value is a deliverable class.
    assert set(callback_store._CALLBACK_TRANSITION_MAP.keys()) >= set(OWNER.KNOWN_TERMINAL_SUBSTATUSES)
    assert set(callback_store._CALLBACK_TRANSITION_MAP.values()) <= set(OWNER.TERMINAL_CALLBACK_CLASSES)


def test_the_five_launcher_outcomes_are_owned_and_classified():
    for substatus in [
        "exited_without_review", "finalize_abandoned", "monitor_error",
        "token_budget_exceeded", "output_budget_exceeded",
    ]:
        assert substatus in OWNER.KNOWN_TERMINAL_SUBSTATUSES
        assert substatus in OWNER.TERMINAL_SUBSTATUS_TO_CALLBACK_CLASS
