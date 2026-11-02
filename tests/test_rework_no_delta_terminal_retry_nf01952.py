"""NF-2026-01952: a terminal retry of never-rejected bytes is not ``rework_no_delta``.

Measured: a worker tree-killed by an MCP reconnect after finishing its edits
was retried with ``task_retry_terminal``, which carried its delta into a new
``rework_predecessor``. The relaunched worker correctly changed nothing, every
validation passed, and the finalizer still minted ``rework_no_delta`` because
the card's surviving ``rejection_disposition`` was an OLDER ``candidate_code``
reject of a different request -- often without ``changed_path_hashes``.

What is locked here:

* a valid ``aiworkhub.terminal_retry.v1`` with a retryable operational
  substatus that carried exactly this predecessor, whose bytes differ from the
  last rejected bytes, mints nothing;
* bytes equal to the rejected review's stay refused, including the
  reject -> no-delta worker_failed -> retry_terminal -> carry chain;
* a legacy disposition without hashes resolves the rejected bytes from the
  ``review_ready`` ledger row of its ``request_id``; unresolvable keeps the
  refusal;
* a never-rejected card (no disposition, ``review_feedback_present`` False)
  qualifies; feedback present or the flag absent does not;
* the launch packet projects ``terminal_retry`` and ``review_feedback_present``
  next to ``rejection_disposition``.
"""
from __future__ import annotations

import ast
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, task_store  # noqa: E402
from aiworkhub import process_launcher_evidence as evidence  # noqa: E402

from test_rework_no_delta_operational_predecessor_nf01555 import (  # noqa: E402
    _PREDECESSOR_REQUEST_ID,
    _disposition,
    _refuse,
    _reproduction,
)

_REJECTED_REQUEST_ID = "nf01952-older-rejected-request"


def _terminal_retry(**overrides) -> dict:
    retry = {
        "schema_id": "aiworkhub.terminal_retry.v1",
        "task_id": "AIWORKHUB_NF1370",
        "request_id": _PREDECESSOR_REQUEST_ID,
        "terminal_substatus": "worker_failed",
        "reason": "mcp reconnect tree-killed the worker",
        "predecessor_carry": "carried",
    }
    retry.update(overrides)
    return retry


def _rejected_elsewhere(predecessor: dict) -> dict:
    """Digests of a different candidate: same paths, other bytes."""
    return {path: "f" * 64 for path in predecessor["changed_path_hashes"]}


def _retried(tmp_path: Path, *, rejected: dict | None = None, **retry) -> tuple:
    workspace, metadata, changed = _reproduction(tmp_path, None)
    predecessor = metadata["rework_predecessor"]
    metadata["terminal_retry"] = _terminal_retry(**retry)
    metadata["review_feedback_present"] = True
    metadata["rejection_disposition"] = _disposition(
        request_id=_REJECTED_REQUEST_ID,
        failure_category="candidate_code",
        changed_path_hashes=(
            _rejected_elsewhere(predecessor) if rejected is None else rejected
        ),
    )
    return workspace, metadata, changed


def _refused(workspace, metadata, changed) -> bool:
    return _refuse(workspace, metadata, changed).startswith(
        evidence.REWORK_NO_DELTA + ":"
    )


def test_a_terminal_retry_of_never_rejected_bytes_is_not_refused(tmp_path):
    workspace, metadata, changed = _retried(tmp_path)

    assert _refuse(workspace, metadata, changed) == ""

    # Contrast: the identical packet without the terminal retry is refused.
    del metadata["terminal_retry"]
    assert _refused(workspace, metadata, changed)


@pytest.mark.parametrize(
    "substatus", sorted(core._RETRYABLE_OPERATIONAL_TERMINAL_SUBSTATUSES)
)
def test_every_retryable_operational_substatus_qualifies(tmp_path, substatus):
    workspace, metadata, changed = _retried(tmp_path, terminal_substatus=substatus)

    assert _refuse(workspace, metadata, changed) == ""


