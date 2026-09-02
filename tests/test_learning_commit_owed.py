"""The lesson a decision owes, named at the moment it can be filed.

A learning commit is a manager judgement -- root cause and invariant are not
derivable from a diff -- so it is deliberately NOT an automatic side effect of
accepting or rejecting a candidate. But the duty was invisible at the one
moment it is cheap to perform: right after the decision, with the evidence
still in hand.

Measured on this repository after the manager surface began reporting it: 198
decided cards over 14 days, 2 carrying a lesson. One percent. The skill
registry waits on lessons that were never written, so this is the link that
blocks the one after it.

commit_owed writes nothing. It returns exactly the arguments the manager would
otherwise reassemble by hand -- including the evidence id in the ``file:`` form
the store accepts, because a ``sha256:`` receipt id is refused and that is a
mistake worth making once rather than every time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub.learning_commit import commit_owed  # noqa: E402

MANIFEST = (
    "file:.aiworkhub/runtime/process_logs/processes/attempt-artifacts/"
    "92279514599a47e2a6c9dcdaf86f53fb/manifest.json"
)


def test_it_carries_everything_the_commit_tool_requires():
    owed = commit_owed(
        task_id="TASK", request_id="r" * 32, outcome="accepted",
        changed_paths=["src/aiworkhub/source_graph.py", "tests/test_x.py"],
        evidence_reference=MANIFEST,
    )
    # every required argument of aiworkhub_manager_learning_commit
    for key in ("task_id", "request_id", "repo_area", "outcome",
                "evidence_ids", "idempotency_key", "provenance"):
        assert owed[key], key
    assert owed["owed"] is True
    assert owed["evidence_ids"] == [MANIFEST]


def test_a_receipt_id_is_not_offered_as_evidence():
    """sha256: is refused by the store; offering it would teach the mistake."""
    owed = commit_owed(
        task_id="TASK", request_id="r" * 32, outcome="accepted",
        evidence_reference="sha256:9bf93081bb8c9f0582fa03ee73dd93456fb583b2e",
    )
    assert owed["evidence_ids"] == []


def test_an_unsafe_file_reference_is_not_offered():
    owed = commit_owed(
        task_id="TASK", request_id="r" * 32, outcome="accepted",
        evidence_reference="file:../../etc/passwd",
    )
    assert owed["evidence_ids"] == []


@pytest.mark.parametrize(
    ("paths", "area"),
    [
        (["src/aiworkhub/source_graph.py", "tests/test_x.py"], "src/aiworkhub"),
        (["src/aiworkhub/dash/a.py", "src/aiworkhub/dash/b.py"], "src/aiworkhub/dash"),
        (["tests/test_a.py", "tests/test_b.py"], "tests"),
        (["src/aiworkhub/a.py", "scripts/b.py"], ""),
        ([], ""),
    ],
)
def test_the_area_comes_from_the_production_paths(paths, area):
    """A fix and its regression share no prefix; the fix is where it lives."""
    assert commit_owed(
        task_id="T", request_id="R", outcome="accepted", changed_paths=paths
    )["repo_area"] == area


def test_the_idempotency_key_is_stable_per_decision():
    args = dict(task_id="TASK", request_id="r" * 32, outcome="accepted")
    assert commit_owed(**args)["idempotency_key"] == commit_owed(**args)["idempotency_key"]
    rejected = commit_owed(task_id="TASK", request_id="r" * 32, outcome="rejected")
    assert rejected["idempotency_key"] != commit_owed(**args)["idempotency_key"]
    assert rejected["provenance"] == "manager_rejected_review"