@pytest.mark.parametrize(
    "substatus", ["rework_no_delta", "review_ready", "accepted", "", None, 3]
)
def test_a_non_operational_or_malformed_substatus_keeps_the_refusal(
    tmp_path, substatus
):
    workspace, metadata, changed = _retried(tmp_path, terminal_substatus=substatus)

    assert _refused(workspace, metadata, changed)


def test_bytes_equal_to_the_rejected_review_stay_refused(tmp_path):
    workspace, metadata, changed = _reproduction(tmp_path, None)
    rejected = dict(metadata["rework_predecessor"]["changed_path_hashes"])
    workspace, metadata, changed = _retried(tmp_path, rejected=rejected)

    assert _refused(workspace, metadata, changed)


def test_reject_then_no_delta_worker_failed_then_retry_carry_launders_nothing(
    tmp_path,
):
    """candidate_code reject of X -> rework reproduces X -> refused
    rework_no_delta (worker_failed) -> retry_terminal carries X -> the relaunch
    reproduces X again: still refused, because X IS the rejected bytes."""
    workspace, metadata, changed = _reproduction(tmp_path, None)
    rejected = dict(metadata["rework_predecessor"]["changed_path_hashes"])
    # Step 1: the rework of the rejected request, no terminal retry yet.
    metadata["rework_predecessor"]["request_id"] = _REJECTED_REQUEST_ID
    metadata["rejection_disposition"] = _disposition(
        request_id=_REJECTED_REQUEST_ID,
        failure_category="candidate_code",
        changed_path_hashes=dict(rejected),
    )
    metadata["review_feedback_present"] = True
    assert _refused(workspace, metadata, changed)
    # Step 2: that refusal ended worker_failed; the retry carried its delta.
    metadata["rework_predecessor"]["request_id"] = _PREDECESSOR_REQUEST_ID
    metadata["terminal_retry"] = _terminal_retry(terminal_substatus="worker_failed")
    assert _refused(workspace, metadata, changed)


def test_a_retry_that_carried_a_different_predecessor_keeps_the_refusal(tmp_path):
    workspace, metadata, changed = _retried(tmp_path, request_id="another-request")

    assert _refused(workspace, metadata, changed)


@pytest.mark.parametrize("carry", ["no_changes", "no_in_scope_changes", None])
def test_a_retry_that_carried_nothing_keeps_the_refusal(tmp_path, carry):
    workspace, metadata, changed = _retried(tmp_path, predecessor_carry=carry)

    assert _refused(workspace, metadata, changed)


@pytest.mark.parametrize(
    "retry", [None, "terminal_retry", [], {"schema_id": "aiworkhub.terminal_retry.v0"}]
)
def test_a_missing_or_malformed_terminal_retry_keeps_the_refusal(tmp_path, retry):
    workspace, metadata, changed = _retried(tmp_path)
    if isinstance(retry, dict):
        retry = {**metadata["terminal_retry"], **retry}
    metadata["terminal_retry"] = retry

    assert _refused(workspace, metadata, changed)


def test_a_wrong_schema_disposition_keeps_the_refusal(tmp_path):
    workspace, metadata, changed = _retried(tmp_path)
    metadata["rejection_disposition"]["schema_id"] = "aiworkhub.rejection_disposition.v0"

    assert _refused(workspace, metadata, changed)


def test_a_never_rejected_card_qualifies(tmp_path):
    workspace, metadata, changed = _retried(tmp_path)
    metadata["rejection_disposition"] = None
    metadata["review_feedback_present"] = False

    assert _refuse(workspace, metadata, changed) == ""


@pytest.mark.parametrize("flag", [True, None, "false", 0])
def test_no_disposition_without_a_false_feedback_flag_keeps_the_refusal(
    tmp_path, flag
):
    workspace, metadata, changed = _retried(tmp_path)
    metadata["rejection_disposition"] = None
    if flag is None:
        del metadata["review_feedback_present"]
    else:
        metadata["review_feedback_present"] = flag

    assert _refused(workspace, metadata, changed)


# --- legacy disposition: the rejected bytes come from the review_ready ledger


def _ledger_repo(workspace) -> Path:
    root = Path(workspace.repo)
    assert task_store.initialize_repository(root)["ok"]
    return root


def _record_review(root: Path, *, request_id: str, substatus: str, hashes) -> None:
    payload = {
        "substatus": substatus,
        "request_id": request_id,
        "evidence": {"changed_path_hashes": hashes},
    }
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    try:
        con.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'terminal_review', 'codex', ?, '2026-11-02T00:00:00+00:00')",
            ("AIWORKHUB_NF1370", json.dumps(payload, sort_keys=True)),
        )
        con.commit()
    finally:
        con.close()


def _legacy(tmp_path: Path) -> tuple:
    workspace, metadata, changed = _retried(tmp_path)
    del metadata["rejection_disposition"]["changed_path_hashes"]
    return workspace, metadata, changed


@pytest.mark.parametrize("pinned", ["absent", {}, None])
def test_a_legacy_disposition_resolves_differing_bytes_from_the_ledger(
    tmp_path, pinned
):
    workspace, metadata, changed = _legacy(tmp_path)
    if pinned != "absent":
        metadata["rejection_disposition"]["changed_path_hashes"] = pinned
    root = _ledger_repo(workspace)
    predecessor = metadata["rework_predecessor"]
    _record_review(
        root,
        request_id=_REJECTED_REQUEST_ID,
        substatus="review_ready",
        hashes=_rejected_elsewhere(predecessor),
    )

    assert _refuse(workspace, metadata, changed) == ""


def test_a_legacy_disposition_whose_ledger_bytes_match_stays_refused(tmp_path):
    workspace, metadata, changed = _legacy(tmp_path)
    root = _ledger_repo(workspace)
    _record_review(
        root,
        request_id=_REJECTED_REQUEST_ID,
        substatus="review_ready",
        hashes=dict(metadata["rework_predecessor"]["changed_path_hashes"]),
    )

    assert _refused(workspace, metadata, changed)


def test_an_unresolvable_legacy_disposition_keeps_the_refusal(tmp_path):
    workspace, metadata, changed = _legacy(tmp_path)
    root = _ledger_repo(workspace)
    predecessor = metadata["rework_predecessor"]
    # Only a non-review_ready row and a row for another request exist.
    _record_review(
        root,
        request_id=_REJECTED_REQUEST_ID,
        substatus="validation_failed",
        hashes=_rejected_elsewhere(predecessor),
    )
    _record_review(
        root,
        request_id="some-other-request",
        substatus="review_ready",
        hashes=_rejected_elsewhere(predecessor),
    )

    assert _refused(workspace, metadata, changed)


def test_a_legacy_disposition_without_a_task_store_keeps_the_refusal(tmp_path):
    workspace, metadata, changed = _legacy(tmp_path)

    assert _refused(workspace, metadata, changed)


@pytest.mark.parametrize("hashes", [{}, None, "x", {"src/a.py": ""}, {"src/a.py": 5}])
def test_malformed_ledger_bytes_resolve_to_unknown(tmp_path, hashes):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    _record_review(
        root, request_id=_REJECTED_REQUEST_ID, substatus="review_ready", hashes=hashes
    )

    assert task_store.review_ready_changed_path_hashes(
        root, "AIWORKHUB_NF1370", _REJECTED_REQUEST_ID
    ) is None


# --- projection into the provider-launch packet


def test_the_launch_packet_projects_terminal_retry_beside_the_disposition():
    source = (_SRC / "aiworkhub" / "process_launcher_launch_isolated.py").read_text(
        encoding="utf-8"
    )
    packets = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Dict)
        and any(
            isinstance(key, ast.Constant) and key.value == "rejection_disposition"
            for key in node.keys
        )
    ]
    assert len(packets) == 1
    keys = [key.value for key in packets[0].keys if isinstance(key, ast.Constant)]
    position = keys.index("rejection_disposition")
    assert keys[position + 1:position + 3] == [
        "terminal_retry",
        "review_feedback_present",
    ]
